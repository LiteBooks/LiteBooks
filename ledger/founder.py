"""Founder-borne expense log.

Business expenses an owner paid personally and agreed to bear without
reimbursement. The operating agreement requires them to be logged, and to be
kept apart from the company's books, financial statements, and tax return: an
owner may deduct them personally as unreimbursed partnership expenses.

So this module never imports the posting services and nothing in it references
a journal entry, account, or period. Expense account codes are borrowed as
category labels only.

Every entry has one logging deadline:
- paid inside the configured special logging window: that window's log-by date;
- otherwise: the date paid plus the standard deadline (30 days unless changed).
Logged on or before the deadline is timely; after it, the entry is flagged
(late). special_window_late is the same flag for entries paid in the window.

Flags are informational and never block saving. approval_reminder marks $250 or
more, which the agreement may require approval for if it creates a company
obligation.

The full charge is `amount`; `business_amount` is the business part (never more
than the full charge, and a note explaining the split is required when less).

"Logged on" means the calendar day in the business's time zone (TIME_ZONE).
"""

import csv
import hashlib
import io
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import PurePath
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.utils import timezone

from .models import Account, Contact, FounderExpense, FounderExpenseReceipt, FounderExpenseSettings
from .services import record_audit


LATE_AFTER_DAYS = 30  # default standard deadline; the live value is FounderExpenseSettings.standard_deadline_days
STANDARD_DEADLINE_MAX_DAYS = 366
RECEIPT_MAX_BYTES = 10 * 1024 * 1024
RECEIPT_TYPES = {"application/pdf": "PDF", "image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WebP"}
RECEIPT_TYPE_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}
APPROVAL_THRESHOLD = Decimal("250.00")
APPROVAL_NOTE = "$250 or more: the operating agreement may require approval if this expense creates a company obligation."
MAX_AMOUNT = Decimal("9999999999.99")  # decimal(12,2)
CSV_MAX_BYTES = 2 * 1024 * 1024
CSV_MAX_ROWS = 1000
CSV_REQUIRED = ("owner_email", "date_paid", "vendor", "description", "amount", "business_purpose", "property_stays_personal")
CSV_OPTIONAL = ("receipt_url", "category", "business_amount", "allocation_note")
EDITABLE = (
    "owner_contact", "date_paid", "vendor", "description", "amount", "business_amount", "allocation_note",
    "business_purpose", "property_stays_personal", "category", "receipt_url",
)


def money(value):
    return f"{value:.2f}"


# -- Validation -----------------------------------------------------------------


def parse_amount(value, field="amount", label="amount"):
    if isinstance(value, Decimal) and value.is_finite() and value > 0:
        text = str(value)
    else:
        text = str(value if value is not None else "").replace(",", "").replace("$", "").strip()
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValidationError({field: f"Enter {'an' if label[0] in 'aeiou' else 'a'} {label} such as 42.50."}) from exc
    if not amount.is_finite():
        raise ValidationError({field: f"Enter {'an' if label[0] in 'aeiou' else 'a'} {label} such as 42.50."})
    if amount <= 0:
        raise ValidationError({field: f"The {label} must be greater than zero."})
    if amount != amount.quantize(Decimal("0.01")):
        raise ValidationError({field: "Use at most two decimal places."})
    if amount > MAX_AMOUNT:
        raise ValidationError({field: f"That {label} is too large."})
    return amount


def parse_date_paid(value):
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ValidationError({"date_paid": "Enter the date paid as YYYY-MM-DD."})


def parse_yes_no(value, field="property_stays_personal"):
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"y", "yes", "true"}:
        return True
    if text in {"n", "no", "false"}:
        return False
    raise ValidationError({field: "Enter Y or N."})


def clean_category(value):
    code = str(value or "").strip()
    if code and not Account.objects.filter(code=code, type=Account.Type.EXPENSE).exists():
        raise ValidationError({"category": f"{code} is not an expense account code."})
    return code


