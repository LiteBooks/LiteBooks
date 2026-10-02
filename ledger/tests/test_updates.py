import json
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ledger import updates
from ledger.models import AuditEvent, SystemUpdateRun, SystemUpdateState, UserProfile


RUNNING_SHA = "1111111111111111111111111111111111111111"
LATEST_SHA = "2222222222222222222222222222222222222222"


def head_payload(sha=LATEST_SHA, message="Add bank reconciliation"):
    return {"sha": sha, "commit": {"message": message + "\n\nlonger body", "committer": {"date": "2026-09-28T10:00:00Z"}}}


def compare_payload(count=3):
    return {
        "ahead_by": count,
        "commits": [
            {"sha": f"{index}" * 40, "commit": {"message": f"Commit {index}", "committer": {"date": "2026-09-2%dT10:00:00Z" % index}}}
            for index in range(1, count + 1)
        ],
    }


class UpdateCheckTests(TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp()

    def settings_override(self, **extra):
        defaults = {
            "LITEBOOKS_GIT_SHA": RUNNING_SHA,
            "LITEBOOKS_UPDATES_ENABLED": True,
            "LITEBOOKS_UPDATE_STATE_DIR": self.state_dir,
            "LITEBOOKS_UPDATE_CHECK_INTERVAL": 86400,
        }
        defaults.update(extra)
        return override_settings(**defaults)

    def test_check_records_latest_commit_and_behind_count(self):
        with self.settings_override(), \
             patch("ledger.updates._get_json", side_effect=[head_payload(), compare_payload(3)]), \
             patch("ledger.updates.image_published", return_value=True):
            state = updates.check_github(force=True)

        self.assertEqual(state.latest_sha, LATEST_SHA)
        self.assertEqual(state.latest_message, "Add bank reconciliation")
        self.assertEqual(state.commits_behind, 3)
        self.assertEqual(len(state.commit_log), 3)
        self.assertTrue(state.image_available)
        self.assertEqual(state.check_error, "")

    def test_commit_log_is_newest_first(self):
        with self.settings_override(), \
             patch("ledger.updates._get_json", side_effect=[head_payload(), compare_payload(3)]), \
             patch("ledger.updates.image_published", return_value=True):
            state = updates.check_github(force=True)

        self.assertEqual([item["message"] for item in state.commit_log], ["Commit 3", "Commit 2", "Commit 1"])

    def test_network_failure_degrades_to_an_error_message(self):
        with self.settings_override(), patch("ledger.updates._get_json", side_effect=OSError("no route to host")):
            state = updates.check_github(force=True)

        self.assertIn("Could not reach GitHub", state.check_error)
        self.assertFalse(updates.update_available(state))
        self.assertIsNotNone(state.last_checked_at)

    def test_fresh_cache_is_not_rechecked(self):
        SystemUpdateState.objects.create(pk=1, last_checked_at=timezone.now(), latest_sha=LATEST_SHA)
        with self.settings_override(), patch("ledger.updates._get_json") as fetch:
            updates.check_github(force=False)
        fetch.assert_not_called()

    def test_stale_cache_is_rechecked(self):
        SystemUpdateState.objects.create(pk=1, last_checked_at=timezone.now() - timedelta(days=2))
        with self.settings_override(), \
             patch("ledger.updates._get_json", side_effect=[head_payload(), compare_payload(1)]) as fetch, \
             patch("ledger.updates.image_published", return_value=True):
            updates.check_github(force=False)
        self.assertTrue(fetch.called)

    def test_unpublished_image_means_no_update_offered(self):
        with self.settings_override(), \
             patch("ledger.updates._get_json", side_effect=[head_payload(), compare_payload(1)]), \
             patch("ledger.updates.image_published", return_value=False):
            state = updates.check_github(force=True)

        self.assertTrue(state.latest_sha)
        self.assertFalse(state.image_available)
        self.assertFalse(updates.update_available(state))

    def test_running_the_latest_sha_is_not_an_update(self):
        with override_settings(LITEBOOKS_GIT_SHA=LATEST_SHA, LITEBOOKS_UPDATES_ENABLED=True, LITEBOOKS_UPDATE_STATE_DIR=self.state_dir), \
             patch("ledger.updates._get_json", side_effect=[head_payload()]), \
             patch("ledger.updates.image_published", return_value=True):
            state = updates.check_github(force=True)

        self.assertEqual(state.commits_behind, 0)
        self.assertFalse(updates.update_available(state))

    def test_disabled_updates_short_circuit(self):
        with self.settings_override(LITEBOOKS_UPDATES_ENABLED=False), patch("ledger.updates._get_json") as fetch:
            state = updates.check_github(force=True)
        fetch.assert_not_called()
        self.assertFalse(updates.update_available(state))


class UpdateRequestTests(TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        self.user = get_user_model().objects.create_user(username="owner", password="very-good-password")
        self.user.profile.role = UserProfile.Role.OWNER
        self.user.profile.save()
        SystemUpdateState.objects.create(
            pk=1, latest_sha=LATEST_SHA, commits_behind=2, image_available=True, last_checked_at=timezone.now(),
        )

    def override(self):
        return override_settings(
            LITEBOOKS_GIT_SHA=RUNNING_SHA,
            LITEBOOKS_UPDATES_ENABLED=True,
            LITEBOOKS_UPDATE_STATE_DIR=self.state_dir,
        )

    def test_request_writes_a_well_formed_file_and_audits(self):
        with self.override():
            run = updates.request_update(self.user, LATEST_SHA)
            with open(updates.request_path()) as stream:
                payload = json.load(stream)

        self.assertEqual(payload["id"], run.pk)
        self.assertEqual(payload["target_sha"], LATEST_SHA)
        self.assertEqual(payload["target_tag"], "sha-2222222")
        self.assertEqual(payload["from_sha"], RUNNING_SHA)
        self.assertEqual(run.status, SystemUpdateRun.Status.PENDING)
        self.assertTrue(AuditEvent.objects.filter(action="requested update").exists())

    def test_concurrent_request_is_rejected(self):
        with self.override():
            updates.request_update(self.user, LATEST_SHA)
            with self.assertRaises(ValidationError):
                updates.request_update(self.user, LATEST_SHA)
        self.assertEqual(SystemUpdateRun.objects.count(), 1)

    def test_stale_target_sha_is_rejected(self):
        with self.override():
            with self.assertRaises(ValidationError):
                updates.request_update(self.user, "deadbeef" * 5)

    def test_request_fails_cleanly_when_the_state_volume_is_missing(self):
        with override_settings(
            LITEBOOKS_GIT_SHA=RUNNING_SHA,
            LITEBOOKS_UPDATES_ENABLED=True,
            LITEBOOKS_UPDATE_STATE_DIR="/nonexistent-state-dir",
        ), patch("ledger.updates._write_atomic", side_effect=OSError("read-only file system")):
            with self.assertRaises(ValidationError):
                updates.request_update(self.user, LATEST_SHA)

        run = SystemUpdateRun.objects.get()
        self.assertEqual(run.status, SystemUpdateRun.Status.FAILED)
        self.assertIn("updater", run.log)

    def test_status_file_is_reconciled_into_the_run(self):
        with self.override():
            run = updates.request_update(self.user, LATEST_SHA)
            with open(updates.status_path(), "w") as stream:
                json.dump({
                    "id": run.pk,
                    "state": "success",
                    "step": "done",
                    "backup_path": "/state/backups/pre-update-1111111.sql.gz",
                    "log": ["Backing up", "Pulling", "Update complete."],
                }, stream)
            reconciled = updates.read_status()

        self.assertEqual(reconciled.status, SystemUpdateRun.Status.SUCCESS)
        self.assertEqual(reconciled.step, "done")
        self.assertIn("Update complete.", reconciled.log)
        self.assertIsNotNone(reconciled.finished_at)
        self.assertFalse(reconciled.is_active)

    def test_status_for_a_different_run_is_ignored(self):
        with self.override():
            run = updates.request_update(self.user, LATEST_SHA)
            with open(updates.status_path(), "w") as stream:
                json.dump({"id": run.pk + 99, "state": "success", "log": []}, stream)
            reconciled = updates.read_status()

        self.assertEqual(reconciled.status, SystemUpdateRun.Status.PENDING)


class UpdateApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.users = {}
        for role in [UserProfile.Role.VIEWER, UserProfile.Role.BOOKKEEPER, UserProfile.Role.ADMIN, UserProfile.Role.OWNER]:
            user = User.objects.create_user(username=role, password="very-good-password")
            user.profile.role = role
            user.profile.save()
            cls.users[role] = user

    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        SystemUpdateState.objects.create(
            pk=1, latest_sha=LATEST_SHA, commits_behind=2, image_available=True, last_checked_at=timezone.now(),
        )

    def override(self):
        return override_settings(
            LITEBOOKS_GIT_SHA=RUNNING_SHA,
            LITEBOOKS_UPDATES_ENABLED=True,
            LITEBOOKS_UPDATE_STATE_DIR=self.state_dir,
        )

    def test_viewing_requires_admin(self):
        for role, expected in [
            (UserProfile.Role.VIEWER, 403),
            (UserProfile.Role.BOOKKEEPER, 403),
            (UserProfile.Role.ADMIN, 200),
            (UserProfile.Role.OWNER, 200),
        ]:
            self.client.force_login(self.users[role])
            with self.override():
                response = self.client.get(reverse("api-system-update"))
            self.assertEqual(response.status_code, expected, role)

    def test_applying_requires_owner(self):
        self.client.force_login(self.users[UserProfile.Role.ADMIN])
        with self.override():
            response = self.client.post(
                reverse("api-system-update-apply"),
                data=json.dumps({"target_sha": LATEST_SHA}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SystemUpdateRun.objects.exists())

    def test_owner_can_apply(self):
        self.client.force_login(self.users[UserProfile.Role.OWNER])
        with self.override():
            response = self.client.post(
                reverse("api-system-update-apply"),
                data=json.dumps({"target_sha": LATEST_SHA}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["run"]["to_tag"], "sha-2222222")

    def test_anonymous_is_rejected(self):
        self.assertEqual(self.client.get(reverse("api-system-update")).status_code, 401)

    def test_session_exposes_the_badge_flag_without_touching_the_network(self):
        self.client.force_login(self.users[UserProfile.Role.ADMIN])
        with self.override(), patch("ledger.updates._get_json") as fetch:
            response = self.client.get(reverse("api-session"))
        fetch.assert_not_called()
        self.assertTrue(response.json()["update_available"])

    def test_bookkeeper_never_sees_the_badge(self):
        self.client.force_login(self.users[UserProfile.Role.BOOKKEEPER])
        with self.override():
            response = self.client.get(reverse("api-session"))
        self.assertFalse(response.json()["update_available"])

    def test_health_endpoint_is_public_and_reports_the_sha(self):
        with self.override():
            response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["sha"], RUNNING_SHA)
