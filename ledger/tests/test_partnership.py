import csv
import io
import json
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ledger.models import Account, AccountingPeriod, ApiToken, AuditEvent, Contact, JournalEntry, OwnerInterestPeriod, PartnershipSettings, ReservedInterestPeriod, UserProfile
from ledger.partnership import (
    capital_account_rollforward,
    delete_interest,
    end_interest,
    largest_remainder,
    load_periods,
    ownership_as_of,
    ownership_on,
    partner_allocation_report,
    pct,
    save_owner_interest,
    save_reserved_interest,
    save_settings,
    segments_for,
)
from ledger.reporting import income_statement, owner_balances
from ledger.services import create_entry, create_owner_activity, delete_entry


D = date.fromisoformat


class PartnershipTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="admin", password="very-good-password")
        cls.user.profile.role = UserProfile.Role.ADMIN
        cls.user.profile.save()
        cls.bank = Account.objects.create(code="1000", name="Checking", type=Account.Type.ASSET, subtype=Account.Subtype.BANK)
        cls.sales = Account.objects.create(code="4000", name="Sales", type=Account.Type.REVENUE, subtype=Account.Subtype.SALES)
        cls.rent = Account.objects.create(code="6000", name="Rent", type=Account.Type.EXPENSE, subtype=Account.Subtype.OPERATING_EXPENSE)
        cls.capital = Account.objects.create(code="3000", name="Owner contributions", type=Account.Type.EQUITY, subtype=Account.Subtype.OWNER_CONTRIBUTION)
        cls.draws = Account.objects.create(code="3100", name="Owner draws", type=Account.Type.EQUITY, subtype=Account.Subtype.OWNER_DRAW)
        cls.alice = Contact.objects.create(name="Alice", kind=Contact.Kind.OWNER)
        cls.bob = Contact.objects.create(name="Bob", kind=Contact.Kind.OWNER)
        cls.carol = Contact.objects.create(name="Carol", kind=Contact.Kind.OWNER)

    def interest(self, owner, start, percentage, end=None):
        return save_owner_interest(user=self.user, owner=owner, effective_start=D(start), effective_end=D(end) if end else None, percentage=percentage)

    def reserved(self, start, percentage, end=None, label="Reserved pool"):
        return save_reserved_interest(user=self.user, label=label, effective_start=D(start), effective_end=D(end) if end else None, percentage=percentage)

    def expense(self, day, amount):
        return create_entry(entry_date=D(day), description="Expense", user=self.user, lines=[
            {"account": self.rent, "debit": Decimal(amount), "credit": 0, "owner": None},
            {"account": self.bank, "debit": 0, "credit": Decimal(amount), "owner": None},
        ])

    def sale(self, day, amount):
        return create_entry(entry_date=D(day), description="Sale", user=self.user, lines=[
            {"account": self.bank, "debit": Decimal(amount), "credit": 0, "owner": None},
            {"account": self.sales, "debit": 0, "credit": Decimal(amount), "owner": None},
        ])

    def sample(self):
        """Alice, Bob, Carol 30% each with 10% reserved from March 1; Carol leaves August 14 and Alice and Bob take 45%."""
        self.interest(self.alice, "2026-03-01", "30", end="2026-08-14")
        self.interest(self.bob, "2026-03-01", "30", end="2026-08-14")
        self.interest(self.carol, "2026-03-01", "30", end="2026-08-14")
        self.reserved("2026-03-01", "10")
        self.interest(self.alice, "2026-08-15", "45")
        self.interest(self.bob, "2026-08-15", "45")

    def amounts(self, segment):
        return {row["name"]: row["amount"] for row in segment["allocations"]}

    def bounds(self, report):
        return [(row["start"], row["end"]) for row in report["segments"]]


class AcceptanceTests(PartnershipTestCase):
    def test_ownership_as_of_applies_the_unissued_policy(self):
        self.sample()
        may = {row["name"]: row for row in ownership_as_of(D("2026-05-01"))["owners"]}
        self.assertEqual({name: row["effective_pct"] for name, row in may.items()}, {"Alice": "33.3333", "Bob": "33.3333", "Carol": "33.3333"})
        self.assertEqual(may["Alice"]["stated_pct"], "30.0000")
        september = ownership_as_of(D("2026-09-01"))
        self.assertEqual({row["name"]: row["effective_pct"] for row in september["owners"]}, {"Alice": "50.0000", "Bob": "50.0000", "Carol": "0.0000"})
        self.assertEqual(september["reserved_pct"], "10.0000")
        self.assertEqual(september["total_issued_pct"], "90.0000")

    def test_two_segments_split_by_the_ownership_in_each(self):
        self.sample()
        self.expense("2026-04-10", "900.00")
        self.expense("2026-10-02", "600.00")
        report = partner_allocation_report(D("2026-03-01"), D("2026-12-31"))
        self.assertEqual(report["method"], "interim_closing")
        self.assertEqual(report["convention"], "calendar_day")
        self.assertEqual(len(report["segments"]), 2)
        first, second = report["segments"]
        self.assertEqual(first["net_income"], "-900.00")
        self.assertEqual(self.amounts(first), {"Alice": "-300.00", "Bob": "-300.00", "Carol": "-300.00"})
        self.assertEqual(second["net_income"], "-600.00")
        self.assertEqual(self.amounts(second), {"Alice": "-300.00", "Bob": "-300.00", "Carol": "0.00"})
        self.assertEqual(first["by_account"][0]["code"], "6000")
        self.assertEqual(first["by_account"][0]["balance"], "900.00")

    def test_owner_totals_tie_to_net_income_for_the_range(self):
        self.sample()
        self.expense("2026-04-10", "900.00")
        self.sale("2026-06-30", "1000.01")
        self.expense("2026-10-02", "600.00")
        self.sale("2026-12-31", "333.33")
        report = partner_allocation_report(D("2026-03-01"), D("2026-12-31"))
        total = sum((Decimal(row["amount"]) for row in report["totals_by_owner"]), Decimal("0"))
        self.assertEqual(total, income_statement(D("2026-03-01"), D("2026-12-31"))["net_income"])
        self.assertEqual(report["net_income"], f"{total:.2f}")
        self.assertEqual(report["unallocated"], "0.00")

    def test_overlapping_periods_for_one_owner_are_rejected(self):
        self.interest(self.alice, "2026-03-01", "30", end="2026-08-14")
        with self.assertRaisesMessage(ValidationError, "overlaps Alice's"):
            self.interest(self.alice, "2026-08-01", "45")
        with self.assertRaisesMessage(ValidationError, "overlaps Alice's"):
            self.interest(self.alice, "2026-01-01", "10")
        self.interest(self.alice, "2026-08-15", "45")
        self.assertEqual(OwnerInterestPeriod.objects.filter(owner_contact=self.alice).count(), 2)

    def test_more_than_100_percent_on_a_date_is_rejected(self):
        self.interest(self.alice, "2026-03-01", "50")
        self.interest(self.bob, "2026-03-01", "50", end="2026-06-30")
        with self.assertRaisesMessage(ValidationError, "would total 110.0000%"):
            self.reserved("2026-01-01", "10")
        with self.assertRaisesMessage(ValidationError, "On 2026-03-01"):
            self.reserved("2026-01-01", "10")
        self.reserved("2026-07-01", "10")
        with self.assertRaisesMessage(ValidationError, "over 100%"):
            self.interest(self.carol, "2026-06-01", "1")

    def test_capital_rollforward_ties_to_owner_balances_plus_allocations(self):
        self.sample()
        for owner, amount in [(self.alice, "1000.00"), (self.bob, "1000.00"), (self.carol, "1000.00")]:
            create_owner_activity(owner=owner, kind="contribution", activity_date=D("2026-03-01"), amount=amount, cash_account=self.bank, offset_account=self.capital, user=self.user)
        create_owner_activity(owner=self.bob, kind="draw", activity_date=D("2026-11-01"), amount="250.00", cash_account=self.bank, offset_account=self.draws, user=self.user)
        self.expense("2026-04-10", "900.00")
        self.expense("2026-10-02", "600.00")
        rows = {row["owner_name"]: row for row in capital_account_rollforward(D("2026-03-01"), D("2026-12-31"))["owners"]}
        balances = {row["owner"].name: row for row in owner_balances(D("2026-12-31"))}
        for name, row in rows.items():
            self.assertEqual(row["beginning"], "0.00")
            expected = balances[name]["contributions"] - balances[name]["draws"] + Decimal(row["allocated_income_loss"]) + Decimal(row["other"])
            self.assertEqual(Decimal(row["ending"]), expected, name)
        self.assertEqual(rows["Bob"]["draws"], "250.00")
        self.assertEqual(rows["Bob"]["ending"], "150.00")
        self.assertEqual(rows["Carol"]["ending"], "700.00")


