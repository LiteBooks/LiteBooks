import json
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib.auth import authenticate, get_user_model, login, logout, update_session_auth_hash
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import IntegrityError
from django.db.models import Q, Sum
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie

from .forms import AccountForm, AccountingPeriodForm, BusinessDocumentForm, ContactForm, ProfileForm, UserCreateForm, UserUpdateForm
from .models import Account, AccountingPeriod, AuditEvent, BusinessDocument, Contact, JournalEntry, JournalLine, OwnerActivity, UserProfile
from .permissions import ROLE_LEVEL, user_role
from .reporting import account_rows, aging, balance_sheet, income_statement, owner_balances, trial_balance
from .services import create_document, create_entry, create_owner_activity, create_payment, record_audit, update_entry


def money(value):
    return f"{Decimal(value or 0):.2f}"


def choice_list(choices):
    return [{"value": value, "label": label} for value, label in choices]


def request_data(request):
    if request.content_type and request.content_type.startswith("application/json"):
        return json.loads(request.body or b"{}")
    return request.POST.dict()


def date_value(value, field="date"):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError({field: "Enter a valid date."}) from exc


def decimal_value(value, field="amount"):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field: "Enter a valid amount."}) from exc


def optional_model(model, value):
    if value in (None, "", 0, "0"):
        return None
    return model.objects.get(pk=value)


def validation_payload(error):
    if hasattr(error, "message_dict"):
        return error.message_dict
    return {"__all__": list(getattr(error, "messages", [str(error)]))}


def form_payload(form):
    return {field: [str(item) for item in errors] for field, errors in form.errors.items()}


def api_view(methods=("GET",), minimum_role=None, authenticated=True):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method not in methods:
                return JsonResponse({"error": "Method not allowed."}, status=405)
            if authenticated and not request.user.is_authenticated:
                return JsonResponse({"error": "Authentication required."}, status=401)
            if minimum_role and ROLE_LEVEL[user_role(request.user)] < ROLE_LEVEL[minimum_role]:
                return JsonResponse({"error": "You do not have permission to perform this action."}, status=403)
            try:
                return view(request, *args, **kwargs)
            except json.JSONDecodeError:
                return JsonResponse({"error": "Invalid JSON request."}, status=400)
            except ValidationError as error:
                return JsonResponse({"error": "Please correct the highlighted fields.", "fields": validation_payload(error)}, status=400)
            except (ObjectDoesNotExist, ValueError, TypeError) as error:
                return JsonResponse({"error": str(error) or "The requested record was not found."}, status=400)
            except IntegrityError:
                return JsonResponse({"error": "That value conflicts with an existing record."}, status=409)

        return wrapped

    return decorator


def account_json(account, balance=None):
    result = {
        "id": account.pk,
        "code": account.code,
        "name": account.name,
        "display_name": str(account),
        "type": account.type,
        "type_label": account.get_type_display(),
        "subtype": account.subtype,
        "subtype_label": account.get_subtype_display() if account.subtype else "",
        "description": account.description,
        "is_active": account.is_active,
        "normal_balance": account.normal_balance,
    }
    if balance is not None:
        result["balance"] = money(balance)
    return result


def contact_json(contact):
    return {
        "id": contact.pk,
        "name": contact.name,
        "kind": contact.kind,
        "kind_label": contact.get_kind_display(),
        "email": contact.email,
        "phone": contact.phone,
        "address": contact.address,
        "notes": contact.notes,
        "is_active": contact.is_active,
    }


