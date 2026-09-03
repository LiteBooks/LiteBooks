from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone

from .models import (
    AccountingPeriod,
    Attachment,
    AuditEvent,
    BusinessDocument,
    EntryNumberSequence,
    JournalEntry,
    JournalLine,
    OwnerActivity,
    Payment,
)


class PostingError(ValidationError):
    pass


def period_is_closed(entry_date):
    return AccountingPeriod.objects.filter(
        year=entry_date.year,
        month=entry_date.month,
        closed_at__isnull=False,
    ).exists()


def ensure_period_open(entry_date):
    if period_is_closed(entry_date):
        raise PostingError(f"{entry_date:%B %Y} is closed. Reopen it before changing entries.")


def next_entry_number(entry_date=None):
    entry_date = entry_date or date.today()
    sequence, created = EntryNumberSequence.objects.select_for_update().get_or_create(entry_date=entry_date)
    prefix = entry_date.strftime("%Y%m%d-")
    if created:
        latest = JournalEntry.objects.filter(number__startswith=prefix).aggregate(value=Max("number"))["value"]
        sequence.last_value = int(latest.rsplit("-", 1)[1]) if latest else 0
    sequence.last_value += 1
    if sequence.last_value > 9999:
        raise PostingError(f"The daily transaction limit for {entry_date:%B %d, %Y} has been reached.")
    sequence.save(update_fields=["last_value"])
    return f"{prefix}{sequence.last_value:04d}"


def entry_snapshot(entry):
    return {
        "number": entry.number,
        "date": entry.date.isoformat(),
        "description": entry.description,
        "memo": entry.memo,
        "status": entry.status,
        "source": entry.source,
        "contact_id": entry.contact_id,
        "linked_entry_id": entry.linked_entry_id,
        "version": entry.version,
        "lines": [
            {
                "account_id": line.account_id,
                "description": line.description,
                "debit": str(line.debit),
                "credit": str(line.credit),
                "owner_id": line.owner_id,
            }
            for line in entry.lines.all()
        ],
    }


def record_audit(actor, action, instance, before=None, after=None):
    AuditEvent.objects.create(
        actor=actor,
        action=action,
        object_type=instance._meta.label,
        object_id=str(instance.pk),
        before=before or {},
        after=after or {},
    )


def validate_entry(entry):
    lines = list(entry.lines.all())
    if len(lines) < 2:
        raise PostingError("A journal entry needs at least two lines.")
    if len({line.account_id for line in lines}) < 2:
        raise PostingError("A journal entry must touch at least two different accounts.")
    for line in lines:
        line.full_clean()
        if not line.account.is_active:
            raise PostingError(f"Account {line.account} is inactive and cannot receive new postings.")
    debit = sum((line.debit for line in lines), Decimal("0.00"))
    credit = sum((line.credit for line in lines), Decimal("0.00"))
    if debit <= 0 or debit != credit:
        raise PostingError(f"Debits ({debit:.2f}) must equal credits ({credit:.2f}).")


@transaction.atomic
def create_entry(*, entry_date, description, lines, user, memo="", contact=None, linked_entry=None, source=JournalEntry.Source.GENERAL, attachment=None):
    ensure_period_open(entry_date)
    if not description.strip():
        raise PostingError("Transaction description is required.")
    entry = JournalEntry.objects.create(
        number=next_entry_number(entry_date),
        date=entry_date,
        description=description,
        memo=memo,
        source=source,
        contact=contact,
        linked_entry=linked_entry,
        created_by=user,
    )
    for index, line in enumerate(lines):
        JournalLine.objects.create(entry=entry, sort_order=index, **line)
    validate_entry(entry)
    entry.status = JournalEntry.Status.POSTED
    entry.posted_by = user
    entry.posted_at = timezone.now()
    entry.save(update_fields=["status", "posted_by", "posted_at", "updated_at"])
    if attachment:
        add_attachment(entry, attachment, user)
    record_audit(user, "created", entry, after=entry_snapshot(entry))
    return entry


