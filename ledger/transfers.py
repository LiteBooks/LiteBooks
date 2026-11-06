"""Recognizing transfers between the business's own accounts during imports.

A payment from checking to the credit card appears on both statements. Import
deduplication by itself only works within one statement account, so whichever
statement is imported second would book the same money again. Here, a row
whose category is another bank, cash, or credit card account is matched to an
existing transaction that already records that transfer. The row is then
linked to that transaction by adding the row's import ref for its statement
account, and nothing new is posted.

A transaction matches a row from statement account S categorized to account C
when all of these hold:

1. it is posted (not draft or deleted) and touches exactly S and C;
2. its net effect on S (debits minus credits) equals the row's signed amount
   to the cent. That is the same sign convention the import posts with, so it
   holds for asset and liability statement accounts alike;
3. its date is within the configured window of the row's date;
4. it has no import ref for S yet;
5. its month is not closed.

Import refs live in JournalEntry.import_ref (legacy, one per transaction) and
in TransactionImportRef (one per transaction and statement account). Legacy
refs always start with "<statement account id>:", which is how the account a
legacy ref belongs to is told.
"""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Account, AccountingPeriod, ImportSettings, JournalEntry, TransactionImportRef
from .services import record_audit


CASH_SUBTYPES = frozenset({Account.Subtype.BANK, Account.Subtype.CASH, Account.Subtype.CREDIT_CARD})


def is_cash_account(account):
    return account.subtype in CASH_SUBTYPES


def match_window_days():
    return ImportSettings.load().transfer_match_window_days


def save_match_window(*, user, days):
    try:
        days = int(days)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"transfer_match_window_days": "Enter a whole number of days."}) from exc
    if not 0 <= days <= ImportSettings.MAX_WINDOW_DAYS:
        raise ValidationError({"transfer_match_window_days": f"Enter a number of days from 0 to {ImportSettings.MAX_WINDOW_DAYS}."})
    found = ImportSettings.load()
    before = settings_json(found)
    found.transfer_match_window_days = days
    found.updated_by = user
    found.save()
    record_audit(user, "updated", found, before=before, after=settings_json(found))
    return found


def settings_json(found=None):
    found = found or ImportSettings.load()
    return {"transfer_match_window_days": found.transfer_match_window_days, "max_window_days": ImportSettings.MAX_WINDOW_DAYS}


# -- Import refs -----------------------------------------------------------------


def legacy_ref_account_id(ref):
    head, _, rest = (ref or "").partition(":")
    return int(head) if head.isdigit() and rest else None


def entry_for_ref(ref):
    """The transaction that already booked this statement row, if any."""
    entry = JournalEntry.objects.filter(import_ref=ref).first()
    if entry:
        return entry
    linked = TransactionImportRef.objects.select_related("entry").filter(account_id=legacy_ref_account_id(ref), import_ref=ref).first()
    return linked.entry if linked else None


def ref_account_ids(entries):
    """{entry id: set of account ids the entry already has an import ref for}."""
    found = defaultdict(set)
    ids = []
    for entry in entries:
        ids.append(entry.pk)
        account_id = legacy_ref_account_id(entry.import_ref)
        if account_id is not None:
            found[entry.pk].add(account_id)
    for entry_id, account_id in TransactionImportRef.objects.filter(entry_id__in=ids).values_list("entry_id", "account_id"):
        found[entry_id].add(account_id)
    return found


def entry_import_refs(entry):
    """Every import ref the entry carries, legacy column included, for display."""
    refs = []
    if entry.import_ref:
        refs.append({"account_id": legacy_ref_account_id(entry.import_ref), "import_ref": entry.import_ref, "legacy": True})
    seen = {entry.import_ref}
    for item in entry.import_refs.all():
        if item.import_ref not in seen:
            refs.append({"account_id": item.account_id, "import_ref": item.import_ref, "legacy": False})
    return refs


@transaction.atomic
def link_import_ref(*, entry, account, ref, user, row):
    """Record that the statement row `ref` on `account` is booked by `entry`. Changes nothing else."""
    TransactionImportRef.objects.create(entry=entry, account=account, import_ref=ref)
    record_audit(user, "linked_import_ref", entry, after={
        "account_id": account.pk,
        "account": str(account),
        "import_ref": ref,
        "statement_row": {"date": row["date"].isoformat(), "amount": f"{row['amount']:.2f}", "description": row["description"]},
    })


# -- Matching --------------------------------------------------------------------


