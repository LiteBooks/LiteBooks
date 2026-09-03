from datetime import date
from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory

from .models import (
    Account,
    AccountingPeriod,
    BusinessDocument,
    Contact,
    JournalEntry,
    JournalLine,
    OwnerActivity,
    UserProfile,
)


class DateInput(forms.DateInput):
    input_type = "date"


class AccountForm(forms.ModelForm):
    class Meta:
        model = Account
        fields = ["code", "name", "type", "subtype", "description", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


class ContactForm(forms.ModelForm):
    class Meta:
        model = Contact
        fields = ["name", "kind", "email", "phone", "address", "notes", "is_active"]
        widgets = {
            "address": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


class QuickTransactionForm(forms.Form):
    date = forms.DateField(initial=date.today, widget=DateInput())
    description = forms.CharField(max_length=240)
    amount = forms.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal("0.01"))
    debit_account = forms.ModelChoiceField(queryset=Account.objects.none())
    credit_account = forms.ModelChoiceField(queryset=Account.objects.none())
    contact = forms.ModelChoiceField(queryset=Contact.objects.none(), required=False)
    linked_entry = forms.ModelChoiceField(queryset=JournalEntry.objects.none(), required=False, help_text="Use this when the transaction settles or relates to an earlier entry.")
    memo = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    attachment = forms.FileField(required=False, help_text="PDF or image, up to 25 MB.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        accounts = Account.objects.filter(is_active=True)
        self.fields["debit_account"].queryset = accounts
        self.fields["credit_account"].queryset = accounts
        self.fields["contact"].queryset = Contact.objects.filter(is_active=True)
        self.fields["linked_entry"].queryset = JournalEntry.objects.filter(status=JournalEntry.Status.POSTED)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("debit_account") == cleaned.get("credit_account"):
            raise forms.ValidationError("Debit and credit accounts must be different.")
        return cleaned


class EntryHeaderForm(forms.ModelForm):
    attachment = forms.FileField(required=False, help_text="PDF or image, up to 25 MB.")

    class Meta:
        model = JournalEntry
        fields = ["date", "description", "contact", "linked_entry", "memo"]
        widgets = {"date": DateInput(), "memo": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["contact"].queryset = Contact.objects.filter(is_active=True)
        self.fields["linked_entry"].queryset = JournalEntry.objects.filter(status=JournalEntry.Status.POSTED).exclude(pk=self.instance.pk)


class JournalLineForm(forms.ModelForm):
    debit = forms.DecimalField(max_digits=15, decimal_places=2, required=False, min_value=0)
    credit = forms.DecimalField(max_digits=15, decimal_places=2, required=False, min_value=0)

    class Meta:
        model = JournalLine
        fields = ["account", "description", "debit", "credit", "owner"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = Account.objects.filter(is_active=True)
        self.fields["owner"].queryset = Contact.objects.filter(kind=Contact.Kind.OWNER, is_active=True)
        self.fields["owner"].required = False

    def clean(self):
        cleaned = super().clean()
        if not cleaned or cleaned.get("DELETE"):
            return cleaned
        debit = cleaned.get("debit") or Decimal("0.00")
        credit = cleaned.get("credit") or Decimal("0.00")
        if bool(debit) == bool(credit):
            raise forms.ValidationError("Enter an amount in either debit or credit.")
        cleaned["debit"] = debit
        cleaned["credit"] = credit
        return cleaned


JournalLineFormSet = inlineformset_factory(
    JournalEntry,
    JournalLine,
    form=JournalLineForm,
    fields=["account", "description", "debit", "credit", "owner"],
    extra=2,
    can_delete=True,
    min_num=2,
    validate_min=True,
)


class BusinessDocumentForm(forms.ModelForm):
    attachment = forms.FileField(required=False, help_text="Optional invoice or receipt PDF/image.")

    class Meta:
        model = BusinessDocument
        fields = ["number", "contact", "issue_date", "due_date", "description", "amount", "control_account", "category_account"]
        widgets = {"issue_date": DateInput(), "due_date": DateInput()}

    def __init__(self, *args, kind, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind
        if kind == BusinessDocument.Kind.INVOICE:
            self.fields["contact"].queryset = Contact.objects.filter(kind__in=[Contact.Kind.CUSTOMER, Contact.Kind.BOTH], is_active=True)
            self.fields["control_account"].queryset = Account.objects.filter(subtype=Account.Subtype.RECEIVABLE, is_active=True)
            self.fields["category_account"].queryset = Account.objects.filter(type=Account.Type.REVENUE, is_active=True)
            self.fields["category_account"].label = "Revenue account"
        else:
            self.fields["contact"].queryset = Contact.objects.filter(kind__in=[Contact.Kind.VENDOR, Contact.Kind.BOTH], is_active=True)
            self.fields["control_account"].queryset = Account.objects.filter(subtype=Account.Subtype.PAYABLE, is_active=True)
            self.fields["category_account"].queryset = Account.objects.filter(type=Account.Type.EXPENSE, is_active=True)
            self.fields["category_account"].label = "Expense account"

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("due_date") and cleaned.get("issue_date") and cleaned["due_date"] < cleaned["issue_date"]:
            self.add_error("due_date", "Due date cannot be before the issue date.")
        return cleaned

    def clean_number(self):
        number = self.cleaned_data["number"].strip()
        if BusinessDocument.objects.filter(kind=self.kind, number=number).exists():
            raise forms.ValidationError(f"That {BusinessDocument.Kind(self.kind).label.lower()} number already exists.")
        return number


class PaymentForm(forms.Form):
    date = forms.DateField(initial=date.today, widget=DateInput())
    amount = forms.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal("0.01"))
    cash_account = forms.ModelChoiceField(queryset=Account.objects.none(), label="Bank or cash account")
    memo = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    attachment = forms.FileField(required=False)

    def __init__(self, *args, document=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.document = document
        self.fields["cash_account"].queryset = Account.objects.filter(subtype__in=[Account.Subtype.BANK, Account.Subtype.CASH], is_active=True)
        if document:
            self.fields["amount"].initial = document.balance_due

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if self.document and amount > self.document.balance_due:
            raise forms.ValidationError(f"The remaining balance is {self.document.balance_due:.2f}.")
        return amount


class OwnerActivityForm(forms.Form):
    owner = forms.ModelChoiceField(queryset=Contact.objects.none())
    kind = forms.ChoiceField(choices=OwnerActivity.Kind.choices)
    date = forms.DateField(initial=date.today, widget=DateInput())
    amount = forms.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal("0.01"))
    cash_account = forms.ModelChoiceField(queryset=Account.objects.none(), label="Bank or cash account")
    offset_account = forms.ModelChoiceField(queryset=Account.objects.none(), label="Owner equity or loan account")
    memo = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    attachment = forms.FileField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["owner"].queryset = Contact.objects.filter(kind=Contact.Kind.OWNER, is_active=True)
        self.fields["cash_account"].queryset = Account.objects.filter(subtype__in=[Account.Subtype.BANK, Account.Subtype.CASH], is_active=True)
        self.fields["offset_account"].queryset = Account.objects.filter(
            subtype__in=[Account.Subtype.OWNER_CONTRIBUTION, Account.Subtype.OWNER_DRAW, Account.Subtype.OWNER_LOAN_PAYABLE],
            is_active=True,
        )

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("kind")
        offset = cleaned.get("offset_account")
        if not kind or not offset:
            return cleaned
        allowed = {
            OwnerActivity.Kind.CONTRIBUTION: {Account.Subtype.OWNER_CONTRIBUTION},
            OwnerActivity.Kind.DRAW: {Account.Subtype.OWNER_DRAW, Account.Subtype.OWNER_CONTRIBUTION},
            OwnerActivity.Kind.LOAN_IN: {Account.Subtype.OWNER_LOAN_PAYABLE},
            OwnerActivity.Kind.LOAN_REPAYMENT: {Account.Subtype.OWNER_LOAN_PAYABLE},
        }
        if offset.subtype not in allowed[kind]:
            self.add_error("offset_account", "That account type does not match the selected owner activity.")
        return cleaned


class AccountingPeriodForm(forms.ModelForm):
    class Meta:
        model = AccountingPeriod
        fields = ["year", "month"]


class UserCreateForm(forms.ModelForm):
    role = forms.ChoiceField(choices=UserProfile.Role.choices)
    password = forms.CharField(widget=forms.PasswordInput, min_length=8)

    class Meta:
        model = get_user_model()
        fields = ["username", "first_name", "last_name", "is_active"]

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        if commit:
            user.save()
            user.profile.role = self.cleaned_data["role"]
            user.profile.save(update_fields=["role"])
        return user


class UserUpdateForm(forms.ModelForm):
    role = forms.ChoiceField(choices=UserProfile.Role.choices)

    class Meta:
        model = get_user_model()
        fields = ["first_name", "last_name", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].initial = self.instance.profile.role

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit:
            user.profile.role = self.cleaned_data["role"]
            user.profile.save(update_fields=["role"])
        return user


class ProfileForm(forms.ModelForm):
    current_password = forms.CharField(required=False, strip=False)
    new_password = forms.CharField(required=False, strip=False)
    confirm_password = forms.CharField(required=False, strip=False)

    class Meta:
        model = get_user_model()
        fields = ["username", "first_name", "last_name"]

    def clean(self):
        cleaned = super().clean()
        current = cleaned.get("current_password")
        new = cleaned.get("new_password")
        confirmation = cleaned.get("confirm_password")
        if not any((current, new, confirmation)):
            return cleaned
        if not current:
            self.add_error("current_password", "Enter your current password.")
        elif not self.instance.check_password(current):
            self.add_error("current_password", "Your current password is incorrect.")
        if not new:
            self.add_error("new_password", "Enter a new password.")
        else:
            try:
                validate_password(new, self.instance)
            except ValidationError as error:
                self.add_error("new_password", error)
        if not confirmation:
            self.add_error("confirm_password", "Confirm your new password.")
        elif new and new != confirmation:
            self.add_error("confirm_password", "The new passwords do not match.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        if self.cleaned_data.get("new_password"):
            user.set_password(self.cleaned_data["new_password"])
        if commit:
            user.save()
        return user
