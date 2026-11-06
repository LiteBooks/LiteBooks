"""Founder-borne expense log. All people, vendors, and amounts are fictional."""

import json
import shutil
import tempfile
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from ledger import founder
from ledger.models import (
    Account, AccountingPeriod, ApiToken, AuditEvent, Contact, FounderExpense, FounderExpenseReceipt, FounderExpenseSettings,
    JournalEntry, JournalLine, UserProfile,
)
from ledger.services import create_entry

from .test_mcp import McpTestCase, make_user


UTC = dt_timezone.utc
MEDIA = tempfile.mkdtemp(prefix="litebooks-founder-tests-")


def at(*parts):
    return datetime(*parts, tzinfo=UTC)


def pdf(name="receipt.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4 sample", content_type="application/pdf")


def csv_file(text, name="founder.csv"):
    return SimpleUploadedFile(name, text.encode(), content_type="text/csv")


HEADER = "owner email,date_paid,vendor,description,amount,business_purpose,property_stays_personal,receipt_url\n"


@override_settings(TIME_ZONE="America/Los_Angeles", MEDIA_ROOT=MEDIA)
class FounderTestCase(McpTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.alice = Contact.objects.create(name="Alice Sample", kind=Contact.Kind.OWNER, email="alice@example.com")
        cls.bob = Contact.objects.create(name="Bob Sample", kind=Contact.Kind.OWNER, email="bob@example.com")
        cls.vendor = Contact.objects.create(name="Sample Vendor Co", kind=Contact.Kind.VENDOR)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def log(self, owner, paid, logged, amount="40.00", receipts=(), confirm_duplicate=True, **extra):
        # Several tests log the same payment at different times on purpose, so duplicates are confirmed by default here.
        data = {"owner_id": owner.pk, "date_paid": paid, "vendor": "Sample Supply", "description": "Printer paper", "amount": amount, "business_purpose": "Client proposal printing", **extra}
        with mock.patch("django.utils.timezone.now", return_value=logged):
            return founder.create_expense(user=self.user, data=data, receipts=receipts, confirm_duplicate=confirm_duplicate)

    def flags(self, expense):
        return founder.flags(FounderExpense.objects.get(pk=expense.pk))

    def set_window(self, start, end, deadline):
        founder.save_settings(user=self.user, start=start, end=end, deadline=deadline)


class AcceptanceTests(FounderTestCase):
    def test_the_sample_scenario(self):
        alice = self.log(self.alice, "2026-08-10", at(2026, 8, 20, 18), amount="40.00")
        bob = self.log(self.bob, "2026-07-01", at(2026, 9, 15, 18), amount="120.00")
        self.assertEqual((self.flags(alice)["days_to_log"], self.flags(alice)["late"]), (10, False))
        self.assertTrue(self.flags(bob)["late"])

        self.set_window(date(2026, 6, 1), date(2026, 9, 27), date(2026, 11, 27))
        self.assertTrue(self.flags(bob)["in_special_window"])
        self.assertFalse(self.flags(bob)["special_window_late"])

        summary = {row["owner_name"]: row for row in self.call("founder_expense_summary", year=2026)["structuredContent"]["owners"]}
        self.assertEqual((summary["Alice Sample"]["total"], summary["Alice Sample"]["count"]), ("40.00", 1))
        self.assertEqual((summary["Bob Sample"]["total"], summary["Bob Sample"]["count"]), ("120.00", 1))

        founder.withdraw_expense(user=self.user, expense=bob, reason="Entered by mistake")
        owners = [row["owner_name"] for row in self.call("founder_expense_summary", year=2026)["structuredContent"]["owners"]]
        self.assertEqual(owners, ["Alice Sample"])
        visible = self.call("list_founder_expenses")["structuredContent"]["founder_expenses"]
        self.assertEqual([row["id"] for row in visible], [alice.pk])
        everything = {row["id"]: row for row in self.call("list_founder_expenses", include_withdrawn=True)["structuredContent"]["founder_expenses"]}
        self.assertEqual(everything[bob.pk]["status"], "withdrawn")
        self.assertEqual(everything[bob.pk]["withdrawn_reason"], "Entered by mistake")


class LateFlagTests(FounderTestCase):
    def test_thirty_days_is_on_time_and_thirty_one_is_late(self):
        on_time = self.log(self.alice, "2026-03-01", at(2026, 3, 31, 18))
        late = self.log(self.alice, "2026-03-01", at(2026, 4, 1, 18))
        self.assertEqual((self.flags(on_time)["days_to_log"], self.flags(on_time)["late"]), (30, False))
        self.assertEqual((self.flags(late)["days_to_log"], self.flags(late)["late"]), (31, True))

    def test_days_are_counted_in_the_business_time_zone(self):
        # 2026-04-01 06:30 UTC is still March 31 in Los Angeles: day 30, on time.
        before_midnight = self.log(self.alice, "2026-03-01", at(2026, 4, 1, 6, 30))
        # 2026-04-01 07:30 UTC is April 1 in Los Angeles: day 31, late.
        after_midnight = self.log(self.alice, "2026-03-01", at(2026, 4, 1, 7, 30))
        self.assertEqual(self.flags(before_midnight)["days_to_log"], 30)
        self.assertFalse(self.flags(before_midnight)["late"])
        self.assertEqual(self.flags(after_midnight)["days_to_log"], 31)
        self.assertTrue(self.flags(after_midnight)["late"])

    def test_future_dates_use_the_local_calendar(self):
        # 2026-04-01 06:30 UTC is March 31 locally, so April 1 is still the future.
        with self.assertRaises(ValidationError) as caught:
            self.log(self.alice, "2026-04-01", at(2026, 4, 1, 6, 30))
        self.assertIn("future", str(caught.exception))
        self.log(self.alice, "2026-03-31", at(2026, 4, 1, 6, 30))


class SpecialWindowTests(FounderTestCase):
    def setUp(self):
        super().setUp()
        self.set_window(date(2026, 1, 1), date(2026, 3, 31), date(2026, 5, 30))

    def test_first_and_last_payment_days_are_inside_the_window(self):
        logged = at(2026, 4, 10, 18)
        cases = {"2025-12-31": False, "2026-01-01": True, "2026-03-31": True, "2026-04-01": False}
        for paid, inside in cases.items():
            with self.subTest(paid=paid):
                self.assertEqual(self.flags(self.log(self.alice, paid, logged))["in_special_window"], inside)

    def test_logged_on_the_deadline_is_on_time_and_the_day_after_is_late(self):
        on_deadline = self.log(self.alice, "2026-03-31", at(2026, 5, 31, 6, 59))  # 23:59 on May 30 locally
        day_after = self.log(self.alice, "2026-03-31", at(2026, 5, 31, 7, 1))  # 00:01 on May 31 locally
        self.assertFalse(self.flags(on_deadline)["special_window_late"])
        self.assertTrue(self.flags(day_after)["special_window_late"])

    def test_payments_outside_the_window_are_never_window_late(self):
        self.assertFalse(self.flags(self.log(self.alice, "2026-04-01", at(2026, 7, 1, 18)))["special_window_late"])

    def test_no_window_configured(self):
        self.set_window(None, None, None)
        flags = self.flags(self.log(self.alice, "2026-02-01", at(2026, 7, 1, 18)))
        self.assertEqual((flags["in_special_window"], flags["special_window_late"]), (False, False))

    def test_window_settings_are_validated(self):
        for start, end, deadline in [(date(2026, 1, 1), None, None), (date(2026, 3, 1), date(2026, 2, 1), date(2026, 5, 1)), (date(2026, 1, 1), date(2026, 3, 31), date(2026, 3, 30))]:
            with self.subTest(start=start, end=end, deadline=deadline), self.assertRaises(ValidationError):
                self.set_window(start, end, deadline)


class ValidationTests(FounderTestCase):
    def test_250_or_more_gets_an_approval_reminder_but_is_still_saved(self):
        under = self.log(self.alice, "2026-02-01", at(2026, 2, 2, 18), amount="249.99")
        threshold = self.log(self.alice, "2026-02-01", at(2026, 2, 2, 18), amount="250.00")
        self.assertFalse(self.flags(under)["approval_reminder"])
        self.assertTrue(self.flags(threshold)["approval_reminder"])
        self.assertIn("approval", founder.expense_json(FounderExpense.objects.get(pk=threshold.pk))["approval_note"])

    def test_zero_negative_and_malformed_amounts_are_rejected(self):
        for amount in ["0", "0.00", "-5.00", "abc", "1.005", "NaN", "Infinity"]:
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                self.log(self.alice, "2026-02-01", at(2026, 2, 2, 18), amount=amount)
        self.assertFalse(FounderExpense.objects.exists())

    def test_required_fields_and_owner_kind(self):
        with self.assertRaises(ValidationError) as caught:
            self.log(self.alice, "2026-02-01", at(2026, 2, 2, 18), business_purpose="  ", vendor="")
        self.assertEqual(set(caught.exception.message_dict), {"business_purpose", "vendor"})
        with self.assertRaises(ValidationError):
            self.log(self.vendor, "2026-02-01", at(2026, 2, 2, 18))

    def test_category_must_be_an_expense_account_code(self):
        self.assertEqual(self.log(self.alice, "2026-02-01", at(2026, 2, 2, 18), category="6100").category, "6100")
        with self.assertRaises(ValidationError):
            self.log(self.alice, "2026-02-01", at(2026, 2, 2, 18), category="4000")

    def test_edits_cannot_move_the_payment_after_the_day_it_was_logged(self):
        expense = self.log(self.alice, "2026-02-01", at(2026, 2, 10, 18))
        with mock.patch("django.utils.timezone.now", return_value=at(2026, 3, 1, 18)), self.assertRaises(ValidationError):
            founder.update_expense(user=self.user, expense=expense, data={"date_paid": "2026-02-20"})
        with mock.patch("django.utils.timezone.now", return_value=at(2026, 3, 1, 18)):
            founder.update_expense(user=self.user, expense=expense, data={"date_paid": "2026-02-05", "amount": "41.00"})
        expense.refresh_from_db()
        self.assertEqual((expense.date_paid, expense.amount, expense.logged_at), (date(2026, 2, 5), Decimal("41.00"), at(2026, 2, 10, 18)))

    def test_withdrawn_entries_are_frozen_and_need_a_reason(self):
        expense = self.log(self.alice, "2026-02-01", at(2026, 2, 2, 18))
        with self.assertRaises(ValidationError):
            founder.withdraw_expense(user=self.user, expense=expense, reason="")
        founder.withdraw_expense(user=self.user, expense=expense, reason="Reimbursed after all")
        for change in (lambda: founder.update_expense(user=self.user, expense=expense, data={"amount": "1.00"}), lambda: founder.withdraw_expense(user=self.user, expense=expense, reason="again")):
            with self.assertRaises(ValidationError):
                change()

    def test_changes_are_audited(self):
        expense = self.log(self.alice, "2026-02-01", at(2026, 2, 2, 18))
        founder.withdraw_expense(user=self.user, expense=expense, reason="Duplicate")
        actions = list(AuditEvent.objects.filter(object_type="ledger.FounderExpense", object_id=str(expense.pk)).order_by("pk").values_list("action", flat=True))
        self.assertEqual(actions, ["created", "withdrew"])


class ReceiptTests(FounderTestCase):
    def test_multiple_receipts_and_missing_receipt_counting(self):
        two_files = self.log(self.alice, "2026-02-01", at(2026, 2, 2, 18), receipts=[pdf("a.pdf"), pdf("b.pdf")])
        linked = self.log(self.alice, "2026-02-03", at(2026, 2, 4, 18), receipt_url="https://example.com/receipts/123")
        missing = self.log(self.alice, "2026-02-05", at(2026, 2, 6, 18))
        self.assertEqual(two_files.receipts.count(), 2)
        rows = {row["id"]: row for row in self.call("list_founder_expenses")["structuredContent"]["founder_expenses"]}
        self.assertEqual((rows[two_files.pk]["has_receipt"], rows[linked.pk]["has_receipt"], rows[missing.pk]["has_receipt"]), (True, True, False))
        summary = founder.summary(2026)[0]
        self.assertEqual(summary["missing_receipts_count"], 1)
        self.assertEqual([row["id"] for row in summary["missing_receipts"]], [missing.pk])
        founder.upload_receipts(user=self.user, expense=missing, receipts=[pdf("late.pdf")])
        self.assertEqual(founder.summary(2026)[0]["missing_receipts_count"], 0)
        founder.remove_receipt(user=self.user, receipt=missing.receipts.get())
        self.assertEqual(founder.summary(2026)[0]["missing_receipts_count"], 1)

    def test_receipts_follow_the_attachment_rules(self):
        bad = SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")
        with self.assertRaises(ValidationError):
            self.log(self.alice, "2026-02-01", at(2026, 2, 2, 18), receipts=[bad])
        self.assertFalse(FounderExpense.objects.exists())
        with self.assertRaises(ValidationError):
            self.log(self.alice, "2026-02-01", at(2026, 2, 2, 18), receipt_url="ftp://example.com/x")


class CsvImportTests(FounderTestCase):
    def run_import(self, body, commit=False):
        with mock.patch("django.utils.timezone.now", return_value=at(2026, 9, 1, 18)):
            return founder.import_csv(user=self.user, upload=csv_file(HEADER + body), commit=commit)

    def test_a_clean_file_previews_then_commits(self):
        body = "alice@example.com,2026-08-10,Sample Supply,Toner,40.00,Office printing,N,\nBOB@example.com,2026-06-01,Sample Air,Flight,$1,200.50,Client visit,Y,https://example.com/r/1\n"
        body = body.replace("$1,200.50", '"$1,200.50"')
        preview = self.run_import(body)
        self.assertEqual((preview["committed"], preview["counts"]), (False, {"rows": 2, "errors": 0, "imported": 0}))
        self.assertFalse(FounderExpense.objects.exists())
        self.assertTrue(any("approval" in flag for flag in preview["rows"][1]["flags"]))
        self.assertTrue(any("Late" in flag for flag in preview["rows"][1]["flags"]))
        result = self.run_import(body, commit=True)
        self.assertTrue(result["committed"])
        bob = FounderExpense.objects.get(owner_contact=self.bob)
        self.assertEqual((bob.amount, bob.property_stays_personal, bob.source, bob.receipt_url), (Decimal("1200.50"), True, "csv_import", "https://example.com/r/1"))

    def test_one_bad_row_rejects_the_whole_file(self):
        body = "alice@example.com,2026-08-10,Sample Supply,Toner,40.00,Office printing,N,\nalice@example.com,2026-08-11,Sample Supply,Ink,-3.00,Office printing,N,\n"
        result = self.run_import(body, commit=True)
        self.assertFalse(result["committed"])
        self.assertEqual(result["counts"]["errors"], 1)
        self.assertEqual(result["rows"][0]["errors"], [])
        self.assertEqual(result["rows"][1]["line"], 3)
        self.assertIn("amount", result["rows"][1]["errors"][0])
        self.assertFalse(FounderExpense.objects.exists())

    def test_row_level_errors(self):
        rows = [
            "nobody@example.com,2026-08-10,Sample Supply,Toner,40.00,Office printing,N,",  # unknown owner email
            "alice@example.com,2026-13-45,Sample Supply,Toner,40.00,Office printing,N,",  # bad date
            "alice@example.com,2026-09-30,Sample Supply,Toner,40.00,Office printing,N,",  # future
            "alice@example.com,2026-08-10,Sample Supply,Toner,40.00,Office printing,maybe,",  # bad Y/N
            "alice@example.com,2026-08-12,Sample Supply,Toner,40.00,,N,",  # no purpose
            "alice@example.com,2026-08-13,Sample Supply,Stapler,12.00,Office,N,",
            "alice@example.com,2026-08-13,sample supply,stapler,12.00,Office,N,",  # duplicate of the line above
        ]
        result = self.run_import("\n".join(rows) + "\n")
        errors = [" ".join(row["errors"]) for row in result["rows"]]
        self.assertIn("No owner contact has the email nobody@example.com", errors[0])
        self.assertIn("date_paid", errors[1])
        self.assertIn("future", errors[2])
        self.assertIn("Y or N", errors[3])
        self.assertIn("business_purpose", errors[4])
        self.assertEqual(errors[5], "")
        self.assertIn("Duplicate of line 7", errors[6])

    def test_rows_already_logged_are_duplicates(self):
        existing = self.log(self.alice, "2026-08-10", at(2026, 8, 11, 18), amount="40.00", vendor="Sample Supply", description="Toner")
        result = self.run_import("alice@example.com,2026-08-10,Sample Supply,Toner,40,Office printing,N,\n")
        self.assertIn(f"Already logged as entry #{existing.pk}", result["rows"][0]["errors"][0])

    def test_yes_no_parsing(self):
        for text, expected in {"Y": True, "y": True, "Yes": True, "N": False, "n": False, "NO": False}.items():
            with self.subTest(text=text):
                self.assertIs(founder.parse_yes_no(text), expected)
        for text in ("", "maybe", "1"):
            with self.subTest(text=text), self.assertRaises(ValidationError):
                founder.parse_yes_no(text)

    def test_file_level_problems(self):
        self.assertIn("Missing column", founder.import_csv(user=self.user, upload=csv_file("owner email,amount\nalice@example.com,1\n"))["file_errors"][0])
        self.assertIn("no data rows", founder.import_csv(user=self.user, upload=csv_file(HEADER))["file_errors"][0])
        self.assertIn("UTF-8", founder.import_csv(user=self.user, upload=SimpleUploadedFile("x.csv", "é".encode("latin-1"), content_type="text/csv"))["file_errors"][0])


FOUNDER_READ_TOOLS = {"list_founder_expenses", "founder_expense_summary", "get_founder_expense_settings", "get_founder_expense_receipt"}
FOUNDER_WRITE_TOOLS = {"log_founder_expense", "update_founder_expense", "withdraw_founder_expense", "attach_founder_expense_receipt"}


class McpTests(FounderTestCase):
    def test_read_tools_for_viewers_write_tools_for_bookkeepers_and_no_settings_writes(self):
        tools = {item["name"]: item for item in self.rpc("tools/list").json()["result"]["tools"]}
        self.assertEqual({name for name in tools if "founder" in name}, FOUNDER_READ_TOOLS | FOUNDER_WRITE_TOOLS)
        for name in FOUNDER_READ_TOOLS:
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"], name)
        for name in FOUNDER_WRITE_TOOLS:
            self.assertFalse(tools[name]["annotations"]["readOnlyHint"], name)
        self.assertTrue(tools["withdraw_founder_expense"]["annotations"]["destructiveHint"])
        _, viewer = ApiToken.issue(user=make_user("reader", UserProfile.Role.VIEWER), name="read")
        self.assertLessEqual(FOUNDER_READ_TOOLS, self.tool_names(viewer))
        self.assertFalse(FOUNDER_WRITE_TOOLS & self.tool_names(viewer))
        _, owner = ApiToken.issue(user=make_user("principal", UserProfile.Role.OWNER), name="owner")
        self.assertFalse({name for name in self.tool_names(owner) if "founder" in name and "setting" in name} - {"get_founder_expense_settings"})

    def test_list_fields_and_filters(self):
        self.log(self.alice, "2026-08-10", at(2026, 8, 20, 18), category="6100")
        self.log(self.bob, "2025-12-31", at(2026, 1, 2, 18))
        rows = self.call("list_founder_expenses", owner_id=self.alice.pk)["structuredContent"]["founder_expenses"]
        self.assertEqual(len(rows), 1)
        self.assertLessEqual({"id", "owner_id", "owner_name", "date_paid", "vendor", "description", "amount", "category", "business_purpose", "property_stays_personal", "has_receipt", "logged_at", "days_to_log", "late", "in_special_window", "special_window_late", "status"}, set(rows[0]))
        self.assertEqual((rows[0]["category"], rows[0]["amount"], rows[0]["days_to_log"]), ("6100", "40.00", 10))
        in_2026 = self.call("list_founder_expenses", start="2026-01-01", end="2026-12-31")["structuredContent"]["founder_expenses"]
        self.assertEqual([row["owner_name"] for row in in_2026], ["Alice Sample"])
        self.assertTrue(self.call("list_founder_expenses", owner_id=self.vendor.pk)["isError"])
        self.assertTrue(self.call("list_founder_expenses", start="2026-12-31", end="2026-01-01")["isError"])

    def test_summary_by_category(self):
        self.log(self.alice, "2026-08-10", at(2026, 8, 20, 18), amount="40.00", category="6100")
        self.log(self.alice, "2026-08-11", at(2026, 8, 20, 18), amount="10.00", category="6100")
        self.log(self.alice, "2026-08-12", at(2026, 8, 20, 18), amount="5.00")
        row = self.call("founder_expense_summary", year=2026, owner_id=self.alice.pk)["structuredContent"]["owners"][0]
        self.assertEqual(row["by_category"], [
            {"category": "6100", "category_name": "Software", "total": "50.00", "count": 2},
            {"category": None, "category_name": "Uncategorized", "total": "5.00", "count": 1},
        ])
        self.assertEqual((row["total"], row["business_total"], row["count"], row["late_count"], row["flagged_count"], row["missing_receipts_count"]), ("55.00", "55.00", 3, 0, 0, 3))
        self.assertEqual(len(row["missing_receipts"]), 3)
        self.assertEqual(self.call("founder_expense_summary", year=2025)["structuredContent"]["owners"], [])
        self.assertTrue(self.call("founder_expense_summary", year="twenty")["isError"])


class WebApiTests(FounderTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_create_with_receipts_edit_withdraw_and_export(self):
        response = self.client.post(reverse("api-founder-expenses"), {
            "owner_id": self.alice.pk, "date_paid": "2026-01-05", "vendor": "Sample Supply", "description": "Toner", "amount": "40.00",
            "business_purpose": "Office printing", "property_stays_personal": "false", "receipts": [pdf("one.pdf"), pdf("two.pdf")],
        })
        self.assertEqual(response.status_code, 201, response.content)
        entry = response.json()["entry"]
        self.assertEqual(len(entry["receipts"]), 2)
        download = self.client.get(entry["receipts"][0]["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.streaming_content), b"%PDF-1.4 sample")

        edited = self.client.post(reverse("api-founder-expense", args=[entry["id"]]), json.dumps({"vendor": "Sample Supply West"}), content_type="application/json")
        self.assertEqual(edited.json()["entry"]["vendor"], "Sample Supply West")
        self.assertEqual(self.client.post(reverse("api-founder-expense-withdraw", args=[entry["id"]]), json.dumps({"reason": "Duplicate"}), content_type="application/json").status_code, 200)
        self.assertEqual(self.client.post(reverse("api-founder-expense", args=[entry["id"]]), json.dumps({"vendor": "x"}), content_type="application/json").status_code, 400)

        listed = self.client.get(reverse("api-founder-expenses"), {"year": 2026}).json()["entries"]
        self.assertEqual(listed, [])
        exported = self.client.get(reverse("api-founder-expense-export"), {"year": 2026, "include_withdrawn": "1"}).content.decode()
        self.assertTrue(exported.startswith("Not company expenses. Not included in company books or tax returns."))
        self.assertIn("Sample Supply West", exported)
        self.assertIn("withdrawn", exported)

    def test_viewers_read_but_cannot_write_and_only_admins_set_the_window(self):
        expense = self.log(self.alice, "2026-02-01", at(2026, 2, 2, 18))
        self.client.force_login(make_user("reader", UserProfile.Role.VIEWER))
        self.assertEqual(len(self.client.get(reverse("api-founder-expenses")).json()["entries"]), 1)
        self.assertEqual(self.client.post(reverse("api-founder-expenses"), {"owner_id": self.alice.pk}).status_code, 403)
        self.assertEqual(self.client.post(reverse("api-founder-expense-withdraw", args=[expense.pk]), {"reason": "x"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("api-founder-expense-import"), {"file": csv_file(HEADER)}).status_code, 403)
        self.client.force_login(self.user)  # bookkeeper
        window = {"special_window_start": "2026-01-01", "special_window_end": "2026-03-31", "special_window_deadline": "2026-05-30"}
        self.assertEqual(self.client.post(reverse("api-founder-expense-settings"), json.dumps(window), content_type="application/json").status_code, 403)
        self.client.force_login(make_user("boss", UserProfile.Role.ADMIN))
        saved = self.client.post(reverse("api-founder-expense-settings"), json.dumps(window), content_type="application/json").json()["settings"]
        self.assertEqual(saved["special_window_deadline"], "2026-05-30")

    def test_csv_import_endpoint_previews_and_commits(self):
        body = HEADER + "alice@example.com,2026-01-10,Sample Supply,Toner,40.00,Office printing,N,\n"
        preview = self.client.post(reverse("api-founder-expense-import"), {"file": csv_file(body)}).json()
        self.assertFalse(preview["committed"])
        committed = self.client.post(reverse("api-founder-expense-import"), {"file": csv_file(body), "commit": "true"}).json()
        self.assertEqual(committed["counts"]["imported"], 1)


class SeparationTests(FounderTestCase):
    """Founder expenses never touch the books."""

    def test_no_relation_to_the_ledger(self):
        ledger_models = {JournalEntry, JournalLine, Account, AccountingPeriod}
        for model in (FounderExpense, FounderExpenseReceipt, FounderExpenseSettings):
            for field in model._meta.get_fields():
                with self.subTest(model=model.__name__, field=field.name):
                    self.assertNotIn(getattr(field, "related_model", None), ledger_models)

    def snapshot(self):
        mcp_calls = {
            "list_accounts": {}, "trial_balance": {"as_of": "2026-12-31"}, "balance_sheet": {"as_of": "2026-12-31"},
            "income_statement": {"start": "2026-01-01", "end": "2026-12-31"}, "owner_balances": {"as_of": "2026-12-31"},
            "general_ledger": {"start": "2026-01-01", "end": "2026-12-31"}, "search_transactions": {}, "aging_report": {"kind": "payables"},
            "partner_allocation_report": {"start": "2026-01-01", "end": "2026-12-31"}, "capital_account_rollforward": {"start": "2026-01-01", "end": "2026-12-31"},
            "account_sync_status": {}, "list_owner_activity": {}, "categorization_history": {"descriptions": ["Printer paper", "Sample Supply"]},
        }
        result = {}
        for name, args in mcp_calls.items():
            called = self.call(name, **args)
            self.assertFalse(called["isError"], f"{name}: {called['content'][0]['text']}")
            result[name] = called["structuredContent"]
        for slug in ("trial-balance", "balance-sheet", "income-statement", "owner-balances", "receivables-aging", "payables-aging", "general-ledger"):
            response = self.client.get(f"/api/reports/{slug}/", {"as_of": "2026-12-31", "start": "2026-01-01", "end": "2026-12-31"})
            self.assertEqual(response.status_code, 200, f"{slug}: {response.content[:200]}")
            result[f"web:{slug}"] = response.content
        result["web:accounts"] = self.client.get(reverse("api-accounts")).content
        result["web:dashboard_metrics"] = self.client.get(reverse("api-dashboard")).json()["metrics"]
        self.assertEqual(len(result["trial_balance"]["rows"]), 3)  # the sample entries really are in the reports
        self.assertEqual(result["income_statement"]["net_income"], "475.00")
        result["counts"] = (JournalEntry.objects.count(), JournalLine.objects.count())
        return result

    def test_reports_are_byte_for_byte_unchanged(self):
        self.client.force_login(self.user)
        create_entry(entry_date=date(2026, 3, 1), description="Sample sale", user=self.user, lines=[
            {"account": self.bank, "debit": 500, "credit": 0, "owner": None},
            {"account": self.sales, "debit": 0, "credit": 500, "owner": None},
        ])
        create_entry(entry_date=date(2026, 3, 2), description="Printer paper", user=self.user, lines=[
            {"account": self.software, "debit": 25, "credit": 0, "owner": None},
            {"account": self.bank, "debit": 0, "credit": 25, "owner": None},
        ])
        before = self.snapshot()

        self.log(self.alice, "2026-08-10", at(2026, 8, 20, 18), amount="40.00", category="6100", receipts=[pdf()])
        bob = self.log(self.bob, "2026-07-01", at(2026, 9, 15, 18), amount="120.00")
        self.set_window(date(2026, 6, 1), date(2026, 9, 27), date(2026, 11, 27))
        with mock.patch("django.utils.timezone.now", return_value=at(2026, 9, 20, 18)):
            founder.import_csv(user=self.user, upload=csv_file(HEADER + "alice@example.com,2026-09-01,Sample Supply,Printer paper,300.00,Printing,N,\n"), commit=True)
        founder.withdraw_expense(user=self.user, expense=bob, reason="Duplicate")
        self.assertEqual(FounderExpense.objects.count(), 3)

        after = self.snapshot()
        for key in before:
            with self.subTest(report=key):
                self.assertEqual(before[key], after[key])