def entry_json(entry, detail=False):
    lines = list(entry.lines.all())
    result = {
        "id": entry.pk,
        "number": entry.number,
        "date": entry.date.isoformat(),
        "description": entry.description,
        "memo": entry.memo,
        "status": entry.status,
        "source": entry.source,
        "source_label": entry.get_source_display(),
        "contact": contact_json(entry.contact) if entry.contact else None,
        "linked_entry": {"id": entry.linked_entry_id, "number": entry.linked_entry.number} if entry.linked_entry else None,
        "total": money(sum((line.debit for line in lines), Decimal("0.00"))),
        "version": entry.version,
        "can_edit": entry.source == JournalEntry.Source.GENERAL,
        "accounts": [line.account.name for line in lines],
    }
    if detail:
        result.update({
            "created_by": entry.created_by.get_full_name() or entry.created_by.username,
            "posted_by": (entry.posted_by.get_full_name() or entry.posted_by.username) if entry.posted_by else "",
            "posted_at": entry.posted_at.isoformat() if entry.posted_at else None,
            "lines": [{
                "id": line.pk,
                "account": account_json(line.account),
                "description": line.description,
                "debit": money(line.debit),
                "credit": money(line.credit),
                "owner": contact_json(line.owner) if line.owner else None,
            } for line in lines],
            "attachments": [{
                "id": item.pk,
                "name": item.original_name,
                "size": item.size,
                "content_type": item.content_type,
                "download_url": f"/attachments/{item.pk}/download/",
            } for item in entry.attachments.all()],
        })
    return result


def document_json(document, detail=False):
    result = {
        "id": document.pk,
        "kind": document.kind,
        "kind_label": document.get_kind_display(),
        "number": document.number,
        "contact": contact_json(document.contact),
        "issue_date": document.issue_date.isoformat(),
        "due_date": document.due_date.isoformat(),
        "description": document.description,
        "amount": money(document.amount),
        "amount_paid": money(document.amount_paid),
        "balance_due": money(document.balance_due),
        "status": document.status,
        "status_label": document.get_status_display(),
        "journal_entry": {"id": document.journal_entry_id, "number": document.journal_entry.number},
    }
    if detail:
        result.update({
            "control_account": account_json(document.control_account),
            "category_account": account_json(document.category_account),
            "payments": [{
                "id": payment.pk,
                "date": payment.date.isoformat(),
                "amount": money(payment.amount),
                "cash_account": account_json(payment.cash_account),
                "journal_entry": {"id": payment.journal_entry_id, "number": payment.journal_entry.number},
            } for payment in document.payments.all()],
        })
    return result


def audit_json(event):
    return {
        "id": event.pk,
        "actor": (event.actor.get_full_name() or event.actor.username) if event.actor else "System",
        "action": event.action,
        "object_type": event.object_type,
        "object_id": event.object_id,
        "before": event.before,
        "after": event.after,
        "created_at": event.created_at.isoformat(),
    }


@ensure_csrf_cookie
@api_view(authenticated=False)
def session_api(request):
    get_token(request)
    if not request.user.is_authenticated:
        return JsonResponse({"authenticated": False})
    role = user_role(request.user)
    return JsonResponse({
        "authenticated": True,
        "user": {"id": request.user.pk, "username": request.user.username, "name": request.user.get_full_name() or request.user.username, "role": role, "role_label": UserProfile.Role(role).label},
        "permissions": {"edit_books": ROLE_LEVEL[role] >= ROLE_LEVEL[UserProfile.Role.BOOKKEEPER], "administer": ROLE_LEVEL[role] >= ROLE_LEVEL[UserProfile.Role.ADMIN]},
    })


@api_view(methods=("POST",), authenticated=False)
def login_api(request):
    data = request_data(request)
    user = authenticate(request, username=data.get("username", ""), password=data.get("password", ""))
    if not user:
        return JsonResponse({"error": "The username or password is incorrect."}, status=400)
    if not user.is_active:
        return JsonResponse({"error": "This login is inactive."}, status=403)
    login(request, user)
    return JsonResponse({"ok": True})


@api_view(methods=("POST",))
def logout_api(request):
    logout(request)
    return JsonResponse({"ok": True})


@api_view(methods=("GET", "POST"))
def profile_api(request):
    user = request.user
    if request.method == "GET":
        return JsonResponse({"profile": {"username": user.username, "first_name": user.first_name, "last_name": user.last_name}})
    before = {"username": user.username, "first_name": user.first_name, "last_name": user.last_name}
    form = ProfileForm(request_data(request), instance=user)
    if not form.is_valid():
        return JsonResponse({"error": "Please correct the highlighted fields.", "fields": form_payload(form)}, status=400)
    password_changed = bool(form.cleaned_data.get("new_password"))
    user = form.save()
    if password_changed:
        update_session_auth_hash(request, user)
    after = {"username": user.username, "first_name": user.first_name, "last_name": user.last_name}
    record_audit(user, "updated own profile", user.profile, before=before, after=after)
    return JsonResponse({"ok": True, "password_changed": password_changed, "profile": after})


