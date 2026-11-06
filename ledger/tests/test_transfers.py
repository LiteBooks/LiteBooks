from datetime import date
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from ledger.models import Account, AccountingPeriod, ApiToken, AuditEvent, ImportSettings, JournalEntry, TransactionImportRef, UserProfile
from ledger.services import create_entry, delete_entry

from .test_mcp import McpTestCase, make_user


class TransferTestCase(McpTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.checking = Account.objects.create(code="1010", name="Sample Checking 9999", type=Account.Type.ASSET, subtype=Account.Subtype.BANK)
        cls.sample_card = Account.objects.create(code="2110", name="Sample Card", type=Account.Type.LIABILITY, subtype=Account.Subtype.CREDIT_CARD)
        cls.cash = Account.objects.create(code="1020", name="Sample Petty Cash", type=Account.Type.ASSET, subtype=Account.Subtype.CASH)

    def import_rows(self, account, rows, dry_run=True):
        result = self.call("import_transactions", statement_account_id=account.pk, rows=rows, dry_run=dry_run)
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]

    def card_payment_row(self, day="2026-03-05", amount="150.00", **extra):
        return {"date": day, "description": "PAYMENT THANK YOU", "amount": amount, "category_account_id": self.checking.pk, **extra}

    def checking_row(self, day="2026-03-03", amount="-150.00", category=None, **extra):
        return {"date": day, "description": "SAMPLE CARD AUTOPAY", "amount": amount, "category_account_id": (category or self.sample_card).pk, **extra}

    def import_card_payment(self, **kwargs):
        result = self.import_rows(self.sample_card, [self.card_payment_row(**kwargs)], dry_run=False)
        self.assertEqual(result["rows"][0]["status"], "posted", result)
        return JournalEntry.objects.get(pk=result["rows"][0]["entry"]["id"])

    def manual_transfer(self, day, amount="150.00", source=None, target=None):
        """Money moved from source (default checking) to target (default the card), entered by hand."""
        source, target = source or self.checking, target or self.sample_card
        return create_entry(entry_date=date.fromisoformat(day), description="Card payment", user=self.user, lines=[
            {"account": target, "debit": Decimal(amount), "credit": 0, "owner": None},
            {"account": source, "debit": 0, "credit": Decimal(amount), "owner": None},
        ])

    def row_status(self, account, row):
        return self.import_rows(account, [row])["rows"][0]