def closed_months(start, end):
    return {
        (period.year, period.month)
        for period in AccountingPeriod.objects.filter(closed_at__isnull=False, year__gte=start.year, year__lte=end.year)
    }


def net_effect(entry, account_id):
    return sum((line.debit - line.credit for line in entry.lines.all() if line.account_id == account_id), Decimal("0.00"))


def two_account_entries(statement_account, start, end, other_account=None):
    """Posted entries in [start, end] that touch the statement account and exactly one other account."""
    entries = JournalEntry.objects.filter(status=JournalEntry.Status.POSTED, date__gte=start, date__lte=end, lines__account=statement_account)
    if other_account is not None:
        entries = entries.filter(lines__account=other_account)
    entries = entries.distinct().prefetch_related("lines__account").order_by("date", "pk")
    for entry in entries:
        accounts = {line.account_id: line.account for line in entry.lines.all()}
        if len(accounts) == 2:
            other = next(account for pk, account in accounts.items() if pk != statement_account.pk)
            yield entry, other


def transfer_candidates(*, statement_account, row, window_days, other_account=None, exclude_ids=()):
    """Entries meeting matching rules 1-5 for the row.

    With other_account, the other side must be exactly that account (a transfer
    candidate row). Without it, the other side can be any bank, cash, or credit
    card account (the possible_transfer warning check).
    """
    start = row["date"] - timedelta(days=window_days)
    end = row["date"] + timedelta(days=window_days)
    closed = closed_months(start, end)
    found = []
    for entry, other in two_account_entries(statement_account, start, end, other_account):
        if entry.pk in exclude_ids or not is_cash_account(other):
            continue
        if net_effect(entry, statement_account.pk) != row["amount"]:
            continue
        if (entry.date.year, entry.date.month) in closed:
            continue
        found.append(entry)
    linked = ref_account_ids(found)
    return [entry for entry in found if statement_account.pk not in linked[entry.pk]]


def explain_mismatch(*, entry, statement_account, category, row, window_days):
    """Why an explicitly chosen transaction cannot be linked to the row, or None if it can."""
    if entry.status != JournalEntry.Status.POSTED:
        return f"transaction #{entry.pk} is {entry.status}, not posted."
    accounts = {line.account_id for line in entry.lines.all()}
    if accounts != {statement_account.pk, category.pk}:
        return f"transaction #{entry.pk} must move money between exactly {statement_account} and {category}, the row's category."
    effect = net_effect(entry, statement_account.pk)
    if effect != row["amount"]:
        return f"transaction #{entry.pk} changes {statement_account} by {effect:.2f}, not the row's {row['amount']:.2f}."
    if abs((entry.date - row["date"]).days) > window_days:
        return f"transaction #{entry.pk} is dated {entry.date.isoformat()}, more than {window_days} day(s) from the row."
    if AccountingPeriod.objects.filter(year=entry.date.year, month=entry.date.month, closed_at__isnull=False).exists():
        return f"transaction #{entry.pk} is in {entry.date:%B %Y}, which is closed."
    if statement_account.pk in ref_account_ids([entry])[entry.pk]:
        return f"transaction #{entry.pk} already books a {statement_account} statement row."
    return None


# -- Unlinked transfers ----------------------------------------------------------


def unlinked_transfers(*, start, end, account=None):
    """Transfers between two bank, cash, or card accounts where a side has no import ref yet."""
    entries = JournalEntry.objects.filter(status=JournalEntry.Status.POSTED, date__gte=start, date__lte=end)
    if account is not None:
        entries = entries.filter(lines__account=account)
    entries = list(entries.distinct().prefetch_related("lines__account").order_by("date", "pk"))
    transfers = []
    for entry in entries:
        accounts = {line.account_id: line.account for line in entry.lines.all()}
        if len(accounts) == 2 and all(is_cash_account(item) for item in accounts.values()):
            transfers.append((entry, sorted(accounts.values(), key=lambda item: item.code)))
    linked = ref_account_ids([entry for entry, _ in transfers])
    closed = closed_months(start, end)
    items = []
    for entry, accounts in transfers:
        sides = [{"account": item, "linked": item.pk in linked[entry.pk], "statement_amount": net_effect(entry, item.pk)} for item in accounts]
        if all(side["linked"] for side in sides):
            continue
        items.append({"entry": entry, "sides": sides, "period_closed": (entry.date.year, entry.date.month) in closed})
    return items