@api_view()
def options_api(request):
    return JsonResponse({
        "accounts": [account_json(item) for item in Account.objects.order_by("code")],
        "contacts": [contact_json(item) for item in Contact.objects.order_by("name")],
        "entries": [{"id": item.pk, "number": item.number, "date": item.date.isoformat(), "description": item.description} for item in JournalEntry.objects.filter(status=JournalEntry.Status.POSTED)[:500]],
        "choices": {
            "account_types": choice_list(Account.Type.choices),
            "account_subtypes": choice_list(Account.Subtype.choices),
            "contact_kinds": choice_list(Contact.Kind.choices),
            "entry_sources": choice_list(JournalEntry.Source.choices),
            "owner_activity_kinds": choice_list(OwnerActivity.Kind.choices),
            "roles": choice_list(UserProfile.Role.choices),
        },
    })


@api_view()
def dashboard_api(request):
    today = date.today()
    pnl = income_statement(date(today.year, 1, 1), today)
    cash = sum((row["balance"] for row in account_rows(end=today) if row["account"].subtype in [Account.Subtype.BANK, Account.Subtype.CASH]), Decimal("0.00"))
    invoices = BusinessDocument.objects.filter(kind=BusinessDocument.Kind.INVOICE).exclude(status__in=[BusinessDocument.Status.PAID, BusinessDocument.Status.VOID])
    bills = BusinessDocument.objects.filter(kind=BusinessDocument.Kind.BILL).exclude(status__in=[BusinessDocument.Status.PAID, BusinessDocument.Status.VOID])
    recent = JournalEntry.objects.filter(status=JournalEntry.Status.POSTED).select_related("contact", "linked_entry").prefetch_related("lines__account")[:8]
    return JsonResponse({
        "metrics": {"cash": money(cash), "receivables": money(sum((item.balance_due for item in invoices), Decimal("0.00"))), "payables": money(sum((item.balance_due for item in bills), Decimal("0.00"))), "net_income": money(pnl["net_income"])},
        "recent_entries": [entry_json(item) for item in recent],
    })


def parse_lines(data):
    raw_lines = data.get("lines", [])
    if isinstance(raw_lines, str):
        raw_lines = json.loads(raw_lines)
    lines = []
    for index, row in enumerate(raw_lines):
        lines.append({
            "account": Account.objects.get(pk=row.get("account_id")),
            "description": row.get("description", ""),
            "debit": decimal_value(row.get("debit") or 0, f"lines.{index}.debit"),
            "credit": decimal_value(row.get("credit") or 0, f"lines.{index}.credit"),
            "owner": optional_model(Contact, row.get("owner_id")),
        })
    return lines


@api_view(methods=("GET", "POST"))
def transactions_api(request):
    if request.method == "GET":
        entries = JournalEntry.objects.select_related("contact", "linked_entry").prefetch_related("lines__account")
        query = request.GET.get("q", "").strip()
        if query:
            entries = entries.filter(Q(number__icontains=query) | Q(description__icontains=query) | Q(memo__icontains=query) | Q(contact__name__icontains=query)).distinct()
        if request.GET.get("account"):
            entries = entries.filter(lines__account_id=request.GET["account"]).distinct()
        if request.GET.get("source"):
            entries = entries.filter(source=request.GET["source"])
        if request.GET.get("start"):
            entries = entries.filter(date__gte=date_value(request.GET["start"], "start"))
        if request.GET.get("end"):
            entries = entries.filter(date__lte=date_value(request.GET["end"], "end"))
        return JsonResponse({"entries": [entry_json(item) for item in entries]})
    if ROLE_LEVEL[user_role(request.user)] < ROLE_LEVEL[UserProfile.Role.BOOKKEEPER]:
        return JsonResponse({"error": "You do not have permission to post transactions."}, status=403)
    data = request_data(request)
    entry_date = date_value(data.get("date"))
    if data.get("mode") == "quick":
        amount = decimal_value(data.get("amount"))
        lines = [
            {"account": Account.objects.get(pk=data.get("debit_account_id")), "debit": amount, "credit": 0, "owner": None},
            {"account": Account.objects.get(pk=data.get("credit_account_id")), "debit": 0, "credit": amount, "owner": None},
        ]
    else:
        lines = parse_lines(data)
    entry = create_entry(
        entry_date=entry_date,
        description=data.get("description", "").strip(),
        memo=data.get("memo", "").strip(),
        contact=optional_model(Contact, data.get("contact_id")),
        linked_entry=optional_model(JournalEntry, data.get("linked_entry_id")),
        attachment=request.FILES.get("attachment"),
        user=request.user,
        lines=lines,
    )
    return JsonResponse({"entry": entry_json(entry, detail=True)}, status=201)