class TransferMatchTests(TransferTestCase):
    def test_card_first_then_checking_links_instead_of_posting(self):
        payment = self.import_card_payment()
        preview = self.row_status(self.checking, self.checking_row())
        self.assertEqual(preview["status"], "matched_transfer")
        self.assertEqual(preview["matched_transaction_id"], payment.pk)
        self.assertEqual(self.import_rows(self.checking, [self.checking_row()])["counts"]["matched_transfer"], 1)

        result = self.import_rows(self.checking, [self.checking_row()], dry_run=False)
        self.assertEqual(result["rows"][0]["status"], "matched_transfer")
        self.assertEqual(result["counts"]["posted"], 0)
        self.assertEqual(JournalEntry.objects.count(), 1)
        link = TransactionImportRef.objects.get(entry=payment, account=self.checking)
        self.assertEqual(link.import_ref, result["rows"][0]["import_ref"])
        payment.refresh_from_db()
        self.assertEqual(payment.version, 1)
        self.assertEqual(payment.lines.count(), 2)

    def test_linking_writes_an_audit_entry(self):
        payment = self.import_card_payment()
        self.import_rows(self.checking, [self.checking_row()], dry_run=False)
        event = AuditEvent.objects.get(action="linked_import_ref")
        self.assertEqual((event.object_type, event.object_id), ("ledger.JournalEntry", str(payment.pk)))
        self.assertEqual(event.actor, self.user)
        self.assertEqual(event.after["account_id"], self.checking.pk)
        self.assertEqual(event.after["statement_row"]["amount"], "-150.00")

    def test_after_linking_both_statements_skip_the_row(self):
        payment = self.import_card_payment()
        self.import_rows(self.checking, [self.checking_row()], dry_run=False)
        for account, row in ((self.checking, self.checking_row()), (self.sample_card, self.card_payment_row())):
            again = self.import_rows(account, [row], dry_run=False)["rows"][0]
            self.assertEqual(again["status"], "already_imported")
            self.assertEqual(again["entry"]["id"], payment.pk)
        self.assertEqual(JournalEntry.objects.count(), 1)

    def test_checking_first_then_card(self):
        posted = self.import_rows(self.checking, [self.checking_row()], dry_run=False)["rows"][0]
        self.assertEqual(posted["status"], "posted")
        card = self.import_rows(self.sample_card, [self.card_payment_row()], dry_run=False)["rows"][0]
        self.assertEqual(card["status"], "matched_transfer")
        self.assertEqual(card["matched_transaction_id"], posted["entry"]["id"])
        self.assertEqual(JournalEntry.objects.count(), 1)
        self.assertEqual(self.row_status(self.checking, self.checking_row())["status"], "already_imported")
        self.assertEqual(self.row_status(self.sample_card, self.card_payment_row())["status"], "already_imported")

    def test_outside_the_window_is_not_a_match(self):
        self.import_card_payment()
        row = self.row_status(self.checking, self.checking_row(day="2026-03-12"))
        self.assertEqual(row["status"], "ready")
        self.assertNotIn("warnings", row)

    def test_window_boundaries_are_inclusive(self):
        self.import_card_payment()  # 2026-03-05, window 5 days
        for day, expected in (("2026-03-10", "matched_transfer"), ("2026-02-28", "matched_transfer"), ("2026-03-11", "ready"), ("2026-02-27", "ready")):
            with self.subTest(day=day):
                self.assertEqual(self.row_status(self.checking, self.checking_row(day=day))["status"], expected)

    def test_the_window_comes_from_settings(self):
        self.import_card_payment()
        found = ImportSettings.load()
        found.transfer_match_window_days = 7
        found.save()
        self.assertEqual(self.row_status(self.checking, self.checking_row(day="2026-03-12"))["status"], "matched_transfer")
        self.assertEqual(self.row_status(self.checking, self.checking_row(day="2026-03-13"))["status"], "ready")
        found.transfer_match_window_days = 0
        found.save()
        self.assertEqual(self.row_status(self.checking, self.checking_row(day="2026-03-05"))["status"], "matched_transfer")
        self.assertEqual(self.row_status(self.checking, self.checking_row(day="2026-03-04"))["status"], "ready")

    def test_amounts_must_match_to_the_cent(self):
        self.import_card_payment()
        self.assertEqual(self.row_status(self.checking, self.checking_row(amount="-149.99"))["status"], "ready")
        self.assertEqual(self.row_status(self.checking, self.checking_row(amount="-150.01"))["status"], "ready")

    def test_the_sign_must_match_for_asset_and_liability_accounts(self):
        self.import_card_payment()
        # Money into checking from the card (a cash advance) is not the payment.
        self.assertEqual(self.row_status(self.checking, self.checking_row(amount="150.00"))["status"], "ready")
        # A $150 charge to checking on the card statement is not the payment either.
        self.assertEqual(self.row_status(self.sample_card, self.card_payment_row(amount="-150.00"))["status"], "ready")

    def test_cash_accounts_match(self):
        atm = {"date": "2026-03-02", "description": "ATM WITHDRAWAL", "amount": "-200.00", "category_account_id": self.cash.pk}
        posted = self.import_rows(self.checking, [atm], dry_run=False)["rows"][0]
        cash_row = {"date": "2026-03-02", "description": "Cash from bank", "amount": "200.00", "category_account_id": self.checking.pk}
        matched = self.import_rows(self.cash, [cash_row], dry_run=False)["rows"][0]
        self.assertEqual(matched["status"], "matched_transfer")
        self.assertEqual(matched["matched_transaction_id"], posted["entry"]["id"])

    def test_bank_references_and_fingerprints_both_link(self):
        payment = self.import_card_payment(reference="CARD-1")
        with_reference = self.import_rows(self.checking, [self.checking_row(reference="ACH-77")], dry_run=False)["rows"][0]
        self.assertEqual(with_reference["status"], "matched_transfer")
        self.assertTrue(with_reference["import_ref"].endswith(":r:ACH-77"))
        renamed = self.checking_row(reference="ACH-77", description="Autopay, renamed")
        self.assertEqual(self.row_status(self.checking, renamed)["status"], "already_imported")
        self.assertEqual(self.row_status(self.sample_card, self.card_payment_row(reference="CARD-1"))["entry"]["id"], payment.pk)

    def test_a_three_account_transaction_is_never_matched(self):
        create_entry(entry_date=date(2026, 3, 5), description="Payment and fee", user=self.user, lines=[
            {"account": self.sample_card, "debit": Decimal("150.00"), "credit": 0, "owner": None},
            {"account": self.software, "debit": Decimal("5.00"), "credit": 0, "owner": None},
            {"account": self.checking, "debit": 0, "credit": Decimal("155.00"), "owner": None},
        ])
        self.assertEqual(self.row_status(self.checking, self.checking_row(amount="-155.00"))["status"], "ready")

    def test_one_transfer_is_claimed_by_only_one_row_per_batch(self):
        self.import_card_payment()
        result = self.import_rows(self.checking, [self.checking_row(), self.checking_row()], dry_run=False)
        self.assertEqual([row["status"] for row in result["rows"]], ["matched_transfer", "posted"])
        self.assertEqual(JournalEntry.objects.count(), 2)


