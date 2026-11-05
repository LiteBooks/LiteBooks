"""Model Context Protocol endpoint for AI assistants.

This speaks the stateless form of MCP's Streamable HTTP transport: every
JSON-RPC request arrives as its own POST and is answered with one JSON
response, so there are no sessions or event streams to keep alive under
gunicorn. Clients authenticate with an ApiToken and act with exactly the role
of the token's user; every write goes through the same services, period locks,
and audit trail as the web app.

The assistant is the one that reads the statement and decides categories. The
tools give it what it needs to do that well -- the chart of accounts, how
similar transactions were booked before, and an import that can be checked
before anything is posted.
"""

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .api import account_json, accounts_with_balances, contact_json, money, validation_payload
from .forms import AccountForm, BusinessDocumentForm, ContactForm
from .models import (
    Account,
    AccountingPeriod,
    ApiToken,
    AuditEvent,
    BusinessDocument,
    Contact,
    JournalEntry,
    JournalLine,
    OwnerActivity,
    Payment,
    UserProfile,
)
from .permissions import ROLE_LEVEL, user_role
from .reporting import aging, balance_sheet, income_statement, owner_balances, trial_balance
from .services import (
    create_document,
    create_entry,
    create_owner_activity,
    create_payment,
    delete_entry,
    record_audit,
    restore_entry,
    update_document,
    update_entry,
    update_owner_activity,
)


PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_IMPORT_ROWS = 500

INSTRUCTIONS = """\
LiteBooks is a double-entry general ledger for one business. To import a bank or credit card statement:

1. Call list_accounts. Find the account the statement belongs to (a bank, cash, or credit card account). If it does not exist, ask the user before creating it.
2. Read the CSV and express every amount as signed from that account's point of view: negative for money out (purchases, charges, fees, withdrawals), positive for money in (deposits, refunds, payments received onto a card).
3. Call categorization_history with the descriptions so you can book merchants the way they were booked before. Choose a category account for every row from list_accounts. Propose new accounts to the user instead of creating them silently.
4. Call import_transactions with dry_run=true. Show the user the proposed categories and any errors or possible duplicates. Transfers between two of the business's own accounts appear on both statements, so book them only once.
5. Only after the user approves, call import_transactions again with the same rows and dry_run=false. Rows already imported are skipped, so re-running an overlapping statement is safe.

For reporting and review, use the report tools (trial_balance, balance_sheet, income_statement, aging_report, owner_balances, general_ledger) rather than adding up search_transactions results yourself. get_transaction shows one entry in full, including its attachments and source document.

Adjusting, accrual, reclassification, and depreciation entries are posted with create_journal_entry. Show the user the proposed lines and get their approval before posting.

Invoices (money owed to the business) and bills (money it owes) are documents: create them with create_document, never as journal entries, and record what is paid against them with record_payment. Each posts its own journal entry, so the ledger, the document's status, and the aging report stay in step.

Money moving between an owner and the business (equity contributions, draws, loans to the business, and loan repayments) is recorded with record_owner_activity, which tags the owner so owner_balances stays right.

Amounts are decimal strings with two places. Dates are ISO 8601 (YYYY-MM-DD). Months that are closed in LiteBooks reject postings; call list_periods to see which are closed. To trace who changed a record and when, use search_audit_log.
"""


