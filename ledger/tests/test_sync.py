"""Server identity, data freshness, statement reconciliation, and the owner-draw normal balance.

All names and amounts here are fictional.
"""

import json
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from ledger import mcp
from ledger.management.commands.check_mcp_tools import declared_tool_names
from ledger.models import Account, AccountSyncState, ApiToken, Contact, StatementBalance, UserProfile
from ledger.reporting import balance_sheet, owner_balances, trial_balance
from ledger.services import create_entry
from ledger.sync import mark_synced, record_statement_balance

from .test_mcp import McpTestCase, make_user


EXPECTED_TOOLS = {
    "server_info",
    "account_sync_status",
    "list_owner_interests",
    "ownership_as_of",
    "partner_allocation_report",
    "capital_account_rollforward",
}


def post(day, debit_account, credit_account, amount, user, owner=None):
    return create_entry(entry_date=day, description="Sample entry", user=user, lines=[
        {"account": debit_account, "debit": amount, "credit": 0, "owner": owner if debit_account.type == Account.Type.EQUITY else None},
        {"account": credit_account, "debit": 0, "credit": amount, "owner": owner if credit_account.type == Account.Type.EQUITY else None},
    ])


class NormalBalanceTests(TestCase):
    # Every subtype and the side its balance normally sits on.
    EXPECTED = {
        "bank": "debit", "cash": "debit", "accounts_receivable": "debit", "fixed_asset": "debit", "other_asset": "debit",
        "credit_card": "credit", "accounts_payable": "credit", "owner_loan_payable": "credit", "other_liability": "credit",
        "owner_contribution": "credit", "owner_draw": "debit", "retained_earnings": "credit",
        "sales": "credit", "other_revenue": "credit",
        "cost_of_goods": "debit", "operating_expense": "debit", "other_expense": "debit",
    }
    TYPE_OF = {
        "bank": "asset", "cash": "asset", "accounts_receivable": "asset", "fixed_asset": "asset", "other_asset": "asset",
        "credit_card": "liability", "accounts_payable": "liability", "owner_loan_payable": "liability", "other_liability": "liability",
        "owner_contribution": "equity", "owner_draw": "equity", "retained_earnings": "equity",
        "sales": "revenue", "other_revenue": "revenue",
        "cost_of_goods": "expense", "operating_expense": "expense", "other_expense": "expense",
    }

    def test_every_subtype_is_covered(self):
        self.assertEqual(set(self.EXPECTED), set(Account.Subtype.values))

    def test_normal_balance_for_every_subtype(self):
        for subtype, expected in self.EXPECTED.items():
            with self.subTest(subtype=subtype):
                self.assertEqual(Account(type=self.TYPE_OF[subtype], subtype=subtype).normal_balance, expected)

    def test_accounts_without_a_subtype_follow_their_type(self):
        for account_type, expected in {"asset": "debit", "liability": "credit", "equity": "credit", "revenue": "credit", "expense": "debit"}.items():
            with self.subTest(type=account_type):
                self.assertEqual(Account(type=account_type, subtype="").normal_balance, expected)