class ValidationTests(PartnershipTestCase):
    def test_end_before_start_is_rejected(self):
        with self.assertRaisesMessage(ValidationError, "cannot be before the start"):
            self.interest(self.alice, "2026-03-01", "30", end="2026-02-28")

    def test_percentages_outside_0_to_100_or_too_precise_are_rejected(self):
        for value in ["-1", "100.0001", "abc", "12.34567"]:
            with self.assertRaises(ValidationError, msg=value):
                self.interest(self.alice, "2026-03-01", value)
        self.interest(self.alice, "2026-03-01", "33.3333")

    def test_only_owner_contacts_can_hold_interests(self):
        customer = Contact.objects.create(name="Customer", kind=Contact.Kind.CUSTOMER)
        with self.assertRaisesMessage(ValidationError, "kind owner"):
            self.interest(customer, "2026-03-01", "10")

    def test_editing_a_period_does_not_clash_with_itself(self):
        period = self.interest(self.alice, "2026-03-01", "60")
        save_owner_interest(user=self.user, owner=self.alice, effective_start=D("2026-03-01"), effective_end=None, percentage="100", period=period)
        period.refresh_from_db()
        self.assertEqual(period.percentage, Decimal("100.0000"))

    def test_ending_a_period_and_auditing_every_change(self):
        period = self.interest(self.alice, "2026-03-01", "60")
        end_interest(user=self.user, period=period, effective_end=D("2026-06-30"))
        period.refresh_from_db()
        self.assertEqual(period.effective_end, D("2026-06-30"))
        self.assertEqual(period.created_by, self.user)
        self.assertEqual(period.updated_by, self.user)
        with self.assertRaisesMessage(ValidationError, "cannot be before the start"):
            end_interest(user=self.user, period=period, effective_end=D("2026-02-01"))
        delete_interest(user=self.user, period=period)
        actions = list(AuditEvent.objects.filter(object_type="ledger.OwnerInterestPeriod").order_by("created_at", "pk").values_list("action", flat=True))
        self.assertEqual(actions, ["created", "ended", "deleted"])

    def test_reserved_pools_with_different_labels_may_overlap(self):
        self.reserved("2026-01-01", "10", label="Option pool")
        self.reserved("2026-01-01", "5", label="Profits pool")
        with self.assertRaisesMessage(ValidationError, "overlaps the Option pool"):
            self.reserved("2026-06-01", "5", label="option pool")

    def test_settings_reject_unknown_choices_and_are_audited(self):
        with self.assertRaises(ValidationError):
            save_settings(user=self.user, convention="weekly")
        save_settings(user=self.user, convention="monthly", written_statement="Interim closing, monthly convention.", written_statement_date=D("2026-12-31"))
        found = PartnershipSettings.load()
        self.assertEqual(found.convention, "monthly")
        self.assertEqual(found.allocation_method, "interim_closing")
        self.assertTrue(AuditEvent.objects.filter(object_type="ledger.PartnershipSettings", action="updated").exists())