class ToolError(Exception):
    """A problem the assistant can fix by calling the tool differently."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: object
    minimum_role: str = UserProfile.Role.VIEWER
    read_only: bool = True
    destructive: bool = False

    def definition(self):
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"readOnlyHint": self.read_only, "destructiveHint": self.destructive, "idempotentHint": self.read_only},
        }


TOOLS = {}


def tool(name, description, properties, required=(), minimum_role=UserProfile.Role.VIEWER, read_only=True, destructive=False):
    schema = {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}

    def register(handler):
        TOOLS[name] = Tool(name, description, schema, handler, minimum_role, read_only, destructive)
        return handler

    return register


def allowed_tools(user):
    level = ROLE_LEVEL[user_role(user)]
    return [item for item in TOOLS.values() if level >= ROLE_LEVEL[item.minimum_role]]


# -- Argument helpers ---------------------------------------------------------


def required(args, key):
    value = args.get(key)
    if value in (None, ""):
        raise ToolError(f"{key} is required.")
    return value


def parse_date(value, field="date"):
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ToolError(f"{field} must be a date in YYYY-MM-DD form, not {value!r}.") from exc


def parse_amount(value, field="amount"):
    try:
        amount = Decimal(str(value).replace(",", "").replace("$", "").strip())
    except InvalidOperation as exc:
        raise ToolError(f"{field} must be a number, not {value!r}.") from exc
    if amount != amount.quantize(Decimal("0.01")):
        raise ToolError(f"{field} has more than two decimal places: {value!r}.")
    return amount


def get_account(pk, field="account_id"):
    try:
        return Account.objects.get(pk=pk)
    except (Account.DoesNotExist, ValueError, TypeError) as exc:
        raise ToolError(f"{field} {pk!r} is not an account. Call list_accounts for valid ids.") from exc


def get_contact(pk, field="contact_id"):
    if pk in (None, ""):
        return None
    try:
        return Contact.objects.get(pk=pk)
    except (Contact.DoesNotExist, ValueError, TypeError) as exc:
        raise ToolError(f"{field} {pk!r} is not a contact. Call list_contacts for valid ids.") from exc


def error_text(error):
    if isinstance(error, ValidationError):
        messages = validation_payload(error)
        return "; ".join(f"{field}: {' '.join(items)}" if field != "__all__" else " ".join(items) for field, items in messages.items())
    return str(error)


def entry_summary(entry):
    return {
        "id": entry.pk,
        "number": entry.number,
        "date": entry.date.isoformat(),
        "description": entry.description,
        "memo": entry.memo,
        "status": entry.status,
        "source": entry.source,
        "contact": {"id": entry.contact_id, "name": entry.contact.name} if entry.contact else None,
        "import_ref": entry.import_ref,
        "lines": [{
            "account_id": line.account_id,
            "account": str(line.account),
            "description": line.description,
            "debit": money(line.debit),
            "credit": money(line.credit),
        } for line in entry.lines.all()],
    }


def entries_queryset():
    return JournalEntry.objects.select_related("contact").prefetch_related("lines__account")


def account_ref(account):
    return {"id": account.pk, "code": account.code, "name": account.name, "type": account.type, "subtype": account.subtype}


def optional_date(args, key):
    return parse_date(args[key], key) if args.get(key) else None


# -- Accounts and contacts ----------------------------------------------------

ACCOUNT_FIELDS = {
    "code": {"type": "string", "description": "Unique account code, e.g. 6100."},
    "name": {"type": "string"},
    "type": {"type": "string", "enum": Account.Type.values},
    "subtype": {"type": "string", "enum": ["", *Account.Subtype.values], "description": "Must belong to the type: bank, cash, accounts_receivable, fixed_asset, other_asset are assets; credit_card, accounts_payable, owner_loan_payable, other_liability are liabilities; owner_contribution, owner_draw, retained_earnings are equity; sales, other_revenue are revenue; cost_of_goods, operating_expense, other_expense are expenses."},
    "description": {"type": "string", "description": "What belongs in this account. Good descriptions make future categorization easier."},
}


@tool(
    "list_accounts",
    "List the chart of accounts with each account's id, code, type, subtype, description, and current balance. Call this before categorizing anything.",
    {"include_inactive": {"type": "boolean", "default": False}},
)
def list_accounts(user, args):
    accounts = Account.objects.order_by("code")
    if not args.get("include_inactive"):
        accounts = accounts.filter(is_active=True)
    return {"accounts": [account_json(account, balance) for account, balance in accounts_with_balances(accounts)]}


def save_account(user, form, before=None):
    if not form.is_valid():
        raise ValidationError(form.errors.as_data())
    account = form.save()
    if before is None:
        record_audit(user, "created", account, after={"code": account.code, "name": account.name, "type": account.type, "subtype": account.subtype})
    else:
        record_audit(user, "updated", account, before=before, after=account_json(account))
    return account


@tool(
    "create_account",
    "Add an account to the chart of accounts. Ask the user before creating accounts they did not request.",
    ACCOUNT_FIELDS,
    required=("code", "name", "type"),
    minimum_role=UserProfile.Role.ADMIN,
    read_only=False,
)
def create_account(user, args):
    data = {key: args.get(key, "") for key in ACCOUNT_FIELDS}
    data["is_active"] = True
    account = save_account(user, AccountForm(data))
    return {"account": account_json(account, Decimal("0.00"))}


@tool(
    "update_account",
    "Rename, renumber, describe, deactivate, or reactivate an account. Only the fields you pass change. Type and subtype are locked once the account has activity.",
    {"account_id": {"type": "integer"}, **ACCOUNT_FIELDS, "is_active": {"type": "boolean"}},
    required=("account_id",),
    minimum_role=UserProfile.Role.ADMIN,
    read_only=False,
)
def update_account(user, args):
    account = get_account(required(args, "account_id"))
    before = account_json(account)
    data = {key: args.get(key, before[key]) for key in [*ACCOUNT_FIELDS, "is_active"]}
    account = save_account(user, AccountForm(data, instance=account), before=before)
    return {"account": account_json(account)}


@tool(
    "list_contacts",
    "List customers, vendors, and owners. Attach a contact to imported transactions when the counterparty is a known one.",
    {"kind": {"type": "string", "enum": Contact.Kind.values}, "include_inactive": {"type": "boolean", "default": False}},
)
def list_contacts(user, args):
    contacts = Contact.objects.all()
    if args.get("kind"):
        contacts = contacts.filter(kind=args["kind"])
    if not args.get("include_inactive"):
        contacts = contacts.filter(is_active=True)
    return {"contacts": [contact_json(item) for item in contacts]}


@tool(
    "create_contact",
    "Add a customer, vendor, or owner.",
    {"name": {"type": "string"}, "kind": {"type": "string", "enum": Contact.Kind.values}, "email": {"type": "string"}, "phone": {"type": "string"}, "notes": {"type": "string"}},
    required=("name", "kind"),
    minimum_role=UserProfile.Role.BOOKKEEPER,
    read_only=False,
)
def create_contact(user, args):
    form = ContactForm({"name": args.get("name", ""), "kind": args.get("kind", ""), "email": args.get("email", ""), "phone": args.get("phone", ""), "notes": args.get("notes", ""), "address": "", "is_active": True})
    if not form.is_valid():
        raise ValidationError(form.errors.as_data())
    contact = form.save()
    record_audit(user, "created", contact, after=contact_json(contact))
    return {"contact": contact_json(contact)}


CONTACT_FIELDS = ("name", "kind", "email", "phone", "address", "notes", "is_active")


@tool(
    "update_contact",
    "Edit a customer, vendor, or owner; only the fields you pass change. Set is_active=false to retire a contact without losing its history.",
    {
        "contact_id": {"type": "integer"},
        "name": {"type": "string"},
        "kind": {"type": "string", "enum": Contact.Kind.values},
        "email": {"type": "string"},
        "phone": {"type": "string"},
        "address": {"type": "string"},
        "notes": {"type": "string"},
        "is_active": {"type": "boolean"},
    },
    required=("contact_id",),
    minimum_role=UserProfile.Role.BOOKKEEPER,
    read_only=False,
)
def update_contact(user, args):
    contact = get_contact(required(args, "contact_id"))
    before = contact_json(contact)
    data = {field: args.get(field, getattr(contact, field)) for field in CONTACT_FIELDS}
    form = ContactForm(data, instance=contact)
    if not form.is_valid():
        raise ValidationError(form.errors.as_data())
    contact = form.save()
    record_audit(user, "updated", contact, before=before, after=contact_json(contact))
    return {"contact": contact_json(contact)}


# -- Transactions -------------------------------------------------------------


@tool(
    "search_transactions",
    "Find transactions by text, account, or date range, newest first. Each result lists its lines with accounts, debits, and credits.",
    {
        "query": {"type": "string", "description": "Matches number, description, memo, or contact name."},
        "account_id": {"type": "integer"},
        "start": {"type": "string", "description": "YYYY-MM-DD"},
        "end": {"type": "string", "description": "YYYY-MM-DD"},
        "include_deleted": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
    },
)
def search_transactions(user, args):
    entries = entries_queryset()
    if not args.get("include_deleted"):
        entries = entries.exclude(status=JournalEntry.Status.DELETED)
    if args.get("query"):
        query = args["query"].strip()
        entries = entries.filter(Q(number__icontains=query) | Q(description__icontains=query) | Q(memo__icontains=query) | Q(contact__name__icontains=query))
    if args.get("account_id"):
        entries = entries.filter(lines__account_id=args["account_id"])
    if args.get("start"):
        entries = entries.filter(date__gte=parse_date(args["start"], "start"))
    if args.get("end"):
        entries = entries.filter(date__lte=parse_date(args["end"], "end"))
    limit = min(max(int(args.get("limit") or 50), 1), 200)
    return {"transactions": [entry_summary(item) for item in entries.distinct()[:limit]]}


# Words that say how a payment was made rather than who it was made to.
NOISE_WORDS = {
    "ach", "card", "checkcard", "com", "debit", "credit", "inc", "llc", "ltd", "online", "pending", "pos",
    "purchase", "recurring", "the", "visa", "mastercard", "amex", "www", "pmt", "ppd", "web", "id",
}
CASH_SUBTYPES = {Account.Subtype.BANK, Account.Subtype.CASH, Account.Subtype.CREDIT_CARD}


def merchant_words(description):
    """The first two words that name the counterparty, e.g. 'SQ *BLUE BOTTLE #42' -> ['blue', 'bottle']."""
    words = re.findall(r"[a-z]{3,}", description.lower())
    meaningful = [word for word in words if word not in NOISE_WORDS]
    return (meaningful or words)[:2]


@tool(
    "categorization_history",
    "For each statement description, show which accounts similar past transactions were booked to, most used first. Use it to keep categorization consistent with the business's history.",
    {
        "descriptions": {"type": "array", "items": {"type": "string"}, "maxItems": 200},
        "bank_account_id": {"type": "integer", "description": "The statement's own account, left out of the results. Without it, bank, cash, and credit card accounts are left out."},
    },
    required=("descriptions",),
)
def categorization_history(user, args):
    descriptions = args.get("descriptions") or []
    if not isinstance(descriptions, list) or len(descriptions) > 200:
        raise ToolError("descriptions must be a list of at most 200 strings.")
    excluded = args.get("bank_account_id")
    results = []
    cache = {}
    for description in descriptions:
        words = tuple(merchant_words(str(description)))
        if words not in cache:
            cache[words] = history_for(words, excluded)
        results.append({"description": description, "matched_on": " ".join(words), "accounts": cache[words]})
    return {"history": results}


def history_for(words, excluded_account_id):
    if not words:
        return []
    entries = entries_queryset().filter(status=JournalEntry.Status.POSTED)
    for word in words:
        entries = entries.filter(description__icontains=word)
    tally = {}
    for entry in entries.order_by("-date")[:200]:
        lines = [line for line in entry.lines.all() if line.account_id != excluded_account_id]
        if excluded_account_id is None:
            lines = [line for line in lines if line.account.subtype not in CASH_SUBTYPES] or lines
        for account in {line.account for line in lines}:
            row = tally.setdefault(account.pk, {"account_id": account.pk, "account": str(account), "times_used": 0, "last_used": entry.date.isoformat(), "example": entry.description})
            row["times_used"] += 1
    return sorted(tally.values(), key=lambda row: -row["times_used"])[:5]


IMPORT_ROW = {
    "type": "object",
    "properties": {
        "date": {"type": "string", "description": "YYYY-MM-DD"},
        "description": {"type": "string", "description": "The statement description as written. Without a reference, re-imports are matched on it, so do not reword it."},
        "amount": {"type": "string", "description": "Signed from the statement account's view: negative is money out or a charge, positive is money in."},
        "category_account_id": {"type": "integer", "description": "The account on the other side, e.g. an expense or revenue account."},
        "contact_id": {"type": "integer"},
        "memo": {"type": "string"},
        "reference": {"type": "string", "description": "The bank's own transaction id, if the CSV has one. Otherwise LiteBooks fingerprints the row."},
    },
    "required": ["date", "description", "amount", "category_account_id"],
    "additionalProperties": False,
}


def normalized(text):
    return " ".join(text.lower().split())


def import_ref(statement_account, row, occurrence):
    if row.get("reference"):
        return f"{statement_account.pk}:r:{str(row['reference']).strip()[:100]}"
    # Identical rows on the same day (two coffees) are told apart by their order in the statement.
    fingerprint = f"{row['date']}|{row['amount']:.2f}|{normalized(row['description'])}|{occurrence}"
    return f"{statement_account.pk}:h:{sha256(fingerprint.encode()).hexdigest()[:32]}"


def possible_duplicates(statement_account, row, batch_refs):
    side = "debit" if row["amount"] > 0 else "credit"
    entries = JournalEntry.objects.filter(
        status=JournalEntry.Status.POSTED,
        date=row["date"],
        lines__account=statement_account,
        **{f"lines__{side}": abs(row["amount"])},
    ).exclude(import_ref__in=batch_refs).distinct()[:3]
    return [{"id": entry.pk, "number": entry.number, "description": entry.description, "import_ref": entry.import_ref} for entry in entries]


@tool(
    "import_transactions",
    "Book statement rows against one bank, cash, or credit card account, one transaction per row. Run with dry_run=true first and show the user the result; nothing is posted unless dry_run is false and every row is valid. Rows already imported are skipped.",
    {
        "statement_account_id": {"type": "integer", "description": "The bank, cash, or credit card account the statement belongs to."},
        "rows": {"type": "array", "items": IMPORT_ROW, "maxItems": MAX_IMPORT_ROWS},
        "dry_run": {"type": "boolean", "default": True},
    },
    required=("statement_account_id", "rows"),
    minimum_role=UserProfile.Role.BOOKKEEPER,
    read_only=False,
)
def import_transactions(user, args):
    statement_account = get_account(required(args, "statement_account_id"), "statement_account_id")
    if statement_account.type not in {Account.Type.ASSET, Account.Type.LIABILITY}:
        raise ToolError(f"{statement_account} is not an asset or liability account, so it cannot hold a statement.")
    raw_rows = args.get("rows") or []
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ToolError("rows must be a non-empty list.")
    if len(raw_rows) > MAX_IMPORT_ROWS:
        raise ToolError(f"Send at most {MAX_IMPORT_ROWS} rows per call.")
    dry_run = args.get("dry_run", True) is not False

    results = []
    seen_rows = {}
    seen_refs = set()
    with transaction.atomic():
        for index, raw in enumerate(raw_rows):
            result = {"row": index}
            results.append(result)
            try:
                row = clean_import_row(raw)
                key = (row["date"], row["amount"], normalized(row["description"]))
                occurrence = seen_rows.get(key, 0)
                seen_rows[key] = occurrence + 1
                ref = import_ref(statement_account, {**raw, **row}, occurrence)
                result["import_ref"] = ref
                if ref in seen_refs:
                    raise ToolError("Another row in this batch has the same reference.")
                seen_refs.add(ref)
                existing = JournalEntry.objects.filter(import_ref=ref).first()
                if existing:
                    result.update(status="already_imported", entry={"id": existing.pk, "number": existing.number, "status": existing.status})
                    continue
                duplicates = possible_duplicates(statement_account, row, seen_refs)
                if duplicates:
                    result["possible_duplicates"] = duplicates
                category = get_account(row["category_account_id"], "category_account_id")
                amount = abs(row["amount"])
                statement_side = {"debit": amount, "credit": 0} if row["amount"] > 0 else {"debit": 0, "credit": amount}
                category_side = {"debit": statement_side["credit"], "credit": statement_side["debit"]}
                with transaction.atomic():
                    entry = create_entry(
                        entry_date=row["date"],
                        description=row["description"],
                        memo=row["memo"],
                        contact=get_contact(raw.get("contact_id")),
                        user=user,
                        import_ref=ref,
                        lines=[
                            {"account": statement_account, "owner": None, **statement_side},
                            {"account": category, "owner": None, **category_side},
                        ],
                    )
                result.update(status="ready", category=str(category))
                if not dry_run:
                    result["entry"] = {"id": entry.pk, "number": entry.number}
            except (ToolError, ValidationError, ObjectDoesNotExist) as error:
                result.update(status="error", error=error_text(error))

        counts = {}
        for result in results:
            counts[result["status"]] = counts.get(result["status"], 0) + 1
        counts["possible_duplicate"] = sum(1 for result in results if result.get("possible_duplicates"))
        failed = counts.get("error", 0)
        if dry_run or failed:
            transaction.set_rollback(True)

    if dry_run:
        outcome = "Dry run only: nothing was posted."
    elif failed:
        outcome = f"Nothing was posted because {failed} row(s) have errors. Fix them and import again."
    else:
        for result in results:
            if result["status"] == "ready":
                result["status"] = "posted"
        counts["posted"] = counts.pop("ready", 0)
        outcome = f"Posted {counts['posted']} transaction(s)."
    return {"outcome": outcome, "dry_run": dry_run, "statement_account": str(statement_account), "counts": counts, "rows": results}


def clean_import_row(raw):
    if not isinstance(raw, dict):
        raise ToolError("Each row must be an object.")
    description = " ".join(str(raw.get("description") or "").split())
    if not description:
        raise ToolError("description is required.")
    amount = parse_amount(required(raw, "amount"))
    if not amount:
        raise ToolError("amount cannot be zero.")
    return {
        "date": parse_date(required(raw, "date")),
        "description": description[:240],
        "amount": amount,
        "category_account_id": required(raw, "category_account_id"),
        "memo": str(raw.get("memo") or "").strip(),
    }


LINE = {
    "type": "object",
    "properties": {
        "account_id": {"type": "integer"},
        "debit": {"type": "string"},
        "credit": {"type": "string"},
        "description": {"type": "string"},
        "owner_id": {"type": ["integer", "null"], "description": "The owner contact this line belongs to, for owner contribution, draw, and loan accounts. Owner balances are tracked by it."},
    },
    "required": ["account_id"],
    "additionalProperties": False,
}


def parse_lines(raw_lines):
    lines = []
    for index, line in enumerate(raw_lines):
        owner = get_contact(line.get("owner_id"), f"lines.{index}.owner_id")
        if owner and owner.kind != Contact.Kind.OWNER:
            raise ToolError(f"lines.{index}.owner_id {owner.pk} is not an owner contact.")
        lines.append({
            "account": get_account(line.get("account_id"), f"lines.{index}.account_id"),
            "description": str(line.get("description") or ""),
            "debit": parse_amount(line.get("debit") or 0, f"lines.{index}.debit"),
            "credit": parse_amount(line.get("credit") or 0, f"lines.{index}.credit"),
            "owner": owner,
        })
    return lines


@tool(
    "update_transaction",
    "Edit a general journal transaction, for example to recategorize it. Only the fields you pass change; lines, when given, replace all existing lines and must balance. Invoice and bill entries are edited with update_document and owner entries with update_owner_activity; payment entries are edited in the web app.",
    {
        "transaction_id": {"type": "integer"},
        "date": {"type": "string", "description": "YYYY-MM-DD"},
        "description": {"type": "string"},
        "memo": {"type": "string"},
        "contact_id": {"type": ["integer", "null"], "description": "null removes the contact."},
        "lines": {"type": "array", "items": LINE, "minItems": 2},
    },
    required=("transaction_id",),
    minimum_role=UserProfile.Role.BOOKKEEPER,
    read_only=False,
)
def update_transaction(user, args):
    entry = entries_queryset().get(pk=required(args, "transaction_id"))
    if "lines" in args:
        lines = parse_lines(args["lines"])
    else:
        lines = [{"account": line.account, "description": line.description, "debit": line.debit, "credit": line.credit, "owner": line.owner} for line in entry.lines.all()]
    entry = update_entry(
        entry=entry,
        entry_date=parse_date(args["date"]) if "date" in args else entry.date,
        description=str(args.get("description", entry.description)).strip(),
        memo=str(args.get("memo", entry.memo)).strip(),
        contact=get_contact(args["contact_id"]) if "contact_id" in args else entry.contact,
        linked_entry=entry.linked_entry,
        user=user,
        lines=lines,
    )
    return {"transaction": entry_summary(entries_queryset().get(pk=entry.pk))}


@tool(
    "delete_transaction",
    "Delete a general journal transaction. It stays in the audit history and can be brought back with restore_transaction.",
    {"transaction_id": {"type": "integer"}, "reason": {"type": "string"}},
    required=("transaction_id", "reason"),
    minimum_role=UserProfile.Role.BOOKKEEPER,
    read_only=False,
    destructive=True,
)
def delete_transaction(user, args):
    entry = entries_queryset().get(pk=required(args, "transaction_id"))
    entry = delete_entry(entry=entry, user=user, reason=str(args.get("reason") or ""))
    return {"transaction": entry_summary(entry)}


@tool(
    "restore_transaction",
    "Bring back a deleted transaction exactly as it was. Its month must still be open and its accounts active.",
    {"transaction_id": {"type": "integer"}},
    required=("transaction_id",),
    minimum_role=UserProfile.Role.BOOKKEEPER,
    read_only=False,
)
def restore_transaction(user, args):
    entry = entries_queryset().get(pk=required(args, "transaction_id"))
    entry = restore_entry(entry=entry, user=user)
    return {"transaction": entry_summary(entries_queryset().get(pk=entry.pk))}


@tool(
    "create_journal_entry",
    "Post a general journal entry: adjusting, accrual, reclassification, depreciation, or correcting entries. Lines must balance and touch at least two accounts; each line has a debit or a credit, not both. Propose the entry to the user before posting it. Use create_document for invoices and bills, record_payment for their payments, and record_owner_activity for owner contributions, draws, and loans, so those stay tied to their source records.",
    {
        "date": {"type": "string", "description": "YYYY-MM-DD. The month must be open."},
        "description": {"type": "string"},
        "memo": {"type": "string", "description": "Why the entry was made, for the reviewer."},
        "contact_id": {"type": ["integer", "null"]},
        "lines": {"type": "array", "items": LINE, "minItems": 2},
    },
    required=("date", "description", "lines"),
    minimum_role=UserProfile.Role.BOOKKEEPER,
    read_only=False,
)
def create_journal_entry(user, args):
    entry = create_entry(
        entry_date=parse_date(required(args, "date")),
        description=str(required(args, "description")).strip()[:240],
        memo=str(args.get("memo") or "").strip(),
        contact=get_contact(args.get("contact_id")),
        lines=parse_lines(args.get("lines") or []),
        user=user,
    )
    return {"transaction": entry_summary(entries_queryset().get(pk=entry.pk))}


@tool(
    "get_transaction",
    "Show one transaction in full: its lines, who created, posted, or deleted it, its attachments, the invoice, bill, payment, or owner activity it came from, and any linked transaction.",
    {"transaction_id": {"type": "integer"}},
    required=("transaction_id",),
)
def get_transaction(user, args):
    entry = (
        JournalEntry.objects
        .select_related("contact", "linked_entry", "created_by", "posted_by", "deleted_by")
        .prefetch_related("lines__account", "lines__owner", "attachments")
        .get(pk=required(args, "transaction_id"))
    )
    result = entry_summary(entry)
    for line, data in zip(entry.lines.all(), result["lines"]):
        data["owner"] = {"id": line.owner_id, "name": line.owner.name} if line.owner else None
    result.update({
        "created_by": entry.created_by.username,
        "posted_by": entry.posted_by.username if entry.posted_by else None,
        "posted_at": entry.posted_at.isoformat() if entry.posted_at else None,
        "deleted_by": entry.deleted_by.username if entry.deleted_by else None,
        "deleted_at": entry.deleted_at.isoformat() if entry.deleted_at else None,
        "deleted_reason": entry.deleted_reason,
        "linked_transaction": {"id": entry.linked_entry_id, "number": entry.linked_entry.number} if entry.linked_entry else None,
        "attachments": [{
            "id": item.pk,
            "name": item.original_name,
            "content_type": item.content_type,
            "size": item.size,
            "download_path": f"/attachments/{item.pk}/download/",
        } for item in entry.attachments.all()],
        "source_record": source_record(entry),
    })
    return {"transaction": result}


def source_record(entry):
    if hasattr(entry, "business_document"):
        document = entry.business_document
        return {"type": document.kind, "id": document.pk, "number": document.number, "status": document.status, "amount": money(document.amount), "balance_due": money(document.balance_due)}
    if hasattr(entry, "payment"):
        payment = entry.payment
        return {"type": "payment", "id": payment.pk, "amount": money(payment.amount), "document": {"id": payment.document_id, "kind": payment.document.kind, "number": payment.document.number}}
    if hasattr(entry, "owner_activity"):
        activity = entry.owner_activity
        return {"type": "owner_activity", "id": activity.pk, "kind": activity.kind, "owner": {"id": activity.owner_id, "name": activity.owner.name}}
    return None


# -- Invoices, bills, and payments --------------------------------------------

PAID = Coalesce(Sum("payments__amount"), Value(Decimal("0.00"), output_field=DecimalField(max_digits=15, decimal_places=2)))
UNPAID_STATUSES = (BusinessDocument.Status.OPEN, BusinessDocument.Status.PARTIAL)


def documents_queryset():
    return BusinessDocument.objects.select_related("contact", "journal_entry", "control_account", "category_account").annotate(paid=PAID)


def document_summary(document):
    paid = getattr(document, "paid", None)
    paid = document.amount_paid if paid is None else paid
    return {
        "id": document.pk,
        "kind": document.kind,
        "number": document.number,
        "contact": {"id": document.contact_id, "name": document.contact.name},
        "issue_date": document.issue_date.isoformat(),
        "due_date": document.due_date.isoformat(),
        "description": document.description,
        "amount": money(document.amount),
        "amount_paid": money(paid),
        "balance_due": money(document.amount - paid),
        "status": document.status,
        "control_account": account_ref(document.control_account),
        "category_account": account_ref(document.category_account),
        "transaction": {"id": document.journal_entry_id, "number": document.journal_entry.number},
    }


def document_detail(document):
    result = document_summary(document)
    result["can_edit"] = document.status != BusinessDocument.Status.VOID and not document.journal_entry.is_deleted
    result["payments"] = [{
        "id": payment.pk,
        "date": payment.date.isoformat(),
        "amount": money(payment.amount),
        "cash_account": account_ref(payment.cash_account),
        "transaction": {"id": payment.journal_entry_id, "number": payment.journal_entry.number, "memo": payment.journal_entry.memo},
    } for payment in document.payments.select_related("cash_account", "journal_entry").order_by("date", "pk")]
    return result


def get_document(pk, field="document_id"):
    try:
        return documents_queryset().get(pk=pk)
    except (BusinessDocument.DoesNotExist, ValueError, TypeError) as exc:
        raise ToolError(f"{field} {pk!r} is not an invoice or bill. Call list_documents for valid ids.") from exc


DOCUMENT_FIELDS = {
    "number": {"type": "string", "description": "Unique per kind, e.g. INV-1042."},
    "contact_id": {"type": "integer", "description": "A customer for invoices, a vendor for bills (contacts of kind both work for either)."},
    "issue_date": {"type": "string", "description": "YYYY-MM-DD. The document's journal entry posts on this date, so its month must be open."},
    "due_date": {"type": "string", "description": "YYYY-MM-DD, on or after issue_date."},
    "description": {"type": "string"},
    "amount": {"type": "string", "description": "Total, a positive decimal string."},
    "control_account_id": {"type": "integer", "description": "Accounts receivable for invoices, accounts payable for bills."},
    "category_account_id": {"type": "integer", "description": "A revenue account for invoices, an expense account for bills."},
}
DOCUMENT_FORM_FIELDS = {
    "number": "number", "contact_id": "contact", "issue_date": "issue_date", "due_date": "due_date",
    "description": "description", "amount": "amount", "control_account_id": "control_account", "category_account_id": "category_account",
}


def validated_document_fields(kind, data, instance=None):
    """Run the web app's form so the MCP tools enforce exactly the same rules."""
    if "amount" in data:
        data["amount"] = parse_amount(data["amount"])
    form = BusinessDocumentForm({DOCUMENT_FORM_FIELDS[key]: value for key, value in data.items()}, instance=instance, kind=kind)
    if not form.is_valid():
        errors = form.errors.as_data()
        renamed = {next((key for key, name in DOCUMENT_FORM_FIELDS.items() if name == field), field): items for field, items in errors.items()}
        raise ValidationError(renamed)
    cleaned = dict(form.cleaned_data)
    cleaned.pop("attachment", None)
    return cleaned


