from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ledger.models import Account, UserProfile


DEFAULT_ACCOUNTS = [
    ("1000", "Checking", Account.Type.ASSET, Account.Subtype.BANK, "Primary operating bank account"),
    ("1010", "Cash on hand", Account.Type.ASSET, Account.Subtype.CASH, "Physical cash"),
    ("1100", "Accounts receivable", Account.Type.ASSET, Account.Subtype.RECEIVABLE, "Amounts owed by customers"),
    ("1200", "Equipment", Account.Type.ASSET, Account.Subtype.FIXED_ASSET, "Equipment and other fixed assets"),
    ("2000", "Accounts payable", Account.Type.LIABILITY, Account.Subtype.PAYABLE, "Amounts owed to vendors"),
    ("2100", "Credit card", Account.Type.LIABILITY, Account.Subtype.CREDIT_CARD, "Business credit card balance"),
    ("2200", "Owner loans payable", Account.Type.LIABILITY, Account.Subtype.OWNER_LOAN_PAYABLE, "Amounts the business owes to owners"),
    ("3000", "Owner contributions", Account.Type.EQUITY, Account.Subtype.OWNER_CONTRIBUTION, "Capital contributed by owners"),
    ("3100", "Owner draws", Account.Type.EQUITY, Account.Subtype.OWNER_DRAW, "Distributions to owners"),
    ("3200", "Retained earnings", Account.Type.EQUITY, Account.Subtype.RETAINED_EARNINGS, "Cumulative retained earnings"),
    ("4000", "Sales", Account.Type.REVENUE, Account.Subtype.SALES, "Revenue from customers"),
    ("4100", "Other revenue", Account.Type.REVENUE, Account.Subtype.OTHER_REVENUE, "Non-operating revenue"),
    ("5000", "Cost of goods sold", Account.Type.EXPENSE, Account.Subtype.COST_OF_GOODS, "Direct cost of goods or services sold"),
    ("6000", "General operating expenses", Account.Type.EXPENSE, Account.Subtype.OPERATING_EXPENSE, "General business operating costs"),
]


class Command(BaseCommand):
    help = "Create the first owner login and a starter chart of accounts."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="admin")
        parser.add_argument("--password", required=True)
        parser.add_argument("--first-name", default="")
        parser.add_argument("--last-name", default="")

    @transaction.atomic
    def handle(self, *args, **options):
        if len(options["password"]) < 8:
            raise CommandError("Password must be at least 8 characters.")
        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=options["username"],
            defaults={"first_name": options["first_name"], "last_name": options["last_name"], "is_staff": True, "is_superuser": True},
        )
        if created:
            user.set_password(options["password"])
            user.save()
        user.profile.role = UserProfile.Role.OWNER
        user.profile.save(update_fields=["role"])
        account_count = 0
        for code, name, account_type, subtype, description in DEFAULT_ACCOUNTS:
            _, was_created = Account.objects.get_or_create(
                code=code,
                defaults={"name": name, "type": account_type, "subtype": subtype, "description": description, "is_system": True},
            )
            account_count += int(was_created)
        self.stdout.write(self.style.SUCCESS(f"Owner '{user.username}' ready; {account_count} starter accounts created."))