def clean_receipt_url(value):
    url = str(value or "").strip()
    if url:
        try:
            URLValidator(schemes=["http", "https"])(url)
        except ValidationError as exc:
            raise ValidationError({"receipt_url": "Enter a full http(s) link to the receipt."}) from exc
        if len(url) > 500:
            raise ValidationError({"receipt_url": "The receipt link is too long."})
    return url


def clean_owner(value):
    owner = value if isinstance(value, Contact) else Contact.objects.filter(pk=value).first() if str(value or "").isdigit() else None
    if owner is None or owner.kind != Contact.Kind.OWNER:
        raise ValidationError({"owner_id": "Choose an owner."})
    return owner


def clean_values(data, *, logged_on=None):
    """Validate one entry's fields. Collects every field error rather than stopping at the first."""
    errors = {}
    values = {}

    def take(key, parse):
        try:
            values[key] = parse()
        except ValidationError as error:
            for field, messages in error.message_dict.items():
                errors.setdefault(field, []).extend(messages)

    take("owner_contact", lambda: clean_owner(data.get("owner_contact") or data.get("owner_id")))
    take("date_paid", lambda: data["date_paid"] if isinstance(data.get("date_paid"), date) else parse_date_paid(data.get("date_paid")))
    take("amount", lambda: parse_amount(data.get("amount")))
    business = data.get("business_amount")
    if business is None or str(business).strip() == "":
        values["business_amount"] = None  # defaults to the full charge below
    else:
        take("business_amount", lambda: parse_amount(business, "business_amount", "business amount"))
    values["allocation_note"] = str(data.get("allocation_note") or "").strip()
    take("property_stays_personal", lambda: parse_yes_no(data.get("property_stays_personal", False)))
    take("category", lambda: clean_category(data.get("category")))
    take("receipt_url", lambda: clean_receipt_url(data.get("receipt_url")))
    for key, label, limit in (("vendor", "Vendor", 160), ("description", "Description", 240), ("business_purpose", "Business purpose", None)):
        text = " ".join(str(data.get(key) or "").split()) if key != "business_purpose" else str(data.get(key) or "").strip()
        if not text:
            errors.setdefault(key, []).append(f"{label} is required.")
        elif limit and len(text) > limit:
            errors.setdefault(key, []).append(f"{label} must be {limit} characters or fewer.")
        values[key] = text

    if values.get("business_amount") is None and "amount" in values:
        values["business_amount"] = values["amount"]
    full, part = values.get("amount"), values.get("business_amount")
    if full is not None and part is not None:
        if part > full:
            errors.setdefault("business_amount", []).append("The business amount cannot be more than the full charge.")
        elif part < full and not values["allocation_note"]:
            errors.setdefault("allocation_note", []).append("Explain how the business amount was worked out when it is less than the full charge.")

    paid = values.get("date_paid")
    if paid:
        today = timezone.localdate()
        if paid > today:
            errors.setdefault("date_paid", []).append("The date paid cannot be in the future.")
        elif logged_on and paid > logged_on:
            errors.setdefault("date_paid", []).append(f"The date paid cannot be after the day it was logged ({logged_on.isoformat()}).")
    if errors:
        raise ValidationError(errors)
    return values


# -- Flags ----------------------------------------------------------------------


def logged_on(expense):
    return timezone.localdate(expense.logged_at)


def in_special_window(date_paid, window):
    return bool(window.special_window_start and window.special_window_start <= date_paid <= window.special_window_end)


def deadline_for(date_paid, window):
    """The last day an entry for this payment can be logged on time (inclusive), and which rule set it."""
    if in_special_window(date_paid, window):
        return window.special_window_deadline, "special_window"
    return date_paid + timedelta(days=window.standard_deadline_days), "standard"