@tool(
    "list_documents",
    "List invoices and bills with amount, paid, balance due, and status, newest first. Filter by kind, status, contact, date range, or text.",
    {
        "kind": {"type": "string", "enum": BusinessDocument.Kind.values},
        "status": {"type": "string", "enum": [*BusinessDocument.Status.values, "unpaid"], "description": "unpaid means open or partially paid."},
        "contact_id": {"type": "integer"},
        "query": {"type": "string", "description": "Matches number, description, or contact name."},
        "start": {"type": "string", "description": "Issue date on or after, YYYY-MM-DD."},
        "end": {"type": "string", "description": "Issue date on or before, YYYY-MM-DD."},
        "due_before": {"type": "string", "description": "Due date before this day, YYYY-MM-DD. With status=unpaid this lists what is overdue."},
        "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
    },
)
def list_documents(user, args):
    documents = documents_queryset()
    if args.get("kind"):
        documents = documents.filter(kind=args["kind"])
    status = args.get("status")
    if status == "unpaid":
        documents = documents.filter(status__in=UNPAID_STATUSES)
    elif status:
        documents = documents.filter(status=status)
    if args.get("contact_id"):
        documents = documents.filter(contact_id=args["contact_id"])
    if args.get("query"):
        query = args["query"].strip()
        documents = documents.filter(Q(number__icontains=query) | Q(description__icontains=query) | Q(contact__name__icontains=query))
    if args.get("start"):
        documents = documents.filter(issue_date__gte=parse_date(args["start"], "start"))
    if args.get("end"):
        documents = documents.filter(issue_date__lte=parse_date(args["end"], "end"))
    if args.get("due_before"):
        documents = documents.filter(due_date__lt=parse_date(args["due_before"], "due_before"))
    limit = min(max(int(args.get("limit") or 100), 1), 500)
    return {"documents": [document_summary(item) for item in documents[:limit]]}


