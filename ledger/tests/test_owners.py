from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ledger.models import Contact, UserProfile


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