class TransferExclusionTests(TransferTestCase):
    def test_deleted_transactions_are_never_matched(self):
        payment = self.import_card_payment()
        delete_entry(entry=payment, user=self.user, reason="entered twice")
        self.assertEqual(self.row_status(self.checking, self.checking_row())["status"], "ready")

    def test_closed_periods_are_never_matched(self):
        self.manual_transfer("2026-02-27")
        AccountingPeriod.objects.create(year=2026, month=2, closed_at=timezone.now())
        self.assertEqual(self.row_status(self.checking, self.checking_row(day="2026-03-02"))["status"], "ready")

    def test_already_linked_transfers_are_not_matched_again(self):
        self.import_card_payment()
        self.import_rows(self.checking, [self.checking_row()], dry_run=False)
        # A second, different $150 checking row is a new transfer.
        self.assertEqual(self.row_status(self.checking, self.checking_row(day="2026-03-04", reference="OTHER"))["status"], "ready")

    def test_a_legacy_import_ref_counts_as_linked(self):
        legacy = create_entry(entry_date=date(2026, 3, 3), description="Old import", user=self.user, import_ref=f"{self.checking.pk}:r:OLD-1", lines=[
            {"account": self.sample_card, "debit": Decimal("150.00"), "credit": 0, "owner": None},
            {"account": self.checking, "debit": 0, "credit": Decimal("150.00"), "owner": None},
        ])
        self.assertFalse(TransactionImportRef.objects.exists())
        self.assertEqual(self.row_status(self.checking, self.checking_row(reference="OLD-1"))["entry"]["id"], legacy.pk)
        self.assertEqual(self.row_status(self.checking, self.checking_row(reference="NEW-2"))["status"], "ready")
        self.assertEqual(self.row_status(self.sample_card, self.card_payment_row(day="2026-03-03"))["matched_transaction_id"], legacy.pk)