class SegmentTests(PartnershipTestCase):
    def test_a_single_owner_year_is_one_segment(self):
        self.interest(self.alice, "2020-01-01", "100")
        self.sale("2026-05-05", "100.00")
        report = partner_allocation_report(D("2026-01-01"), D("2026-12-31"))
        self.assertEqual(self.bounds(report), [("2026-01-01", "2026-12-31")])
        self.assertEqual(self.amounts(report["segments"][0]), {"Alice": "100.00"})

    def test_a_year_without_changes_is_one_segment_even_when_periods_are_split(self):
        self.interest(self.alice, "2025-01-01", "50", end="2026-06-30")
        self.interest(self.alice, "2026-07-01", "50")
        self.interest(self.bob, "2025-01-01", "50")
        report = partner_allocation_report(D("2026-01-01"), D("2026-12-31"))
        self.assertEqual(self.bounds(report), [("2026-01-01", "2026-12-31")])

    def change_on(self, day):
        """Alice owns everything, then from `day` Alice and Bob own half each."""
        self.interest(self.alice, "2026-01-01", "100", end=(D(day) - timedelta(days=1)).isoformat())
        self.interest(self.alice, day, "50")
        self.interest(self.bob, day, "50")

    def second_segment_start(self, convention, belongs_to="outgoing"):
        segments = segments_for(D("2026-01-01"), D("2026-12-31"), convention, belongs_to)
        self.assertEqual(len(segments), 2)
        return segments[1].start.isoformat()

    def test_conventions_place_changes_on_the_1st_15th_16th_and_last_day(self):
        # Keyed by the new percentages' effective_start; the variation date is the day before
        # (the outgoing interest's last day), and conventions are applied to the variation date.
        expected = {
            # effective_start: (calendar day outgoing, calendar day incoming, semi-monthly, monthly)
            "2026-05-01": ("2026-05-01", "2026-04-30", "2026-04-16", "2026-05-01"),  # variation 04-30
            "2026-05-15": ("2026-05-15", "2026-05-14", "2026-05-01", "2026-05-01"),  # variation 05-14
            "2026-05-16": ("2026-05-16", "2026-05-15", "2026-05-01", "2026-05-01"),  # variation 05-15
            "2026-05-17": ("2026-05-17", "2026-05-16", "2026-05-16", "2026-06-01"),  # variation 05-16
            "2026-05-31": ("2026-05-31", "2026-05-30", "2026-05-16", "2026-06-01"),  # variation 05-30
            "2026-02-28": ("2026-02-28", "2026-02-27", "2026-02-16", "2026-03-01"),  # variation 02-27
        }
        for day, (outgoing, incoming, semi, monthly) in expected.items():
            with self.subTest(day=day):
                OwnerInterestPeriod.objects.all().delete()
                self.change_on(day)
                self.assertEqual(self.second_segment_start("calendar_day", "outgoing"), outgoing)
                self.assertEqual(self.second_segment_start("calendar_day", "incoming"), incoming)
                self.assertEqual(self.second_segment_start("semi_monthly"), semi)
                self.assertEqual(self.second_segment_start("monthly"), monthly)
                # change_day_belongs_to only matters under the calendar day convention.
                self.assertEqual(self.second_segment_start("semi_monthly", "incoming"), semi)
                self.assertEqual(self.second_segment_start("monthly", "incoming"), monthly)

    def test_a_december_change_under_the_monthly_convention_moves_past_year_end(self):
        # Bob's change on the 20th is deemed to happen January 1, so he holds nothing in this year.
        self.change_on("2026-12-20")
        report = partner_allocation_report(D("2026-01-01"), D("2026-12-31"), convention="monthly")
        self.assertEqual(self.bounds(report), [("2026-01-01", "2026-12-31")])
        self.assertEqual({row["name"]: row["stated_pct"] for row in report["segments"][0]["allocations"]}, {"Alice": "100.0000"})

    def test_activity_on_the_variation_date_follows_change_day_belongs_to(self):
        self.change_on("2026-07-01")  # Alice's 100% ends 06-30, the variation date
        self.sale("2026-06-30", "100.00")
        self.sale("2026-07-01", "10.00")
        outgoing = partner_allocation_report(D("2026-01-01"), D("2026-12-31"), change_day_belongs_to="outgoing")
        self.assertEqual(self.bounds(outgoing), [("2026-01-01", "2026-06-30"), ("2026-07-01", "2026-12-31")])
        self.assertEqual(self.amounts(outgoing["segments"][0]), {"Alice": "100.00", "Bob": "0.00"})
        self.assertEqual(self.amounts(outgoing["segments"][1]), {"Alice": "5.00", "Bob": "5.00"})
        save_settings(user=self.user, change_day_belongs_to="incoming")
        incoming = partner_allocation_report(D("2026-01-01"), D("2026-12-31"))
        self.assertEqual(incoming["change_day_belongs_to"], "incoming")
        self.assertEqual(self.bounds(incoming), [("2026-01-01", "2026-06-29"), ("2026-06-30", "2026-12-31")])
        self.assertEqual(incoming["segments"][0]["net_income"], "0.00")
        self.assertEqual(self.amounts(incoming["segments"][1]), {"Alice": "55.00", "Bob": "55.00"})

    def test_changes_collapsed_by_a_convention_keep_the_latest_ownership(self):
        self.interest(self.alice, "2026-01-01", "100", end="2026-05-02")
        self.interest(self.alice, "2026-05-03", "60", end="2026-05-09")
        self.interest(self.bob, "2026-05-03", "40", end="2026-05-09")
        self.interest(self.alice, "2026-05-10", "50")
        self.interest(self.bob, "2026-05-10", "50")
        report = partner_allocation_report(D("2026-01-01"), D("2026-12-31"), convention="semi_monthly")
        self.assertEqual(self.bounds(report), [("2026-01-01", "2026-04-30"), ("2026-05-01", "2026-12-31")])
        self.assertEqual({row["name"]: row["stated_pct"] for row in report["segments"][1]["allocations"]}, {"Alice": "50.0000", "Bob": "50.0000"})
        # Both variations (05-02 and 05-09) were folded onto the 05-01 deemed start.
        self.assertEqual(report["segments"][1]["change_dates"], ["2026-05-02", "2026-05-09"])

    def test_first_ownership_starts_on_its_own_date(self):
        self.interest(self.alice, "2026-03-16", "100")
        self.sale("2026-03-16", "10.00")
        for convention in ["calendar_day", "semi_monthly", "monthly"]:
            report = partner_allocation_report(D("2026-03-16"), D("2026-12-31"), convention=convention)
            self.assertEqual(self.bounds(report), [("2026-03-16", "2026-12-31")], convention)
            self.assertEqual(report["unallocated"], "0.00")


