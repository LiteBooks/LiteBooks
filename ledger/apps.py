from datetime import datetime, timezone

from django.apps import AppConfig


# When this server process started serving (each gunicorn worker is its own process).
PROCESS_STARTED_AT = datetime.now(timezone.utc)


class LedgerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ledger"

    def ready(self):
        from . import signals  # noqa: F401

