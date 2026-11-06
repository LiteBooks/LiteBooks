"""How current and how accurate a bank or card account's books are.

last_synced_at answers "how current": it is stamped whenever an import into the
account completes, even one that adds nothing, so an account with no new
activity is not mistaken for one that was never synced. Statement balances
answer "how accurate": the ending balance the bank reports, compared with the
book balance on the same date.

Balances are signed toward the account's normal side, the same way
list_accounts reports them: money in the bank is positive, and so is the amount
owed on a card. Enter card statement balances as the amount owed.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db.models import Max, Sum
from django.utils import timezone

from .models import Account, AccountSyncState, JournalEntry, JournalLine, StatementBalance


SYNCED_SUBTYPES = (Account.Subtype.BANK, Account.Subtype.CREDIT_CARD)


def is_synced_account(account):
    return account.subtype in SYNCED_SUBTYPES


def mark_synced(account, at=None):
    """Record that the account's activity is up to date as of `at` (default now)."""
    at = at or timezone.now()
    AccountSyncState.objects.update_or_create(account=account, defaults={"last_synced_at": at})
    return at


def record_statement_balance(*, account, statement_end_date, statement_ending_balance, user=None):
    if not is_synced_account(account):
        raise ValidationError({"account": "Statement balances can only be entered for bank and credit card accounts."})
    if not statement_ending_balance.is_finite() or abs(statement_ending_balance) >= Decimal("1e13"):
        raise ValidationError({"statement_ending_balance": "Enter a valid amount."})
    if statement_ending_balance != statement_ending_balance.quantize(Decimal("0.01")):
        raise ValidationError({"statement_ending_balance": "Use at most two decimal places."})
    return StatementBalance.objects.create(account=account, statement_end_date=statement_end_date, statement_ending_balance=statement_ending_balance, entered_by=user)


def book_balance(account, as_of=None):
    lines = JournalLine.objects.filter(account=account, entry__status=JournalEntry.Status.POSTED)
    if as_of is not None:
        lines = lines.filter(entry__date__lte=as_of)
    totals = lines.aggregate(debits=Sum("debit"), credits=Sum("credit"))
    raw = (totals["debits"] or Decimal("0.00")) - (totals["credits"] or Decimal("0.00"))
    return raw if account.normal_balance == "debit" else -raw


def money(value):
    return None if value is None else f"{value:.2f}"


def sync_status(account):
    state = AccountSyncState.objects.filter(account=account).first()
    last_date = JournalLine.objects.filter(account=account, entry__status=JournalEntry.Status.POSTED).aggregate(latest=Max("entry__date"))["latest"]
    statement = StatementBalance.objects.filter(account=account).first()
    at_statement = book_balance(account, statement.statement_end_date) if statement else None
    return {
        "account_id": account.pk,
        "code": account.code,
        "name": account.name,
        "last_synced_at": state.last_synced_at.isoformat() if state else None,
        "last_transaction_date": last_date.isoformat() if last_date else None,
        "book_balance": money(book_balance(account)),
        "latest_statement": {
            "statement_end_date": statement.statement_end_date.isoformat(),
            "statement_ending_balance": money(statement.statement_ending_balance),
        } if statement else None,
        "book_balance_at_statement_date": money(at_statement),
        "difference_vs_statement": money(at_statement - statement.statement_ending_balance) if statement else None,
    }


def synced_accounts():
    return Account.objects.filter(subtype__in=SYNCED_SUBTYPES).order_by("code")
