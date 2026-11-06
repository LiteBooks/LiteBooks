"""Founder expense log over MCP. All people, vendors, and amounts are fictional sample data."""

import base64
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
from django.urls import reverse

from ledger import founder
from ledger.models import ApiToken, AuditEvent, Contact, FounderExpense, FounderExpenseReceipt, FounderExpenseSettings, JournalEntry, JournalLine, TransactionImportRef
from ledger.models import UserProfile
from ledger.services import create_entry

from .test_mcp import make_user
from .test_founder import HEADER, MEDIA, FounderTestCase, SeparationTests, at, csv_file


PDF = b"%PDF-1.4 sample receipt"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8


def b64(content):
    return base64.b64encode(content).decode()


def receipt(content=PDF, name="receipt.pdf", mime="application/pdf"):
    return {"filename": name, "mime_type": mime, "content_base64": b64(content)}


def noon(day):
    """Noon in Los Angeles on a YYYY-MM-DD day, as UTC."""
    y, m, d = map(int, day.split("-"))
    return at(y, m, d, 19)


class FounderMcpCase(FounderTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.owner_a = Contact.objects.create(name="Owner A", kind=Contact.Kind.OWNER, email="owner-a@example.com")

    def setUp(self):
        super().setUp()
        founder.save_settings(user=self.user, start=date(2030, 1, 1), end=date(2030, 3, 31), deadline=date(2030, 5, 30), standard_deadline_days=30)

    def sample(self, **extra):
        return {"owner_id": self.owner_a.pk, "date_paid": "2030-02-15", "vendor": "Sample SaaS", "description": "Team plan", "amount": "40.00",
                "business_amount": "20.00", "allocation_note": "50% business", "business_purpose": "Project tracking", "property_stays_personal": False, **extra}

    def mcp(self, tool, today, **arguments):
        with mock.patch("django.utils.timezone.now", return_value=noon(today) if isinstance(today, str) else today):
            result = self.call(tool, **arguments)
        self.assertFalse(result["isError"], result["content"][0]["text"])
        return result["structuredContent"]

    def mcp_error(self, tool, today, **arguments):
        with mock.patch("django.utils.timezone.now", return_value=noon(today)):
            result = self.call(tool, **arguments)
        self.assertTrue(result["isError"])
        return result["content"][0]["text"]

    def log(self, today, dry_run=False, **fields):
        return self.mcp("log_founder_expense", today, dry_run=dry_run, **self.sample(**fields))

    def counts(self):
        return (FounderExpense.objects.count(), FounderExpenseReceipt.objects.count(), AuditEvent.objects.count())


class AcceptanceCriteriaTests(FounderMcpCase):
    def test_in_window_entry_is_timely_by_the_log_by_date(self):
        result = self.log("2030-05-10")
        self.assertEqual(result["status"], "created")
        entry = result["entry"]
        self.assertEqual((entry["deadline"], entry["deadline_rule"], entry["timely"], entry["late"]), ("2030-05-30", "special_window", True, False))
        self.assertEqual((entry["amount"], entry["business_amount"], entry["allocation_note"]), ("40.00", "20.00", "50% business"))
        self.assertEqual(entry["days_to_log"], 84)  # far past 30 days, yet timely: the window rule governs

    def test_outside_window_uses_the_standard_deadline_and_is_saved_late(self):
        result = self.log("2030-05-15", date_paid="2030-04-10")
        self.assertEqual(result["status"], "created")
        self.assertEqual((result["deadline"], result["timely"]), ("2030-05-10", False))
        self.assertTrue(any("flagged late" in item for item in result["warnings"]))
        self.assertTrue(FounderExpense.objects.filter(pk=result["entry"]["id"]).exists())

    def test_business_more_than_full_is_invalid(self):
        result = self.log("2030-05-10", business_amount="45.00")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("business_amount", {item["field"] for item in result["errors"]})
        self.assertFalse(FounderExpense.objects.exists())

    def test_partial_business_amount_needs_an_allocation_note(self):
        result = self.log("2030-05-10", allocation_note="  ")
        self.assertEqual(result["status"], "invalid")
        self.assertEqual({item["field"] for item in result["errors"]}, {"allocation_note"})

    def test_duplicate_needs_confirmation(self):
        first = self.log("2030-05-10")
        again = self.log("2030-05-10")
        self.assertEqual((again["status"], again["duplicate_of"]), ("possible_duplicate", [first["entry"]["id"]]))
        self.assertEqual(FounderExpense.objects.count(), 1)
        confirmed = self.log("2030-05-10", confirm_duplicate=True)
        self.assertEqual(confirmed["status"], "created")
        self.assertEqual(FounderExpense.objects.count(), 2)

    def test_dry_run_writes_nothing(self):
        before = self.counts()
        files_before = sorted(path.name for path in Path(MEDIA).rglob("*") if path.is_file())
        preview = self.mcp("log_founder_expense", "2030-05-10", **self.sample(receipts=[receipt()]))  # dry_run defaults to true
        self.assertEqual((preview["status"], preview["dry_run"]), ("ready", True))
        self.assertEqual((preview["preview"]["deadline"], preview["preview"]["timely"]), ("2030-05-30", True))
        self.assertEqual(preview["preview"]["receipts"][0]["sha256"], hashlib.sha256(PDF).hexdigest())
        self.assertEqual(self.counts(), before)
        self.assertEqual(sorted(path.name for path in Path(MEDIA).rglob("*") if path.is_file()), files_before)
        # Every outcome of a dry run leaves nothing behind.
        self.log("2030-05-10")
        before = self.counts()
        for fields in ({}, {"confirm_duplicate": True}, {"business_amount": "99.00"}):
            self.log("2030-05-10", dry_run=True, **fields)
        self.assertEqual(self.counts(), before)

    def test_withdrawn_hidden_by_default_and_out_of_the_summary(self):
        kept = self.log("2030-05-10")["entry"]
        gone = self.log("2030-05-10", vendor="Sample Cloud", amount="10.00", business_amount="10.00", allocation_note="")["entry"]
        self.mcp("withdraw_founder_expense", "2030-05-11", id=gone["id"], reason="Reimbursed after all")
        visible = self.mcp("list_founder_expenses", "2030-05-11", year_paid=2030)["founder_expenses"]
        self.assertEqual([row["id"] for row in visible], [kept["id"]])
        everything = self.mcp("list_founder_expenses", "2030-05-11", year_paid=2030, include_withdrawn=True)["founder_expenses"]
        self.assertEqual({row["id"]: row["status"] for row in everything}, {kept["id"]: "logged", gone["id"]: "withdrawn"})
        summary = self.mcp("founder_expense_summary", "2030-05-11", year_paid=2030)["owners"]
        self.assertEqual([(row["owner_name"], row["count"], row["total"], row["business_total"]) for row in summary], [("Owner A", 1, "40.00", "20.00")])
        # The web page's toggle shows the same thing.
        self.client.force_login(self.user)
        web = lambda **query: [row["id"] for row in self.client.get(reverse("api-founder-expenses"), {"year": 2030, **query}).json()["entries"]]
        self.assertEqual(web(), [kept["id"]])
        self.assertEqual(sorted(web(include_withdrawn="1")), sorted([kept["id"], gone["id"]]))
        self.assertEqual(self.client.get(reverse("api-founder-expense-summary"), {"year": 2030}).json()["owners"], summary)

    def test_audit_log_has_create_update_receipt_and_withdraw(self):
        entry = self.log("2030-05-10")["entry"]
        self.mcp("update_founder_expense", "2030-05-11", id=entry["id"], description="Team plan, annual")
        self.mcp("attach_founder_expense_receipt", "2030-05-11", id=entry["id"], **receipt())
        self.mcp("withdraw_founder_expense", "2030-05-12", id=entry["id"], reason="Logged under the wrong owner")
        events = self.mcp("search_audit_log", "2030-05-12", record_type="founder_expense", record_id=entry["id"])["events"]
        self.assertEqual([event["action"] for event in reversed(events)], ["created", "updated", "updated", "withdrew"])
        self.assertTrue(all(event["record_type"] == "founder_expense" and event["user"] == "books" for event in events))
        receipt_event = list(reversed(events))[2]
        self.assertEqual(receipt_event["before"]["receipt_files"], [])
        self.assertEqual(receipt_event["after"]["receipt_files"][0]["sha256"], hashlib.sha256(PDF).hexdigest())
        self.assertEqual(events[0]["after"]["withdrawn_reason"], "Logged under the wrong owner")
        settings_events = self.mcp("search_audit_log", "2030-05-12", record_type="founder_expense_settings")["events"]
        self.assertTrue(settings_events)


@override_settings(TIME_ZONE="America/Los_Angeles")
class ParityTests(FounderMcpCase):
    """An entry made over MCP is indistinguishable from one made with the web page's Log expense."""

    COMPARABLE_SKIP = {"id", "logged_at", "receipts"}

    def web_create(self, data):
        self.client.force_login(self.user)
        return self.client.post(reverse("api-founder-expenses"), data)

    def comparable(self, entry):
        result = {key: value for key, value in entry.items() if key not in self.COMPARABLE_SKIP}
        result["receipts"] = [(item["name"], item["content_type"], item["size"], item["sha256"]) for item in entry["receipts"]]
        return result

    def audit(self, pk):
        events = AuditEvent.objects.filter(object_type="ledger.FounderExpense", object_id=str(pk)).order_by("pk")
        strip = lambda snapshot: {key: value for key, value in snapshot.items() if key != "receipt_files"} | {"files": [(item["name"], item["sha256"]) for item in snapshot.get("receipt_files", [])]}
        return [(event.action, event.actor_id, strip(event.before), strip(event.after)) for event in events]

    def test_create_update_and_withdraw_match(self):
        fields = self.sample(property_stays_personal=True, category="6100")
        with mock.patch("django.utils.timezone.now", return_value=noon("2030-05-10")):
            web = self.web_create({**{key: str(value).lower() if isinstance(value, bool) else value for key, value in fields.items()}, "receipts": [SimpleUploadedFile("r.pdf", PDF, content_type="application/pdf")]})
        self.assertEqual(web.status_code, 201, web.content)
        mcp = self.log("2030-05-10", property_stays_personal=True, category="6100", vendor="Sample SaaS", receipts=[receipt(name="r.pdf")], confirm_duplicate=True)
        web_id, mcp_id = web.json()["entry"]["id"], mcp["entry"]["id"]
        rows = lambda: {row.pk: founder.expense_json(row) for row in FounderExpense.objects.all()}
        self.assertEqual(self.comparable(rows()[web_id]), self.comparable(rows()[mcp_id]))
        self.assertEqual(rows()[mcp_id]["source"], "manual")
        self.assertEqual(self.audit(web_id), self.audit(mcp_id))

        change = {"date_paid": "2030-04-02", "amount": "50.00", "business_amount": "25.00", "allocation_note": "Half"}
        with mock.patch("django.utils.timezone.now", return_value=noon("2030-05-20")):
            self.assertEqual(self.client.post(reverse("api-founder-expense", args=[web_id]), json.dumps(change), content_type="application/json").status_code, 200)
        self.mcp("update_founder_expense", "2030-05-20", id=mcp_id, **change)
        self.assertEqual(self.comparable(rows()[web_id]), self.comparable(rows()[mcp_id]))
        self.assertEqual(rows()[mcp_id]["deadline"], "2030-05-02")  # moved out of the window: standard deadline
        self.assertEqual(self.audit(web_id), self.audit(mcp_id))

        self.assertEqual(self.client.post(reverse("api-founder-expense-withdraw", args=[web_id]), json.dumps({"reason": "Duplicate"}), content_type="application/json").status_code, 200)
        self.mcp("withdraw_founder_expense", "2030-05-21", id=mcp_id, reason="Duplicate")
        web_row, mcp_row = rows()[web_id], rows()[mcp_id]
        self.assertEqual(self.comparable(web_row) | {"withdrawn_at": None}, self.comparable(mcp_row) | {"withdrawn_at": None})
        self.assertEqual([item[0] for item in self.audit(web_id)], [item[0] for item in self.audit(mcp_id)])
        self.assertEqual(self.audit(web_id)[-1][3], self.audit(mcp_id)[-1][3])

    def test_both_paths_refuse_the_same_bad_input_and_changes_to_withdrawn_entries(self):
        bad = self.sample(business_amount="45.00")
        with mock.patch("django.utils.timezone.now", return_value=noon("2030-05-10")):
            self.client.force_login(self.user)
            web = self.client.post(reverse("api-founder-expenses"), bad)
        self.assertEqual(web.status_code, 400)
        mcp = self.log("2030-05-10", business_amount="45.00")
        self.assertEqual({item["field"]: [item["message"]] for item in mcp["errors"]}, web.json()["fields"])

        entry = self.log("2030-05-10")["entry"]
        self.mcp("withdraw_founder_expense", "2030-05-11", id=entry["id"], reason="Duplicate")
        self.assertIn("withdrawn", self.mcp_error("update_founder_expense", "2030-05-12", id=entry["id"], vendor="x"))
        self.assertIn("withdrawn", self.mcp_error("withdraw_founder_expense", "2030-05-12", id=entry["id"], reason="again"))
        self.assertIn("withdrawn", self.mcp_error("attach_founder_expense_receipt", "2030-05-12", id=entry["id"], **receipt()))
        self.assertIn("reason", self.mcp_error("withdraw_founder_expense", "2030-05-12", id=self.log("2030-05-12", vendor="Other")["entry"]["id"], reason=" "))
        self.assertEqual(FounderExpense.objects.filter(pk=entry["id"]).count(), 1)  # withdrawn, never deleted

    def test_web_log_expense_warns_about_duplicates_too(self):
        data = {**self.sample(), "property_stays_personal": "false"}
        with mock.patch("django.utils.timezone.now", return_value=noon("2030-05-10")):
            self.client.force_login(self.user)
            self.assertEqual(self.client.post(reverse("api-founder-expenses"), data).status_code, 201)
            again = self.client.post(reverse("api-founder-expenses"), data)
            self.assertEqual(again.status_code, 400)
            self.assertIn("duplicate", again.json()["fields"]["confirm_duplicate"][0])
            self.assertEqual(self.client.post(reverse("api-founder-expenses"), {**data, "confirm_duplicate": "true"}).status_code, 201)
        self.assertEqual(FounderExpense.objects.count(), 2)

    def test_viewers_cannot_write_over_mcp(self):
        _, viewer = ApiToken.issue(user=make_user("reader", UserProfile.Role.VIEWER), name="read")
        with mock.patch("django.utils.timezone.now", return_value=noon("2030-05-10")):
            response = self.rpc("tools/call", {"name": "log_founder_expense", "arguments": {**self.sample(), "dry_run": False}}, secret=viewer).json()
        self.assertFalse(FounderExpense.objects.exists())
        self.assertTrue("error" in response or response["result"].get("isError"))


class DeadlineTests(FounderMcpCase):
    def deadline(self, paid, logged):
        result = self.log(logged if isinstance(logged, str) else logged, date_paid=paid, confirm_duplicate=True)
        return result["entry"]

    def test_window_first_and_last_days(self):
        cases = {"2029-12-31": ("2030-01-30", "standard"), "2030-01-01": ("2030-05-30", "special_window"),
                 "2030-03-31": ("2030-05-30", "special_window"), "2030-04-01": ("2030-05-01", "standard")}
        for paid, expected in cases.items():
            with self.subTest(paid=paid):
                entry = self.deadline(paid, "2030-04-15")
                self.assertEqual((entry["deadline"], entry["deadline_rule"]), expected)

    def test_log_by_day_is_inclusive_in_the_business_time_zone(self):
        on_time = self.deadline("2030-03-31", at(2030, 5, 31, 6, 59))  # 23:59 May 30 in Los Angeles
        late = self.deadline("2030-03-31", at(2030, 5, 31, 7, 1))  # 00:01 May 31 in Los Angeles
        self.assertEqual((on_time["timely"], late["timely"]), (True, False))
        self.assertEqual((on_time["special_window_late"], late["special_window_late"]), (False, True))

    def test_standard_deadline_is_inclusive_near_midnight(self):
        # Paid 2030-04-10: deadline 2030-05-10.
        on_time = self.deadline("2030-04-10", at(2030, 5, 11, 6, 30))  # 23:30 May 10 locally
        late = self.deadline("2030-04-10", at(2030, 5, 11, 7, 30))  # 00:30 May 11 locally
        self.assertEqual((on_time["deadline"], on_time["timely"]), ("2030-05-10", True))
        self.assertEqual((late["deadline"], late["timely"]), ("2030-05-10", False))

    def test_month_ends_and_leap_years(self):
        window = FounderExpenseSettings.load()
        window.special_window_start = window.special_window_end = window.special_window_deadline = None
        cases = {
            date(2030, 1, 31): date(2030, 3, 2),  # February 2030 has 28 days
            date(2028, 1, 31): date(2028, 3, 1),  # 2028 is a leap year
            date(2028, 2, 29): date(2028, 3, 30),
            date(2030, 12, 15): date(2031, 1, 14),
            date(2030, 4, 30): date(2030, 5, 30),
        }
        for paid, expected in cases.items():
            with self.subTest(paid=paid):
                self.assertEqual(founder.deadline_for(paid, window), (expected, "standard"))
        entry = self.mcp("log_founder_expense", "2028-03-02", dry_run=False, **self.sample(date_paid="2028-02-29"))["entry"]
        self.assertEqual((entry["deadline"], entry["timely"]), ("2028-03-30", True))

    def test_empty_special_window(self):
        founder.save_settings(user=self.user, start=None, end=None, deadline=None)
        settings = self.mcp("get_founder_expense_settings", "2030-05-10")["settings"]
        self.assertEqual((settings["special_window_start"], settings["special_window_end"], settings["special_window_deadline"], settings["standard_deadline_days"]), (None, None, None, 30))
        entry = self.deadline("2030-02-15", "2030-05-10")
        self.assertEqual((entry["deadline"], entry["deadline_rule"], entry["timely"], entry["in_special_window"]), ("2030-03-17", "standard", False, False))

    def test_standard_days_are_configurable_and_validated(self):
        founder.save_settings(user=self.user, start=None, end=None, deadline=None, standard_deadline_days=45)
        self.assertEqual(self.deadline("2030-04-10", "2030-05-15")["deadline"], "2030-05-25")
        for bad in (0, -1, 367, "soon"):
            with self.subTest(days=bad), self.assertRaises(ValidationError):
                founder.save_settings(user=self.user, start=None, end=None, deadline=None, standard_deadline_days=bad)
        founder.save_settings(user=self.user, start=None, end=None, deadline=None)  # None leaves the days alone
        self.assertEqual(FounderExpenseSettings.load().standard_deadline_days, 45)

    def test_settings_are_editable_only_in_the_web_app(self):
        self.client.force_login(make_user("boss", UserProfile.Role.ADMIN))
        saved = self.client.post(reverse("api-founder-expense-settings"), json.dumps({"special_window_start": None, "special_window_end": None, "special_window_deadline": None, "standard_deadline_days": 60}), content_type="application/json")
        self.assertEqual(saved.json()["settings"]["standard_deadline_days"], 60)
        settings_tools = {name for name in self.tool_names() if "setting" in name}
        self.assertEqual(settings_tools, {"get_founder_expense_settings"})
        # Extra arguments to the write tools cannot touch the settings.
        self.log("2030-05-10", special_window_start="2000-01-01", standard_deadline_days=1)
        self.assertEqual(self.mcp("get_founder_expense_settings", "2030-05-10")["settings"]["standard_deadline_days"], 60)

    def test_update_recomputes_the_deadline_and_never_changes_logged_at(self):
        entry = self.log("2030-05-10")["entry"]
        logged_at = FounderExpense.objects.get(pk=entry["id"]).logged_at
        updated = self.mcp("update_founder_expense", "2030-06-01", id=entry["id"], date_paid="2030-04-20")
        self.assertEqual((updated["entry"]["deadline"], updated["entry"]["timely"], updated["deadline_changed"]), ("2030-05-20", True, True))
        self.assertEqual(updated["entry"]["logged_at"], logged_at.isoformat())
        for field in ("logged_at", "status", "source"):
            with self.subTest(field=field):
                self.assertIn("cannot be changed", self.mcp_error("update_founder_expense", "2030-06-01", id=entry["id"], **{field: "2030-06-01T00:00:00Z"}))
        self.assertIn("Unknown field", self.mcp_error("update_founder_expense", "2030-06-01", id=entry["id"], colour="blue"))
        self.client.force_login(self.user)
        self.client.post(reverse("api-founder-expense", args=[entry["id"]]), json.dumps({"logged_at": "2030-06-01T00:00:00Z", "vendor": "Sample SaaS Inc"}), content_type="application/json")
        self.assertEqual(FounderExpense.objects.get(pk=entry["id"]).logged_at, logged_at)

    def test_business_amount_follows_a_full_charge_change_unless_split(self):
        whole = self.log("2030-05-10", vendor="Whole", business_amount="40.00", allocation_note="")["entry"]
        self.assertEqual(self.mcp("update_founder_expense", "2030-05-11", id=whole["id"], amount="55.00")["entry"]["business_amount"], "55.00")
        split = self.log("2030-05-10")["entry"]
        self.assertEqual(self.mcp("update_founder_expense", "2030-05-11", id=split["id"], amount="60.00")["entry"]["business_amount"], "20.00")
        self.assertIn("business_amount", self.mcp_error("update_founder_expense", "2030-05-11", id=split["id"], amount="10.00"))


class ValidationRuleTests(FounderMcpCase):
    CASES = {
        "owner_id": [{"owner_id": None}, {"owner_id": 999999}, "vendor_contact"],
        "date_paid": [{"date_paid": "15/02/2030x"}, {"date_paid": "2030-05-11"}, {"date_paid": ""}],
        "vendor": [{"vendor": " "}, {"vendor": "x" * 161}],
        "description": [{"description": ""}, {"description": "x" * 241}],
        "amount": [{"amount": "0"}, {"amount": "-1"}, {"amount": "1.005"}, {"amount": "abc"}, {"amount": "NaN"}, {"amount": "99999999999"}],
        "business_amount": [{"business_amount": "0"}, {"business_amount": "-5"}, {"business_amount": "40.01"}, {"business_amount": "x"}],
        "allocation_note": [{"business_amount": "39.99", "allocation_note": ""}],
        "business_purpose": [{"business_purpose": "   "}],
        "property_stays_personal": [{"property_stays_personal": "maybe"}],
        "category": [{"category": "4000"}, {"category": "nope"}],
        "receipt_url": [{"receipt_url": "ftp://example.com/r"}, {"receipt_url": "not a url"}],
    }

    def test_every_rule(self):
        for field, cases in self.CASES.items():
            for case in cases:
                if case == "vendor_contact":
                    case = {"owner_id": self.vendor.pk}
                with self.subTest(field=field, case=case):
                    result = self.log("2030-05-10", dry_run=True, **case)
                    self.assertEqual(result["status"], "invalid")
                    self.assertIn(field, {item["field"] for item in result["errors"]})
        self.assertFalse(FounderExpense.objects.exists())

    def test_errors_are_collected_together(self):
        result = self.log("2030-05-10", dry_run=True, vendor="", business_purpose="", business_amount="50.00")
        self.assertEqual({item["field"] for item in result["errors"]}, {"vendor", "business_purpose", "business_amount"})

    def test_valid_edges(self):
        for case in ({"business_amount": "40.00", "allocation_note": ""}, {"business_amount": "0.01", "allocation_note": "Tiny share"}, {"property_stays_personal": True}, {"date_paid": "2030-05-10"}):
            with self.subTest(case=case):
                result = self.log("2030-05-10", dry_run=True, **case)
                self.assertEqual(result["status"], "ready", result.get("errors"))
        omitted = {key: value for key, value in self.sample(allocation_note="").items() if key != "business_amount"}
        result = self.mcp("log_founder_expense", "2030-05-10", dry_run=True, **omitted)
        self.assertEqual((result["status"], result["preview"]["business_amount"]), ("ready", "40.00"))

    def test_unknown_ids(self):
        self.assertIn("not a founder expense", self.mcp_error("update_founder_expense", "2030-05-10", id=424242, vendor="x"))
        self.assertIn("not a founder expense", self.mcp_error("withdraw_founder_expense", "2030-05-10", id=424242, reason="x"))
        self.assertIn("not a founder expense receipt", self.mcp_error("get_founder_expense_receipt", "2030-05-10", receipt_id=424242))


class DuplicateTests(FounderMcpCase):
    def test_the_duplicate_key(self):
        first = self.log("2030-05-10")["entry"]
        same = {"description": "Something else entirely", "vendor": "  sample   SAAS ", "business_amount": "40.00", "allocation_note": ""}
        self.assertEqual(self.log("2030-05-10", dry_run=True, **same)["status"], "possible_duplicate")
        for different in ({"amount": "40.01"}, {"date_paid": "2030-02-16"}, {"vendor": "Sample SaaS Ltd"}, {"owner_id": self.alice.pk}):
            with self.subTest(different=different):
                self.assertEqual(self.log("2030-05-10", dry_run=True, **different)["status"], "ready")
        self.mcp("withdraw_founder_expense", "2030-05-10", id=first["id"], reason="Wrong card")
        self.assertEqual(self.log("2030-05-10", dry_run=True)["status"], "ready")  # withdrawn entries are not active

    def test_possible_duplicate_returns_a_preview_and_saves_nothing(self):
        self.log("2030-05-10")
        before = self.counts()
        result = self.log("2030-05-10", receipts=[receipt()])
        self.assertEqual(result["status"], "possible_duplicate")
        self.assertEqual(result["preview"]["deadline"], "2030-05-30")
        self.assertEqual(self.counts(), before)
        created = self.log("2030-05-10", confirm_duplicate=True)
        self.assertTrue(any("possible duplicate" in item for item in created["warnings"]))


class ReceiptTests(FounderMcpCase):
    def test_types_are_checked_against_the_contents(self):
        entry = self.log("2030-05-10")["entry"]
        for content, name, mime in ((PDF, "a.pdf", "application/pdf"), (PNG, "a.png", "image/png"), (JPEG, "a.jpg", "image/jpeg"), (JPEG, "b.jpg", "image/jpg"), (WEBP, "a.webp", "image/webp")):
            with self.subTest(mime=mime):
                self.assertEqual(self.mcp("attach_founder_expense_receipt", "2030-05-10", id=entry["id"], **receipt(content, name, mime))["receipt"]["content_type"], mime.replace("jpg", "jpeg"))
        for content, name, mime in ((b"hello", "a.txt", "text/plain"), (PDF, "a.png", "image/png"), (b"GIF89a....", "a.gif", "image/gif"), (b"<html>", "a.pdf", "application/pdf"), (b"", "e.pdf", "application/pdf")):
            with self.subTest(rejected=name):
                self.mcp_error("attach_founder_expense_receipt", "2030-05-10", id=entry["id"], **receipt(content, name, mime))
        self.assertIn("base64", self.mcp_error("attach_founder_expense_receipt", "2030-05-10", id=entry["id"], filename="a.pdf", mime_type="application/pdf", content_base64="!!!not base64"))
        self.assertEqual(FounderExpenseReceipt.objects.count(), 5)

    def test_size_limit_is_ten_megabytes_on_every_path(self):
        exactly = PDF + b"\x00" * (founder.RECEIPT_MAX_BYTES - len(PDF))
        founder.validate_receipt(SimpleUploadedFile("max.pdf", exactly, content_type="application/pdf"))
        with self.assertRaises(ValidationError):
            founder.validate_receipt(SimpleUploadedFile("big.pdf", exactly + b"\x00", content_type="application/pdf"))
        entry = self.log("2030-05-10")["entry"]
        self.assertIn("10 MB", self.mcp_error("attach_founder_expense_receipt", "2030-05-10", id=entry["id"], **receipt(exactly + b"\x00" * 4)))
        self.assertIn("10 MB", self.mcp_error("attach_founder_expense_receipt", "2030-05-10", id=entry["id"], **receipt(exactly + b"\x00" * 3000)))
        self.client.force_login(self.user)
        web = self.client.post(reverse("api-founder-expense-receipts", args=[entry["id"]]), {"receipts": [SimpleUploadedFile("big.pdf", exactly + b"\x00", content_type="application/pdf")]})
        self.assertEqual(web.status_code, 400)
        self.assertIn("10 MB", web.json()["fields"]["receipts"][0])
        invalid = self.log("2030-05-10", dry_run=True, vendor="Other", receipts=[receipt(exactly + b"\x00")])
        self.assertEqual(invalid["status"], "invalid")

    def test_hash_round_trip_and_private_storage(self):
        content = PNG + b"sample image bytes"
        entry = self.log("2030-05-10", receipts=[receipt(content, "../../etc/scan.png", "image/png")])["entry"]
        stored = FounderExpenseReceipt.objects.get(expense_id=entry["id"])
        self.assertEqual(stored.sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(stored.original_name, "scan.png")
        self.assertTrue(stored.file.name.startswith("attachments/founder/2030/02/"))
        self.assertNotIn("scan", stored.file.name)
        fetched = self.mcp("get_founder_expense_receipt", "2030-05-10", receipt_id=stored.pk)
        self.assertEqual(base64.b64decode(fetched["content_base64"]), content)
        self.assertEqual((fetched["receipt"]["sha256"], fetched["sha256_verified"], fetched["receipt"]["expense_id"]), (stored.sha256, True, entry["id"]))
        # Tampering on disk is detected.
        with open(stored.file.path, "ab") as handle:
            handle.write(b"x")
        self.assertFalse(self.mcp("get_founder_expense_receipt", "2030-05-10", receipt_id=stored.pk)["sha256_verified"])
        # No public path, and the download endpoint needs a login.
        self.assertEqual(self.client.get(f"/media/{stored.file.name}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/founder-expenses/receipts/{stored.pk}/download/").status_code, 401)

    def test_web_uploads_are_hashed_too(self):
        self.client.force_login(self.user)
        entry = self.log("2030-05-10")["entry"]
        self.client.post(reverse("api-founder-expense-receipts", args=[entry["id"]]), {"receipts": [SimpleUploadedFile("web.pdf", PDF, content_type="application/pdf")]})
        self.assertEqual(FounderExpenseReceipt.objects.get().sha256, hashlib.sha256(PDF).hexdigest())

    def test_audit_after_snapshot_includes_files_added_during_a_web_edit(self):
        # Regression: the edit endpoint prefetches receipts, which used to leave new files out of the audit snapshot.
        entry = self.log("2030-05-10")["entry"]
        with mock.patch("django.utils.timezone.now", return_value=noon("2030-05-11")):
            self.client.force_login(self.user)
            response = self.client.post(reverse("api-founder-expense", args=[entry["id"]]), {**self.sample(property_stays_personal="false"), "receipts": [SimpleUploadedFile("edit.pdf", PDF, content_type="application/pdf")]})
        self.assertEqual(response.status_code, 200, response.content)
        event = AuditEvent.objects.filter(object_type="ledger.FounderExpense", object_id=str(entry["id"]), action="updated").get()
        self.assertEqual((event.before["receipts"], event.after["receipts"]), ([], ["edit.pdf"]))

    def test_list_shows_receipt_counts_and_missing_receipts(self):
        with_files = self.log("2030-05-10", receipts=[receipt(), receipt(PNG, "b.png", "image/png")])["entry"]
        without = self.log("2030-05-10", vendor="No Receipt Co")["entry"]
        rows = {row["id"]: row for row in self.mcp("list_founder_expenses", "2030-05-10", year_paid=2030)["founder_expenses"]}
        self.assertEqual((rows[with_files["id"]]["receipt_count"], rows[without["id"]]["receipt_count"]), (2, 0))
        summary = self.mcp("founder_expense_summary", "2030-05-10", year_paid=2030)["owners"][0]
        self.assertEqual([row["id"] for row in summary["missing_receipts"]], [without["id"]])


class ListAndSummaryTests(FounderMcpCase):
    def test_filters_and_flagged_counts(self):
        timely = self.log("2030-05-10")["entry"]
        late = self.log("2030-05-15", date_paid="2030-04-10", vendor="Late Co", business_amount="40.00", allocation_note="")["entry"]
        other_year = self.mcp("log_founder_expense", "2031-01-05", dry_run=False, **self.sample(date_paid="2030-12-31", vendor="Year End"))["entry"]
        self.mcp("log_founder_expense", "2030-05-10", dry_run=False, **self.sample(owner_id=self.alice.pk, vendor="Alice Co"))
        ids = lambda **filters: sorted(row["id"] for row in self.mcp("list_founder_expenses", "2031-01-05", **filters)["founder_expenses"])
        self.assertEqual(ids(owner_id=self.owner_a.pk, flagged=True), [late["id"]])
        self.assertEqual(ids(owner_id=self.owner_a.pk, flagged=False), sorted([timely["id"], other_year["id"]]))
        self.assertEqual(len(ids(year_paid=2030)), 4)
        self.assertEqual(ids(year_paid=2030, owner_id=self.owner_a.pk), sorted([timely["id"], late["id"], other_year["id"]]))
        summary = self.mcp("founder_expense_summary", "2031-01-05", year_paid=2030, owner_id=self.owner_a.pk)["owners"][0]
        self.assertEqual((summary["count"], summary["total"], summary["business_total"], summary["flagged_count"], summary["late_count"]), (3, "120.00", "80.00", 1, 1))
        self.assertEqual(self.mcp("founder_expense_summary", "2031-01-05", year=2030, owner_id=self.owner_a.pk)["owners"][0], summary)
        self.assertIn("year_paid", self.mcp_error("founder_expense_summary", "2031-01-05"))
        self.assertIn("flagged", self.mcp_error("list_founder_expenses", "2031-01-05", flagged="yes"))


class CsvTests(FounderMcpCase):
    def run_import(self, body, commit=False, header=HEADER):
        with mock.patch("django.utils.timezone.now", return_value=noon("2030-05-10")):
            return founder.import_csv(user=self.user, upload=csv_file(header + body), commit=commit)

    def test_old_files_still_import_with_business_amount_defaulting_to_full(self):
        result = self.run_import("owner-a@example.com,2030-02-15,Sample SaaS,Plan,40.00,Tracking,N,\n", commit=True)
        self.assertEqual(result["counts"]["imported"], 1)
        expense = FounderExpense.objects.get()
        self.assertEqual((expense.business_amount, expense.source), (Decimal("40.00"), "csv_import"))

    def test_new_columns_and_their_rules(self):
        header = HEADER.strip() + ",business_amount,allocation_note\n"
        result = self.run_import("owner-a@example.com,2030-02-15,Sample SaaS,Plan,40.00,Tracking,N,,20.00,\nowner-a@example.com,2030-02-16,Sample SaaS,Plan,40.00,Tracking,N,,20.00,Half\n", header=header)
        self.assertEqual([bool(row["errors"]) for row in result["rows"]], [True, False])
        self.assertIn("allocation_note", result["rows"][0]["errors"][0])
        result = self.run_import("owner-a@example.com,2030-04-01,Sample SaaS,Plan,40.00,Tracking,N,,20.00,Half\n", header=header, commit=True)
        self.assertEqual(result["counts"]["imported"], 1)
        self.assertTrue(any("2030-05-01" in flag for flag in result["rows"][0]["flags"]))  # logged 05-10, deadline 05-01: late
        self.assertEqual(FounderExpense.objects.get().business_amount, Decimal("20.00"))

    def test_late_flag_in_preview_uses_the_deadline(self):
        result = self.run_import("owner-a@example.com,2030-04-01,Sample SaaS,Plan,40.00,Tracking,N,\nowner-a@example.com,2030-02-01,Sample SaaS,Plan,40.00,Tracking,N,\n")
        self.assertTrue(any("2030-05-01" in flag for flag in result["rows"][0]["flags"]))
        self.assertFalse(any("Late" in flag for flag in result["rows"][1]["flags"]))  # in the window, before log-by

    def test_export_keeps_its_columns_and_appends_the_new_ones(self):
        self.log("2030-05-10")
        self.client.force_login(self.user)
        lines = self.client.get(reverse("api-founder-expense-export"), {"year": 2030}).content.decode().splitlines()
        header = lines[1].split(",")
        self.assertEqual(header[:23], ["Id", "Owner", "Owner email", "Date paid", "Vendor", "Description", "Amount", "Category", "Category name", "Business purpose", "Property stays personal", "Has receipt", "Receipt link", "Receipt files", "Logged at", "Days to log", "Late", "In special window", "Special window late", "Approval reminder", "Source", "Status", "Withdrawn reason"])
        self.assertEqual(header[23:], ["Business amount", "Allocation note", "Deadline", "Timely"])
        self.assertTrue(lines[2].endswith("20.00,50% business,2030-05-30,Y"))


class LedgerIsolationTests(FounderMcpCase):
    snapshot = SeparationTests.snapshot

    def test_mcp_founder_tools_leave_every_report_unchanged(self):
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
        refs_before = TransactionImportRef.objects.count()
        ledger_audit_before = AuditEvent.objects.exclude(object_type__startswith="ledger.FounderExpense").count()

        today = "2026-09-20"
        entry = self.mcp("log_founder_expense", today, dry_run=False, **self.sample(date_paid="2026-08-10", category="6100", business_amount="40.00", allocation_note="", receipts=[receipt()]))["entry"]
        self.mcp("log_founder_expense", today, dry_run=False, **self.sample(date_paid="2026-07-01", owner_id=self.alice.pk, vendor="Sample Supply", amount="300.00", business_amount="150.00"))
        self.mcp("update_founder_expense", today, id=entry["id"], amount="45.00")
        self.mcp("attach_founder_expense_receipt", today, id=entry["id"], **receipt(PNG, "b.png", "image/png"))
        self.mcp("withdraw_founder_expense", today, id=entry["id"], reason="Duplicate")
        self.assertEqual(FounderExpense.objects.count(), 2)

        after = self.snapshot()
        for key in before:
            with self.subTest(report=key):
                self.assertEqual(before[key], after[key])
        self.assertEqual(TransactionImportRef.objects.count(), refs_before)
        self.assertEqual(AuditEvent.objects.exclude(object_type__startswith="ledger.FounderExpense").count(), ledger_audit_before)
        self.assertEqual((JournalEntry.objects.count(), JournalLine.objects.count()), (2, 4))


@override_settings(MEDIA_ROOT=MEDIA)
class BackfillMigrationTests(TransactionTestCase):
    before = [("ledger", "0010_transfer_import_refs")]
    after = [("ledger", "0011_founder_expense_business_amount_and_hashes")]

    def test_backfill_fills_new_columns_without_changing_existing_values(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.before)
        apps = executor.loader.project_state(self.before).apps
        Contact_, Expense, Receipt = (apps.get_model("ledger", name) for name in ("Contact", "FounderExpense", "FounderExpenseReceipt"))
        owner = Contact_.objects.create(name="Owner B", kind="owner")
        expense = Expense.objects.create(owner_contact=owner, date_paid=date(2026, 1, 5), vendor="Sample", description="Old", amount=Decimal("12.34"), business_purpose="Old entry")
        logged_at = Expense.objects.get(pk=expense.pk).logged_at
        present = Receipt.objects.create(expense=expense, file=SimpleUploadedFile("old.pdf", PDF, content_type="application/pdf"), original_name="old.pdf", size=len(PDF))
        missing = Receipt.objects.create(expense=expense, file="attachments/founder/2026/01/gone.pdf", original_name="gone.pdf", size=1)

        executor = MigrationExecutor(connection)
        executor.migrate(self.after)
        migrated = FounderExpense.objects.get(pk=expense.pk)
        self.assertEqual((migrated.amount, migrated.business_amount, migrated.allocation_note, migrated.logged_at), (Decimal("12.34"), Decimal("12.34"), "", logged_at))
        self.assertEqual(FounderExpenseReceipt.objects.get(pk=present.pk).sha256, hashlib.sha256(PDF).hexdigest())
        self.assertEqual(FounderExpenseReceipt.objects.get(pk=missing.pk).sha256, "")
        self.assertEqual(FounderExpenseSettings.load().standard_deadline_days, 30)
