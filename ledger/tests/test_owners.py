from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ledger.models import Account, AccountingPeriod, AuditEvent, Contact, UserProfile
from ledger.services import PostingError, create_owner_activity, update_owner_activity


class OwnerListingTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="books", password="very-good-password")
        user.profile.role = UserProfile.Role.BOOKKEEPER
        user.profile.save()
        self.client.force_login(user)

    def owners(self):
        return self.client.get(reverse("api-owners")).json()["owners"]

    def test_an_owner_with_no_activity_is_listed_with_zero_balances(self):
        owner = Contact.objects.create(name="Dana Reed", kind=Contact.Kind.OWNER)
        rows = self.owners()
        self.assertEqual([row["owner"]["id"] for row in rows], [owner.pk])
        self.assertEqual(rows[0]["contributions"], "0.00")
        self.assertEqual(rows[0]["draws"], "0.00")
        self.assertEqual(rows[0]["owed"], "0.00")

    def test_inactive_owners_and_other_contact_kinds_are_not_listed(self):
        Contact.objects.create(name="Retired Owner", kind=Contact.Kind.OWNER, is_active=False)
        Contact.objects.create(name="A Customer", kind=Contact.Kind.CUSTOMER)
        self.assertEqual(self.owners(), [])

    def test_owners_are_sorted_by_name(self):
        Contact.objects.create(name="Zoe Vance", kind=Contact.Kind.OWNER)
        Contact.objects.create(name="Avery Cole", kind=Contact.Kind.OWNER)
        self.assertEqual([row["owner"]["name"] for row in self.owners()], ["Avery Cole", "Zoe Vance"])