class OwnerDrawPresentationTests(McpTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.owner = Contact.objects.create(name="Sample Owner", kind=Contact.Kind.OWNER)
        cls.capital = Account.objects.create(code="3000", name="Owner contributions", type=Account.Type.EQUITY, subtype=Account.Subtype.OWNER_CONTRIBUTION)
        cls.draws = Account.objects.create(code="3100", name="Owner draws", type=Account.Type.EQUITY, subtype=Account.Subtype.OWNER_DRAW)

    def setUp(self):
        super().setUp()
        post(date(2026, 1, 2), self.bank, self.capital, Decimal("2000.00"), self.user, self.owner)
        post(date(2026, 1, 3), self.bank, self.sales, Decimal("300.00"), self.user)
        post(date(2026, 1, 4), self.draws, self.bank, Decimal("500.00"), self.user, self.owner)

    def test_list_accounts_reports_draws_as_debit_and_leaves_other_accounts_alone(self):
        accounts = {row["code"]: row for row in self.call("list_accounts")["structuredContent"]["accounts"]}
        self.assertEqual(accounts["3100"]["normal_balance"], "debit")
        self.assertEqual(accounts["3100"]["balance"], "500.00")
        expected = {"1000": ("debit", "1800.00"), "2100": ("credit", "0.00"), "4000": ("credit", "300.00"), "6100": ("debit", "0.00"), "3000": ("credit", "2000.00")}
        for code, (side, balance) in expected.items():
            with self.subTest(code=code):
                self.assertEqual((accounts[code]["normal_balance"], accounts[code]["balance"]), (side, balance))

    def test_balance_sheet_still_shows_draws_as_a_reduction_of_equity(self):
        equity = balance_sheet(date(2026, 1, 31))[Account.Type.EQUITY]
        rows = {row["account"].code: row["balance"] for row in equity["rows"]}
        self.assertEqual(rows["3100"], Decimal("-500.00"))
        self.assertEqual(rows["3000"], Decimal("2000.00"))
        self.assertEqual(equity["total"], Decimal("1800.00"))  # 2000 - 500 + 300 current earnings
        assets = balance_sheet(date(2026, 1, 31))[Account.Type.ASSET]["total"]
        self.assertEqual(assets, equity["total"])
        mcp_sheet = self.call("balance_sheet", as_of="2026-01-31")["structuredContent"]
        self.assertEqual(mcp_sheet["equity"]["total"], "1800.00")
        self.assertIn({"account": mcp.account_ref(self.draws), "balance": "-500.00"}, mcp_sheet["equity"]["rows"])

    def test_trial_balance_puts_draws_on_the_debit_side(self):
        rows, debits, credits = trial_balance(date(2026, 1, 31))
        draw = next(row for row in rows if row["account"] == self.draws)
        self.assertEqual((draw["debit_balance"], draw["credit_balance"]), (Decimal("500.00"), Decimal("0.00")))
        self.assertEqual(debits, credits)

    def test_owner_balances_report_draws_as_a_positive_amount(self):
        row = owner_balances(date(2026, 1, 31))[0]
        self.assertEqual((row["contributions"], row["draws"]), (Decimal("2000.00"), Decimal("500.00")))

    def test_general_ledger_runs_the_draw_balance_upward(self):
        ledger = self.call("general_ledger", account_id=self.draws.pk, start="2026-01-01", end="2026-01-31")["structuredContent"]
        self.assertEqual(ledger["closing_balance"], "500.00")


class SyncStatusTests(McpTestCase):
    def import_rows(self, rows, dry_run=False):
        return self.call("import_transactions", statement_account_id=self.bank.pk, rows=rows, dry_run=dry_run)["structuredContent"]

    def status(self, **args):
        return self.call("account_sync_status", **args)["structuredContent"]["accounts"]

    def test_a_never_synced_account_with_no_statement_reports_nulls(self):
        row = next(item for item in self.status() if item["account_id"] == self.bank.pk)
        self.assertEqual(row, {
            "account_id": self.bank.pk, "code": "1000", "name": "Checking",
            "last_synced_at": None, "last_transaction_date": None, "book_balance": "0.00",
            "latest_statement": None, "book_balance_at_statement_date": None, "difference_vs_statement": None,
        })

    def test_only_bank_and_card_accounts_are_listed(self):
        self.assertEqual([row["code"] for row in self.status()], ["1000", "2100"])
        self.assertTrue(self.call("account_sync_status", account_id=self.sales.pk)["isError"])
        self.assertEqual([row["code"] for row in self.status(account_id=self.card.pk)], ["2100"])

    def test_an_import_with_zero_new_rows_still_updates_last_synced_at(self):
        rows = [{"date": "2026-01-10", "description": "Sample deposit", "amount": "40.00", "category_account_id": self.sales.pk}]
        with mock.patch("django.utils.timezone.now", return_value=datetime(2026, 1, 10, 9, 0, tzinfo=dt_timezone.utc)):
            self.import_rows(rows)
        with mock.patch("django.utils.timezone.now", return_value=datetime(2026, 1, 15, 9, 0, tzinfo=dt_timezone.utc)):
            again = self.import_rows(rows)
        self.assertEqual(again["counts"]["posted"], 0)
        row = self.status(account_id=self.bank.pk)[0]
        self.assertTrue(row["last_synced_at"].startswith("2026-01-15"))
        self.assertEqual(row["last_transaction_date"], "2026-01-10")

    def test_dry_runs_and_failed_imports_do_not_count_as_a_sync(self):
        self.import_rows([{"date": "2026-01-10", "description": "Sample deposit", "amount": "40.00", "category_account_id": self.sales.pk}], dry_run=True)
        self.import_rows([{"date": "2026-01-10", "description": "Bad row", "amount": "40.00", "category_account_id": 999999}])
        self.assertIsNone(self.status(account_id=self.bank.pk)[0]["last_synced_at"])

    def test_sample_checking_against_its_january_statement(self):
        checking = Account.objects.create(code="1010", name="Sample Checking", type=Account.Type.ASSET, subtype=Account.Subtype.BANK)
        post(date(2026, 1, 5), checking, self.sales, Decimal("990.00"), self.user)
        mark_synced(checking, datetime(2026, 1, 15, 12, 0, tzinfo=dt_timezone.utc))
        record_statement_balance(account=checking, statement_end_date=date(2026, 1, 31), statement_ending_balance=Decimal("1000.00"))
        post(date(2026, 2, 3), checking, self.sales, Decimal("25.00"), self.user)  # after the statement date
        row = self.status(account_id=checking.pk)[0]
        self.assertTrue(row["last_synced_at"].startswith("2026-01-15"))
        self.assertEqual(row["last_transaction_date"], "2026-02-03")
        self.assertEqual(row["book_balance"], "1015.00")
        self.assertEqual(row["latest_statement"], {"statement_end_date": "2026-01-31", "statement_ending_balance": "1000.00"})
        self.assertEqual(row["book_balance_at_statement_date"], "990.00")
        self.assertEqual(row["difference_vs_statement"], "-10.00")

    def test_the_latest_statement_is_the_one_with_the_latest_end_date(self):
        record_statement_balance(account=self.bank, statement_end_date=date(2026, 2, 28), statement_ending_balance=Decimal("5.00"))
        record_statement_balance(account=self.bank, statement_end_date=date(2026, 1, 31), statement_ending_balance=Decimal("7.00"))
        self.assertEqual(self.status(account_id=self.bank.pk)[0]["latest_statement"]["statement_end_date"], "2026-02-28")

    def test_card_balances_are_amounts_owed(self):
        post(date(2026, 1, 8), self.software, self.card, Decimal("60.00"), self.user)
        record_statement_balance(account=self.card, statement_end_date=date(2026, 1, 31), statement_ending_balance=Decimal("60.00"))
        row = self.status(account_id=self.card.pk)[0]
        self.assertEqual((row["book_balance"], row["difference_vs_statement"]), ("60.00", "0.00"))


class StatementEntryApiTests(McpTestCase):
    def test_a_bookkeeper_enters_a_statement_balance_from_the_account_page(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("api-account-statements", args=[self.bank.pk]), json.dumps({"statement_end_date": "2026-01-31", "statement_ending_balance": "1000.00"}), content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["sync"]["difference_vs_statement"], "-1000.00")
        detail = self.client.get(reverse("api-account", args=[self.bank.pk])).json()
        self.assertEqual(detail["sync"]["latest_statement"]["statement_ending_balance"], "1000.00")
        self.assertNotIn("sync", self.client.get(reverse("api-account", args=[self.sales.pk])).json())

    def test_bad_input_and_viewers_are_refused(self):
        self.client.force_login(self.user)
        url = reverse("api-account-statements", args=[self.bank.pk])
        for body in [{"statement_end_date": "", "statement_ending_balance": "1"}, {"statement_end_date": "2026-01-31", "statement_ending_balance": "abc"}, {"statement_end_date": "2026-01-31", "statement_ending_balance": "1.005"}, {"statement_end_date": "2026-01-31", "statement_ending_balance": "Infinity"}]:
            with self.subTest(body=body):
                self.assertEqual(self.client.post(url, json.dumps(body), content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post(reverse("api-account-statements", args=[self.sales.pk]), json.dumps({"statement_end_date": "2026-01-31", "statement_ending_balance": "1"}), content_type="application/json").status_code, 400)
        self.client.force_login(make_user("reader", UserProfile.Role.VIEWER))
        self.assertEqual(self.client.post(url, json.dumps({"statement_end_date": "2026-01-31", "statement_ending_balance": "1"}), content_type="application/json").status_code, 403)
        self.assertFalse(StatementBalance.objects.exists())


class ServerInfoTests(McpTestCase):
    @override_settings(LITEBOOKS_GIT_SHA="0123456789abcdef0123456789abcdef01234567", LITEBOOKS_BUILD_TIME="2026-01-15T00:00:00Z", LITEBOOKS_VERSION="9.9.9")
    def test_server_info_identifies_the_build_and_lists_the_callers_tools(self):
        info = self.call("server_info")["structuredContent"]
        self.assertEqual(info["git_commit"], "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual((info["app_version"], info["build_time"]), ("9.9.9", "2026-01-15T00:00:00Z"))
        self.assertTrue(datetime.fromisoformat(info["mcp_server_started_at"]))
        self.assertEqual(set(info["tools"]), self.tool_names())
        self.assertIn("in_app_updates", info["feature_flags"])


class ToolsListTests(McpTestCase):
    """What a brand-new client session sees, end to end over the HTTP endpoint."""

    def test_a_new_session_lists_every_expected_tool(self):
        initialize = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}).json()["result"]
        self.assertIn("account_sync_status", initialize["instructions"])
        _, viewer = ApiToken.issue(user=make_user("reader", UserProfile.Role.VIEWER), name="read only")
        for secret in (None, viewer):
            self.assertLessEqual(EXPECTED_TOOLS, self.tool_names(secret))

    def test_new_tools_are_read_only(self):
        listed = {item["name"]: item for item in self.rpc("tools/list").json()["result"]["tools"]}
        for name in EXPECTED_TOOLS:
            with self.subTest(name=name):
                self.assertTrue(listed[name]["annotations"]["readOnlyHint"])
                self.assertEqual(mcp.TOOLS[name].minimum_role, UserProfile.Role.VIEWER)

    def test_each_read_only_tool_answers_with_sample_data(self):
        owner = Contact.objects.create(name="Sample Owner", kind=Contact.Kind.OWNER)
        calls = {
            "server_info": {},
            "account_sync_status": {},
            "list_owner_interests": {"owner_id": owner.pk},
            "ownership_as_of": {"date": "2026-01-31"},
            "partner_allocation_report": {"start": "2026-01-01", "end": "2026-01-31"},
            "capital_account_rollforward": {"start": "2026-01-01", "end": "2026-01-31"},
        }
        self.assertEqual(set(calls), EXPECTED_TOOLS)
        for name, arguments in calls.items():
            with self.subTest(name=name):
                result = self.call(name, **arguments)
                self.assertFalse(result["isError"], result["content"][0]["text"])

    def test_every_tool_declared_in_code_is_offered_by_tools_list(self):
        declared = declared_tool_names()
        self.assertEqual(len(declared), len(set(declared)), "a tool name is declared twice")
        _, owner = ApiToken.issue(user=make_user("principal", UserProfile.Role.OWNER), name="owner")
        self.assertEqual(set(declared), self.tool_names(owner))

    def test_the_ci_check_passes(self):
        out = StringIO()
        call_command("check_mcp_tools", stdout=out)
        self.assertIn("declared and registered", out.getvalue())
