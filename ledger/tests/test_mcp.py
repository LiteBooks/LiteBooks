import json
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ledger.models import Account, AccountingPeriod, ApiToken, AuditEvent, Contact, JournalEntry, UserProfile
from ledger.services import create_document, create_entry


def make_user(username, role):
    user = get_user_model().objects.create_user(username=username, password="very-good-password")
    user.profile.role = role
    user.profile.save()
    return user


class McpTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = make_user("books", UserProfile.Role.BOOKKEEPER)
        cls.bank = Account.objects.create(code="1000", name="Checking", type=Account.Type.ASSET, subtype=Account.Subtype.BANK)
        cls.card = Account.objects.create(code="2100", name="Visa", type=Account.Type.LIABILITY, subtype=Account.Subtype.CREDIT_CARD)
        cls.sales = Account.objects.create(code="4000", name="Sales", type=Account.Type.REVENUE, subtype=Account.Subtype.SALES)
        cls.software = Account.objects.create(code="6100", name="Software", type=Account.Type.EXPENSE, subtype=Account.Subtype.OPERATING_EXPENSE)
        cls.meals = Account.objects.create(code="6200", name="Meals", type=Account.Type.EXPENSE, subtype=Account.Subtype.OPERATING_EXPENSE)

    def setUp(self):
        _, self.secret = ApiToken.issue(user=self.user, name="Claude")

    def rpc(self, method, params=None, secret=None, message_id=1):
        body = {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}}
        return self.client.post(reverse("mcp"), json.dumps(body), content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {secret or self.secret}")

    def call(self, tool_name, secret=None, **arguments):
        response = self.rpc("tools/call", {"name": tool_name, "arguments": arguments}, secret=secret)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["result"]

    def tool_names(self, secret=None):
        return {item["name"] for item in self.rpc("tools/list", secret=secret).json()["result"]["tools"]}


class TransportTests(McpTestCase):
    def test_requests_without_a_valid_token_are_refused(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(self.client.post(reverse("mcp"), body, content_type="application/json").status_code, 401)
        self.assertEqual(self.rpc("ping", secret="lb_not-a-real-token").status_code, 401)

    def test_a_token_stops_working_when_its_user_is_deactivated(self):
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.rpc("ping").status_code, 401)

    def test_initialize_negotiates_a_version_and_explains_the_import_workflow(self):
        result = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}).json()["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertIn("tools", result["capabilities"])
        self.assertIn("dry_run", result["instructions"])
        unknown = self.rpc("initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
        self.assertEqual(unknown["protocolVersion"], "2025-06-18")

    def test_notifications_are_accepted_without_a_reply(self):
        body = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = self.client.post(reverse("mcp"), body, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.secret}")
        self.assertEqual(response.status_code, 202)

    def test_get_is_not_supported_because_the_server_is_stateless(self):
        self.assertEqual(self.client.get(reverse("mcp"), HTTP_AUTHORIZATION=f"Bearer {self.secret}").status_code, 405)

    def test_the_path_without_a_trailing_slash_answers_instead_of_redirecting(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        response = self.client.post("/mcp", body, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.secret}")
        self.assertEqual(response.status_code, 200)

    def test_tools_are_limited_to_what_the_tokens_user_may_do(self):
        _, viewer = ApiToken.issue(user=make_user("viewer", UserProfile.Role.VIEWER), name="read only")
        _, admin = ApiToken.issue(user=make_user("admin", UserProfile.Role.ADMIN), name="admin")
        self.assertNotIn("import_transactions", self.tool_names(viewer))
        self.assertIn("search_transactions", self.tool_names(viewer))
        self.assertIn("import_transactions", self.tool_names())
        self.assertNotIn("create_account", self.tool_names())
        self.assertIn("create_account", self.tool_names(admin))
        refused = self.rpc("tools/call", {"name": "create_account", "arguments": {"code": "9", "name": "X", "type": "expense"}}).json()
        self.assertIn("error", refused)
        self.assertFalse(Account.objects.filter(code="9").exists())


class AccountToolTests(McpTestCase):
    def test_list_accounts_includes_balances(self):
        create_entry(entry_date=date(2026, 3, 1), description="Sale", user=self.user, lines=[
            {"account": self.bank, "debit": 50, "credit": 0, "owner": None},
            {"account": self.sales, "debit": 0, "credit": 50, "owner": None},
        ])
        accounts = {row["code"]: row for row in self.call("list_accounts")["structuredContent"]["accounts"]}
        self.assertEqual(accounts["1000"]["balance"], "50.00")
        self.assertEqual(accounts["4000"]["balance"], "50.00")

    def test_admins_can_create_and_update_accounts_with_audit(self):
        _, admin = ApiToken.issue(user=make_user("admin", UserProfile.Role.ADMIN), name="admin")
        created = self.call("create_account", secret=admin, code="6300", name="Travel", type="expense", subtype="operating_expense")
        self.assertFalse(created["isError"], created)
        account_id = created["structuredContent"]["account"]["id"]
        updated = self.call("update_account", secret=admin, account_id=account_id, description="Flights and hotels")
        self.assertEqual(updated["structuredContent"]["account"]["description"], "Flights and hotels")
        self.assertEqual(updated["structuredContent"]["account"]["name"], "Travel")
        self.assertEqual(AuditEvent.objects.filter(object_type="ledger.Account", object_id=str(account_id)).count(), 2)

    def test_account_validation_errors_come_back_as_tool_errors(self):
        _, admin = ApiToken.issue(user=make_user("admin", UserProfile.Role.ADMIN), name="admin")
        result = self.call("create_account", secret=admin, code="6400", name="Wrong", type="expense", subtype="bank")
        self.assertTrue(result["isError"])
        self.assertIn("subtype", result["content"][0]["text"])


class ImportTests(McpTestCase):
    def rows(self):
        return [
            {"date": "2026-04-02", "description": "GITHUB INC", "amount": "-21.00", "category_account_id": self.software.pk},
            {"date": "2026-04-03", "description": "Client payment", "amount": "1500.00", "category_account_id": self.sales.pk},
        ]

    def import_rows(self, rows, dry_run, account=None):
        return self.call("import_transactions", statement_account_id=(account or self.bank).pk, rows=rows, dry_run=dry_run)["structuredContent"]

    def test_a_dry_run_validates_but_posts_nothing(self):
        result = self.import_rows(self.rows(), dry_run=True)
        self.assertEqual(result["counts"]["ready"], 2)
        self.assertFalse(JournalEntry.objects.exists())
        self.assertFalse(AuditEvent.objects.filter(object_type="ledger.JournalEntry").exists())

    def test_rows_post_with_the_sign_deciding_the_side(self):
        result = self.import_rows(self.rows(), dry_run=False)
        self.assertEqual(result["counts"]["posted"], 2)
        github = JournalEntry.objects.get(description="GITHUB INC")
        self.assertEqual(github.lines.get(account=self.bank).credit, Decimal("21.00"))
        self.assertEqual(github.lines.get(account=self.software).debit, Decimal("21.00"))
        deposit = JournalEntry.objects.get(description="Client payment")
        self.assertEqual(deposit.lines.get(account=self.bank).debit, Decimal("1500.00"))
        self.assertEqual(github.created_by, self.user)

    def test_a_charge_on_a_credit_card_increases_what_is_owed(self):
        self.import_rows([{"date": "2026-04-02", "description": "Lunch", "amount": "-30", "category_account_id": self.meals.pk}], dry_run=False, account=self.card)
        entry = JournalEntry.objects.get(description="Lunch")
        self.assertEqual(entry.lines.get(account=self.card).credit, Decimal("30.00"))

    def test_reimporting_an_overlapping_statement_skips_what_is_already_booked(self):
        self.import_rows(self.rows(), dry_run=False)
        overlap = self.rows()[1:] + [{"date": "2026-04-05", "description": "Figma", "amount": "-15", "category_account_id": self.software.pk}]
        result = self.import_rows(overlap, dry_run=False)
        self.assertEqual([row["status"] for row in result["rows"]], ["already_imported", "posted"])
        self.assertEqual(JournalEntry.objects.count(), 3)

    def test_identical_rows_on_the_same_day_are_both_booked(self):
        coffee = {"date": "2026-04-02", "description": "Blue Bottle", "amount": "-5.50", "category_account_id": self.meals.pk}
        result = self.import_rows([coffee, coffee], dry_run=False)
        self.assertEqual(result["counts"]["posted"], 2)
        self.assertNotIn("possible_duplicates", result["rows"][1])
        again = self.import_rows([coffee, coffee], dry_run=False)
        self.assertEqual(again["counts"]["already_imported"], 2)

    def test_a_bank_reference_identifies_the_row(self):
        row = {**self.rows()[0], "reference": "TX-991"}
        self.import_rows([row], dry_run=False)
        renamed = {**row, "description": "GitHub subscription"}
        self.assertEqual(self.import_rows([renamed], dry_run=True)["rows"][0]["status"], "already_imported")

    def test_any_invalid_row_stops_the_whole_import(self):
        rows = self.rows() + [{"date": "2026-04-31", "description": "Bad date", "amount": "-1", "category_account_id": self.software.pk}]
        result = self.import_rows(rows, dry_run=False)
        self.assertEqual(result["counts"]["error"], 1)
        self.assertIn("Nothing was posted", result["outcome"])
        self.assertIn("date", result["rows"][2]["error"])
        self.assertFalse(JournalEntry.objects.exists())

    def test_closed_months_reject_rows(self):
        AccountingPeriod.objects.create(year=2026, month=4, closed_at=timezone.now())
        result = self.import_rows(self.rows()[:1], dry_run=True)
        self.assertEqual(result["rows"][0]["status"], "error")
        self.assertIn("closed", result["rows"][0]["error"])

    def test_a_matching_manual_entry_is_flagged_as_a_possible_duplicate(self):
        manual = create_entry(entry_date=date(2026, 4, 2), description="GitHub", user=self.user, lines=[
            {"account": self.software, "debit": 21, "credit": 0, "owner": None},
            {"account": self.bank, "debit": 0, "credit": 21, "owner": None},
        ])
        result = self.import_rows(self.rows()[:1], dry_run=True)
        self.assertEqual(result["rows"][0]["possible_duplicates"][0]["id"], manual.pk)
        self.assertEqual(result["counts"]["possible_duplicate"], 1)

    def test_viewers_cannot_import(self):
        _, viewer = ApiToken.issue(user=make_user("viewer", UserProfile.Role.VIEWER), name="read only")
        response = self.rpc("tools/call", {"name": "import_transactions", "arguments": {"statement_account_id": self.bank.pk, "rows": self.rows(), "dry_run": False}}, secret=viewer)
        self.assertIn("error", response.json())
        self.assertFalse(JournalEntry.objects.exists())


class HistoryAndEditTests(McpTestCase):
    def test_history_reports_how_a_merchant_was_booked_before(self):
        for day in (1, 2):
            create_entry(entry_date=date(2026, 2, day), description=f"SQ *BLUE BOTTLE #{day}", user=self.user, lines=[
                {"account": self.meals, "debit": 5, "credit": 0, "owner": None},
                {"account": self.card, "debit": 0, "credit": 5, "owner": None},
            ])
        history = self.call("categorization_history", descriptions=["POS SQ *Blue Bottle Coffee 0042", "Unknown merchant"])["structuredContent"]["history"]
        self.assertEqual(history[0]["accounts"][0]["account_id"], self.meals.pk)
        self.assertEqual(history[0]["accounts"][0]["times_used"], 2)
        self.assertEqual(history[1]["accounts"], [])

    def test_a_transaction_can_be_recategorized_and_deleted(self):
        entry = create_entry(entry_date=date(2026, 2, 1), description="Adobe", user=self.user, lines=[
            {"account": self.meals, "debit": 60, "credit": 0, "owner": None},
            {"account": self.bank, "debit": 0, "credit": 60, "owner": None},
        ])
        result = self.call("update_transaction", transaction_id=entry.pk, lines=[
            {"account_id": self.software.pk, "debit": "60.00"},
            {"account_id": self.bank.pk, "credit": "60.00"},
        ])
        self.assertFalse(result["isError"], result)
        entry.refresh_from_db()
        self.assertEqual({line.account_id for line in entry.lines.all()}, {self.software.pk, self.bank.pk})
        self.assertEqual(entry.description, "Adobe")
        self.call("delete_transaction", transaction_id=entry.pk, reason="Duplicate")
        entry.refresh_from_db()
        self.assertTrue(entry.is_deleted)

    def test_unbalanced_edits_are_rejected(self):
        entry = create_entry(entry_date=date(2026, 2, 1), description="Adobe", user=self.user, lines=[
            {"account": self.software, "debit": 60, "credit": 0, "owner": None},
            {"account": self.bank, "debit": 0, "credit": 60, "owner": None},
        ])
        result = self.call("update_transaction", transaction_id=entry.pk, lines=[{"account_id": self.software.pk, "debit": "60"}, {"account_id": self.bank.pk, "credit": "50"}])
        self.assertTrue(result["isError"])
        self.assertIn("must equal", result["content"][0]["text"])


class JournalEntryToolTests(McpTestCase):
    def adjusting_lines(self, amount="120.00"):
        return [{"account_id": self.software.pk, "debit": amount, "description": "Accrue March license"}, {"account_id": self.card.pk, "credit": amount}]

    def test_bookkeepers_can_post_an_adjusting_entry_with_audit(self):
        result = self.call("create_journal_entry", date="2026-03-31", description="Accrue software", memo="Invoice arrives in April", lines=self.adjusting_lines())
        self.assertFalse(result["isError"], result)
        transaction = result["structuredContent"]["transaction"]
        entry = JournalEntry.objects.get(pk=transaction["id"])
        self.assertEqual(entry.status, JournalEntry.Status.POSTED)
        self.assertEqual(entry.source, JournalEntry.Source.GENERAL)
        self.assertEqual(entry.memo, "Invoice arrives in April")
        self.assertEqual([(line["debit"], line["credit"]) for line in transaction["lines"]], [("120.00", "0.00"), ("0.00", "120.00")])
        self.assertTrue(AuditEvent.objects.filter(action="created", object_id=str(entry.pk), actor=self.user).exists())

    def test_unbalanced_or_closed_month_entries_post_nothing(self):
        unbalanced = self.call("create_journal_entry", date="2026-03-31", description="Bad", lines=[{"account_id": self.software.pk, "debit": "10"}, {"account_id": self.card.pk, "credit": "9"}])
        self.assertTrue(unbalanced["isError"])
        AccountingPeriod.objects.create(year=2026, month=1, closed_at=timezone.now())
        closed = self.call("create_journal_entry", date="2026-01-15", description="Late", lines=self.adjusting_lines())
        self.assertTrue(closed["isError"])
        self.assertFalse(JournalEntry.objects.exists())

    def test_owner_ids_must_name_an_owner(self):
        result = self.call("create_journal_entry", date="2026-03-31", description="Draw", lines=[
            {"account_id": self.software.pk, "debit": "5", "owner_id": 999999}, {"account_id": self.bank.pk, "credit": "5"},
        ])
        self.assertTrue(result["isError"])
        self.assertIn("not a contact", result["content"][0]["text"])

    def test_viewers_cannot_post_entries(self):
        _, viewer = ApiToken.issue(user=make_user("viewer", UserProfile.Role.VIEWER), name="read only")
        self.assertNotIn("create_journal_entry", self.tool_names(viewer))
        self.assertNotIn("restore_transaction", self.tool_names(viewer))
        self.assertIn("get_transaction", self.tool_names(viewer))
        self.assertIn("trial_balance", self.tool_names(viewer))

    def test_deleted_transactions_can_be_restored(self):
        entry_id = self.call("create_journal_entry", date="2026-03-31", description="Accrue", lines=self.adjusting_lines())["structuredContent"]["transaction"]["id"]
        self.call("delete_transaction", transaction_id=entry_id, reason="Wrong month")
        restored = self.call("restore_transaction", transaction_id=entry_id)
        self.assertFalse(restored["isError"], restored)
        self.assertEqual(JournalEntry.objects.get(pk=entry_id).status, JournalEntry.Status.POSTED)
        again = self.call("restore_transaction", transaction_id=entry_id)
        self.assertTrue(again["isError"])

    def test_get_transaction_shows_history_and_source(self):
        entry_id = self.call("create_journal_entry", date="2026-03-31", description="Accrue", lines=self.adjusting_lines())["structuredContent"]["transaction"]["id"]
        self.call("delete_transaction", transaction_id=entry_id, reason="Wrong month")
        detail = self.call("get_transaction", transaction_id=entry_id)["structuredContent"]["transaction"]
        self.assertEqual(detail["created_by"], "books")
        self.assertEqual(detail["deleted_by"], "books")
        self.assertEqual(detail["deleted_reason"], "Wrong month")
        self.assertEqual(detail["attachments"], [])
        self.assertIsNone(detail["source_record"])
        self.assertTrue(self.call("get_transaction", transaction_id=999999)["isError"])


class ReportToolTests(McpTestCase):
    def setUp(self):
        super().setUp()
        self.receivable = Account.objects.create(code="1200", name="Receivables", type=Account.Type.ASSET, subtype=Account.Subtype.RECEIVABLE)
        create_entry(entry_date=date(2025, 12, 15), description="Prior year sale", user=self.user, lines=[
            {"account": self.bank, "debit": 100, "credit": 0, "owner": None},
            {"account": self.sales, "debit": 0, "credit": 100, "owner": None},
        ])
        create_entry(entry_date=date(2026, 2, 10), description="Figma", user=self.user, lines=[
            {"account": self.software, "debit": 30, "credit": 0, "owner": None},
            {"account": self.bank, "debit": 0, "credit": 30, "owner": None},
        ])
        customer = Contact.objects.create(name="Acme", kind=Contact.Kind.CUSTOMER)
        create_document(kind="invoice", number="INV-1", contact=customer, issue_date=date(2026, 1, 1), due_date=date(2026, 1, 31), description="Consulting", amount="500.00", control_account=self.receivable, category_account=self.sales, user=self.user)

    def report(self, name, **arguments):
        result = self.call(name, **arguments)
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def test_trial_balance_balances(self):
        report = self.report("trial_balance", as_of="2026-03-31")
        self.assertEqual(report["total_debits"], report["total_credits"])
        self.assertEqual(report["total_debits"], "600.00")
        codes = {row["account"]["code"] for row in report["rows"]}
        self.assertEqual(codes, {"1000", "1200", "4000", "6100"})

    def test_balance_sheet_includes_current_earnings(self):
        report = self.report("balance_sheet", as_of="2026-03-31")
        self.assertEqual(report["asset"]["total"], "570.00")
        self.assertEqual(report["equity"]["current_earnings"], "570.00")
        self.assertEqual(report["equity"]["total"], report["asset"]["total"])

    def test_income_statement_defaults_to_year_to_date(self):
        report = self.report("income_statement", end="2026-03-31")
        self.assertEqual(report["start"], "2026-01-01")
        self.assertEqual(report["revenue_total"], "500.00")
        self.assertEqual(report["expense_total"], "30.00")
        self.assertEqual(report["net_income"], "470.00")
        self.assertTrue(self.call("income_statement", start="2026-04-01", end="2026-03-01")["isError"])

    def test_aging_buckets_unpaid_invoices(self):
        report = self.report("aging_report", kind="receivables", as_of="2026-03-15")
        self.assertEqual(report["buckets"]["31-60"], "500.00")
        self.assertEqual(report["total"], "500.00")
        self.assertEqual(report["rows"][0]["document"]["number"], "INV-1")
        self.assertEqual(report["rows"][0]["days_past_due"], 43)
        self.assertEqual(self.report("aging_report", kind="payables")["rows"], [])

    def test_owner_balances_follow_owner_tagged_lines(self):
        owner = Contact.objects.create(name="Pat", kind=Contact.Kind.OWNER)
        draws = Account.objects.create(code="3200", name="Draws", type=Account.Type.EQUITY, subtype=Account.Subtype.OWNER_DRAW)
        self.call("create_journal_entry", date="2026-03-01", description="Owner draw", lines=[
            {"account_id": draws.pk, "debit": "40", "owner_id": owner.pk}, {"account_id": self.bank.pk, "credit": "40"},
        ])
        owners = self.report("owner_balances")["owners"]
        self.assertEqual(owners, [{"owner": {"id": owner.pk, "name": "Pat"}, "contributions": "0.00", "draws": "40.00", "owed": "0.00"}])

    def test_general_ledger_runs_an_account_balance_from_its_opening(self):
        report = self.report("general_ledger", account_id=self.bank.pk, start="2026-01-01", end="2026-12-31")
        self.assertEqual(report["opening_balance"], "100.00")
        self.assertEqual([line["balance"] for line in report["lines"]], ["70.00"])
        self.assertEqual(report["closing_balance"], "70.00")
        self.assertFalse(report["truncated"])

    def test_general_ledger_says_when_it_is_cut_short(self):
        report = self.report("general_ledger", start="2025-01-01", end="2026-12-31", limit=3)
        self.assertEqual(len(report["lines"]), 3)
        self.assertTrue(report["truncated"])
        self.assertIn("note", report)

    def test_deleted_entries_drop_out_of_reports(self):
        figma = JournalEntry.objects.get(description="Figma")
        self.call("delete_transaction", transaction_id=figma.pk, reason="Duplicate")
        self.assertEqual(self.report("income_statement", end="2026-03-31")["expense_total"], "0.00")


class TokenApiTests(TestCase):
    def setUp(self):
        self.user = make_user("books", UserProfile.Role.BOOKKEEPER)
        self.client.force_login(self.user)

    def test_the_secret_is_shown_once_and_only_its_hash_is_stored(self):
        response = self.client.post(reverse("api-tokens"), {"name": "Claude Desktop"}, content_type="application/json")
        self.assertEqual(response.status_code, 201)
        secret = response.json()["secret"]
        token = ApiToken.objects.get()
        self.assertNotEqual(token.token_hash, secret)
        self.assertEqual(ApiToken.authenticate(secret), self.user)
        listed = self.client.get(reverse("api-tokens")).json()["tokens"]
        self.assertNotIn("secret", listed[0])
        self.assertEqual(listed[0]["hint"], secret[-4:])

    def test_users_can_only_revoke_their_own_tokens(self):
        other, _ = ApiToken.issue(user=make_user("other", UserProfile.Role.BOOKKEEPER), name="theirs")
        self.assertEqual(self.client.delete(reverse("api-token", args=[other.pk])).status_code, 404)
        mine, secret = ApiToken.issue(user=self.user, name="mine")
        self.assertEqual(self.client.delete(reverse("api-token", args=[mine.pk])).status_code, 200)
        self.assertIsNone(ApiToken.authenticate(secret))
