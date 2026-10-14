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
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .api import account_json, accounts_with_balances, contact_json, money, validation_payload
from .forms import AccountForm, ContactForm
from .models import Account, ApiToken, Contact, JournalEntry, UserProfile
from .permissions import ROLE_LEVEL, user_role
from .services import create_entry, delete_entry, record_audit, update_entry


PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_IMPORT_ROWS = 500

INSTRUCTIONS = """\
LiteBooks is a double-entry general ledger for one business. To import a bank or credit card statement:

1. Call list_accounts. Find the account the statement belongs to (a bank, cash, or credit card account). If it does not exist, ask the user before creating it.
2. Read the CSV and express every amount as signed from that account's point of view: negative for money out (purchases, charges, fees, withdrawals), positive for money in (deposits, refunds, payments received onto a card).
3. Call categorization_history with the descriptions so you can book merchants the way they were booked before. Choose a category account for every row from list_accounts. Propose new accounts to the user instead of creating them silently.
4. Call import_transactions with dry_run=true. Show the user the proposed categories and any errors or possible duplicates. Transfers between two of the business's own accounts appear on both statements, so book them only once.
5. Only after the user approves, call import_transactions again with the same rows and dry_run=false. Rows already imported are skipped, so re-running an overlapping statement is safe.

Amounts are decimal strings with two places. Dates are ISO 8601 (YYYY-MM-DD). Months that are closed in LiteBooks reject postings.
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
    },
    "required": ["account_id"],
    "additionalProperties": False,
}


@tool(
    "update_transaction",
    "Edit a general journal transaction, for example to recategorize it. Only the fields you pass change; lines, when given, replace all existing lines and must balance. Invoice, bill, payment, and owner entries are edited from their source in the web app.",
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
        lines = [{
            "account": get_account(line.get("account_id"), f"lines.{index}.account_id"),
            "description": str(line.get("description") or ""),
            "debit": parse_amount(line.get("debit") or 0, f"lines.{index}.debit"),
            "credit": parse_amount(line.get("credit") or 0, f"lines.{index}.credit"),
            "owner": None,
        } for index, line in enumerate(args["lines"])]
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
    "Delete a general journal transaction. It stays in the audit history and can be restored from the web app.",
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