class AmbiguousTransferTests(TransferTestCase):
    def setUp(self):
        super().setUp()
        self.first = self.manual_transfer("2026-03-02")
        self.second = self.manual_transfer("2026-03-04")

    def test_several_candidates_post_nothing_for_that_row(self):
        other = {"date": "2026-03-03", "description": "GITHUB", "amount": "-21.00", "category_account_id": self.software.pk}
        result = self.import_rows(self.checking, [self.checking_row(), other], dry_run=False)
        row = result["rows"][0]
        self.assertEqual(row["status"], "ambiguous_transfer")
        self.assertEqual(row["candidate_transaction_ids"], [self.first.pk, self.second.pk])
        self.assertEqual(result["counts"]["ambiguous_transfer"], 1)
        self.assertEqual(result["counts"]["posted"], 1)
        self.assertIn("match_transaction_id", result["outcome"])
        self.assertEqual(JournalEntry.objects.count(), 3)
        self.assertFalse(TransactionImportRef.objects.filter(account=self.checking, entry__in=[self.first, self.second]).exists())

    def test_match_transaction_id_links_the_chosen_one(self):
        row = self.checking_row(match_transaction_id=self.second.pk)
        self.assertEqual(self.row_status(self.checking, row)["status"], "matched_transfer")
        result = self.import_rows(self.checking, [row], dry_run=False)["rows"][0]
        self.assertEqual(result["matched_transaction_id"], self.second.pk)
        self.assertTrue(TransactionImportRef.objects.filter(entry=self.second, account=self.checking).exists())
        self.assertEqual(JournalEntry.objects.count(), 2)
        # The other candidate is still open for the next row.
        self.assertEqual(self.row_status(self.checking, self.checking_row(day="2026-03-02", reference="X"))["matched_transaction_id"], self.first.pk)

    def test_force_new_posts_a_new_transaction(self):
        result = self.import_rows(self.checking, [self.checking_row(force_new=True)], dry_run=False)["rows"][0]
        self.assertEqual(result["status"], "posted")
        self.assertEqual(JournalEntry.objects.count(), 3)

    def test_invalid_overrides_are_errors_and_stop_the_import(self):
        third_party = self.manual_transfer("2026-03-03", source=self.bank)
        cases = {
            "not a transaction": self.checking_row(match_transaction_id=999999),
            "not posted": self.checking_row(match_transaction_id=delete_entry(entry=self.manual_transfer("2026-03-03"), user=self.user, reason="x").pk),
            "wrong accounts": self.checking_row(match_transaction_id=third_party.pk),
            "wrong amount": self.checking_row(amount="-149.99", match_transaction_id=self.first.pk),
            "outside the window": self.checking_row(day="2026-03-20", match_transaction_id=self.first.pk),
            "both flags": self.checking_row(match_transaction_id=self.first.pk, force_new=True),
            "not an integer": self.checking_row(match_transaction_id="12"),
            "force_new not a boolean": self.checking_row(force_new="yes"),
        }
        for label, row in cases.items():
            with self.subTest(label):
                result = self.import_rows(self.checking, [row], dry_run=False)
                self.assertEqual(result["rows"][0]["status"], "error", result["rows"][0])
                self.assertIn("Nothing was posted", result["outcome"])
        self.assertFalse(TransactionImportRef.objects.filter(account=self.checking).exists())

    def test_an_already_linked_or_closed_transfer_cannot_be_chosen(self):
        self.import_rows(self.checking, [self.checking_row(match_transaction_id=self.first.pk)], dry_run=False)
        linked = self.row_status(self.checking, self.checking_row(reference="B", match_transaction_id=self.first.pk))
        self.assertEqual(linked["status"], "error")
        self.assertIn("already", linked["error"])
        twice = self.import_rows(self.checking, [self.checking_row(reference="C", match_transaction_id=self.second.pk), self.checking_row(reference="D", match_transaction_id=self.second.pk)])
        self.assertEqual([row["status"] for row in twice["rows"]], ["matched_transfer", "error"])
        closed = self.manual_transfer("2026-02-27")
        AccountingPeriod.objects.create(year=2026, month=2, closed_at=timezone.now())
        refused = self.row_status(self.checking, self.checking_row(day="2026-03-01", reference="E", match_transaction_id=closed.pk))
        self.assertIn("closed", refused["error"])