@transaction.atomic
def update_entry(*, entry, entry_date, description, lines, user, memo="", contact=None, linked_entry=None, attachment=None):
    if hasattr(entry, "business_document") or hasattr(entry, "payment") or hasattr(entry, "owner_activity"):
        raise PostingError("This entry is managed by its source document and cannot be edited here.")
    ensure_period_open(entry.date)
    ensure_period_open(entry_date)
    if not description.strip():
        raise PostingError("Transaction description is required.")
    if linked_entry == entry:
        raise PostingError("A transaction cannot link to itself.")
    before = entry_snapshot(entry)
    if entry.date != entry_date:
        entry.number = next_entry_number(entry_date)
    entry.date = entry_date
    entry.description = description
    entry.memo = memo
    entry.contact = contact
    entry.linked_entry = linked_entry
    entry.version += 1
    entry.save()
    entry.lines.all().delete()
    for index, line in enumerate(lines):
        JournalLine.objects.create(entry=entry, sort_order=index, **line)
    validate_entry(entry)
    if attachment:
        add_attachment(entry, attachment, user)
    record_audit(user, "updated", entry, before=before, after=entry_snapshot(entry))
    return entry


def add_attachment(entry, upload, user):
    allowed = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
    content_type = getattr(upload, "content_type", "")
    if upload.size > 25 * 1024 * 1024:
        raise PostingError("Attachments must be 25 MB or smaller.")
    if content_type not in allowed:
        raise PostingError("Attachments must be PDF, JPEG, PNG, or WebP files.")
    return Attachment.objects.create(
        entry=entry,
        file=upload,
        original_name=upload.name,
        content_type=content_type,
        size=upload.size,
        uploaded_by=user,
    )


@transaction.atomic
def create_document(*, kind, number, contact, issue_date, due_date, description, amount, control_account, category_account, user, attachment=None):
    amount = Decimal(amount)
    if amount <= 0:
        raise PostingError("Document amount must be greater than zero.")
    if kind == BusinessDocument.Kind.INVOICE:
        if control_account.subtype != control_account.Subtype.RECEIVABLE:
            raise PostingError("Invoices require an accounts receivable control account.")
        if category_account.type != category_account.Type.REVENUE:
            raise PostingError("Invoices require a revenue category account.")
        lines = [
            {"account": control_account, "debit": amount, "credit": 0, "owner": None},
            {"account": category_account, "debit": 0, "credit": amount, "owner": None},
        ]
        source = JournalEntry.Source.INVOICE
    else:
        if control_account.subtype != control_account.Subtype.PAYABLE:
            raise PostingError("Bills require an accounts payable control account.")
        if category_account.type != category_account.Type.EXPENSE:
            raise PostingError("Bills require an expense category account.")
        lines = [
            {"account": category_account, "debit": amount, "credit": 0, "owner": None},
            {"account": control_account, "debit": 0, "credit": amount, "owner": None},
        ]
        source = JournalEntry.Source.BILL
    entry = create_entry(
        entry_date=issue_date,
        description=f"{BusinessDocument.Kind(kind).label} {number}: {description}",
        lines=lines,
        user=user,
        contact=contact,
        source=source,
        attachment=attachment,
    )
    document = BusinessDocument.objects.create(
        kind=kind,
        number=number,
        contact=contact,
        issue_date=issue_date,
        due_date=due_date,
        description=description,
        amount=amount,
        control_account=control_account,
        category_account=category_account,
        journal_entry=entry,
    )
    record_audit(user, "created", document, after={"number": number, "amount": str(amount), "journal_entry_id": entry.pk})
    return document