def deadline_status(date_paid, day_logged, amount, window):
    deadline, rule = deadline_for(date_paid, window)
    late = day_logged > deadline
    in_window = rule == "special_window"
    return {
        "days_to_log": (day_logged - date_paid).days,
        "deadline": deadline.isoformat(),
        "deadline_rule": rule,
        "timely": not late,
        "late": late,
        "in_special_window": in_window,
        "special_window_late": in_window and late,
        "approval_reminder": amount >= APPROVAL_THRESHOLD,
    }


def flags(expense, window=None):
    window = window or FounderExpenseSettings.load()
    return deadline_status(expense.date_paid, logged_on(expense), expense.amount, window)


def business_amount(expense):
    return expense.amount if expense.business_amount is None else expense.business_amount


def has_receipt(expense):
    return bool(expense.receipt_url) or bool(expense.receipts.all())


def category_names():
    return dict(Account.objects.filter(type=Account.Type.EXPENSE).values_list("code", "name"))


def expense_json(expense, window=None, names=None):
    names = category_names() if names is None else names
    return {
        "id": expense.pk,
        "owner_id": expense.owner_contact_id,
        "owner_name": expense.owner_contact.name,
        "date_paid": expense.date_paid.isoformat(),
        "vendor": expense.vendor,
        "description": expense.description,
        "amount": money(expense.amount),
        "business_amount": money(business_amount(expense)),
        "allocation_note": expense.allocation_note,
        "category": expense.category or None,
        "category_name": names.get(expense.category, "") if expense.category else "",
        "business_purpose": expense.business_purpose,
        "property_stays_personal": expense.property_stays_personal,
        "has_receipt": has_receipt(expense),
        "receipt_url": expense.receipt_url,
        "receipts": [receipt_json(item) for item in expense.receipts.all()],
        "receipt_count": len(expense.receipts.all()),
        "logged_at": expense.logged_at.isoformat(),
        **flags(expense, window),
        "approval_note": APPROVAL_NOTE if expense.amount >= APPROVAL_THRESHOLD else "",
        "source": expense.source,
        "status": expense.status,
        "withdrawn_reason": expense.withdrawn_reason,
        "withdrawn_at": expense.withdrawn_at.isoformat() if expense.withdrawn_at else None,
    }


def receipt_json(receipt):
    return {
        "id": receipt.pk, "name": receipt.original_name, "content_type": receipt.content_type, "size": receipt.size, "sha256": receipt.sha256,
        "uploaded_at": receipt.uploaded_at.isoformat() if receipt.uploaded_at else None,
        "download_url": f"/api/founder-expenses/receipts/{receipt.pk}/download/",
    }


def audit_snapshot(expense):
    return {
        "owner_id": expense.owner_contact_id, "date_paid": expense.date_paid.isoformat(), "vendor": expense.vendor, "description": expense.description,
        "amount": money(expense.amount), "business_amount": money(business_amount(expense)), "allocation_note": expense.allocation_note,
        "business_purpose": expense.business_purpose, "property_stays_personal": expense.property_stays_personal,
        "category": expense.category, "receipt_url": expense.receipt_url, "source": expense.source, "status": expense.status, "withdrawn_reason": expense.withdrawn_reason,
        "receipts": [item.original_name for item in expense.receipts.all()],
        "receipt_files": [{"id": item.pk, "name": item.original_name, "sha256": item.sha256} for item in expense.receipts.all()],
    }


# -- Queries --------------------------------------------------------------------


def expenses(*, owner_id=None, start=None, end=None, year=None, include_withdrawn=False, flagged=None, window=None):
    """Entries by date paid. With flagged=True/False, a list of only the late (or only the timely) ones."""
    query = FounderExpense.objects.select_related("owner_contact").prefetch_related("receipts")
    if owner_id is not None:
        query = query.filter(owner_contact_id=owner_id)
    if year is not None:
        query = query.filter(date_paid__year=year)
    if start is not None:
        query = query.filter(date_paid__gte=start)
    if end is not None:
        query = query.filter(date_paid__lte=end)
    if not include_withdrawn:
        query = query.filter(status=FounderExpense.Status.LOGGED)
    query = query.order_by("date_paid", "pk")
    if flagged is None:
        return query
    window = window or FounderExpenseSettings.load()
    return [item for item in query if flags(item, window)["late"] == bool(flagged)]