@tool(
    "get_document",
    "Show one invoice or bill in full, including every payment recorded against it and whether it can still be edited.",
    {"document_id": {"type": "integer"}},
    required=("document_id",),
)
def get_document_tool(user, args):
    return {"document": document_detail(get_document(required(args, "document_id")))}


@tool(
    "create_document",
    "Create an invoice or bill and post its journal entry (debit receivable and credit revenue for an invoice; debit expense and credit payable for a bill). Confirm the details with the user before creating it.",
    {"kind": {"type": "string", "enum": BusinessDocument.Kind.values}, **DOCUMENT_FIELDS},
    required=("kind", *DOCUMENT_FIELDS),
    minimum_role=UserProfile.Role.BOOKKEEPER,
    read_only=False,
)
def create_document_tool(user, args):
    kind = args.get("kind")
    if kind not in BusinessDocument.Kind.values:
        raise ToolError("kind must be invoice or bill.")
    fields = validated_document_fields(kind, {key: args.get(key, "") for key in DOCUMENT_FIELDS})
    document = create_document(kind=kind, user=user, **fields)
    return {"document": document_detail(get_document(document.pk))}


@tool(
    "update_document",
    "Edit an invoice or bill; only the fields you pass change, and its journal entry is rewritten to match. Once a payment is recorded, the contact and control account are fixed and the amount cannot drop below what has been paid. Void documents cannot be edited.",
    {"document_id": {"type": "integer"}, **DOCUMENT_FIELDS},
    required=("document_id",),
    minimum_role=UserProfile.Role.BOOKKEEPER,
    read_only=False,
)
def update_document_tool(user, args):
    document = get_document(required(args, "document_id"))
    current = {
        "number": document.number, "contact_id": document.contact_id, "issue_date": document.issue_date.isoformat(),
        "due_date": document.due_date.isoformat(), "description": document.description, "amount": str(document.amount),
        "control_account_id": document.control_account_id, "category_account_id": document.category_account_id,
    }
    changes = {key: args[key] for key in DOCUMENT_FIELDS if key in args}
    # The form writes cleaned values onto its instance, so give it a fresh copy and let the service work from the stored row.
    fields = validated_document_fields(document.kind, {**current, **changes}, instance=BusinessDocument.objects.get(pk=document.pk))
    update_document(document=document, user=user, **fields)
    return {"document": document_detail(get_document(document.pk))}