@transaction.atomic
def create_payment(*, document, payment_date, amount, cash_account, user, memo="", attachment=None):
    document = BusinessDocument.objects.select_for_update().get(pk=document.pk)
    amount = Decimal(amount)
    if amount <= 0 or amount > document.balance_due:
        raise PostingError(f"Payment must be greater than zero and no more than {document.balance_due:.2f}.")
    if document.status == BusinessDocument.Status.VOID:
        raise PostingError("A void document cannot be paid.")
    if cash_account.subtype not in {cash_account.Subtype.BANK, cash_account.Subtype.CASH}:
        raise PostingError("Payments require a bank or cash account.")
    if document.kind == BusinessDocument.Kind.INVOICE:
        lines = [
            {"account": cash_account, "debit": amount, "credit": 0, "owner": None},
            {"account": document.control_account, "debit": 0, "credit": amount, "owner": None},
        ]
        description = f"Payment received for invoice {document.number}"
    else:
        lines = [
            {"account": document.control_account, "debit": amount, "credit": 0, "owner": None},
            {"account": cash_account, "debit": 0, "credit": amount, "owner": None},
        ]
        description = f"Payment made for bill {document.number}"
    entry = create_entry(
        entry_date=payment_date,
        description=description,
        memo=memo,
        lines=lines,
        user=user,
        contact=document.contact,
        linked_entry=document.journal_entry,
        source=JournalEntry.Source.PAYMENT,
        attachment=attachment,
    )
    payment = Payment.objects.create(
        document=document,
        date=payment_date,
        amount=amount,
        cash_account=cash_account,
        journal_entry=entry,
    )
    paid = document.payments.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    document.status = BusinessDocument.Status.PAID if paid >= document.amount else BusinessDocument.Status.PARTIAL
    document.save(update_fields=["status", "updated_at"])
    record_audit(user, "created", payment, after={"document_id": document.pk, "amount": str(amount), "journal_entry_id": entry.pk})
    return payment


@transaction.atomic
def create_owner_activity(*, owner, kind, activity_date, amount, cash_account, offset_account, user, memo="", attachment=None):
    amount = Decimal(amount)
    if amount <= 0:
        raise PostingError("Amount must be greater than zero.")
    if owner.kind != owner.Kind.OWNER:
        raise PostingError("Owner activity requires an owner contact.")
    if cash_account.subtype not in {cash_account.Subtype.BANK, cash_account.Subtype.CASH}:
        raise PostingError("Owner activity requires a bank or cash account.")
    expected_subtypes = {
        OwnerActivity.Kind.CONTRIBUTION: {offset_account.Subtype.OWNER_CONTRIBUTION},
        OwnerActivity.Kind.DRAW: {offset_account.Subtype.OWNER_DRAW},
        OwnerActivity.Kind.LOAN_IN: {offset_account.Subtype.OWNER_LOAN_PAYABLE},
        OwnerActivity.Kind.LOAN_REPAYMENT: {offset_account.Subtype.OWNER_LOAN_PAYABLE},
    }
    if offset_account.subtype not in expected_subtypes[kind]:
        raise PostingError("The owner equity or loan account does not match the selected activity.")
    if kind == OwnerActivity.Kind.LOAN_REPAYMENT:
        owed = JournalLine.objects.filter(
            entry__status=JournalEntry.Status.POSTED,
            owner=owner,
            account__subtype=offset_account.Subtype.OWNER_LOAN_PAYABLE,
        ).aggregate(credits=Sum("credit"), debits=Sum("debit"))
        outstanding = (owed["credits"] or Decimal("0.00")) - (owed["debits"] or Decimal("0.00"))
        if amount > outstanding:
            raise PostingError(f"The business currently owes this owner {outstanding:.2f}.")
    if kind in {OwnerActivity.Kind.CONTRIBUTION, OwnerActivity.Kind.LOAN_IN}:
        lines = [
            {"account": cash_account, "debit": amount, "credit": 0, "owner": owner},
            {"account": offset_account, "debit": 0, "credit": amount, "owner": owner},
        ]
    else:
        lines = [
            {"account": offset_account, "debit": amount, "credit": 0, "owner": owner},
            {"account": cash_account, "debit": 0, "credit": amount, "owner": owner},
        ]
    entry = create_entry(
        entry_date=activity_date,
        description=f"{OwnerActivity.Kind(kind).label}: {owner.name}",
        memo=memo,
        lines=lines,
        user=user,
        contact=owner,
        source=JournalEntry.Source.OWNER,
        attachment=attachment,
    )
    activity = OwnerActivity.objects.create(owner=owner, kind=kind, amount=amount, journal_entry=entry)
    record_audit(user, "created", activity, after={"owner_id": owner.pk, "kind": kind, "amount": str(amount)})
    return activity