def summary(year, owner_id=None):
    """Per owner, for logged (not withdrawn) entries paid in `year`."""
    window = FounderExpenseSettings.load()
    names = category_names()
    owners = {}
    for expense in expenses(owner_id=owner_id, year=year):
        row = owners.setdefault(expense.owner_contact_id, {
            "owner_id": expense.owner_contact_id, "owner_name": expense.owner_contact.name,
            "total": Decimal("0.00"), "business_total": Decimal("0.00"), "count": 0, "categories": defaultdict(lambda: [Decimal("0.00"), 0]),
            "late_count": 0, "flagged_count": 0, "missing_receipts": [],
        })
        row["total"] += expense.amount
        row["business_total"] += business_amount(expense)
        row["count"] += 1
        row["categories"][expense.category][0] += expense.amount
        row["categories"][expense.category][1] += 1
        late = flags(expense, window)["late"]
        row["late_count"] += late
        row["flagged_count"] += late
        if not has_receipt(expense):
            row["missing_receipts"].append({"id": expense.pk, "date_paid": expense.date_paid.isoformat(), "vendor": expense.vendor, "amount": money(expense.amount)})
    result = []
    for row in sorted(owners.values(), key=lambda item: (item["owner_name"], item["owner_id"])):
        categories = sorted(row.pop("categories").items(), key=lambda item: (item[0] == "", item[0]))
        row["by_category"] = [{"category": code or None, "category_name": names.get(code, "") if code else "Uncategorized", "total": money(total), "count": count} for code, (total, count) in categories]
        row["total"] = money(row["total"])
        row["business_total"] = money(row["business_total"])
        row["missing_receipts_count"] = len(row["missing_receipts"])
        result.append(row)
    return result


# -- Changes --------------------------------------------------------------------


def sniff_type(head):
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_receipt(upload):
    """Founder receipts: PDF, PNG, JPEG, or WebP, 10 MB or smaller, and the bytes must be what the type says. Returns the content type."""
    declared = str(getattr(upload, "content_type", "") or "").strip().lower()
    declared = RECEIPT_TYPE_ALIASES.get(declared, declared)
    name = PurePath(str(upload.name or "")).name
    if upload.size > RECEIPT_MAX_BYTES:
        raise ValidationError({"receipts": f"{name or 'Receipt'}: receipts must be 10 MB or smaller."})
    if upload.size == 0:
        raise ValidationError({"receipts": f"{name or 'Receipt'}: the file is empty."})
    if declared not in RECEIPT_TYPES:
        raise ValidationError({"receipts": f"{name or 'Receipt'}: receipts must be PDF, PNG, JPEG, or WebP files."})
    upload.seek(0)
    head = upload.read(12)
    upload.seek(0)
    actual = sniff_type(head)
    if actual != declared:
        found = RECEIPT_TYPES.get(actual, "not a PDF or image")
        raise ValidationError({"receipts": f"{name or 'Receipt'}: the file says it is {RECEIPT_TYPES[declared]} but its contents are {found}."})
    return declared


def file_sha256(upload):
    digest = hashlib.sha256()
    upload.seek(0)
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)
    return digest.hexdigest()


def add_receipt(*, user, expense, upload):
    content_type = validate_receipt(upload)
    digest = file_sha256(upload)
    name = PurePath(str(upload.name)).name[:255] or "receipt"
    return FounderExpenseReceipt.objects.create(expense=expense, file=upload, original_name=name, content_type=content_type, size=upload.size, sha256=digest, uploaded_by=user)


def possible_duplicates(values, exclude_pk=None):
    """Active entries with the same owner, vendor, date paid, and full charge."""
    query = FounderExpense.objects.filter(
        status=FounderExpense.Status.LOGGED, owner_contact=values["owner_contact"], date_paid=values["date_paid"], amount=values["amount"],
    ).order_by("pk")
    if exclude_pk is not None:
        query = query.exclude(pk=exclude_pk)
    return [item.pk for item in query if normalized(item.vendor) == normalized(values["vendor"])]


