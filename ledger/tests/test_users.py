from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ledger.models import Account, AuditEvent, UserProfile
from ledger.services import create_entry


def make_user(username, role, **kwargs):
    user = get_user_model().objects.create_user(username=username, password="very-good-password", **kwargs)
    user.profile.role = role
    user.profile.save()
    return user


class UserDeleteTests(TestCase):
    def setUp(self):
        self.admin = make_user("admin", UserProfile.Role.ADMIN)
        self.viewer = make_user("viewer", UserProfile.Role.VIEWER)
        self.owner = make_user("owner", UserProfile.Role.OWNER)

    def url(self, user):
        return reverse("api-user", args=[user.pk])

    def test_admin_deletes_a_user_and_the_removal_is_audited(self):
        self.client.force_login(self.admin)
        response = self.client.delete(self.url(self.viewer))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(pk=self.viewer.pk).exists())
        event = AuditEvent.objects.get(action="deleted")
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.before["username"], "viewer")

    def test_bookkeeper_cannot_delete(self):
        self.client.force_login(make_user("books", UserProfile.Role.BOOKKEEPER))
        self.assertEqual(self.client.delete(self.url(self.viewer)).status_code, 403)
        self.assertTrue(get_user_model().objects.filter(pk=self.viewer.pk).exists())

    def test_anonymous_cannot_delete(self):
        self.assertEqual(self.client.delete(self.url(self.viewer)).status_code, 401)

    def test_you_cannot_delete_yourself(self):
        self.client.force_login(self.admin)
        response = self.client.delete(self.url(self.admin))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(get_user_model().objects.filter(pk=self.admin.pk).exists())

    def test_an_admin_cannot_delete_an_owner(self):
        self.client.force_login(self.admin)
        response = self.client.delete(self.url(self.owner))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(get_user_model().objects.filter(pk=self.owner.pk).exists())

    def test_an_owner_can_be_deleted_while_another_owner_remains(self):
        self.client.force_login(make_user("owner-two", UserProfile.Role.OWNER))
        self.assertEqual(self.client.delete(self.url(self.owner)).status_code, 200)
        self.assertFalse(get_user_model().objects.filter(pk=self.owner.pk).exists())

    def test_an_owner_cannot_delete_their_own_login_leaving_nobody(self):
        superuser = get_user_model().objects.create_superuser(username="root", password="very-good-password")
        self.client.force_login(superuser)
        response = self.client.delete(self.url(self.owner))
        self.assertEqual(response.status_code, 200)
        response = self.client.delete(self.url(superuser))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(get_user_model().objects.filter(pk=superuser.pk).exists())

    def test_a_user_with_postings_cannot_be_deleted(self):
        bank = Account.objects.create(code="1000", name="Bank", type=Account.Type.ASSET, subtype=Account.Subtype.BANK)
        sales = Account.objects.create(code="4000", name="Sales", type=Account.Type.REVENUE, subtype=Account.Subtype.SALES)
        poster = make_user("poster", UserProfile.Role.BOOKKEEPER)
        create_entry(
            entry_date=date(2026, 1, 5), description="Cash sale", user=poster,
            lines=[{"account": bank, "debit": 100, "credit": 0, "owner": None}, {"account": sales, "debit": 0, "credit": 100, "owner": None}],
        )
        self.client.force_login(self.admin)
        response = self.client.delete(self.url(poster))
        self.assertEqual(response.status_code, 409)
        self.assertTrue(get_user_model().objects.filter(pk=poster.pk).exists())
        self.assertFalse(AuditEvent.objects.filter(action="deleted").exists())