@api_view(methods=("GET", "POST"))
def transaction_api(request, pk):
    entry = get_object_or_404(JournalEntry.objects.select_related("contact", "linked_entry", "created_by", "posted_by").prefetch_related("lines__account", "lines__owner", "attachments"), pk=pk)
    if request.method == "GET":
        audits = AuditEvent.objects.filter(object_type="ledger.JournalEntry", object_id=str(pk))
        return JsonResponse({"entry": entry_json(entry, detail=True), "audits": [audit_json(item) for item in audits]})
    if ROLE_LEVEL[user_role(request.user)] < ROLE_LEVEL[UserProfile.Role.BOOKKEEPER]:
        return JsonResponse({"error": "You do not have permission to edit transactions."}, status=403)
    data = request_data(request)
    entry = update_entry(
        entry=entry,
        entry_date=date_value(data.get("date")),
        description=data.get("description", "").strip(),
        memo=data.get("memo", "").strip(),
        contact=optional_model(Contact, data.get("contact_id")),
        linked_entry=optional_model(JournalEntry, data.get("linked_entry_id")),
        attachment=request.FILES.get("attachment"),
        user=request.user,
        lines=parse_lines(data),
    )
    return JsonResponse({"entry": entry_json(entry, detail=True)})


@api_view(methods=("GET", "POST"))
def accounts_api(request):
    if request.method == "GET":
        rows = []
        for account in Account.objects.annotate(debits=Sum("journal_lines__debit", filter=Q(journal_lines__entry__status="posted")), credits=Sum("journal_lines__credit", filter=Q(journal_lines__entry__status="posted"))):
            raw = (account.debits or 0) - (account.credits or 0)
            balance = raw if account.normal_balance == "debit" else -raw
            rows.append(account_json(account, balance))
        return JsonResponse({"accounts": rows})
    if ROLE_LEVEL[user_role(request.user)] < ROLE_LEVEL[UserProfile.Role.ADMIN]:
        return JsonResponse({"error": "Administrator access is required."}, status=403)
    form = AccountForm(request_data(request))
    if not form.is_valid():
        return JsonResponse({"error": "Please correct the highlighted fields.", "fields": form_payload(form)}, status=400)
    account = form.save()
    record_audit(request.user, "created", account, after={"code": account.code, "name": account.name, "type": account.type, "subtype": account.subtype})
    return JsonResponse({"account": account_json(account, Decimal("0.00"))}, status=201)


@api_view(methods=("GET", "POST"))
def account_api(request, pk):
    account = get_object_or_404(Account, pk=pk)
    lines = JournalLine.objects.filter(account=account, entry__status=JournalEntry.Status.POSTED).select_related("entry", "owner").order_by("entry__date", "entry__number", "sort_order")
    if request.method == "GET":
        running = Decimal("0.00")
        ledger = []
        for line in lines:
            movement = line.debit - line.credit
            running += movement if account.normal_balance == "debit" else -movement
            ledger.append({"id": line.pk, "date": line.entry.date.isoformat(), "entry": {"id": line.entry_id, "number": line.entry.number}, "description": line.description or line.entry.description, "debit": money(line.debit), "credit": money(line.credit), "balance": money(running)})
        return JsonResponse({"account": account_json(account, running), "ledger": ledger})
    if ROLE_LEVEL[user_role(request.user)] < ROLE_LEVEL[UserProfile.Role.ADMIN]:
        return JsonResponse({"error": "Administrator access is required."}, status=403)
    before = account_json(account)
    form = AccountForm(request_data(request), instance=account)
    if not form.is_valid():
        return JsonResponse({"error": "Please correct the highlighted fields.", "fields": form_payload(form)}, status=400)
    account = form.save()
    record_audit(request.user, "updated", account, before=before, after=account_json(account))
    return JsonResponse({"account": account_json(account)})


