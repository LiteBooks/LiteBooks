from calendar import monthrange
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


MONEY = {"max_digits": 15, "decimal_places": 2, "default": Decimal("0.00")}


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class UserProfile(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Administrator"
        BOOKKEEPER = "bookkeeper", "Bookkeeper"
        VIEWER = "viewer", "Viewer"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.VIEWER)

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"


class Account(TimeStampedModel):
    class Type(models.TextChoices):
        ASSET = "asset", "Asset"
        LIABILITY = "liability", "Liability"
        EQUITY = "equity", "Equity"
        REVENUE = "revenue", "Revenue"
        EXPENSE = "expense", "Expense"

    class Subtype(models.TextChoices):
        BANK = "bank", "Bank"
        CASH = "cash", "Cash"
        RECEIVABLE = "accounts_receivable", "Accounts receivable"
        FIXED_ASSET = "fixed_asset", "Fixed asset"
        OTHER_ASSET = "other_asset", "Other asset"
        CREDIT_CARD = "credit_card", "Credit card"
        PAYABLE = "accounts_payable", "Accounts payable"
        OWNER_LOAN_PAYABLE = "owner_loan_payable", "Owner loan payable"
        OTHER_LIABILITY = "other_liability", "Other liability"
        OWNER_CONTRIBUTION = "owner_contribution", "Owner contribution"
        OWNER_DRAW = "owner_draw", "Owner draw"
        RETAINED_EARNINGS = "retained_earnings", "Retained earnings"
        SALES = "sales", "Sales"
        OTHER_REVENUE = "other_revenue", "Other revenue"
        COST_OF_GOODS = "cost_of_goods", "Cost of goods sold"
        OPERATING_EXPENSE = "operating_expense", "Operating expense"
        OTHER_EXPENSE = "other_expense", "Other expense"

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=120)
    type = models.CharField(max_length=20, choices=Type.choices)
    subtype = models.CharField(max_length=40, choices=Subtype.choices, blank=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    is_system = models.BooleanField(default=False)

    class Meta:
        ordering = ["code", "name"]

    def __str__(self):
        return f"{self.code} - {self.name}"

    def clean(self):
        subtype_types = {
            self.Subtype.BANK: self.Type.ASSET,
            self.Subtype.CASH: self.Type.ASSET,
            self.Subtype.RECEIVABLE: self.Type.ASSET,
            self.Subtype.FIXED_ASSET: self.Type.ASSET,
            self.Subtype.OTHER_ASSET: self.Type.ASSET,
            self.Subtype.CREDIT_CARD: self.Type.LIABILITY,
            self.Subtype.PAYABLE: self.Type.LIABILITY,
            self.Subtype.OWNER_LOAN_PAYABLE: self.Type.LIABILITY,
            self.Subtype.OTHER_LIABILITY: self.Type.LIABILITY,
            self.Subtype.OWNER_CONTRIBUTION: self.Type.EQUITY,
            self.Subtype.OWNER_DRAW: self.Type.EQUITY,
            self.Subtype.RETAINED_EARNINGS: self.Type.EQUITY,
            self.Subtype.SALES: self.Type.REVENUE,
            self.Subtype.OTHER_REVENUE: self.Type.REVENUE,
            self.Subtype.COST_OF_GOODS: self.Type.EXPENSE,
            self.Subtype.OPERATING_EXPENSE: self.Type.EXPENSE,
            self.Subtype.OTHER_EXPENSE: self.Type.EXPENSE,
        }
        if self.subtype and subtype_types.get(self.subtype) != self.type:
            raise ValidationError({"subtype": "This subtype does not belong to the selected account type."})
        if self.pk:
            original = Account.objects.filter(pk=self.pk).values("type", "subtype").first()
            if original and (original["type"] != self.type or original["subtype"] != self.subtype) and self.journal_lines.exists():
                raise ValidationError("Account type and subtype cannot change after the account has posted activity.")

    @property
    def normal_balance(self):
        return "debit" if self.type in {self.Type.ASSET, self.Type.EXPENSE} else "credit"


class Contact(TimeStampedModel):
    class Kind(models.TextChoices):
        CUSTOMER = "customer", "Customer"
        VENDOR = "vendor", "Vendor"
        BOTH = "both", "Customer and vendor"
        OWNER = "owner", "Owner"

    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    address = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class AccountingPeriod(models.Model):
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-year", "-month"]
        constraints = [
            models.UniqueConstraint(fields=["year", "month"], name="unique_accounting_period"),
            models.CheckConstraint(condition=Q(month__gte=1, month__lte=12), name="valid_accounting_month"),
        ]

    @property
    def is_closed(self):
        return self.closed_at is not None

    @property
    def start_date(self):
        return date(self.year, self.month, 1)

    @property
    def end_date(self):
        return date(self.year, self.month, monthrange(self.year, self.month)[1])

    def __str__(self):
        return self.start_date.strftime("%B %Y")


class JournalEntry(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        POSTED = "posted", "Posted"

    class Source(models.TextChoices):
        GENERAL = "general", "General"
        INVOICE = "invoice", "Invoice"
        BILL = "bill", "Bill"
        PAYMENT = "payment", "Payment"
        OWNER = "owner", "Owner activity"
        OPENING = "opening", "Opening balance"

    number = models.CharField(max_length=30, unique=True, blank=True)
    date = models.DateField(default=date.today, db_index=True)
    description = models.CharField(max_length=240)
    memo = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.GENERAL)
    contact = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.PROTECT, related_name="journal_entries")
    linked_entry = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="linked_payments")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="entries_created")
    posted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="entries_posted")
    posted_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["-date", "-number"]

    def __str__(self):
        return f"{self.number or 'Draft'} - {self.description}"

    @property
    def total(self):
        return self.lines.aggregate(total=models.Sum("debit"))["total"] or Decimal("0.00")

    @property
    def is_balanced(self):
        totals = self.lines.aggregate(debits=models.Sum("debit"), credits=models.Sum("credit"))
        return (totals["debits"] or 0) == (totals["credits"] or 0) and (totals["debits"] or 0) > 0