@tool(
    "record_payment",
    "Record money received against an invoice or paid against a bill, from or into a bank or cash account. The amount cannot exceed the balance due; partial payments are fine and the document's status follows.",
    {
        "document_id": {"type": "integer"},
        "date": {"type": "string", "description": "YYYY-MM-DD. The month must be open."},
        "amount": {"type": "string", "description": "Positive decimal string, at most the balance due."},
        "cash_account_id": {"type": "integer", "description": "A bank or cash account."},
        "memo": {"type": "string", "description": "e.g. check number or transfer reference."},
    },
    required=("document_id", "date", "amount", "cash_account_id"),
    minimum_role=UserProfile.Role.BOOKKEEPER,
    read_only=False,
)
def record_payment(user, args):
    document = get_document(required(args, "document_id"))
    payment = create_payment(
        document=document,
        payment_date=parse_date(required(args, "date")),
        amount=parse_amount(required(args, "amount")),
        cash_account=get_account(required(args, "cash_account_id"), "cash_account_id"),
        memo=str(args.get("memo") or "").strip(),
        user=user,
    )
    return {"payment_transaction_id": payment.journal_entry_id, "document": document_detail(get_document(document.pk))}


# -- Owner activity -----------------------------------------------------------

CASH_ONLY = {Account.Subtype.BANK, Account.Subtype.CASH}