class OwnerActivitySetupTests(TestCase):
    """The owner activity form needs an owner, a cash account, and a matching offset account."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="admin", password="very-good-password", is_superuser=True)
        self.client.force_login(self.user)

    def options(self):
        return self.client.get(reverse("api-options")).json()

    def test_the_subtypes_the_setup_hint_links_to_are_real_choices(self):
        from ledger.models import Account

        values = {item["value"] for item in self.options()["choices"]["account_subtypes"]}
        self.assertLessEqual(
            {"bank", Account.Subtype.OWNER_CONTRIBUTION, Account.Subtype.OWNER_DRAW, Account.Subtype.OWNER_LOAN_PAYABLE},
            values,
        )

    def test_options_expose_the_subtype_of_each_account_so_offsets_can_be_matched(self):
        from ledger.models import Account

        Account.objects.create(code="3100", name="Owner capital", type=Account.Type.EQUITY, subtype=Account.Subtype.OWNER_CONTRIBUTION)
        account = next(item for item in self.options()["accounts"] if item["code"] == "3100")
        self.assertEqual(account["subtype"], "owner_contribution")
        self.assertTrue(account["is_active"])


class OwnerActivityEditTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="books", password="very-good-password")
        cls.user.profile.role = UserProfile.Role.BOOKKEEPER
        cls.user.profile.save()
        cls.bank = Account.objects.create(code="1000", name="Bank", type=Account.Type.ASSET, subtype=Account.Subtype.BANK)
        cls.loan = Account.objects.create(code="2200", name="Owner loans", type=Account.Type.LIABILITY, subtype=Account.Subtype.OWNER_LOAN_PAYABLE)
        cls.capital = Account.objects.create(code="3000", name="Contributions", type=Account.Type.EQUITY, subtype=Account.Subtype.OWNER_CONTRIBUTION)
        cls.draws = Account.objects.create(code="3100", name="Draws", type=Account.Type.EQUITY, subtype=Account.Subtype.OWNER_DRAW)
        cls.owner = Contact.objects.create(name="Dana Reed", kind=Contact.Kind.OWNER)
        cls.partner = Contact.objects.create(name="Sam Park", kind=Contact.Kind.OWNER)

    def setUp(self):
        self.client.force_login(self.user)

    def post(self, kind, amount, offset, owner=None, when=None):
        return create_owner_activity(owner=owner or self.owner, kind=kind, activity_date=when or date(2026, 3, 1), amount=amount, cash_account=self.bank, offset_account=offset, user=self.user)

    def edit(self, activity, **changes):
        fields = {"owner": activity.owner, "kind": activity.kind, "activity_date": activity.journal_entry.date, "amount": activity.amount, "cash_account": self.bank, "offset_account": self.capital, "memo": ""}
        fields.update(changes)
        return update_owner_activity(activity=activity, user=self.user, **fields)

    def test_editing_rewrites_the_journal_entry_and_is_audited(self):
        activity = self.post("contribution", 500, self.capital)
        activity = self.edit(activity, amount=750, activity_date=date(2026, 3, 9), memo="Corrected")
        entry = activity.journal_entry
        entry.refresh_from_db()
        self.assertEqual(activity.amount, Decimal("750"))
        self.assertEqual(entry.date, date(2026, 3, 9))
        self.assertTrue(entry.number.startswith("20260309-"))
        self.assertEqual(entry.memo, "Corrected")
        self.assertEqual(entry.lines.get(account=self.bank).debit, Decimal("750.00"))
        self.assertEqual(entry.version, 2)
        self.assertTrue(AuditEvent.objects.filter(object_type="ledger.OwnerActivity", object_id=str(activity.pk), action="updated").exists())

    def test_a_contribution_can_become_a_draw(self):
        activity = self.post("contribution", 200, self.capital)
        activity = self.edit(activity, kind="draw", offset_account=self.draws)
        entry = activity.journal_entry
        self.assertEqual(entry.lines.get(account=self.draws).debit, Decimal("200.00"))
        self.assertEqual(entry.lines.get(account=self.bank).credit, Decimal("200.00"))
        self.assertEqual(entry.description, "Owner draw: Dana Reed")

    def test_a_repayment_can_be_edited_up_to_the_loan_excluding_itself(self):
        self.post("loan_in", 1000, self.loan)
        repayment = self.post("loan_repayment", 400, self.loan)
        self.edit(repayment, amount=1000, offset_account=self.loan)
        with self.assertRaises(PostingError):
            self.edit(repayment, amount=1000.01, offset_account=self.loan)

    def test_shrinking_a_loan_below_its_repayments_is_refused(self):
        loan = self.post("loan_in", 1000, self.loan)
        self.post("loan_repayment", 600, self.loan)
        with self.assertRaisesMessage(PostingError, "repaid more than they lent"):
            self.edit(loan, amount=500, offset_account=self.loan)
        with self.assertRaisesMessage(PostingError, "repaid more than they lent"):
            self.edit(loan, owner=self.partner, offset_account=self.loan)
        loan.refresh_from_db()
        self.assertEqual(loan.amount, 1000)

    def test_closed_months_block_edits(self):
        activity = self.post("contribution", 100, self.capital)
        AccountingPeriod.objects.create(year=2026, month=3, closed_at=timezone.now())
        with self.assertRaises(PostingError):
            self.edit(activity, amount=150)

    def test_owner_entries_link_to_their_activity_and_save_through_the_api(self):
        activity = self.post("contribution", 100, self.capital)
        entry = self.client.get(reverse("api-transaction", args=[activity.journal_entry_id])).json()["entry"]
        self.assertTrue(entry["can_edit"])
        self.assertEqual(entry["edit_url"], f"/owners/activity/{activity.pk}/edit/")
        detail = self.client.get(reverse("api-owner-activity", args=[activity.pk])).json()["activity"]
        self.assertEqual(detail["offset_account"]["id"], self.capital.pk)
        self.assertEqual(detail["cash_account"]["id"], self.bank.pk)
        response = self.client.post(reverse("api-owner-activity", args=[activity.pk]), {
            "owner_id": self.owner.pk, "kind": "contribution", "date": "2026-03-02", "amount": "125.00",
            "cash_account_id": self.bank.pk, "offset_account_id": self.capital.pk, "memo": "",
        }, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        activity.refresh_from_db()
        self.assertEqual(str(activity.amount), "125.00")

    def test_viewers_cannot_edit_owner_activity(self):
        viewer = get_user_model().objects.create_user(username="viewer", password="very-good-password")
        self.client.force_login(viewer)
        activity = self.post("contribution", 100, self.capital)
        response = self.client.post(reverse("api-owner-activity", args=[activity.pk]), {"amount": "1"}, content_type="application/json")
        self.assertEqual(response.status_code, 403)