@api_view(methods=("GET", "POST"))
def contacts_api(request):
    if request.method == "GET":
        contacts = Contact.objects.all()
        if request.GET.get("kind"):
            contacts = contacts.filter(kind=request.GET["kind"])
        return JsonResponse({"contacts": [contact_json(item) for item in contacts]})
    if ROLE_LEVEL[user_role(request.user)] < ROLE_LEVEL[UserProfile.Role.BOOKKEEPER]:
        return JsonResponse({"error": "Bookkeeper access is required."}, status=403)
    form = ContactForm(request_data(request))
    if not form.is_valid():
        return JsonResponse({"error": "Please correct the highlighted fields.", "fields": form_payload(form)}, status=400)
    contact = form.save()
    record_audit(request.user, "created", contact, after=contact_json(contact))
    return JsonResponse({"contact": contact_json(contact)}, status=201)


@api_view(methods=("GET", "POST"))
def contact_api(request, pk):
    contact = get_object_or_404(Contact, pk=pk)
    if request.method == "GET":
        return JsonResponse({"contact": contact_json(contact)})
    if ROLE_LEVEL[user_role(request.user)] < ROLE_LEVEL[UserProfile.Role.BOOKKEEPER]:
        return JsonResponse({"error": "Bookkeeper access is required."}, status=403)
    before = contact_json(contact)
    form = ContactForm(request_data(request), instance=contact)
    if not form.is_valid():
        return JsonResponse({"error": "Please correct the highlighted fields.", "fields": form_payload(form)}, status=400)
    contact = form.save()
    record_audit(request.user, "updated", contact, before=before, after=contact_json(contact))
    return JsonResponse({"contact": contact_json(contact)})


@api_view(methods=("GET", "POST"))
def documents_api(request):
    if request.method == "GET":
        kind = request.GET.get("kind", BusinessDocument.Kind.INVOICE)
        documents = BusinessDocument.objects.filter(kind=kind).select_related("contact", "journal_entry")
        return JsonResponse({"documents": [document_json(item) for item in documents], "kind": kind})
    if ROLE_LEVEL[user_role(request.user)] < ROLE_LEVEL[UserProfile.Role.BOOKKEEPER]:
        return JsonResponse({"error": "Bookkeeper access is required."}, status=403)
    data = request_data(request)
    kind = data.get("kind")
    if kind not in BusinessDocument.Kind.values:
        raise ValidationError({"kind": "Choose invoice or bill."})
    form = BusinessDocumentForm(data, request.FILES, kind=kind)
    if not form.is_valid():
        return JsonResponse({"error": "Please correct the highlighted fields.", "fields": form_payload(form)}, status=400)
    document = create_document(kind=kind, user=request.user, attachment=form.cleaned_data.pop("attachment"), **form.cleaned_data)
    return JsonResponse({"document": document_json(document, detail=True)}, status=201)


@api_view()
def document_api(request, pk):
    document = get_object_or_404(BusinessDocument.objects.select_related("contact", "journal_entry", "control_account", "category_account").prefetch_related("payments__journal_entry", "payments__cash_account"), pk=pk)
    return JsonResponse({"document": document_json(document, detail=True)})


@api_view(methods=("POST",), minimum_role=UserProfile.Role.BOOKKEEPER)
def payment_api(request, pk):
    document = get_object_or_404(BusinessDocument, pk=pk)
    data = request_data(request)
    payment = create_payment(document=document, payment_date=date_value(data.get("date")), amount=decimal_value(data.get("amount")), cash_account=Account.objects.get(pk=data.get("cash_account_id")), memo=data.get("memo", ""), attachment=request.FILES.get("attachment"), user=request.user)
    return JsonResponse({"entry_id": payment.journal_entry_id}, status=201)


