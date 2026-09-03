from datetime import date
from decimal import Decimal

from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce

from .models import Account, BusinessDocument, JournalLine


ZERO = Value(Decimal("0.00"), output_field=DecimalField(max_digits=15, decimal_places=2))


def account_rows(*, start=None, end=None, account_type=None):
    line_filter = Q(journal_lines__entry__status="posted")
    if start:
        line_filter &= Q(journal_lines__entry__date__gte=start)
    if end:
        line_filter &= Q(journal_lines__entry__date__lte=end)
    accounts = Account.objects.all()
    if account_type:
        accounts = accounts.filter(type=account_type)
    accounts = accounts.annotate(
        debits=Coalesce(Sum("journal_lines__debit", filter=line_filter), ZERO),
        credits=Coalesce(Sum("journal_lines__credit", filter=line_filter), ZERO),
    )
    rows = []
    for account in accounts:
        raw = account.debits - account.credits
        balance = raw if account.normal_balance == "debit" else -raw
        rows.append({"account": account, "debits": account.debits, "credits": account.credits, "balance": balance})
    return rows


def trial_balance(as_of=None):
    rows = []
    for row in account_rows(end=as_of):
        net = row["debits"] - row["credits"]
        row["debit_balance"] = net if net > 0 else Decimal("0.00")
        row["credit_balance"] = -net if net < 0 else Decimal("0.00")
        rows.append(row)
    return (
        rows,
        sum((row["debit_balance"] for row in rows), Decimal("0.00")),
        sum((row["credit_balance"] for row in rows), Decimal("0.00")),
    )


def balance_sheet(as_of=None):
    as_of = as_of or date.today()
    groups = {}
    for account_type in [Account.Type.ASSET, Account.Type.LIABILITY, Account.Type.EQUITY]:
        rows = [row for row in account_rows(end=as_of, account_type=account_type) if row["balance"]]
        groups[account_type] = {"rows": rows, "total": sum((row["balance"] for row in rows), Decimal("0.00"))}
    revenue = sum((row["balance"] for row in account_rows(end=as_of, account_type=Account.Type.REVENUE)), Decimal("0.00"))
    expense = sum((row["balance"] for row in account_rows(end=as_of, account_type=Account.Type.EXPENSE)), Decimal("0.00"))
    groups[Account.Type.EQUITY]["current_earnings"] = revenue - expense
    groups[Account.Type.EQUITY]["total"] += revenue - expense
    return groups


def income_statement(start=None, end=None):
    end = end or date.today()
    start = start or date(end.year, 1, 1)
    revenue = [row for row in account_rows(start=start, end=end, account_type=Account.Type.REVENUE) if row["balance"]]
    expenses = [row for row in account_rows(start=start, end=end, account_type=Account.Type.EXPENSE) if row["balance"]]
    revenue_total = sum((row["balance"] for row in revenue), Decimal("0.00"))
    expense_total = sum((row["balance"] for row in expenses), Decimal("0.00"))
    return {"revenue": revenue, "expenses": expenses, "revenue_total": revenue_total, "expense_total": expense_total, "net_income": revenue_total - expense_total}


def aging(kind, as_of=None):
    as_of = as_of or date.today()
    documents = BusinessDocument.objects.filter(kind=kind, issue_date__lte=as_of).exclude(status=BusinessDocument.Status.VOID).select_related("contact").prefetch_related("payments")
    buckets = {"current": Decimal("0.00"), "1-30": Decimal("0.00"), "31-60": Decimal("0.00"), "61-90": Decimal("0.00"), "90+": Decimal("0.00")}
    rows = []
    for document in documents:
        paid_as_of = sum((payment.amount for payment in document.payments.all() if payment.date <= as_of), Decimal("0.00"))
        balance = document.amount - paid_as_of
        if balance <= 0:
            continue
        days = (as_of - document.due_date).days
        bucket = "current" if days <= 0 else "1-30" if days <= 30 else "31-60" if days <= 60 else "61-90" if days <= 90 else "90+"
        buckets[bucket] += balance
        rows.append({"document": document, "days": max(days, 0), "bucket": bucket, "balance": balance})
    return rows, buckets


def owner_balances(as_of=None):
    query = JournalLine.objects.filter(entry__status="posted", owner__isnull=False).select_related("owner", "account")
    if as_of:
        query = query.filter(entry__date__lte=as_of)
    owners = {}
    for line in query:
        row = owners.setdefault(line.owner_id, {"owner": line.owner, "contributions": Decimal("0.00"), "draws": Decimal("0.00"), "owed": Decimal("0.00")})
        if line.account.subtype == Account.Subtype.OWNER_CONTRIBUTION:
            row["contributions"] += line.credit - line.debit
        elif line.account.subtype == Account.Subtype.OWNER_DRAW:
            row["draws"] += line.debit - line.credit
        elif line.account.subtype == Account.Subtype.OWNER_LOAN_PAYABLE:
            row["owed"] += line.credit - line.debit
    return sorted(owners.values(), key=lambda row: row["owner"].name)
