"""Copy legacy JournalEntry.import_ref values into transaction_import_refs.

    python manage.py backfill_import_refs           # report what would be copied
    python manage.py backfill_import_refs --apply   # copy them

Not required: import deduplication and transfer matching read both places. It
only makes the new table the complete record. It adds rows to the new table
and never changes or removes a journal entry or its import_ref.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from ledger.models import Account, JournalEntry, TransactionImportRef
from ledger.transfers import legacy_ref_account_id


class Command(BaseCommand):
    help = "Copy legacy import_ref values into transaction_import_refs (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the rows. Without it, only report.")

    def handle(self, *args, apply=False, **options):
        account_ids = set(Account.objects.values_list("pk", flat=True))
        existing_refs = set(TransactionImportRef.objects.values_list("account_id", "import_ref"))
        existing_pairs = set(TransactionImportRef.objects.values_list("entry_id", "account_id"))
        to_create, skipped = [], []
        for entry_id, ref in JournalEntry.objects.exclude(import_ref="").values_list("pk", "import_ref").order_by("pk"):
            account_id = legacy_ref_account_id(ref)
            if account_id not in account_ids:
                skipped.append((entry_id, ref, "no statement account in ref"))
            elif (account_id, ref) in existing_refs:
                continue
            elif (entry_id, account_id) in existing_pairs:
                skipped.append((entry_id, ref, "entry already has a different ref for this account"))
            else:
                to_create.append(TransactionImportRef(entry_id=entry_id, account_id=account_id, import_ref=ref))
        for entry_id, ref, reason in skipped:
            self.stdout.write(f"skip entry {entry_id} ({ref}): {reason}")
        if apply:
            with transaction.atomic():
                TransactionImportRef.objects.bulk_create(to_create)
            self.stdout.write(self.style.SUCCESS(f"Copied {len(to_create)} import ref(s); skipped {len(skipped)}."))
        else:
            self.stdout.write(f"Would copy {len(to_create)} import ref(s); would skip {len(skipped)}. Run with --apply to write them.")