class AllocationTests(PartnershipTestCase):
    def test_a_segment_with_no_activity_allocates_zero(self):
        self.sample()
        self.expense("2026-04-10", "900.00")
        second = partner_allocation_report(D("2026-03-01"), D("2026-12-31"))["segments"][1]
        self.assertEqual(second["net_income"], "0.00")
        self.assertEqual(second["by_account"], [])
        self.assertEqual(set(self.amounts(second).values()), {"0.00"})

    def test_net_income_and_net_loss_segments(self):
        self.sample()
        self.sale("2026-04-01", "1200.00")
        self.expense("2026-04-02", "300.00")
        self.expense("2026-09-01", "1000.00")
        self.sale("2026-09-02", "400.00")
        first, second = partner_allocation_report(D("2026-03-01"), D("2026-12-31"))["segments"]
        self.assertEqual(first["net_income"], "900.00")
        self.assertEqual(self.amounts(first), {"Alice": "300.00", "Bob": "300.00", "Carol": "300.00"})
        self.assertEqual(second["net_income"], "-600.00")
        self.assertEqual(self.amounts(second), {"Alice": "-300.00", "Bob": "-300.00", "Carol": "0.00"})

    def test_100_dollars_three_ways_sums_exactly(self):
        thirds = {key: Fraction(1, 3) for key in "abc"}
        self.assertEqual(largest_remainder(10000, thirds), {"a": 3334, "b": 3333, "c": 3333})
        self.assertEqual(largest_remainder(-10000, thirds), {"a": -3334, "b": -3333, "c": -3333})
        self.interest(self.alice, "2026-01-01", "30")
        self.interest(self.bob, "2026-01-01", "30")
        self.interest(self.carol, "2026-01-01", "30")
        self.sale("2026-02-01", "100.00")
        report = partner_allocation_report(D("2026-01-01"), D("2026-12-31"))
        amounts = [Decimal(row["amount"]) for row in report["totals_by_owner"]]
        self.assertEqual(sum(amounts), Decimal("100.00"))
        self.assertEqual(sorted(amounts), [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")])

    def test_leftover_cents_go_to_the_largest_remainders(self):
        shares = largest_remainder(101, {"big": Fraction(2, 3), "small": Fraction(1, 3)})
        self.assertEqual(shares, {"big": 67, "small": 34})
        self.assertEqual(largest_remainder(1, {"a": Fraction(0), "b": Fraction(1)}), {"a": 0, "b": 1})

    def test_reserved_pool_at_zero_and_at_ten_percent(self):
        self.interest(self.alice, "2026-01-01", "60")
        self.interest(self.bob, "2026-01-01", "30")
        self.reserved("2026-01-01", "0", end="2026-06-30")
        self.reserved("2026-07-01", "10")
        self.assertEqual(ownership_as_of(D("2026-03-01"))["reserved_pct"], "0.0000")
        self.assertEqual(ownership_as_of(D("2026-08-01"))["reserved_pct"], "10.0000")
        for day in ["2026-03-01", "2026-08-01"]:
            effective = {row["name"]: row["effective_pct"] for row in ownership_as_of(D(day))["owners"]}
            self.assertEqual(effective, {"Alice": "66.6667", "Bob": "33.3333"})

    def test_an_owner_at_zero_percent_gets_nothing(self):
        self.interest(self.alice, "2026-01-01", "100")
        self.interest(self.carol, "2026-01-01", "0")
        self.sale("2026-02-01", "99.99")
        report = partner_allocation_report(D("2026-01-01"), D("2026-12-31"))
        self.assertEqual({row["name"]: row["amount"] for row in report["totals_by_owner"]}, {"Alice": "99.99", "Carol": "0.00"})

    def test_deleted_transactions_are_excluded_and_closed_months_included(self):
        self.interest(self.alice, "2026-01-01", "100")
        self.sale("2026-01-15", "40.00")
        delete_entry(entry=self.sale("2026-01-20", "1000.00"), user=self.user)
        AccountingPeriod.objects.create(year=2026, month=1, closed_at=timezone.now())
        report = partner_allocation_report(D("2026-01-01"), D("2026-12-31"))
        self.assertEqual(report["net_income"], "40.00")
        self.assertEqual(report["totals_by_owner"][0]["amount"], "40.00")

    def test_days_without_owners_are_reported_as_unallocated(self):
        self.interest(self.alice, "2026-03-01", "100")
        self.sale("2026-02-01", "50.00")
        self.sale("2026-04-01", "10.00")
        report = partner_allocation_report(D("2026-01-01"), D("2026-12-31"))
        self.assertEqual(self.bounds(report), [("2026-01-01", "2026-02-28"), ("2026-03-01", "2026-12-31")])
        self.assertEqual(report["unallocated"], "50.00")
        self.assertEqual(report["totals_by_owner"], [{"owner_id": self.alice.pk, "name": "Alice", "amount": "10.00"}])
        self.assertTrue(report["warnings"])

    def test_proration_spreads_income_over_days(self):
        self.change_on_july_1()
        self.sale("2026-01-10", "365.00")
        report = partner_allocation_report(D("2026-01-01"), D("2026-12-31"), method="proration")
        first, second = report["segments"]
        self.assertEqual((first["days"], second["days"]), (181, 184))
        self.assertEqual((first["allocated_income"], second["allocated_income"]), ("181.00", "184.00"))
        self.assertEqual(self.amounts(second), {"Alice": "92.00", "Bob": "92.00"})
        self.assertEqual({row["name"]: row["amount"] for row in report["totals_by_owner"]}, {"Alice": "273.00", "Bob": "92.00"})

    def change_on_july_1(self):
        self.interest(self.alice, "2026-01-01", "100", end="2026-06-30")
        self.interest(self.alice, "2026-07-01", "50")
        self.interest(self.bob, "2026-07-01", "50")

    def test_reports_never_post_entries(self):
        self.sample()
        self.expense("2026-04-10", "900.00")
        before = JournalEntry.objects.count(), AuditEvent.objects.count()
        partner_allocation_report(D("2026-01-01"), D("2026-12-31"))
        capital_account_rollforward(D("2026-01-01"), D("2026-12-31"))
        ownership_as_of(D("2026-05-01"))
        self.assertEqual((JournalEntry.objects.count(), AuditEvent.objects.count()), before)


class RollforwardTests(PartnershipTestCase):
    def test_forfeiture_between_contribution_accounts_is_an_other_movement(self):
        self.sample()
        for owner in [self.alice, self.bob, self.carol]:
            create_owner_activity(owner=owner, kind="contribution", activity_date=D("2026-03-01"), amount="900.00", cash_account=self.bank, offset_account=self.capital, user=self.user)
        # A journal entry needs two different accounts, so forfeitures move through a second capital account.
        forfeited = Account.objects.create(code="3010", name="Forfeited capital", type=Account.Type.EQUITY, subtype=Account.Subtype.OWNER_CONTRIBUTION)
        create_entry(entry_date=D("2026-08-15"), description="Carol forfeits her capital", user=self.user, lines=[
            {"account": self.capital, "debit": Decimal("900.00"), "credit": 0, "owner": self.carol},
            {"account": forfeited, "debit": 0, "credit": Decimal("450.00"), "owner": self.alice},
            {"account": forfeited, "debit": 0, "credit": Decimal("450.00"), "owner": self.bob},
        ])
        rows = {row["owner_name"]: row for row in capital_account_rollforward(D("2026-03-01"), D("2026-12-31"))["owners"]}
        self.assertEqual((rows["Carol"]["contributions"], rows["Carol"]["other"], rows["Carol"]["ending"]), ("900.00", "-900.00", "0.00"))
        self.assertEqual((rows["Alice"]["contributions"], rows["Alice"]["other"], rows["Alice"]["ending"]), ("900.00", "450.00", "1350.00"))

    def test_beginning_balance_carries_earlier_years_including_their_allocations(self):
        self.interest(self.alice, "2025-01-01", "50")
        self.interest(self.bob, "2025-01-01", "50")
        create_owner_activity(owner=self.alice, kind="contribution", activity_date=D("2025-01-02"), amount="500.00", cash_account=self.bank, offset_account=self.capital, user=self.user)
        self.sale("2025-06-01", "200.00")
        self.sale("2026-06-01", "100.00")
        report = capital_account_rollforward(D("2026-01-01"), D("2026-12-31"))
        rows = {row["owner_name"]: row for row in report["owners"]}
        self.assertEqual(rows["Alice"], {"owner_id": self.alice.pk, "owner_name": "Alice", "beginning": "600.00", "contributions": "0.00", "draws": "0.00", "allocated_income_loss": "50.00", "other": "0.00", "ending": "650.00"})
        self.assertEqual((rows["Bob"]["beginning"], rows["Bob"]["ending"]), ("100.00", "150.00"))
        only_bob = capital_account_rollforward(D("2026-01-01"), D("2026-12-31"), owner_id=self.bob.pk)["owners"]
        self.assertEqual([row["owner_name"] for row in only_bob], ["Bob"])

    def test_owner_loans_are_not_capital(self):
        loans = Account.objects.create(code="2500", name="Owner loans", type=Account.Type.LIABILITY, subtype=Account.Subtype.OWNER_LOAN_PAYABLE)
        self.interest(self.alice, "2026-01-01", "100")
        create_owner_activity(owner=self.alice, kind="loan_in", activity_date=D("2026-02-01"), amount="5000.00", cash_account=self.bank, offset_account=loans, user=self.user)
        row = capital_account_rollforward(D("2026-01-01"), D("2026-12-31"))["owners"][0]
        self.assertEqual(row["ending"], "0.00")


class CoverageWarningTests(PartnershipTestCase):
    """Warnings for days whose recorded interests leave income unallocated or do not add up to 100%."""

    def raw_interest(self, owner, start, percentage, end=None):
        # Bypasses the save-time 100% check, as imported or hand-edited records can.
        return OwnerInterestPeriod.objects.create(owner_contact=owner, effective_start=D(start), effective_end=D(end) if end else None, percentage=Decimal(percentage))

    def warnings(self, start, end):
        return partner_allocation_report(D(start), D(end))["warnings"]

    def test_empty_table_warns_for_the_whole_range_even_with_no_income(self):
        report = partner_allocation_report(D("2025-01-01"), D("2025-12-31"))
        self.assertEqual(report["unallocated"], "0.00")
        self.assertEqual(report["warnings"], ["No ownership interests on file for 2025-01-01 to 2025-12-31 (365 days); income for these days is unallocated."])

    def test_interests_starting_mid_range_warn_for_the_uncovered_prefix_only(self):
        self.interest(self.alice, "2025-03-15", "100")
        self.assertEqual(self.warnings("2025-01-01", "2025-12-31"), ["No ownership interests on file for 2025-01-01 to 2025-03-14 (73 days); income for these days is unallocated."])

    def test_a_gap_in_the_middle_is_named(self):
        self.interest(self.alice, "2026-01-01", "100", end="2026-03-31")
        self.interest(self.alice, "2026-05-01", "100")
        self.assertEqual(self.warnings("2026-01-01", "2026-12-31"), ["No ownership interests on file for 2026-04-01 to 2026-04-30 (30 days); income for these days is unallocated."])

    def test_interests_ending_before_the_range_ends_warn_for_the_suffix(self):
        self.interest(self.alice, "2026-01-01", "100", end="2026-09-30")
        self.assertEqual(self.warnings("2026-01-01", "2026-12-31"), ["No ownership interests on file for 2026-10-01 to 2026-12-31 (92 days); income for these days is unallocated."])

    def test_uncovered_days_with_income_also_report_the_amount(self):
        self.interest(self.alice, "2026-03-01", "100")
        self.sale("2026-02-01", "50.00")
        warnings = self.warnings("2026-01-01", "2026-12-31")
        self.assertEqual(warnings[0], "No ownership interests on file for 2026-01-01 to 2026-02-28 (59 days); income for these days is unallocated.")
        self.assertIn("50.00 of net income", warnings[1])

    def test_exactly_100_percent_coverage_has_no_warnings(self):
        self.sample()
        self.interest(self.alice, "2026-01-01", "50", end="2026-02-28")
        self.interest(self.bob, "2026-01-01", "50", end="2026-02-28")
        self.assertEqual(self.warnings("2026-01-01", "2026-12-31"), [])

    def test_open_ended_final_period_covers_every_later_day(self):
        self.interest(self.alice, "2026-01-01", "100", end="2026-06-30")
        self.interest(self.alice, "2026-07-01", "60")
        self.interest(self.bob, "2026-07-01", "40")
        self.assertEqual(self.warnings("2026-01-01", "2035-12-31"), [])

    def test_overlap_over_100_percent_names_the_dates(self):
        self.interest(self.alice, "2026-01-01", "60")
        self.interest(self.bob, "2026-01-01", "40")
        self.raw_interest(self.carol, "2026-06-01", "10", end="2026-06-10")
        self.assertEqual(self.warnings("2026-01-01", "2026-12-31"), [
            "Issued and reserved interests total 110.0000% for 2026-06-01 to 2026-06-10 (10 days), which is over 100%; check the ownership periods on file for these dates. Income is allocated by each owner's share of issued interests.",
        ])

    def test_reserved_interests_count_toward_the_overlap(self):
        self.interest(self.alice, "2026-01-01", "95")
        ReservedInterestPeriod.objects.create(label="Pool", effective_start=D("2026-03-01"), effective_end=D("2026-03-01"), percentage=Decimal("10"))
        self.assertEqual(self.warnings("2026-01-01", "2026-12-31")[1], "Issued and reserved interests total 105.0000% on 2026-03-01 (1 day), which is over 100%; check the ownership periods on file for these dates. Income is allocated by each owner's share of issued interests.")

    def test_under_100_percent_names_the_gap(self):
        self.interest(self.alice, "2026-01-01", "45")
        self.interest(self.bob, "2026-01-01", "45")
        self.assertEqual(self.warnings("2026-01-01", "2026-12-31"), [
            "Issued and reserved interests total 90.0000% for 2026-01-01 to 2026-12-31 (365 days), leaving 10.0000% unaccounted for; income for these days is shared by the issued owners in proportion to their holdings.",
        ])

    def test_single_day_range_on_a_change_date(self):
        self.change_on_july_1_with_a_gap_before()
        self.assertEqual(self.warnings("2026-07-01", "2026-07-01"), [])
        self.assertEqual(self.warnings("2026-06-30", "2026-06-30"), ["No ownership interests on file on 2026-06-30 (1 day); income for these days is unallocated."])
        self.assertEqual(self.warnings("2026-03-01", "2026-03-01"), [])

    def change_on_july_1_with_a_gap_before(self):
        self.interest(self.alice, "2026-03-01", "100", end="2026-06-29")
        self.interest(self.alice, "2026-07-01", "50")
        self.interest(self.bob, "2026-07-01", "50")

    def test_rollforward_carries_the_coverage_warnings(self):
        self.interest(self.alice, "2026-03-01", "100")
        report = capital_account_rollforward(D("2026-01-01"), D("2026-12-31"))
        self.assertEqual(report["warnings"], ["No ownership interests on file for 2026-01-01 to 2026-02-28 (59 days); income for these days is unallocated."])

    def test_rollforward_with_no_interests_warns_for_the_range(self):
        report = capital_account_rollforward(D("2025-01-01"), D("2025-12-31"))
        self.assertEqual(report["warnings"], ["No ownership interests on file for 2025-01-01 to 2025-12-31 (365 days); income for these days is unallocated."])

    def test_warnings_change_no_records(self):
        self.interest(self.alice, "2026-03-01", "100")
        before = OwnerInterestPeriod.objects.count(), AuditEvent.objects.count(), JournalEntry.objects.count()
        self.warnings("2026-01-01", "2026-12-31")
        capital_account_rollforward(D("2026-01-01"), D("2026-12-31"))
        self.assertEqual((OwnerInterestPeriod.objects.count(), AuditEvent.objects.count(), JournalEntry.objects.count()), before)


class ApiTests(PartnershipTestCase):
    def setUp(self):
        self.client.force_login(self.user)

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    def test_admins_add_edit_end_and_delete_owner_periods(self):
        created = self.post(reverse("api-owner-interests"), {"owner_id": self.alice.pk, "effective_start": "2026-03-01", "percentage": "30", "source_document": "Operating Agreement v2"})
        self.assertEqual(created.status_code, 201, created.content)
        period = created.json()["period"]
        self.assertEqual((period["percentage"], period["effective_end"], period["source_document"]), ("30.0000", None, "Operating Agreement v2"))
        edited = self.post(reverse("api-owner-interest", args=[period["id"]]), {"effective_start": "2026-03-01", "percentage": "35", "notes": "Corrected"})
        self.assertEqual(edited.json()["period"]["percentage"], "35.0000")
        ended = self.post(reverse("api-interest-end", args=["owner", period["id"]]), {"effective_end": "2026-08-14"})
        self.assertEqual(ended.json()["period"]["effective_end"], "2026-08-14")
        listed = self.client.get(reverse("api-owner-interests"), {"owner_id": self.alice.pk}).json()["periods"]
        self.assertEqual([row["id"] for row in listed], [period["id"]])
        self.assertEqual(self.client.delete(reverse("api-owner-interest", args=[period["id"]])).status_code, 200)
        self.assertFalse(OwnerInterestPeriod.objects.exists())

    def test_validation_errors_come_back_by_field(self):
        self.post(reverse("api-owner-interests"), {"owner_id": self.alice.pk, "effective_start": "2026-03-01", "percentage": "30"})
        response = self.post(reverse("api-owner-interests"), {"owner_id": self.alice.pk, "effective_start": "2026-05-01", "percentage": "30"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("overlaps", response.json()["fields"]["effective_start"][0])
        response = self.post(reverse("api-reserved-interests"), {"label": "Pool", "effective_start": "2026-05-01", "percentage": "71"})
        self.assertIn("over 100%", response.json()["fields"]["percentage"][0])

    def test_viewers_and_bookkeepers_can_read_but_not_change_ownership(self):
        bookkeeper = get_user_model().objects.create_user(username="books", password="very-good-password")
        bookkeeper.profile.role = UserProfile.Role.BOOKKEEPER
        bookkeeper.profile.save()
        self.client.force_login(bookkeeper)
        self.assertEqual(self.client.get(reverse("api-owner-interests")).status_code, 200)
        self.assertEqual(self.post(reverse("api-owner-interests"), {"owner_id": self.alice.pk, "effective_start": "2026-03-01", "percentage": "30"}).status_code, 403)
        self.assertEqual(self.post(reverse("api-partnership-settings"), {"convention": "monthly"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("api-partnership-settings")).json()["settings"]["convention"], "calendar_day")

    def test_settings_round_trip(self):
        response = self.post(reverse("api-partnership-settings"), {"allocation_method": "proration", "written_statement": "We elect...", "written_statement_date": "2027-03-15"})
        settings = response.json()["settings"]
        self.assertEqual((settings["allocation_method"], settings["convention"], settings["written_statement_date"]), ("proration", "calendar_day", "2027-03-15"))
        self.assertEqual(self.post(reverse("api-partnership-settings"), {"convention": "weekly"}).status_code, 400)

    def test_reports_and_csv_export(self):
        self.sample()
        self.expense("2026-04-10", "900.00")
        self.expense("2026-10-02", "600.00")
        report = self.client.get(reverse("api-report-partner-allocation"), {"start": "2026-03-01", "end": "2026-12-31"}).json()
        self.assertEqual((report["title"], len(report["segments"])), ("Partner allocation", 2))
        rollforward = self.client.get(reverse("api-report-capital-rollforward"), {"start": "2026-03-01", "end": "2026-12-31"}).json()
        self.assertEqual([row["ending"] for row in rollforward["owners"]], ["-600.00", "-600.00", "-300.00"])
        export = self.client.get(reverse("api-report-csv", args=["partner-allocation"]), {"start": "2026-03-01", "end": "2026-12-31"})
        self.assertEqual(export["Content-Type"], "text/csv")
        rows = list(csv.reader(io.StringIO(export.content.decode())))
        self.assertEqual(rows[0][:3], ["Segment start", "Segment end", "Days"])
        self.assertIn(["2026-08-15", "2026-12-31", "139", "-600.00", "-600.00", "Carol", str(self.carol.pk), "0.0000", "0.0000", "0.00"], rows)
        export = self.client.get(reverse("api-report-csv", args=["capital-rollforward"]), {"start": "2026-03-01", "end": "2026-12-31"})
        self.assertIn("Alice", export.content.decode())
        self.assertEqual(self.client.get(reverse("api-report-csv", args=["trial-balance"])).status_code, 404)


class McpTests(PartnershipTestCase):
    def setUp(self):
        viewer = get_user_model().objects.create_user(username="viewer", password="very-good-password")
        _, self.secret = ApiToken.issue(user=viewer, name="Tax agent")

    def call(self, name, **arguments):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        response = self.client.post(reverse("mcp"), json.dumps(body), content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.secret}")
        return response.json()["result"]

    def test_viewers_get_the_read_only_tools_and_no_write_tools(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        tools = {item["name"]: item for item in self.client.post(reverse("mcp"), json.dumps(body), content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {self.secret}").json()["result"]["tools"]}
        for name in ["list_owner_interests", "ownership_as_of", "partner_allocation_report", "capital_account_rollforward"]:
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"], name)
        self.assertFalse([name for name in tools if "interest" in name and name != "list_owner_interests"])

    def test_tools_answer_the_acceptance_questions(self):
        self.sample()
        self.expense("2026-04-10", "900.00")
        self.expense("2026-10-02", "600.00")
        interests = self.call("list_owner_interests")["structuredContent"]
        self.assertEqual(len(interests["owner_interests"]), 5)
        self.assertEqual(interests["reserved_interests"][0]["percentage"], "10.0000")
        alice = self.call("list_owner_interests", owner_id=self.alice.pk, include_reserved=False)["structuredContent"]
        self.assertEqual([row["percentage"] for row in alice["owner_interests"]], ["30.0000", "45.0000"])
        self.assertNotIn("reserved_interests", alice)
        september = self.call("ownership_as_of", date="2026-09-01")["structuredContent"]
        self.assertEqual({row["name"]: row["effective_pct"] for row in september["owners"]}, {"Alice": "50.0000", "Bob": "50.0000", "Carol": "0.0000"})
        report = self.call("partner_allocation_report", start="2026-03-01", end="2026-12-31")["structuredContent"]
        self.assertEqual([segment["net_income"] for segment in report["segments"]], ["-900.00", "-600.00"])
        monthly = self.call("partner_allocation_report", start="2026-03-01", end="2026-12-31", convention="monthly")["structuredContent"]
        self.assertEqual(monthly["segments"][1]["start"], "2026-08-01")  # a change on the 15th is deemed to occur on the 1st
        rollforward = self.call("capital_account_rollforward", start="2026-03-01", end="2026-12-31", owner_id=self.carol.pk)["structuredContent"]
        self.assertEqual(rollforward["owners"][0]["ending"], "-300.00")

    def test_bad_arguments_are_tool_errors(self):
        self.assertTrue(self.call("ownership_as_of", date="May 1")["isError"])
        self.assertTrue(self.call("partner_allocation_report", start="2026-12-31", end="2026-01-01")["isError"])
        self.assertTrue(self.call("partner_allocation_report", start="2026-01-01", end="2026-12-31", method="equal")["isError"])
        customer = Contact.objects.create(name="Customer", kind=Contact.Kind.CUSTOMER)
        self.assertIn("not an owner", self.call("capital_account_rollforward", start="2026-01-01", end="2026-12-31", owner_id=customer.pk)["content"][0]["text"])

    def test_existing_tools_return_the_same_output_with_ownership_recorded(self):
        create_owner_activity(owner=self.alice, kind="contribution", activity_date=D("2026-03-01"), amount="1000.00", cash_account=self.bank, offset_account=self.capital, user=self.user)
        self.expense("2026-04-10", "900.00")
        self.sale("2026-10-02", "600.00")
        calls = [
            ("list_accounts", {}),
            ("list_contacts", {}),
            ("search_transactions", {}),
            ("categorization_history", {"descriptions": ["Expense", "Sale"]}),
            ("general_ledger", {"start": "2026-01-01", "end": "2026-12-31"}),
            ("owner_balances", {"as_of": "2026-12-31"}),
            ("trial_balance", {"as_of": "2026-12-31"}),
            ("income_statement", {"start": "2026-01-01", "end": "2026-12-31"}),
            ("balance_sheet", {"as_of": "2026-12-31"}),
        ]
        before = {name: self.call(name, **arguments) for name, arguments in calls}
        self.sample()
        save_settings(user=self.user, allocation_method="proration", convention="monthly")
        partner_allocation_report(D("2026-01-01"), D("2026-12-31"))
        after = {name: self.call(name, **arguments) for name, arguments in calls}
        for name, _ in calls:
            self.assertEqual(after[name], before[name], name)


class PageRouteTests(PartnershipTestCase):
    def test_the_partnership_settings_page_is_served(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/settings/partnership/").status_code, 200)


class VariationDateTests(PartnershipTestCase):
    """Segment boundaries sit at the end of the variation date: the last day of the outgoing interest."""

    YEAR = (D("2025-01-01"), D("2025-12-31"))

    def sample_2025(self):
        """A, B, C 30% each Jan 1 - Jun 30 with 10% reserved; from Jul 1 A and B 45% each, C nothing."""
        for owner in [self.alice, self.bob, self.carol]:
            self.interest(owner, "2025-01-01", "30", end="2025-06-30")
        self.reserved("2025-01-01", "10")
        self.interest(self.alice, "2025-07-01", "45")
        self.interest(self.bob, "2025-07-01", "45")

    def report(self, start=None, end=None, **options):
        return partner_allocation_report(start or self.YEAR[0], end or self.YEAR[1], **options)

    def ownership_by_day(self, start, end, convention="calendar_day", belongs_to="outgoing"):
        days = {}
        for segment in segments_for(start, end, convention, belongs_to):
            day = segment.start
            while day <= segment.end:
                days[day] = segment.ownership
                day += timedelta(days=1)
        return days

    def assert_matches_ownership_on_every_day(self, start, end):
        owner_periods, reserved_periods = load_periods()
        by_day = self.ownership_by_day(start, end)
        self.assertEqual(min(by_day), start)
        self.assertEqual(max(by_day), end)
        self.assertEqual(len(by_day), (end - start).days + 1)
        for day, ownership in by_day.items():
            self.assertEqual(ownership, ownership_on(day, owner_periods, reserved_periods), day)

    # -- the reported case -------------------------------------------------------

    def test_segments_close_at_the_end_of_the_variation_date(self):
        self.sample_2025()
        report = self.report(change_day_belongs_to="outgoing")
        self.assertEqual(self.bounds(report), [("2025-01-01", "2025-06-30"), ("2025-07-01", "2025-12-31")])
        self.assertEqual([segment["days"] for segment in report["segments"]], [181, 184])
        self.assertEqual(report["segments"][0]["change_dates"], [])
        self.assertEqual(report["segments"][1]["change_dates"], ["2025-06-30"])

    def test_income_on_either_side_of_the_variation_goes_to_who_owned_that_day(self):
        self.sample_2025()
        self.sale("2025-07-01", "300.00")
        july = self.report()
        self.assertEqual({row["name"]: row["amount"] for row in july["totals_by_owner"]}, {"Alice": "150.00", "Bob": "150.00", "Carol": "0.00"})
        self.assertEqual({row["name"]: row["effective_pct"] for row in ownership_as_of(D("2025-07-01"))["owners"]}, {"Alice": "50.0000", "Bob": "50.0000", "Carol": "0.0000"})
        JournalEntry.objects.all().delete()
        self.sale("2025-06-30", "300.00")
        june = self.report()
        self.assertEqual({row["name"]: row["amount"] for row in june["totals_by_owner"]}, {"Alice": "100.00", "Bob": "100.00", "Carol": "100.00"})

    def test_totals_tie_to_net_income_with_nothing_unallocated(self):
        self.sample_2025()
        for day, amount in [("2025-01-01", "10.00"), ("2025-06-30", "300.00"), ("2025-07-01", "300.00"), ("2025-12-31", "0.01")]:
            self.sale(day, amount)
        self.expense("2025-07-02", "55.55")
        report = self.report()
        total = sum((Decimal(row["amount"]) for row in report["totals_by_owner"]), Decimal("0"))
        self.assertEqual(total, income_statement(*self.YEAR)["net_income"])
        self.assertEqual(report["net_income"], f"{total:.2f}")
        self.assertEqual(report["unallocated"], "0.00")

    def test_response_shape_is_unchanged(self):
        self.sample_2025()
        report = self.report()
        self.assertEqual(set(report), {"start", "end", "method", "convention", "change_day_belongs_to", "unissued_allocation_policy", "written_statement", "written_statement_date", "segments", "totals_by_owner", "net_income", "unallocated", "warnings"})
        self.assertEqual(set(report["segments"][1]), {"start", "end", "days", "change_dates", "net_income", "allocated_income", "by_account", "reserved_pct", "total_issued_pct", "allocations"})

    # -- the invariant -----------------------------------------------------------

    def test_every_day_uses_the_ownership_on_file_for_that_day(self):
        self.sample_2025()
        self.assert_matches_ownership_on_every_day(*self.YEAR)
        owner_periods, reserved_periods = load_periods()
        by_day = self.ownership_by_day(*self.YEAR)
        for day in ["2025-01-01", "2025-06-30", "2025-07-01", "2025-12-31"]:
            stated = {row["owner_id"]: row["stated_pct"] for row in ownership_as_of(D(day))["owners"] if row["stated_pct"] != "0.0000"}
            self.assertEqual({owner_id: pct(value) for owner_id, value in by_day[D(day)].stated if value}, stated, day)

    def test_invariant_holds_through_many_changes(self):
        self.interest(self.alice, "2025-01-01", "100", end="2025-01-31")
        self.interest(self.alice, "2025-02-01", "50", end="2025-03-15")
        self.interest(self.bob, "2025-02-01", "50", end="2025-06-29")
        self.interest(self.alice, "2025-03-16", "25", end="2025-06-29")
        self.interest(self.carol, "2025-03-16", "25", end="2025-06-30")
        self.interest(self.alice, "2025-06-30", "75", end="2025-12-30")
        self.interest(self.bob, "2025-07-01", "25")
        self.interest(self.alice, "2025-12-31", "75")
        self.assert_matches_ownership_on_every_day(*self.YEAR)

    def test_incoming_moves_only_the_variation_date(self):
        self.sample_2025()
        owner_periods, reserved_periods = load_periods()
        outgoing = self.ownership_by_day(*self.YEAR, belongs_to="outgoing")
        incoming = self.ownership_by_day(*self.YEAR, belongs_to="incoming")
        different = [day for day in outgoing if outgoing[day] != incoming[day]]
        self.assertEqual(different, [D("2025-06-30")])
        self.assertEqual(incoming[D("2025-06-30")], ownership_on(D("2025-07-01"), owner_periods, reserved_periods))
        report = self.report(change_day_belongs_to="incoming")
        self.assertEqual(self.bounds(report), [("2025-01-01", "2025-06-29"), ("2025-06-30", "2025-12-31")])
        self.assertEqual(report["segments"][1]["change_dates"], ["2025-06-30"])

    # -- where the change falls --------------------------------------------------

    def change_on(self, effective_start):
        """Alice owns everything, then from effective_start Alice and Bob own half each."""
        day = D(effective_start)
        self.interest(self.alice, "2024-01-01", "100", end=(day - timedelta(days=1)).isoformat())
        self.interest(self.alice, effective_start, "50")
        self.interest(self.bob, effective_start, "50")

    def test_change_at_month_start(self):
        self.change_on("2025-04-01")
        self.assertEqual(self.bounds(self.report()), [("2025-01-01", "2025-03-31"), ("2025-04-01", "2025-12-31")])
        self.assertEqual(self.report()["segments"][1]["change_dates"], ["2025-03-31"])
        self.assert_matches_ownership_on_every_day(*self.YEAR)

    def test_change_mid_month(self):
        self.change_on("2025-04-17")
        self.assertEqual(self.bounds(self.report()), [("2025-01-01", "2025-04-16"), ("2025-04-17", "2025-12-31")])
        self.assertEqual(self.bounds(self.report(change_day_belongs_to="incoming")), [("2025-01-01", "2025-04-15"), ("2025-04-16", "2025-12-31")])
        # Variation 04-16: semi-monthly deems it the end of the 15th, monthly the end of April.
        self.assertEqual(self.bounds(self.report(convention="semi_monthly")), [("2025-01-01", "2025-04-15"), ("2025-04-16", "2025-12-31")])
        self.assertEqual(self.bounds(self.report(convention="monthly")), [("2025-01-01", "2025-04-30"), ("2025-05-01", "2025-12-31")])
        self.assert_matches_ownership_on_every_day(*self.YEAR)

    def test_change_on_january_1_is_clamped_to_the_range_start(self):
        # The variation date is Dec 31 of the prior year.
        self.change_on("2025-01-01")
        self.sale("2025-01-01", "10.00")
        for belongs_to in ["outgoing", "incoming"]:
            report = self.report(change_day_belongs_to=belongs_to)
            self.assertEqual(self.bounds(report), [("2025-01-01", "2025-12-31")], belongs_to)
            self.assertEqual(report["segments"][0]["change_dates"], [], belongs_to)
            self.assertEqual({row["name"]: row["amount"] for row in report["totals_by_owner"]}, {"Alice": "5.00", "Bob": "5.00"}, belongs_to)
        prior = partner_allocation_report(D("2024-01-01"), D("2024-12-31"), change_day_belongs_to="incoming")
        self.assertEqual(self.bounds(prior), [("2024-01-01", "2024-12-30"), ("2024-12-31", "2024-12-31")])
        self.assertEqual(self.bounds(partner_allocation_report(D("2024-01-01"), D("2024-12-31"))), [("2024-01-01", "2024-12-31")])

    def test_change_on_december_31(self):
        self.change_on("2025-12-31")
        self.assertEqual(self.bounds(self.report()), [("2025-01-01", "2025-12-30"), ("2025-12-31", "2025-12-31")])
        self.assertEqual(self.report()["segments"][1]["change_dates"], ["2025-12-30"])
        self.assertEqual(self.bounds(self.report(change_day_belongs_to="incoming")), [("2025-01-01", "2025-12-29"), ("2025-12-30", "2025-12-31")])
        self.assert_matches_ownership_on_every_day(*self.YEAR)

    def test_variation_on_december_31_stays_in_the_old_year_unless_incoming(self):
        self.change_on("2026-01-01")
        self.assertEqual(self.bounds(self.report()), [("2025-01-01", "2025-12-31")])
        self.assertEqual(self.bounds(self.report(change_day_belongs_to="incoming")), [("2025-01-01", "2025-12-30"), ("2025-12-31", "2025-12-31")])

    def test_two_changes_on_consecutive_days(self):
        self.interest(self.alice, "2025-01-01", "100", end="2025-06-29")
        self.interest(self.alice, "2025-06-30", "50")
        self.interest(self.bob, "2025-06-30", "50", end="2025-06-30")
        self.interest(self.carol, "2025-07-01", "50")
        self.sale("2025-06-30", "10.00")
        self.sale("2025-07-01", "20.00")
        report = self.report()
        self.assertEqual(self.bounds(report), [("2025-01-01", "2025-06-29"), ("2025-06-30", "2025-06-30"), ("2025-07-01", "2025-12-31")])
        self.assertEqual([segment["change_dates"] for segment in report["segments"]], [[], ["2025-06-29"], ["2025-06-30"]])
        self.assertEqual(self.amounts(report["segments"][1]), {"Alice": "5.00", "Bob": "5.00", "Carol": "0.00"})
        self.assertEqual(self.amounts(report["segments"][2]), {"Alice": "10.00", "Bob": "0.00", "Carol": "10.00"})
        self.assert_matches_ownership_on_every_day(*self.YEAR)
        incoming = self.report(change_day_belongs_to="incoming")
        self.assertEqual(self.bounds(incoming), [("2025-01-01", "2025-06-28"), ("2025-06-29", "2025-06-29"), ("2025-06-30", "2025-12-31")])

    def test_open_ended_final_period_runs_to_the_range_end(self):
        self.sample_2025()
        report = self.report(end=D("2030-12-31"))
        self.assertEqual(self.bounds(report), [("2025-01-01", "2025-06-30"), ("2025-07-01", "2030-12-31")])
        self.assertEqual(report["warnings"], [])

    # -- conventions -----------------------------------------------------------------

    def test_semi_monthly_and_monthly_use_the_variation_date(self):
        self.sample_2025()  # variation 2025-06-30, a 16th-or-later date
        self.assertEqual(self.bounds(self.report(convention="semi_monthly")), [("2025-01-01", "2025-06-15"), ("2025-06-16", "2025-12-31")])
        self.assertEqual(self.bounds(self.report(convention="monthly")), [("2025-01-01", "2025-06-30"), ("2025-07-01", "2025-12-31")])

    def join_and_leave(self, joins, leaves):
        self.interest(self.alice, "2025-01-01", "100", end=(D(joins) - timedelta(days=1)).isoformat())
        self.interest(self.alice, joins, "60", end=leaves)
        self.interest(self.carol, joins, "40", end=leaves)
        self.interest(self.alice, (D(leaves) + timedelta(days=1)).isoformat(), "100")

    def test_partner_who_joins_and_leaves_within_one_conventional_period_uses_actual_dates(self):
        self.join_and_leave("2025-05-03", "2025-05-09")
        self.sale("2025-05-05", "100.00")
        for convention in ["calendar_day", "semi_monthly", "monthly"]:
            report = self.report(convention=convention)
            self.assertEqual(self.bounds(report), [("2025-01-01", "2025-05-02"), ("2025-05-03", "2025-05-09"), ("2025-05-10", "2025-12-31")], convention)
            self.assertEqual({row["name"]: row["amount"] for row in report["totals_by_owner"]}, {"Alice": "60.00", "Carol": "40.00"}, convention)
            self.assertEqual([segment["change_dates"] for segment in report["segments"]], [[], ["2025-05-02"], ["2025-05-09"]], convention)

    def test_partner_who_spans_two_conventional_periods_is_placed_by_the_convention(self):
        self.join_and_leave("2025-05-03", "2025-05-20")
        report = self.report(convention="semi_monthly")
        self.assertEqual(self.bounds(report), [("2025-01-01", "2025-04-30"), ("2025-05-01", "2025-05-15"), ("2025-05-16", "2025-12-31")])

    def test_a_later_deemed_change_does_not_erase_an_actual_dated_partner(self):
        self.interest(self.alice, "2025-01-01", "100", end="2025-05-02")
        self.interest(self.alice, "2025-05-03", "60", end="2025-05-09")
        self.interest(self.carol, "2025-05-03", "40", end="2025-05-09")
        self.interest(self.alice, "2025-05-10", "100", end="2025-05-12")
        self.interest(self.alice, "2025-05-13", "50")
        self.interest(self.bob, "2025-05-13", "50")
        self.sale("2025-05-05", "100.00")
        report = self.report(convention="semi_monthly")
        self.assertEqual(self.bounds(report), [("2025-01-01", "2025-05-02"), ("2025-05-03", "2025-05-09"), ("2025-05-10", "2025-12-31")])
        self.assertEqual({row["name"]: row["stated_pct"] for row in report["segments"][2]["allocations"]}, {"Alice": "50.0000", "Bob": "50.0000", "Carol": "0.0000"})
        self.assertEqual(report["segments"][2]["change_dates"], ["2025-05-09", "2025-05-12"])
        self.assertEqual({row["name"]: row["amount"] for row in report["totals_by_owner"]}, {"Alice": "60.00", "Bob": "0.00", "Carol": "40.00"})

    # -- rounding and the rollforward ---------------------------------------------

    def test_three_way_split_uses_largest_remainder(self):
        self.sample_2025()
        self.sale("2025-03-01", "100.00")
        report = self.report()
        self.assertEqual(self.amounts(report["segments"][0]), {"Alice": "33.34", "Bob": "33.33", "Carol": "33.33"})
        self.assertEqual(sum((Decimal(row["amount"]) for row in report["totals_by_owner"]), Decimal("0")), Decimal("100.00"))

    def test_capital_rollforward_picks_up_the_corrected_allocation(self):
        self.sample_2025()
        self.sale("2025-07-01", "300.00")
        rows = {row["owner_name"]: row["allocated_income_loss"] for row in capital_account_rollforward(*self.YEAR)["owners"]}
        self.assertEqual(rows, {"Alice": "150.00", "Bob": "150.00", "Carol": "0.00"})
        later = {row["owner_name"]: row["beginning"] for row in capital_account_rollforward(D("2026-01-01"), D("2026-12-31"))["owners"]}
        self.assertEqual(later, {"Alice": "150.00", "Bob": "150.00", "Carol": "0.00"})
