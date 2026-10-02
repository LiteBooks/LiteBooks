from django.test import TestCase, override_settings
from django.urls import reverse


@override_settings(ALLOWED_HOSTS=["books.example.com"])
class HealthProbeHostTests(TestCase):
    """The updater probes http://web:8000/healthz/, a host nobody allowlists."""

    def test_the_probe_answers_on_the_container_service_name(self):
        response = self.client.get(reverse("healthz"), headers={"host": "web:8000"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_the_probe_reports_the_sha_the_updater_compares(self):
        with self.settings(LITEBOOKS_GIT_SHA="a004a952f33f4f7505d0ed7ac2ad01e4a0398ed1"):
            response = self.client.get(reverse("healthz"), headers={"host": "web:8000"})
        self.assertEqual(response.json()["short_sha"], "a004a95")

    def test_the_probe_answers_without_the_trailing_slash(self):
        response = self.client.get("/healthz", headers={"host": "web:8000"})
        self.assertEqual(response.status_code, 200)

    def test_the_probe_still_answers_on_an_allowed_host(self):
        response = self.client.get(reverse("healthz"), headers={"host": "books.example.com"})
        self.assertEqual(response.status_code, 200)

    def test_host_validation_is_untouched_for_every_other_url(self):
        for path in ["/", reverse("api-session"), reverse("api-users")]:
            response = self.client.get(path, headers={"host": "web:8000"})
            self.assertEqual(response.status_code, 400, path)