def duplicate_message(ids):
    listed = ", ".join(f"#{pk}" for pk in ids)
    return f"Possibly a duplicate of entry {listed} (same owner, vendor, date paid, and full amount). Confirm to save it anyway."


def check_new_expense(data, receipts=()):
    """Everything create_expense would decide, without saving: values, field errors, duplicates, and the deadline if logged today."""
    errors = {}
    try:
        values = clean_values(data)
    except ValidationError as error:
        values = None
        errors.update(error.message_dict)
    for upload in receipts:
        try:
            validate_receipt(upload)
        except ValidationError as error:
            errors.setdefault("receipts", []).extend(error.message_dict["receipts"])
    if errors:
        return {"values": None, "errors": errors, "duplicates": [], "status": None}
    window = FounderExpenseSettings.load()
    return {
        "values": values, "errors": {}, "duplicates": possible_duplicates(values),
        "status": deadline_status(values["date_paid"], timezone.localdate(), values["amount"], window),
    }


@transaction.atomic
def create_expense(*, user, data, receipts=(), source=FounderExpense.Source.MANUAL, confirm_duplicate=False):
    values = clean_values(data)
    for upload in receipts:
        validate_receipt(upload)
    if not confirm_duplicate:
        duplicates = possible_duplicates(values)
        if duplicates:
            raise ValidationError({"confirm_duplicate": ValidationError(duplicate_message(duplicates), code="possible_duplicate")})
    expense = FounderExpense.objects.create(**values, source=source, created_by=user, updated_by=user)
    for upload in receipts:
        add_receipt(user=user, expense=expense, upload=upload)
    record_audit(user, "created", expense, after=audit_snapshot(expense))
    return expense


def forget_cached_receipts(expense):
    """A caller may have prefetched receipts; drop that list so the audit snapshot sees the files just added or removed."""
    getattr(expense, "_prefetched_objects_cache", {}).pop("receipts", None)


def ensure_logged(expense):
    if expense.status == FounderExpense.Status.WITHDRAWN:
        raise ValidationError("This entry is withdrawn and can no longer be changed.")


@transaction.atomic
def update_expense(*, user, expense, data, receipts=()):
    ensure_logged(expense)
    before = audit_snapshot(expense)
    merged = {key: getattr(expense, key) for key in EDITABLE}
    # Rows saved before business amounts existed, and rows where the business part is the
    # whole charge, keep following the full charge when only the amount is edited.
    if expense.business_amount is None or expense.business_amount == expense.amount:
        merged["business_amount"] = None
    merged.update({key: value for key, value in data.items() if key in EDITABLE or key == "owner_id"})
    if "owner_id" in data:
        merged.pop("owner_contact")
    values = clean_values(merged, logged_on=logged_on(expense))
    for upload in receipts:
        validate_receipt(upload)
    for key, value in values.items():
        setattr(expense, key, value)
    expense.updated_by = user
    expense.save()
    for upload in receipts:
        add_receipt(user=user, expense=expense, upload=upload)
    forget_cached_receipts(expense)
    record_audit(user, "updated", expense, before=before, after=audit_snapshot(expense))
    return expense


@transaction.atomic
def withdraw_expense(*, user, expense, reason):
    ensure_logged(expense)
    reason = str(reason or "").strip()
    if not reason:
        raise ValidationError({"withdrawn_reason": "Say why this entry is being withdrawn."})
    before = audit_snapshot(expense)
    expense.status = FounderExpense.Status.WITHDRAWN
    expense.withdrawn_reason = reason
    expense.withdrawn_at = timezone.now()
    expense.withdrawn_by = user
    expense.updated_by = user
    expense.save()
    record_audit(user, "withdrew", expense, before=before, after=audit_snapshot(expense))
    return expense