class PossibleTransferWarningTests(TransferTestCase):
    def test_an_expense_row_that_looks_like_a_transfer_is_ready_with_a_warning(self):
        payment = self.import_card_payment()
        row = self.row_status(self.checking, self.checking_row(category=self.software))
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["warnings"], [f"possible_transfer: matches transaction #{payment.pk}"])

    def test_no_warning_without_a_lookalike(self):
        self.import_card_payment()
        self.assertNotIn("warnings", self.row_status(self.checking, self.checking_row(category=self.software, amount="-149.99")))
        self.assertNotIn("warnings", self.row_status(self.checking, self.checking_row(category=self.software, day="2026-03-12")))


class DryRunAndCompatibilityTests(TransferTestCase):
    def test_dry_run_writes_nothing(self):
        self.import_card_payment()
        self.manual_transfer("2026-03-20")
        self.manual_transfer("2026-03-21")
        before = (JournalEntry.objects.count(), TransactionImportRef.objects.count(), AuditEvent.objects.count())
        rows = [self.checking_row(), self.checking_row(day="2026-03-20", reference="A"), self.checking_row(day="2026-03-21", reference="B", match_transaction_id=JournalEntry.objects.order_by("pk").last().pk)]
        result = self.import_rows(self.checking, rows, dry_run=True)
        self.assertEqual([row["status"] for row in result["rows"]], ["matched_transfer", "ambiguous_transfer", "matched_transfer"])
        self.assertEqual((JournalEntry.objects.count(), TransactionImportRef.objects.count(), AuditEvent.objects.count()), before)

    def test_imports_without_transfers_behave_as_before(self):
        rows = [
            {"date": "2026-04-02", "description": "GITHUB INC", "amount": "-21.00", "category_account_id": self.software.pk},
            {"date": "2026-04-03", "description": "Client payment", "amount": "1500.00", "category_account_id": self.sales.pk},
        ]
        result = self.import_rows(self.checking, rows, dry_run=False)
        self.assertEqual(result["counts"], {"posted": 2, "possible_duplicate": 0, "matched_transfer": 0, "ambiguous_transfer": 0})
        for row in result["rows"]:
            self.assertEqual(set(row), {"row", "import_ref", "status", "category", "entry"})
        entry = JournalEntry.objects.get(description="GITHUB INC")
        self.assertEqual(entry.import_ref, result["rows"][0]["import_ref"])
        self.assertEqual(TransactionImportRef.objects.get(entry=entry).import_ref, entry.import_ref)
        self.assertEqual(self.import_rows(self.checking, rows)["counts"]["already_imported"], 2)

    def test_an_unknown_category_is_still_an_error(self):
        result = self.import_rows(self.checking, [self.checking_row() | {"category_account_id": 999999}])
        self.assertEqual(result["rows"][0]["status"], "error")
        self.assertIn("category_account_id", result["rows"][0]["error"])

    def test_get_transaction_shows_every_import_ref(self):
        payment = self.import_card_payment()
        self.import_rows(self.checking, [self.checking_row()], dry_run=False)
        refs = self.call("get_transaction", transaction_id=payment.pk)["structuredContent"]["transaction"]["import_refs"]
        self.assertEqual({ref["account_id"] for ref in refs}, {self.sample_card.pk, self.checking.pk})