@api_view(methods=("GET", "POST"))
def owners_api(request):
    if request.method == "GET":
        rows = owner_balances()
        activities = OwnerActivity.objects.select_related("owner", "journal_entry")[:50]
        return JsonResponse({
            "owners": [{"owner": contact_json(row["owner"]), "contributions": money(row["contributions"]), "draws": money(row["draws"]), "owed": money(row["owed"])} for row in rows],
            "activities": [{"id": item.pk, "date": item.journal_entry.date.isoformat(), "owner": contact_json(item.owner), "kind": item.kind, "kind_label": item.get_kind_display(), "amount": money(item.amount), "entry": {"id": item.journal_entry_id, "number": item.journal_entry.number}} for item in activities],
        })
    if ROLE_LEVEL[user_role(request.user)] < ROLE_LEVEL[UserProfile.Role.BOOKKEEPER]:
        return JsonResponse({"error": "Bookkeeper access is required."}, status=403)
    data = request_data(request)
    activity = create_owner_activity(owner=Contact.objects.get(pk=data.get("owner_id")), kind=data.get("kind"), activity_date=date_value(data.get("date")), amount=decimal_value(data.get("amount")), cash_account=Account.objects.get(pk=data.get("cash_account_id")), offset_account=Account.objects.get(pk=data.get("offset_account_id")), memo=data.get("memo", ""), attachment=request.FILES.get("attachment"), user=request.user)
    return JsonResponse({"entry_id": activity.journal_entry_id}, status=201)


@api_view()
def report_api(request, report):
    as_of = date_value(request.GET.get("as_of", date.today().isoformat()), "as_of")
    start = date_value(request.GET.get("start", date(as_of.year, 1, 1).isoformat()), "start")
    end = date_value(request.GET.get("end", as_of.isoformat()), "end")
    base = {"report": report, "as_of": as_of.isoformat(), "start": start.isoformat(), "end": end.isoformat()}
    if report == "trial-balance":
        rows, debits, credits = trial_balance(as_of)
        base.update({"title": "Trial balance", "rows": [{"account": account_json(row["account"]), "debit": money(row["debit_balance"]), "credit": money(row["credit_balance"])} for row in rows if row["debit_balance"] or row["credit_balance"]], "total_debits": money(debits), "total_credits": money(credits)})
    elif report == "balance-sheet":
        groups = balance_sheet(as_of)
        base.update({"title": "Balance sheet", "groups": {key: {"rows": [{"account": account_json(row["account"]), "balance": money(row["balance"])} for row in group["rows"]], "total": money(group["total"]), "current_earnings": money(group.get("current_earnings", 0))} for key, group in groups.items()}})
    elif report == "income-statement":
        statement = income_statement(start, end)
        base.update({"title": "Profit and loss", "revenue": [{"account": account_json(row["account"]), "balance": money(row["balance"])} for row in statement["revenue"]], "expenses": [{"account": account_json(row["account"]), "balance": money(row["balance"])} for row in statement["expenses"]], "revenue_total": money(statement["revenue_total"]), "expense_total": money(statement["expense_total"]), "net_income": money(statement["net_income"])})
    elif report in {"receivables-aging", "payables-aging"}:
        kind = BusinessDocument.Kind.INVOICE if report == "receivables-aging" else BusinessDocument.Kind.BILL
        rows, buckets = aging(kind, as_of)
        base.update({"title": "Accounts receivable aging" if kind == BusinessDocument.Kind.INVOICE else "Accounts payable aging", "rows": [{"document": document_json(row["document"]), "days": row["days"], "bucket": row["bucket"], "balance": money(row["balance"])} for row in rows], "buckets": {key: money(value) for key, value in buckets.items()}})
    elif report == "owner-balances":
        rows = owner_balances(as_of)
        base.update({"title": "Owner balances", "rows": [{"owner": contact_json(row["owner"]), "contributions": money(row["contributions"]), "draws": money(row["draws"]), "owed": money(row["owed"])} for row in rows]})
    elif report == "general-ledger":
        lines = JournalLine.objects.filter(entry__status=JournalEntry.Status.POSTED, entry__date__range=(start, end)).select_related("entry", "account", "owner").order_by("entry__date", "entry__number", "sort_order")
        base.update({"title": "General ledger", "lines": [{"id": line.pk, "date": line.entry.date.isoformat(), "entry": {"id": line.entry_id, "number": line.entry.number}, "account": account_json(line.account), "description": line.description or line.entry.description, "debit": money(line.debit), "credit": money(line.credit)} for line in lines]})
    else:
        return JsonResponse({"error": "Report not found."}, status=404)
    return JsonResponse(base)