def owner_activities_queryset():
    return OwnerActivity.objects.select_related("owner", "journal_entry").prefetch_related("journal_entry__lines__account")


def owner_activity_accounts(activity):
    lines = list(activity.journal_entry.lines.all())
    cash = next((line.account for line in lines if line.account.subtype in CASH_ONLY), None)
    offset = next((line.account for line in lines if line.account != cash), None)
    return cash, offset


def owner_activity_summary(activity):
    entry = activity.journal_entry
    cash, offset = owner_activity_accounts(activity)
    return {
        "id": activity.pk,
        "owner": {"id": activity.owner_id, "name": activity.owner.name},
        "kind": activity.kind,
        "date": entry.date.isoformat(),
        "amount": money(activity.amount),
        "cash_account": account_ref(cash) if cash else None,
        "offset_account": account_ref(offset) if offset else None,
        "memo": entry.memo,
        "transaction": {"id": entry.pk, "number": entry.number, "status": entry.status},
    }


def get_owner_activity(pk):
    try:
        return owner_activities_queryset().get(pk=pk)
    except (OwnerActivity.DoesNotExist, ValueError, TypeError) as exc:
        raise ToolError(f"activity_id {pk!r} is not an owner activity. Call list_owner_activity for valid ids.") from exc


def get_owner(pk):
    owner = get_contact(required({"owner_id": pk}, "owner_id"), "owner_id")
    if owner.kind != Contact.Kind.OWNER:
        raise ToolError(f"owner_id {pk!r} is {owner.name}, who is not an owner. Call list_contacts to find owners.")
    return owner


OWNER_ACTIVITY_FIELDS = {
    "owner_id": {"type": "integer", "description": "A contact of kind owner."},
    "kind": {"type": "string", "enum": OwnerActivity.Kind.values, "description": "contribution: owner puts equity in. draw: owner takes money out. loan_in: owner lends the business money. loan_repayment: the business repays an owner loan (no more than it owes them)."},
    "date": {"type": "string", "description": "YYYY-MM-DD. The month must be open."},
    "amount": {"type": "string", "description": "Positive decimal string."},
    "cash_account_id": {"type": "integer", "description": "The bank or cash account the money moved through."},
    "offset_account_id": {"type": "integer", "description": "owner_contribution account for contributions, owner_draw for draws, owner_loan_payable for loans and repayments."},
    "memo": {"type": "string"},
}


@tool(
    "list_owner_activity",
    "List owner contributions, draws, loans, and loan repayments, newest first, with the accounts each used. Deleted ones are left out unless asked for.",
    {
        "owner_id": {"type": "integer"},
        "kind": {"type": "string", "enum": OwnerActivity.Kind.values},
        "start": {"type": "string", "description": "YYYY-MM-DD"},
        "end": {"type": "string", "description": "YYYY-MM-DD"},
        "include_deleted": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
    },
)
def list_owner_activity(user, args):
    activities = owner_activities_queryset()
    if not args.get("include_deleted"):
        activities = activities.exclude(journal_entry__status=JournalEntry.Status.DELETED)
    if args.get("owner_id"):
        activities = activities.filter(owner_id=args["owner_id"])
    if args.get("kind"):
        activities = activities.filter(kind=args["kind"])
    if args.get("start"):
        activities = activities.filter(journal_entry__date__gte=parse_date(args["start"], "start"))
    if args.get("end"):
        activities = activities.filter(journal_entry__date__lte=parse_date(args["end"], "end"))
    limit = min(max(int(args.get("limit") or 100), 1), 500)
    return {"activities": [owner_activity_summary(item) for item in activities[:limit]]}


@tool(
    "record_owner_activity",
    "Record money moving between an owner and the business and post its journal entry, tagged to the owner. Confirm the details with the user first.",
    OWNER_ACTIVITY_FIELDS,
    required=("owner_id", "kind", "date", "amount", "cash_account_id", "offset_account_id"),
    minimum_role=UserProfile.Role.BOOKKEEPER,
    read_only=False,
)
def record_owner_activity(user, args):
    activity = create_owner_activity(
        owner=get_owner(args.get("owner_id")),
        kind=required(args, "kind"),
        activity_date=parse_date(required(args, "date")),
        amount=parse_amount(required(args, "amount")),
        cash_account=get_account(required(args, "cash_account_id"), "cash_account_id"),
        offset_account=get_account(required(args, "offset_account_id"), "offset_account_id"),
        memo=str(args.get("memo") or "").strip(),
        user=user,
    )
    return {"activity": owner_activity_summary(get_owner_activity(activity.pk))}


@tool(
    "update_owner_activity",
    "Edit an owner contribution, draw, loan, or repayment; only the fields you pass change, and its journal entry is rewritten to match. An edit may not leave an owner repaid more than they lent.",
    {"activity_id": {"type": "integer"}, **OWNER_ACTIVITY_FIELDS},
    required=("activity_id",),
    minimum_role=UserProfile.Role.BOOKKEEPER,
    read_only=False,
)
def update_owner_activity_tool(user, args):
    activity = get_owner_activity(required(args, "activity_id"))
    cash, offset = owner_activity_accounts(activity)
    update_owner_activity(
        activity=activity,
        owner=get_owner(args["owner_id"]) if "owner_id" in args else activity.owner,
        kind=args.get("kind", activity.kind),
        activity_date=parse_date(args["date"]) if "date" in args else activity.journal_entry.date,
        amount=parse_amount(args["amount"]) if "amount" in args else activity.amount,
        cash_account=get_account(args["cash_account_id"], "cash_account_id") if "cash_account_id" in args else cash,
        offset_account=get_account(args["offset_account_id"], "offset_account_id") if "offset_account_id" in args else offset,
        memo=str(args.get("memo", activity.journal_entry.memo)).strip(),
        user=user,
    )
    return {"activity": owner_activity_summary(get_owner_activity(activity.pk))}