class UnlinkedTransferTests(TransferTestCase):
    def unlinked(self, **arguments):
        result = self.call("list_unlinked_transfers", start="2026-03-01", end="2026-03-31", **arguments)
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]["transfers"]

    def test_lists_the_transfer_until_the_other_statement_is_imported(self):
        payment = self.import_card_payment()
        listed = self.unlinked()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["transaction_id"], payment.pk)
        self.assertEqual(listed[0]["amount"], "150.00")
        self.assertEqual(listed[0]["unlinked_account_ids"], [self.checking.pk])
        checking_side = next(side for side in listed[0]["sides"] if side["account"]["id"] == self.checking.pk)
        self.assertEqual((checking_side["linked"], checking_side["statement_amount"]), (False, "-150.00"))
        self.import_rows(self.checking, [self.checking_row()], dry_run=False)
        self.assertEqual(self.unlinked(), [])

    def test_filters_and_scope(self):
        manual = self.manual_transfer("2026-03-10")
        self.manual_transfer("2026-04-10")
        create_entry(entry_date=date(2026, 3, 11), description="Not a transfer", user=self.user, lines=[
            {"account": self.software, "debit": 5, "credit": 0, "owner": None},
            {"account": self.checking, "debit": 0, "credit": 5, "owner": None},
        ])
        listed = self.unlinked()
        self.assertEqual([item["transaction_id"] for item in listed], [manual.pk])
        self.assertEqual(sorted(listed[0]["unlinked_account_ids"]), sorted([self.checking.pk, self.sample_card.pk]))
        self.assertEqual(self.unlinked(account_id=self.cash.pk), [])
        self.assertEqual(len(self.unlinked(account_id=self.sample_card.pk)), 1)

    def test_is_read_only_and_open_to_viewers(self):
        self.manual_transfer("2026-03-10")
        _, viewer = ApiToken.issue(user=make_user("viewer", UserProfile.Role.VIEWER), name="read only")
        definition = next(item for item in self.rpc("tools/list", secret=viewer).json()["result"]["tools"] if item["name"] == "list_unlinked_transfers")
        self.assertTrue(definition["annotations"]["readOnlyHint"])
        before = (JournalEntry.objects.count(), TransactionImportRef.objects.count(), AuditEvent.objects.count())
        self.assertEqual(len(self.call("list_unlinked_transfers", secret=viewer, start="2026-03-01", end="2026-03-31")["structuredContent"]["transfers"]), 1)
        self.assertEqual((JournalEntry.objects.count(), TransactionImportRef.objects.count(), AuditEvent.objects.count()), before)

    def test_bad_ranges_are_tool_errors(self):
        self.assertTrue(self.call("list_unlinked_transfers", start="2026-03-31", end="2026-03-01")["isError"])
        self.assertTrue(self.call("list_unlinked_transfers", start="March", end="2026-03-01")["isError"])


class ImportSettingsApiTests(TransferTestCase):
    def test_admins_set_the_window_with_audit_and_others_cannot(self):
        url = reverse("api-import-settings")
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(url).json()["settings"]["transfer_match_window_days"], 5)
        self.assertEqual(self.client.post(url, {"transfer_match_window_days": 9}, content_type="application/json").status_code, 403)
        self.client.force_login(make_user("admin", UserProfile.Role.ADMIN))
        self.assertEqual(self.client.post(url, {"transfer_match_window_days": 9}, content_type="application/json").json()["settings"]["transfer_match_window_days"], 9)
        self.assertEqual(ImportSettings.load().transfer_match_window_days, 9)
        self.assertTrue(AuditEvent.objects.filter(object_type="ledger.ImportSettings").exists())
        for bad in (-1, 32, "soon"):
            self.assertEqual(self.client.post(url, {"transfer_match_window_days": bad}, content_type="application/json").status_code, 400)


class BackfillCommandTests(TransferTestCase):
    def test_reports_by_default_and_copies_only_with_apply(self):
        legacy = create_entry(entry_date=date(2026, 3, 3), description="Old import", user=self.user, import_ref=f"{self.checking.pk}:h:abc", lines=[
            {"account": self.software, "debit": 5, "credit": 0, "owner": None},
            {"account": self.checking, "debit": 0, "credit": 5, "owner": None},
        ])
        output = StringIO()
        call_command("backfill_import_refs", stdout=output)
        self.assertIn("Would copy 1", output.getvalue())
        self.assertFalse(TransactionImportRef.objects.exists())
        call_command("backfill_import_refs", "--apply", stdout=StringIO())
        self.assertEqual(TransactionImportRef.objects.get().entry, legacy)
        legacy.refresh_from_db()
        self.assertEqual(legacy.import_ref, f"{self.checking.pk}:h:abc")
        call_command("backfill_import_refs", "--apply", stdout=StringIO())
        self.assertEqual(TransactionImportRef.objects.count(), 1)