class EntryNumberSequence(models.Model):
    entry_date = models.DateField(unique=True)
    last_value = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.entry_date:%Y-%m-%d}: {self.last_value}"


class JournalLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="journal_lines")
    description = models.CharField(max_length=240, blank=True)
    debit = models.DecimalField(**MONEY)
    credit = models.DecimalField(**MONEY)
    owner = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.PROTECT, related_name="owner_lines")
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(debit__gte=0) & Q(credit__gte=0), name="nonnegative_journal_line"),
            models.CheckConstraint(
                condition=(Q(debit__gt=0, credit=0) | Q(credit__gt=0, debit=0)),
                name="journal_line_one_side",
            ),
        ]

    def clean(self):
        if bool(self.debit) == bool(self.credit):
            raise ValidationError("Enter either a debit or a credit, but not both.")
        if self.owner and self.owner.kind != Contact.Kind.OWNER:
            raise ValidationError({"owner": "The selected contact is not an owner."})


def attachment_path(instance, filename):
    suffix = Path(filename).suffix.lower()
    return f"attachments/{instance.entry.date:%Y/%m}/{uuid4().hex}{suffix}"


class Attachment(TimeStampedModel):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=attachment_path)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    def __str__(self):
        return self.original_name


class BusinessDocument(TimeStampedModel):
    class Kind(models.TextChoices):
        INVOICE = "invoice", "Invoice"
        BILL = "bill", "Bill"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        PARTIAL = "partial", "Partially paid"
        PAID = "paid", "Paid"
        VOID = "void", "Void"

    kind = models.CharField(max_length=10, choices=Kind.choices, db_index=True)
    number = models.CharField(max_length=40)
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name="documents")
    issue_date = models.DateField(default=date.today)
    due_date = models.DateField()
    description = models.CharField(max_length=240)
    amount = models.DecimalField(**MONEY)
    control_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="controlled_documents")
    category_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="categorized_documents")
    journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, related_name="business_document")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)

    class Meta:
        ordering = ["-issue_date", "-number"]
        constraints = [models.UniqueConstraint(fields=["kind", "number"], name="unique_document_number_by_kind")]

    def __str__(self):
        return f"{self.get_kind_display()} {self.number}"

    @property
    def amount_paid(self):
        return self.payments.aggregate(total=models.Sum("amount"))["total"] or Decimal("0.00")

    @property
    def balance_due(self):
        return self.amount - self.amount_paid


class Payment(TimeStampedModel):
    document = models.ForeignKey(BusinessDocument, on_delete=models.PROTECT, related_name="payments")
    date = models.DateField(default=date.today)
    amount = models.DecimalField(**MONEY)
    cash_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="document_payments")
    journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, related_name="payment")

    class Meta:
        ordering = ["-date", "-id"]


class OwnerActivity(TimeStampedModel):
    class Kind(models.TextChoices):
        CONTRIBUTION = "contribution", "Equity contribution"
        LOAN_IN = "loan_in", "Owner loan to business"
        LOAN_REPAYMENT = "loan_repayment", "Repayment to owner"
        DRAW = "draw", "Owner draw"

    owner = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name="owner_activities")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    amount = models.DecimalField(**MONEY)
    journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, related_name="owner_activity")

    class Meta:
        ordering = ["-journal_entry__date", "-id"]


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=30)
    object_type = models.CharField(max_length=60)
    object_id = models.CharField(max_length=60)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} {self.object_type} {self.object_id}"