# -- Closing and review -------------------------------------------------------


def period_json(year, month, period=None):
    closed = period is not None and period.is_closed
    return {
        "year": year,
        "month": month,
        "label": date(year, month, 1).strftime("%B %Y"),
        "is_closed": closed,
        "closed_at": period.closed_at.isoformat() if closed else None,
        "closed_by": period.closed_by.username if closed and period.closed_by else None,
    }


def period_args(args):
    try:
        year, month = int(required(args, "year")), int(required(args, "month"))
        date(year, month, 1)
    except (ValueError, TypeError) as exc:
        raise ToolError("year and month must name a real month, e.g. year=2026, month=3.") from exc
    return year, month


@tool(
    "list_periods",
    "Show which months are closed. Closed months reject every posting, edit, delete, and restore dated in them. Months never closed are open and are not listed unless they fall within year.",
    {"year": {"type": "integer", "description": "List all twelve months of this year, open or closed."}},
)
def list_periods(user, args):
    periods = AccountingPeriod.objects.select_related("closed_by")
    if args.get("year"):
        year = int(args["year"])
        stored = {period.month: period for period in periods.filter(year=year)}
        return {"periods": [period_json(year, month, stored.get(month)) for month in range(1, 13)]}
    return {"closed_periods": [period_json(period.year, period.month, period) for period in periods if period.is_closed]}


def set_period_closed(user, args, closed):
    year, month = period_args(args)
    with transaction.atomic():
        period, _ = AccountingPeriod.objects.select_for_update().get_or_create(year=year, month=month)
        if period.is_closed == closed:
            raise ToolError(f"{period} is already {'closed' if closed else 'open'}.")
        period.closed_at = timezone.now() if closed else None
        period.closed_by = user if closed else None
        period.save(update_fields=["closed_at", "closed_by"])
        record_audit(user, "closed" if closed else "reopened", period, after={"year": year, "month": month, "is_closed": closed})
    return {"period": period_json(year, month, period)}


@tool(
    "close_period",
    "Close a month so nothing dated in it can be posted, edited, deleted, or restored. Run the trial balance and get the user's explicit go-ahead first.",
    {"year": {"type": "integer"}, "month": {"type": "integer", "minimum": 1, "maximum": 12}},
    required=("year", "month"),
    minimum_role=UserProfile.Role.ADMIN,
    read_only=False,
)
def close_period(user, args):
    return set_period_closed(user, args, closed=True)


@tool(
    "reopen_period",
    "Reopen a closed month so its entries can change again. This undoes a close the business may have reported or filed on, so only do it when the user explicitly asks.",
    {"year": {"type": "integer"}, "month": {"type": "integer", "minimum": 1, "maximum": 12}},
    required=("year", "month"),
    minimum_role=UserProfile.Role.ADMIN,
    read_only=False,
    destructive=True,
)
def reopen_period(user, args):
    return set_period_closed(user, args, closed=False)


AUDIT_RECORD_TYPES = {
    "transaction": JournalEntry,
    "document": BusinessDocument,
    "payment": Payment,
    "owner_activity": OwnerActivity,
    "account": Account,
    "contact": Contact,
    "period": AccountingPeriod,
    "user": UserProfile,
    "api_token": ApiToken,
}
AUDIT_LABELS = {model._meta.label: name for name, model in AUDIT_RECORD_TYPES.items()}


@tool(
    "search_audit_log",
    "Who changed what and when, newest first, with each record's values before and after. Filter by record type and id (for example a transaction's history), action, user, or date range.",
    {
        "record_type": {"type": "string", "enum": list(AUDIT_RECORD_TYPES)},
        "record_id": {"type": "integer", "description": "With record_type, the history of one record."},
        "action": {"type": "string", "description": "e.g. created, updated, deleted, restored, closed, reopened."},
        "username": {"type": "string"},
        "start": {"type": "string", "description": "YYYY-MM-DD"},
        "end": {"type": "string", "description": "YYYY-MM-DD"},
        "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
    },
)
def search_audit_log(user, args):
    events = AuditEvent.objects.select_related("actor")
    if args.get("record_type"):
        if args["record_type"] not in AUDIT_RECORD_TYPES:
            raise ToolError(f"record_type must be one of: {', '.join(AUDIT_RECORD_TYPES)}.")
        events = events.filter(object_type=AUDIT_RECORD_TYPES[args["record_type"]]._meta.label)
    if args.get("record_id") is not None:
        if not args.get("record_type"):
            raise ToolError("record_id needs a record_type, since ids are only unique within a type.")
        events = events.filter(object_id=str(args["record_id"]))
    if args.get("action"):
        events = events.filter(action=args["action"].strip())
    if args.get("username"):
        events = events.filter(actor__username=args["username"].strip())
    if args.get("start"):
        events = events.filter(created_at__date__gte=parse_date(args["start"], "start"))
    if args.get("end"):
        events = events.filter(created_at__date__lte=parse_date(args["end"], "end"))
    limit = min(max(int(args.get("limit") or 100), 1), 500)
    return {"events": [{
        "id": event.pk,
        "at": event.created_at.isoformat(),
        "user": event.actor.username if event.actor else None,
        "action": event.action,
        "record_type": AUDIT_LABELS.get(event.object_type, event.object_type),
        "record_id": event.object_id,
        "before": event.before,
        "after": event.after,
    } for event in events[:limit]]}


# -- Reports ------------------------------------------------------------------
#
# Every report counts posted entries only, the same as the web app's reports.

AS_OF = {"as_of": {"type": "string", "description": "YYYY-MM-DD. Defaults to today."}}


@tool(
    "trial_balance",
    "Every account's debit or credit balance as of a date, with totals. Use it to check that the books balance and as the starting point for review or adjustments.",
    AS_OF,
)
def trial_balance_report(user, args):
    as_of = optional_date(args, "as_of") or date.today()
    rows, debits, credits = trial_balance(as_of)
    return {
        "as_of": as_of.isoformat(),
        "rows": [{"account": account_ref(row["account"]), "debit": money(row["debit_balance"]), "credit": money(row["credit_balance"])} for row in rows if row["debit_balance"] or row["credit_balance"]],
        "total_debits": money(debits),
        "total_credits": money(credits),
    }


@tool(
    "balance_sheet",
    "Assets, liabilities, and equity as of a date. Equity includes current earnings (revenue minus expenses not yet closed to retained earnings).",
    AS_OF,
)
def balance_sheet_report(user, args):
    as_of = optional_date(args, "as_of") or date.today()
    groups = balance_sheet(as_of)
    return {
        "as_of": as_of.isoformat(),
        **{key: {
            "rows": [{"account": account_ref(row["account"]), "balance": money(row["balance"])} for row in group["rows"]],
            "total": money(group["total"]),
            **({"current_earnings": money(group["current_earnings"])} if "current_earnings" in group else {}),
        } for key, group in groups.items()},
    }