@transaction.atomic
def upload_receipts(*, user, expense, receipts):
    ensure_logged(expense)
    if not receipts:
        raise ValidationError({"receipts": "Choose at least one file."})
    for upload in receipts:
        validate_receipt(upload)
    before = audit_snapshot(expense)
    for upload in receipts:
        add_receipt(user=user, expense=expense, upload=upload)
    forget_cached_receipts(expense)
    record_audit(user, "updated", expense, before=before, after=audit_snapshot(expense))
    return expense


@transaction.atomic
def remove_receipt(*, user, receipt):
    expense = receipt.expense
    ensure_logged(expense)
    before = audit_snapshot(expense)
    receipt.file.delete(save=False)
    receipt.delete()
    record_audit(user, "updated", expense, before=before, after=audit_snapshot(expense))


def settings_json(window=None):
    window = window or FounderExpenseSettings.load()
    return {
        "special_window_start": window.special_window_start.isoformat() if window.special_window_start else None,
        "special_window_end": window.special_window_end.isoformat() if window.special_window_end else None,
        "special_window_deadline": window.special_window_deadline.isoformat() if window.special_window_deadline else None,
        "standard_deadline_days": window.standard_deadline_days,
        "late_after_days": window.standard_deadline_days,  # older name for standard_deadline_days
        "approval_threshold": money(APPROVAL_THRESHOLD),
    }


@transaction.atomic
def save_settings(*, user, start, end, deadline, standard_deadline_days=None):
    """The window is set (or cleared) as a whole; standard_deadline_days=None leaves that value unchanged."""
    if standard_deadline_days is not None:
        try:
            standard_deadline_days = int(str(standard_deadline_days).strip())
        except (TypeError, ValueError) as exc:
            raise ValidationError({"standard_deadline_days": "Enter a whole number of days."}) from exc
        if not 1 <= standard_deadline_days <= STANDARD_DEADLINE_MAX_DAYS:
            raise ValidationError({"standard_deadline_days": f"Enter between 1 and {STANDARD_DEADLINE_MAX_DAYS} days."})
    given = [value is not None for value in (start, end, deadline)]
    if any(given) and not all(given):
        raise ValidationError({"special_window_deadline": "Set the first payment date, last payment date, and deadline together, or clear all three."})
    if start and start > end:
        raise ValidationError({"special_window_end": "The last payment date must be on or after the first."})
    if deadline and deadline < end:
        raise ValidationError({"special_window_deadline": "The deadline must be on or after the last payment date."})
    window = FounderExpenseSettings.load()
    before = settings_json(window)
    window.special_window_start, window.special_window_end, window.special_window_deadline = start, end, deadline
    if standard_deadline_days is not None:
        window.standard_deadline_days = standard_deadline_days
    window.updated_by = user
    window.save()
    record_audit(user, "updated", window, before=before, after=settings_json(window))
    return window


# -- CSV ------------------------------------------------------------------------


def normalized(text):
    return " ".join(str(text).lower().split())


def header_key(name):
    return re.sub(r"[\s\-]+", "_", str(name or "").strip().lower())


def duplicate_key(values):
    return (values["owner_contact"].pk, values["date_paid"], normalized(values["vendor"]), values["amount"], normalized(values["description"]))