@api_view(methods=("GET", "POST"), minimum_role=UserProfile.Role.ADMIN)
def periods_api(request):
    if request.method == "GET":
        periods = AccountingPeriod.objects.select_related("closed_by")
        return JsonResponse({"periods": [{"id": item.pk, "year": item.year, "month": item.month, "label": str(item), "is_closed": item.is_closed, "closed_at": item.closed_at.isoformat() if item.closed_at else None, "closed_by": item.closed_by.username if item.closed_by else ""} for item in periods]})
    form = AccountingPeriodForm(request_data(request))
    if not form.is_valid():
        return JsonResponse({"error": "Please correct the highlighted fields.", "fields": form_payload(form)}, status=400)
    period, created = AccountingPeriod.objects.get_or_create(**form.cleaned_data)
    return JsonResponse({"id": period.pk, "created": created}, status=201 if created else 200)


@api_view(methods=("POST",), minimum_role=UserProfile.Role.ADMIN)
def period_toggle_api(request, pk):
    period = get_object_or_404(AccountingPeriod, pk=pk)
    if period.is_closed:
        period.closed_at = None
        period.closed_by = None
        action = "reopened"
    else:
        period.closed_at = timezone.now()
        period.closed_by = request.user
        action = "closed"
    period.save(update_fields=["closed_at", "closed_by"])
    record_audit(request.user, action, period, after={"year": period.year, "month": period.month, "is_closed": period.is_closed})
    return JsonResponse({"ok": True, "action": action})


@api_view(methods=("GET", "POST"), minimum_role=UserProfile.Role.ADMIN)
def users_api(request):
    if request.method == "GET":
        users = get_user_model().objects.select_related("profile")
        return JsonResponse({"users": [{"id": item.pk, "username": item.username, "first_name": item.first_name, "last_name": item.last_name, "name": item.get_full_name(), "role": item.profile.role, "role_label": item.profile.get_role_display(), "is_active": item.is_active} for item in users]})
    form = UserCreateForm(request_data(request))
    if not form.is_valid():
        return JsonResponse({"error": "Please correct the highlighted fields.", "fields": form_payload(form)}, status=400)
    user = form.save()
    record_audit(request.user, "created", user.profile, after={"username": user.username, "role": user.profile.role, "is_active": user.is_active})
    return JsonResponse({"id": user.pk}, status=201)


@api_view(methods=("GET", "POST"), minimum_role=UserProfile.Role.ADMIN)
def user_api(request, pk):
    user = get_object_or_404(get_user_model().objects.select_related("profile"), pk=pk)
    if request.method == "GET":
        return JsonResponse({"user": {"id": user.pk, "username": user.username, "first_name": user.first_name, "last_name": user.last_name, "role": user.profile.role, "is_active": user.is_active}})
    before = {"role": user.profile.role, "is_active": user.is_active}
    form = UserUpdateForm(request_data(request), instance=user)
    if not form.is_valid():
        return JsonResponse({"error": "Please correct the highlighted fields.", "fields": form_payload(form)}, status=400)
    if user == request.user and not form.cleaned_data["is_active"]:
        return JsonResponse({"error": "You cannot deactivate your own login.", "fields": {"is_active": ["Keep your current login active."]}}, status=400)
    user = form.save()
    record_audit(request.user, "updated", user.profile, before=before, after={"username": user.username, "role": user.profile.role, "is_active": user.is_active})
    return JsonResponse({"ok": True})


@api_view()
def audit_api(request):
    events = AuditEvent.objects.select_related("actor")[:500]
    return JsonResponse({"events": [audit_json(item) for item in events]})