@tool(
    "income_statement",
    "Profit and loss for a date range: revenue and expense accounts with totals and net income.",
    {
        "start": {"type": "string", "description": "YYYY-MM-DD. Defaults to January 1 of the end date's year."},
        "end": {"type": "string", "description": "YYYY-MM-DD. Defaults to today."},
    },
)
def income_statement_report(user, args):
    end = optional_date(args, "end") or date.today()
    start = optional_date(args, "start") or date(end.year, 1, 1)
    if start > end:
        raise ToolError("start must be on or before end.")
    statement = income_statement(start, end)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "revenue": [{"account": account_ref(row["account"]), "balance": money(row["balance"])} for row in statement["revenue"]],
        "expenses": [{"account": account_ref(row["account"]), "balance": money(row["balance"])} for row in statement["expenses"]],
        "revenue_total": money(statement["revenue_total"]),
        "expense_total": money(statement["expense_total"]),
        "net_income": money(statement["net_income"]),
    }


@tool(
    "aging_report",
    "Unpaid invoices (receivables) or bills (payables) as of a date, bucketed by days past due: current, 1-30, 31-60, 61-90, 90+.",
    {
        "kind": {"type": "string", "enum": ["receivables", "payables"]},
        **AS_OF,
    },
    required=("kind",),
)
def aging_report(user, args):
    kinds = {"receivables": BusinessDocument.Kind.INVOICE, "payables": BusinessDocument.Kind.BILL}
    if args.get("kind") not in kinds:
        raise ToolError("kind must be receivables or payables.")
    as_of = optional_date(args, "as_of") or date.today()
    rows, buckets = aging(kinds[args["kind"]], as_of)
    return {
        "kind": args["kind"],
        "as_of": as_of.isoformat(),
        "rows": [{
            "document": {
                "id": row["document"].pk,
                "number": row["document"].number,
                "contact": {"id": row["document"].contact_id, "name": row["document"].contact.name},
                "issue_date": row["document"].issue_date.isoformat(),
                "due_date": row["document"].due_date.isoformat(),
                "amount": money(row["document"].amount),
            },
            "days_past_due": row["days"],
            "bucket": row["bucket"],
            "balance": money(row["balance"]),
        } for row in rows],
        "buckets": {key: money(value) for key, value in buckets.items()},
        "total": money(sum(buckets.values(), Decimal("0.00"))),
    }


@tool(
    "owner_balances",
    "Each owner's cumulative contributions, draws, and what the business owes them on owner loans, as of a date.",
    AS_OF,
)
def owner_balances_report(user, args):
    as_of = optional_date(args, "as_of") or date.today()
    return {
        "as_of": as_of.isoformat(),
        "owners": [{
            "owner": {"id": row["owner"].pk, "name": row["owner"].name},
            "contributions": money(row["contributions"]),
            "draws": money(row["draws"]),
            "owed": money(row["owed"]),
        } for row in owner_balances(as_of)],
    }


@tool(
    "general_ledger",
    "Posted journal lines for a date range in date order, optionally for one account. With account_id, each line carries the account's running balance, starting from its balance the day before start.",
    {
        "start": {"type": "string", "description": "YYYY-MM-DD. Defaults to January 1 of the end date's year."},
        "end": {"type": "string", "description": "YYYY-MM-DD. Defaults to today."},
        "account_id": {"type": "integer"},
        "limit": {"type": "integer", "default": 500, "minimum": 1, "maximum": 2000},
    },
)
def general_ledger(user, args):
    end = optional_date(args, "end") or date.today()
    start = optional_date(args, "start") or date(end.year, 1, 1)
    if start > end:
        raise ToolError("start must be on or before end.")
    limit = min(max(int(args.get("limit") or 500), 1), 2000)
    posted = JournalLine.objects.filter(entry__status=JournalEntry.Status.POSTED)
    lines = posted.filter(entry__date__range=(start, end))
    account = None
    if args.get("account_id"):
        account = get_account(args["account_id"])
        lines = lines.filter(account=account)
    lines = lines.select_related("entry", "account").order_by("entry__date", "entry__number", "sort_order", "pk")
    page = list(lines[:limit + 1])
    truncated = len(page) > limit
    page = page[:limit]

    result = {"start": start.isoformat(), "end": end.isoformat(), "truncated": truncated}
    sign = 1
    balance = Decimal("0.00")
    if account:
        sign = 1 if account.normal_balance == "debit" else -1
        opening = posted.filter(account=account, entry__date__lt=start).aggregate(debits=Sum("debit"), credits=Sum("credit"))
        balance = sign * ((opening["debits"] or Decimal("0.00")) - (opening["credits"] or Decimal("0.00")))
        result.update({"account": account_ref(account), "opening_balance": money(balance)})
    rows = []
    for line in page:
        row = {
            "date": line.entry.date.isoformat(),
            "transaction": {"id": line.entry_id, "number": line.entry.number},
            "account": account_ref(line.account),
            "description": line.description or line.entry.description,
            "debit": money(line.debit),
            "credit": money(line.credit),
        }
        if account:
            balance += sign * (line.debit - line.credit)
            row["balance"] = money(balance)
        rows.append(row)
    result["lines"] = rows
    if account and not truncated:
        result["closing_balance"] = money(balance)
    if truncated:
        result["note"] = f"Only the first {limit} lines are shown. Narrow the date range or raise limit for the rest."
    return result


# -- Transport ----------------------------------------------------------------


def rpc_result(message_id, result):
    return JsonResponse({"jsonrpc": "2.0", "id": message_id, "result": result})


def rpc_error(message_id, code, message, status=200):
    return JsonResponse({"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}, status=status)


def bearer_user(request):
    header = request.headers.get("Authorization", "")
    scheme, _, raw = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return ApiToken.authenticate(raw.strip())


def call_tool(user, params):
    name = params.get("name")
    found = next((item for item in allowed_tools(user) if item.name == name), None)
    if found is None:
        return None
    args = params.get("arguments") or {}
    try:
        payload = found.handler(user, args)
    except (ToolError, ValidationError, ObjectDoesNotExist, ValueError, TypeError) as error:
        message = "That record was not found." if isinstance(error, ObjectDoesNotExist) and not str(error) else error_text(error)
        return {"content": [{"type": "text", "text": message}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=1)}], "structuredContent": payload, "isError": False}


@csrf_exempt
def mcp_endpoint(request):
    if request.method != "POST":
        # Stateless server: there is no event stream to GET and no session to DELETE.
        response = HttpResponse(status=405)
        response["Allow"] = "POST"
        return response
    user = bearer_user(request)
    if user is None:
        response = JsonResponse({"error": "A valid LiteBooks API token is required."}, status=401)
        response["WWW-Authenticate"] = 'Bearer realm="LiteBooks"'
        return response
    try:
        message = json.loads(request.body or b"")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return rpc_error(None, -32700, "Parse error.", status=400)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return rpc_error(None, -32600, "Expected a single JSON-RPC 2.0 message.", status=400)
    if "id" not in message or "method" not in message:
        # Notifications and client responses need no reply.
        return HttpResponse(status=202)

    message_id, method, params = message["id"], message["method"], message.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        return rpc_result(message_id, {
            "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "litebooks", "title": "LiteBooks", "version": settings.LITEBOOKS_VERSION},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return rpc_result(message_id, {})
    if method == "tools/list":
        return rpc_result(message_id, {"tools": [item.definition() for item in allowed_tools(user)]})
    if method == "tools/call":
        result = call_tool(user, params)
        if result is None:
            return rpc_error(message_id, -32602, f"Unknown tool or not permitted for your role: {params.get('name')!r}.")
        return rpc_result(message_id, result)
    return rpc_error(message_id, -32601, f"Method not found: {method}.")