def read_csv(upload):
    """Parse and validate a whole file. Returns (file_errors, rows); nothing is saved."""
    if upload.size > CSV_MAX_BYTES:
        return ["The file must be 2 MB or smaller."], []
    try:
        text = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return ["The file must be UTF-8 text (save it from your spreadsheet as CSV UTF-8)."], []
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header:
        return ["The file is empty."], []
    columns = [header_key(name) for name in header]
    missing = [name for name in CSV_REQUIRED if name not in columns]
    if missing:
        return [f"Missing column(s): {', '.join(missing)}. Expected: {', '.join(CSV_REQUIRED + CSV_OPTIONAL)}."], []

    owners = defaultdict(list)
    for owner in Contact.objects.filter(kind=Contact.Kind.OWNER).exclude(email=""):
        owners[owner.email.strip().lower()].append(owner)
    existing = {duplicate_key({"owner_contact": item.owner_contact, **{key: getattr(item, key) for key in ("date_paid", "vendor", "amount", "description")}}): item.pk for item in FounderExpense.objects.filter(status=FounderExpense.Status.LOGGED).select_related("owner_contact")}
    window = FounderExpenseSettings.load()
    seen = {}
    rows = []
    for cells in reader:
        if not any(cell.strip() for cell in cells):
            continue
        if len(rows) >= CSV_MAX_ROWS:
            return [f"Import at most {CSV_MAX_ROWS} rows per file."], []
        raw = {name: (cells[index].strip() if index < len(cells) else "") for index, name in enumerate(columns) if name}
        row = {"line": reader.line_num, "values": {name: raw.get(name, "") for name in CSV_REQUIRED + CSV_OPTIONAL}, "errors": [], "flags": []}
        rows.append(row)
        email = raw.get("owner_email", "").lower()
        matches = owners.get(email, [])
        data = {key: raw.get(key, "") for key in ("date_paid", "vendor", "description", "amount", "business_amount", "allocation_note", "business_purpose", "property_stays_personal", "receipt_url", "category")}
        if not email:
            row["errors"].append("owner_email: Owner email is required.")
        elif not matches:
            row["errors"].append(f"owner_email: No owner contact has the email {raw['owner_email']}.")
        elif len(matches) > 1:
            row["errors"].append(f"owner_email: More than one owner contact has the email {raw['owner_email']}.")
        else:
            data["owner_contact"] = matches[0]
        try:
            values = clean_values(data)
        except ValidationError as error:
            for field, messages in error.message_dict.items():
                if field == "owner_id" and "owner_contact" not in data:
                    continue  # already reported against owner_email
                row["errors"].extend(f"{field}: {message}" for message in messages)
            continue
        key = duplicate_key(values)
        if key in seen:
            row["errors"].append(f"Duplicate of line {seen[key]} in this file.")
            continue
        seen[key] = row["line"]
        if key in existing:
            row["errors"].append(f"Already logged as entry #{existing[key]}.")
            continue
        row["clean"] = values
        status = deadline_status(values["date_paid"], timezone.localdate(), values["amount"], window)
        if status["late"]:
            row["flags"].append(f"Late: past the {status['deadline']} logging deadline (logged {status['days_to_log']} days after payment).")
        if values["amount"] >= APPROVAL_THRESHOLD:
            row["flags"].append(APPROVAL_NOTE)
    if not rows:
        return ["The file has no data rows."], []
    return [], rows


def import_csv(*, user, upload, commit=False):
    file_errors, rows = read_csv(upload)
    error_count = sum(1 for row in rows if row["errors"])
    committed = []
    if commit and not file_errors and not error_count:
        with transaction.atomic():
            for row in rows:
                expense = FounderExpense.objects.create(**row["clean"], source=FounderExpense.Source.CSV_IMPORT, created_by=user, updated_by=user)
                record_audit(user, "created", expense, after=audit_snapshot(expense))
                committed.append(expense.pk)
    if file_errors or error_count:
        outcome = "Nothing was imported. Fix the errors below and upload the file again." if commit else "Fix the errors below before importing. Nothing has been saved."
    elif commit:
        outcome = f"Imported {len(committed)} entr{'y' if len(committed) == 1 else 'ies'}."
    else:
        outcome = f"{len(rows)} row(s) are ready to import. Nothing has been saved yet."
    return {
        "committed": bool(committed),
        "outcome": outcome,
        "file_errors": file_errors,
        "counts": {"rows": len(rows), "errors": error_count, "imported": len(committed)},
        "rows": [{key: value for key, value in row.items() if key != "clean"} for row in rows],
        "imported_ids": committed,
    }
