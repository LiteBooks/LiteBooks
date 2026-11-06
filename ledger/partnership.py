"""Owner interests over time and the partnership allocation reports built on them.

When ownership changes during the year, IRC §706(d) and Treas. Reg. §1.706-4
require the year's income or loss to be split by who owned what, and when. The
default here is the interim closing method: the year is cut into segments at
every ownership change, each segment's income is summed from the ledger by
transaction date, and that income is divided by the segment's percentages.

Everything in this module either validates and saves interest periods or reads
the ledger. Nothing here posts a journal entry; allocations are reports only.

Terms used below:

* stated percentage: what an interest period says an owner (or a reserved pool) holds.
* effective percentage: an owner's share of income after the unissued policy is
  applied. With pro_rata_to_issued, unissued interests (reserved pools and any gap
  below 100%) are shared by the issued owners in proportion to their holdings,
  so 45/45 with 10 reserved becomes 50/50.
* variation date: the last day of the outgoing interest, i.e. the effective_end of
  the old percentages, which is the day before the new ones' effective_start. Under
  Treas. Reg. §1.706-4(c)(1)(i) a variation is deemed to occur at the end of the day
  it occurs, so under the calendar day convention the outgoing owners keep the
  variation date and the next segment starts the day after; change_day_belongs_to
  = incoming gives the variation date to the new owners instead. The semi-monthly
  and monthly conventions move every variation to the convention's deemed date,
  except that a partner who enters and exits within one conventional period is
  placed on the actual dates (Reg. §1.706-4(c)(2)(ii)).
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum

from .models import Account, Contact, JournalEntry, JournalLine, OwnerInterestPeriod, PartnershipSettings, ReservedInterestPeriod
from .services import record_audit


HUNDRED = Decimal("100")
PERCENT_PLACES = Decimal("0.0001")
CENT = Decimal("0.01")
ONE_DAY = timedelta(days=1)


def pct(value):
    """A percentage as a 4-place string, e.g. Fraction(100, 3) -> '33.3333'."""
    if isinstance(value, Fraction):
        value = Decimal(value.numerator) / Decimal(value.denominator)
    return f"{Decimal(value).quantize(PERCENT_PLACES, rounding=ROUND_HALF_UP):.4f}"


def money(value):
    return f"{Decimal(value or 0):.2f}"


def iso(value):
    return value.isoformat() if value else None


# -- Saving interest periods ---------------------------------------------------


def clean_percentage(value):
    try:
        number = Decimal(str(value).strip().rstrip("%"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({"percentage": "Enter a percentage between 0 and 100."}) from exc
    if not number.is_finite() or number < 0 or number > HUNDRED:
        raise ValidationError({"percentage": "Enter a percentage between 0 and 100."})
    if number != number.quantize(PERCENT_PLACES):
        raise ValidationError({"percentage": "Percentages can have at most four decimal places."})
    return number.quantize(PERCENT_PLACES)


def overlaps(query, start, end):
    """Narrow a period queryset to the periods sharing at least one day with start..end (end None = open)."""
    query = query.filter(Q(effective_end__isnull=True) | Q(effective_end__gte=start))
    if end is not None:
        query = query.filter(effective_start__lte=end)
    return query


def check_total_interest(start, end, percentage, exclude_owner_pk=None, exclude_reserved_pk=None):
    """Refuse a period that would push issued plus reserved interests over 100% on any day.

    Totals only rise where a period starts, so checking the new period's first day
    and every other period's first day inside it covers every day it spans.
    """
    owners = overlaps(OwnerInterestPeriod.objects.select_related("owner_contact"), start, end)
    reserved = overlaps(ReservedInterestPeriod.objects.all(), start, end)
    if exclude_owner_pk:
        owners = owners.exclude(pk=exclude_owner_pk)
    if exclude_reserved_pk:
        reserved = reserved.exclude(pk=exclude_reserved_pk)
    others = [*owners, *reserved]
    days = sorted({start, *(item.effective_start for item in others if item.effective_start > start)})
    for day in days:
        present = [item for item in others if item.covers(day)]
        total = percentage + sum((item.percentage for item in present), Decimal("0"))
        if total > HUNDRED:
            parts = ", ".join(f"{period_holder(item)} {item.percentage:.4f}%" for item in present)
            raise ValidationError({"percentage": f"On {day.isoformat()} issued and reserved interests would total {total:.4f}%, which is over 100% ({parts}, plus this {percentage:.4f}%)."})


def period_holder(period):
    return period.owner_contact.name if isinstance(period, OwnerInterestPeriod) else period.label


def check_dates(start, end):
    if not isinstance(start, date):
        raise ValidationError({"effective_start": "Enter the date this percentage takes effect."})
    if end is not None and end < start:
        raise ValidationError({"effective_end": "The end date cannot be before the start date."})


def owner_period_snapshot(period):
    return {
        "owner_contact_id": period.owner_contact_id,
        "effective_start": iso(period.effective_start),
        "effective_end": iso(period.effective_end),
        "percentage": pct(period.percentage),
        "notes": period.notes,
        "source_document": period.source_document,
    }


def reserved_period_snapshot(period):
    return {
        "label": period.label,
        "effective_start": iso(period.effective_start),
        "effective_end": iso(period.effective_end),
        "percentage": pct(period.percentage),
        "notes": period.notes,
        "source_document": period.source_document,
    }


def lock_interests():
    """Serialize interest edits so two saves cannot each pass the 100% check and together break it."""
    PartnershipSettings.load()
    PartnershipSettings.objects.select_for_update().get(pk=1)


@transaction.atomic
def save_owner_interest(*, user, action=None, owner, effective_start, effective_end, percentage, notes="", source_document="", period=None):
    lock_interests()
    if owner is None or owner.kind != Contact.Kind.OWNER:
        raise ValidationError({"owner_contact": "Ownership periods can only be recorded for contacts of kind owner."})
    if period is not None and period.owner_contact_id != owner.pk:
        raise ValidationError({"owner_contact": "A period cannot be moved to a different owner. End it and add one for the other owner."})
    check_dates(effective_start, effective_end)
    percentage = clean_percentage(percentage)
    clash = overlaps(OwnerInterestPeriod.objects.filter(owner_contact=owner), effective_start, effective_end)
    if period is not None:
        clash = clash.exclude(pk=period.pk)
    clash = clash.order_by("effective_start").first()
    if clash:
        ends = clash.effective_end.isoformat() if clash.effective_end else "open-ended"
        raise ValidationError({"effective_start": f"This overlaps {owner.name}'s {clash.percentage:.4f}% period from {clash.effective_start.isoformat()} to {ends}. End that period first, or change the dates."})
    check_total_interest(effective_start, effective_end, percentage, exclude_owner_pk=period.pk if period else None)
    before = owner_period_snapshot(period) if period else None
    period = period or OwnerInterestPeriod(owner_contact=owner, created_by=user)
    period.effective_start = effective_start
    period.effective_end = effective_end
    period.percentage = percentage
    period.notes = notes or ""
    period.source_document = source_document or ""
    period.updated_by = user
    period.save()
    record_audit(user, action or ("updated" if before else "created"), period, before=before, after=owner_period_snapshot(period))
    return period


@transaction.atomic
def save_reserved_interest(*, user, action=None, label, effective_start, effective_end, percentage, notes="", source_document="", period=None):
    lock_interests()
    label = (label or "").strip()
    if not label:
        raise ValidationError({"label": "Name the reserved interest, e.g. Reserved pool."})
    check_dates(effective_start, effective_end)
    percentage = clean_percentage(percentage)
    clash = overlaps(ReservedInterestPeriod.objects.filter(label__iexact=label), effective_start, effective_end)
    if period is not None:
        clash = clash.exclude(pk=period.pk)
    clash = clash.order_by("effective_start").first()
    if clash:
        ends = clash.effective_end.isoformat() if clash.effective_end else "open-ended"
        raise ValidationError({"effective_start": f"This overlaps the {clash.label} period from {clash.effective_start.isoformat()} to {ends}. End that period first, or change the dates."})
    check_total_interest(effective_start, effective_end, percentage, exclude_reserved_pk=period.pk if period else None)
    before = reserved_period_snapshot(period) if period else None
    period = period or ReservedInterestPeriod(created_by=user)
    period.label = label
    period.effective_start = effective_start
    period.effective_end = effective_end
    period.percentage = percentage
    period.notes = notes or ""
    period.source_document = source_document or ""
    period.updated_by = user
    period.save()
    record_audit(user, action or ("updated" if before else "created"), period, before=before, after=reserved_period_snapshot(period))
    return period


@transaction.atomic
def end_interest(*, user, period, effective_end):
    """Close an open (or later-ending) period on a date, leaving everything else about it as recorded."""
    check_dates(period.effective_start, effective_end)
    if isinstance(period, OwnerInterestPeriod):
        return save_owner_interest(user=user, action="ended", owner=period.owner_contact, effective_start=period.effective_start, effective_end=effective_end, percentage=period.percentage, notes=period.notes, source_document=period.source_document, period=period)
    return save_reserved_interest(user=user, action="ended", label=period.label, effective_start=period.effective_start, effective_end=effective_end, percentage=period.percentage, notes=period.notes, source_document=period.source_document, period=period)


@transaction.atomic
def delete_interest(*, user, period):
    before = owner_period_snapshot(period) if isinstance(period, OwnerInterestPeriod) else reserved_period_snapshot(period)
    record_audit(user, "deleted", period, before=before)
    period.delete()


SETTINGS_FIELDS = ("allocation_method", "convention", "unissued_allocation_policy", "change_day_belongs_to", "written_statement", "written_statement_date")


def settings_snapshot(found):
    return {
        "allocation_method": found.allocation_method,
        "convention": found.convention,
        "unissued_allocation_policy": found.unissued_allocation_policy,
        "change_day_belongs_to": found.change_day_belongs_to,
        "written_statement": found.written_statement,
        "written_statement_date": iso(found.written_statement_date),
    }


@transaction.atomic
def save_settings(*, user, **values):
    found = PartnershipSettings.objects.select_for_update().get(pk=PartnershipSettings.load().pk)
    choices = {
        "allocation_method": PartnershipSettings.Method.values,
        "convention": PartnershipSettings.Convention.values,
        "unissued_allocation_policy": PartnershipSettings.UnissuedPolicy.values,
        "change_day_belongs_to": PartnershipSettings.ChangeDay.values,
    }
    for key, allowed in choices.items():
        if key in values and values[key] not in allowed:
            raise ValidationError({key: f"Choose one of: {', '.join(allowed)}."})
    before = settings_snapshot(found)
    for key in SETTINGS_FIELDS:
        if key in values:
            setattr(found, key, values[key] if values[key] is not None or key == "written_statement_date" else "")
    found.updated_by = user
    found.save()
    record_audit(user, "updated", found, before=before, after=settings_snapshot(found))
    return found


# -- Ownership on a date -------------------------------------------------------


@dataclass(frozen=True)
class Ownership:
    """Who held what over a stretch of days. stated maps owner id -> stated percentage."""

    stated: tuple  # sorted ((owner_id, Decimal), ...), so equal holdings compare equal
    reserved: Decimal

    @property
    def issued(self):
        return sum((value for _, value in self.stated), Decimal("0"))

    @property
    def has_owners(self):
        return self.issued > 0

    def effective(self):
        """owner id -> exact effective share of income as a Fraction of 1 (pro rata to issued)."""
        issued = self.issued
        if not issued:
            return {owner_id: Fraction(0) for owner_id, _ in self.stated}
        return {owner_id: Fraction(value) / Fraction(issued) for owner_id, value in self.stated}


NO_OWNERSHIP = Ownership(stated=(), reserved=Decimal("0"))


def load_periods():
    return list(OwnerInterestPeriod.objects.select_related("owner_contact")), list(ReservedInterestPeriod.objects.all())


def ownership_on(day, owner_periods, reserved_periods):
    stated = {}
    for period in owner_periods:
        if period.covers(day):
            stated[period.owner_contact_id] = period.percentage
    reserved = sum((period.percentage for period in reserved_periods if period.covers(day)), Decimal("0"))
    return Ownership(stated=tuple(sorted(stated.items())), reserved=reserved)


def ownership_as_of(day):
    """Each owner's stated and effective percentage on a day, plus reserved and issued totals.

    Every owner with any recorded interest is listed, so an owner who has left shows 0.
    """
    owner_periods, reserved_periods = load_periods()
    state = ownership_on(day, owner_periods, reserved_periods)
    effective = state.effective()
    stated = dict(state.stated)
    owners = sorted({period.owner_contact for period in owner_periods}, key=lambda owner: (owner.name, owner.pk))
    return {
        "date": day.isoformat(),
        "owners": [{
            "owner_id": owner.pk,
            "name": owner.name,
            "stated_pct": pct(stated.get(owner.pk, 0)),
            "effective_pct": pct(effective.get(owner.pk, Fraction(0)) * 100),
        } for owner in owners],
        "reserved_pct": pct(state.reserved),
        "total_issued_pct": pct(state.issued),
        "unissued_pct": pct(HUNDRED - state.issued),
    }


# -- Segments --------------------------------------------------------------------


def ownership_regimes(owner_periods, reserved_periods):
    """Split all of time into runs of identical ownership: [(first_day, Ownership)], first run from date.min."""
    edges = set()
    for period in [*owner_periods, *reserved_periods]:
        edges.add(period.effective_start)
        if period.effective_end and period.effective_end < date.max:
            edges.add(period.effective_end + ONE_DAY)
    regimes = [(date.min, NO_OWNERSHIP)]
    for day in sorted(edges):
        state = ownership_on(day, owner_periods, reserved_periods)
        if state != regimes[-1][1]:
            regimes.append((day, state))
    return regimes


def describe_span(first, last):
    days = (last - first).days + 1
    when = f"on {first.isoformat()}" if first == last else f"for {first.isoformat()} to {last.isoformat()}"
    return f"{when} ({days} day{'' if days == 1 else 's'})"


def coverage_warnings(start, end, owner_periods, reserved_periods):
    """Warnings for days in start..end whose recorded interests do not add up cleanly.

    Judged from the stated periods on file, day by day, before any convention moves a
    change: days with no issued interest (their income goes unallocated), days where
    issued plus reserved is over 100%, and days where it is under 100%. These warn even
    when the days carry no income, so a gap in the records is visible before income lands in it.
    """
    regimes = ownership_regimes(owner_periods, reserved_periods)
    spans = []  # [kind, total, first, last], adjacent days of the same kind and total merged
    for index, (first, state) in enumerate(regimes):
        last = regimes[index + 1][0] - ONE_DAY if index + 1 < len(regimes) else date.max
        first, last = max(first, start), min(last, end)
        if first > last:
            continue
        total = state.issued + state.reserved
        if not state.has_owners:
            kind, total = "uncovered", None
        elif total > HUNDRED:
            kind = "over"
        elif total < HUNDRED:
            kind = "under"
        else:
            continue
        if spans and spans[-1][0] == kind and spans[-1][1] == total and spans[-1][3] + ONE_DAY == first:
            spans[-1][3] = last
        else:
            spans.append([kind, total, first, last])
    warnings = []
    for kind, total, first, last in spans:
        span = describe_span(first, last)
        if kind == "uncovered":
            warnings.append(f"No ownership interests on file {span}; income for these days is unallocated.")
        elif kind == "over":
            warnings.append(f"Issued and reserved interests total {total:.4f}% {span}, which is over 100%; check the ownership periods on file for these dates. Income is allocated by each owner's share of issued interests.")
        else:
            warnings.append(f"Issued and reserved interests total {total:.4f}% {span}, leaving {HUNDRED - total:.4f}% unaccounted for; income for these days is shared by the issued owners in proportion to their holdings.")
    return warnings


def first_of_next_month(day):
    return date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)


def deemed_start(variation_date, convention, change_day_belongs_to):
    """The first day the new ownership is treated as in effect, given the variation date.

    The variation date is the last day of the outgoing interest, and the variation is
    deemed to happen at the end of that day (Treas. Reg. §1.706-4(c)(1)(i)).
    Calendar day: the outgoing owners keep the variation date and the new ownership
    starts the next day; choosing incoming gives the variation date to the new ownership.
    Semi-monthly: a variation on the 1st-15th is deemed to occur at the end of the prior
    month (new ownership from the 1st); on the 16th-end, at the end of the 15th (from the 16th).
    Monthly: a variation on the 1st-15th is deemed to occur at the end of the prior month
    (from the 1st); on the 16th-end, at the end of the month (from the 1st of the next month).
    change_day_belongs_to only applies under the calendar day convention.
    """
    if convention == PartnershipSettings.Convention.SEMI_MONTHLY:
        return variation_date.replace(day=1 if variation_date.day <= 15 else 16)
    if convention == PartnershipSettings.Convention.MONTHLY:
        return variation_date.replace(day=1) if variation_date.day <= 15 else first_of_next_month(variation_date)
    if change_day_belongs_to == PartnershipSettings.ChangeDay.INCOMING:
        return variation_date
    return variation_date + ONE_DAY


@dataclass
class Segment:
    start: date
    end: date
    ownership: Ownership
    change_dates: list = field(default_factory=list)  # variation dates that opened this segment

    @property
    def days(self):
        return (self.end - self.start).days + 1


def actual_date_regimes(regimes, placements):
    """Regime indexes whose change must be placed on its actual date, not a deemed one.

    Reg. §1.706-4(c)(2)(ii): when a partner's entry and exit would be deemed to happen
    at the same moment (both inside one conventional period), the convention would
    erase the partner's interest, so both the entry and the exit use the actual dates.
    """
    actual = set()
    entered = {}  # owner id -> regime index where their current run of interest began
    for index in range(1, len(regimes)):
        before = dict(regimes[index - 1][1].stated)
        after = dict(regimes[index][1].stated)
        for owner_id in after.keys() - before.keys():
            entered[owner_id] = index
        for owner_id in before.keys() - after.keys():
            entry = entered.pop(owner_id, None)
            if entry is not None and placements[index] <= placements[entry]:
                actual.update({entry, index})
    return actual


def segments_for(start, end, convention, change_day_belongs_to, owner_periods=None, reserved_periods=None):
    """Cut start..end into segments at every ownership change, as the convention places them.

    For every day the segment's ownership is ownership_on(that day), except where the
    convention (or change_day_belongs_to = incoming) moves a variation.
    """
    if owner_periods is None:
        owner_periods, reserved_periods = load_periods()
    regimes = ownership_regimes(owner_periods, reserved_periods)

    def variation(index):
        return regimes[index][0] - ONE_DAY

    def placement(index):
        # When nobody owned anything before, nothing is being taken from an outgoing owner, so the
        # first ownership simply starts on its own date rather than at a deemed one.
        if not regimes[index - 1][1].has_owners:
            return regimes[index][0]
        return deemed_start(variation(index), convention, change_day_belongs_to)

    placements = {index: placement(index) for index in range(1, len(regimes))}
    actual = actual_date_regimes(regimes, placements)
    placed = [[date.min, regimes[0][1], [], False]]  # [first_day, ownership, variation dates, on actual dates]
    for index in range(1, len(regimes)):
        is_actual = index in actual
        first_day = regimes[index][0] if is_actual else placements[index]
        folded = []
        # A convention can pull several changes onto one deemed day; the latest ownership wins it.
        # A change kept on its actual date is not swept back by a later deemed one: the later
        # change lands on the actual date instead.
        while len(placed) > 1 and placed[-1][0] >= first_day:
            if placed[-1][3] and placed[-1][0] > first_day:
                first_day = placed[-1][0]
            folded = placed.pop()[2] + folded
        placed.append([first_day, regimes[index][1], folded + [variation(index)], is_actual])
    segments = []
    for index, (first_day, state, variations, _) in enumerate(placed):
        last_day = placed[index + 1][0] - ONE_DAY if index + 1 < len(placed) else date.max
        first_day, last_day = max(first_day, start), min(last_day, end)
        if first_day > last_day:
            continue
        if segments and segments[-1].ownership == state:
            segments[-1].end = last_day
            continue
        segments.append(Segment(first_day, last_day, state, list(variations) if first_day > start else []))
    return segments


# -- Allocation ------------------------------------------------------------------


def to_cents(amount):
    return int((Decimal(amount) * 100).to_integral_value())


def from_cents(cents):
    return (Decimal(cents) / 100).quantize(CENT)


def largest_remainder(total_cents, weights):
    """Split whole cents by weights (key -> Fraction, summing to 1) so the parts add up exactly.

    Each key gets the floor of its exact share; the cents left over go one each to the
    largest fractional remainders, ties to the larger weight, then to the earlier key.
    A loss is split the same way and given its sign back.
    """
    sign = -1 if total_cents < 0 else 1
    remaining = abs(total_cents)
    keys = list(weights)
    exact = {key: remaining * weights[key] for key in keys}
    shares = {key: int(exact[key]) for key in keys}  # Fractions here are >= 0, so int() floors.
    leftover = remaining - sum(shares.values())
    order = sorted(range(len(keys)), key=lambda i: (-(exact[keys[i]] - shares[keys[i]]), -weights[keys[i]], i))
    for i in order[:leftover]:
        shares[keys[i]] += 1
    return {key: sign * value for key, value in shares.items()}


def income_activity(start, end):
    """Posted revenue and expense activity by date and account, in cents of net-income effect.

    Returns {date: {account_id: cents}}. Deleted and draft entries never count; entries in
    closed months do, since a closed month is still part of the year.
    """
    rows = (
        JournalLine.objects.filter(
            entry__status=JournalEntry.Status.POSTED,
            entry__date__range=(start, end),
            account__type__in=[Account.Type.REVENUE, Account.Type.EXPENSE],
        )
        .values("entry__date", "account_id")
        .annotate(debits=Sum("debit"), credits=Sum("credit"))
        .order_by()
    )
    activity = {}
    for row in rows:
        effect = to_cents((row["credits"] or 0) - (row["debits"] or 0))
        if effect:
            day = activity.setdefault(row["entry__date"], {})
            day[row["account_id"]] = day.get(row["account_id"], 0) + effect
    return activity


def resolve_options(method=None, convention=None, change_day_belongs_to=None):
    found = PartnershipSettings.load()
    method = method or found.allocation_method
    convention = convention or found.convention
    change_day_belongs_to = change_day_belongs_to or found.change_day_belongs_to
    for value, allowed, name in [
        (method, PartnershipSettings.Method.values, "method"),
        (convention, PartnershipSettings.Convention.values, "convention"),
        (change_day_belongs_to, PartnershipSettings.ChangeDay.values, "change_day_belongs_to"),
    ]:
        if value not in allowed:
            raise ValidationError({name: f"{name} must be one of: {', '.join(allowed)}."})
    return found, method, convention, change_day_belongs_to


def compute_allocation(start, end, method=None, convention=None, change_day_belongs_to=None):
    """The allocation in plain Python values (cents and Fractions), for the report and the rollforward."""
    if start > end:
        raise ValidationError({"start": "start must be on or before end."})
    found, method, convention, change_day_belongs_to = resolve_options(method, convention, change_day_belongs_to)
    owner_periods, reserved_periods = load_periods()
    segments = segments_for(start, end, convention, change_day_belongs_to, owner_periods, reserved_periods)
    activity = income_activity(start, end)
    involved = {owner_id for segment in segments for owner_id, _ in segment.ownership.stated}
    owners = sorted({period.owner_contact for period in owner_periods if period.owner_contact_id in involved}, key=lambda owner: (owner.name, owner.pk))

    for segment in segments:
        by_account = {}
        for day, accounts in activity.items():
            if segment.start <= day <= segment.end:
                for account_id, cents in accounts.items():
                    by_account[account_id] = by_account.get(account_id, 0) + cents
        segment.by_account = by_account
        segment.net_cents = sum(by_account.values())

    total_cents = sum(segment.net_cents for segment in segments)
    if method == PartnershipSettings.Method.PRORATION:
        # Proration spreads the whole range's income evenly over its days, so each segment
        # carries its share by days rather than what was actually booked in it.
        total_days = sum(segment.days for segment in segments)
        by_days = largest_remainder(total_cents, {index: Fraction(segment.days, total_days) for index, segment in enumerate(segments)}) if total_days else {}
        for index, segment in enumerate(segments):
            segment.allocated_cents = by_days.get(index, 0)
    else:
        for segment in segments:
            segment.allocated_cents = segment.net_cents

    totals = {owner.pk: 0 for owner in owners}
    unallocated = 0
    for segment in segments:
        effective = segment.ownership.effective()
        weights = {owner.pk: effective.get(owner.pk, Fraction(0)) for owner in owners}
        if segment.ownership.has_owners:
            segment.allocations = largest_remainder(segment.allocated_cents, weights)
        else:
            segment.allocations = {owner.pk: 0 for owner in owners}
            unallocated += segment.allocated_cents
        segment.weights = weights
        for owner_id, cents in segment.allocations.items():
            totals[owner_id] += cents

    warnings = coverage_warnings(start, end, owner_periods, reserved_periods)
    if unallocated:
        warnings.append(f"{money(from_cents(unallocated))} of net income falls on days with no recorded owner interests and is not allocated to anyone.")
    if method == PartnershipSettings.Method.PRORATION and convention != PartnershipSettings.Convention.CALENDAR_DAY:
        warnings.append("Treas. Reg. §1.706-4 pairs the proration method with the calendar day convention; confirm this combination with your tax advisor.")
    return {
        "settings": found,
        "method": method,
        "convention": convention,
        "change_day_belongs_to": change_day_belongs_to,
        "segments": segments,
        "owners": owners,
        "totals": totals,
        "total_cents": total_cents,
        "unallocated_cents": unallocated,
        "warnings": warnings,
    }


def partner_allocation_report(start, end, method=None, convention=None, change_day_belongs_to=None):
    result = compute_allocation(start, end, method, convention, change_day_belongs_to)
    accounts = Account.objects.in_bulk({account_id for segment in result["segments"] for account_id in segment.by_account})
    names = {owner.pk: owner.name for owner in result["owners"]}

    def account_row(account_id, cents):
        account = accounts[account_id]
        balance = cents if account.type == Account.Type.REVENUE else -cents
        return {
            "account_id": account.pk,
            "code": account.code,
            "name": account.name,
            "type": account.type,
            "balance": money(from_cents(balance)),
            "net_income_effect": money(from_cents(cents)),
        }

    found = result["settings"]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "method": result["method"],
        "convention": result["convention"],
        "change_day_belongs_to": result["change_day_belongs_to"],
        "unissued_allocation_policy": found.unissued_allocation_policy,
        "written_statement": found.written_statement,
        "written_statement_date": iso(found.written_statement_date),
        "segments": [{
            "start": segment.start.isoformat(),
            "end": segment.end.isoformat(),
            "days": segment.days,
            "change_dates": [day.isoformat() for day in segment.change_dates],
            "net_income": money(from_cents(segment.net_cents)),
            "allocated_income": money(from_cents(segment.allocated_cents)),
            "by_account": [account_row(account_id, cents) for account_id, cents in sorted(segment.by_account.items(), key=lambda item: accounts[item[0]].code)],
            "reserved_pct": pct(segment.ownership.reserved),
            "total_issued_pct": pct(segment.ownership.issued),
            "allocations": [{
                "owner_id": owner_id,
                "name": names[owner_id],
                "stated_pct": pct(dict(segment.ownership.stated).get(owner_id, 0)),
                "effective_pct": pct(segment.weights[owner_id] * 100),
                "amount": money(from_cents(cents)),
            } for owner_id, cents in segment.allocations.items()],
        } for segment in result["segments"]],
        "totals_by_owner": [{"owner_id": owner.pk, "name": owner.name, "amount": money(from_cents(result["totals"][owner.pk]))} for owner in result["owners"]],
        "net_income": money(from_cents(result["total_cents"])),
        "unallocated": money(from_cents(result["unallocated_cents"])),
        "warnings": result["warnings"],
    }


# -- Capital accounts ------------------------------------------------------------


def capital_movements(start, end):
    """Owner-tagged equity movements by owner: contributions, draws, and other, in cents.

    Contributions and draws are owner-tagged lines on the owner contribution and owner
    draw accounts in entries that bring something in or take it out (cash, property, a
    forgiven loan). Entries made only of equity lines, such as a forfeiture moving capital
    from one owner's contribution account to another's, and owner-tagged lines on other
    equity accounts, are other movements. Loans are liabilities, not capital, and are left out.
    """
    lines = JournalLine.objects.filter(
        entry__status=JournalEntry.Status.POSTED,
        owner__isnull=False,
        account__type=Account.Type.EQUITY,
    ).select_related("account")
    if start:
        lines = lines.filter(entry__date__gte=start)
    lines = lines.filter(entry__date__lte=end)
    equity_only = set(
        JournalEntry.objects.filter(pk__in=lines.values("entry_id")).exclude(lines__account__type__in=[
            Account.Type.ASSET, Account.Type.LIABILITY, Account.Type.REVENUE, Account.Type.EXPENSE,
        ]).values_list("pk", flat=True)
    )
    movements = {}
    for line in lines:
        row = movements.setdefault(line.owner_id, {"contributions": 0, "draws": 0, "other": 0})
        credit_less_debit = to_cents(line.credit - line.debit)
        if line.entry_id in equity_only:
            row["other"] += credit_less_debit
        elif line.account.subtype == Account.Subtype.OWNER_CONTRIBUTION:
            row["contributions"] += credit_less_debit
        elif line.account.subtype == Account.Subtype.OWNER_DRAW:
            row["draws"] -= credit_less_debit
        else:
            row["other"] += credit_less_debit
    return movements


def earliest_activity_date():
    first_entry = JournalEntry.objects.filter(status=JournalEntry.Status.POSTED).order_by("date").values_list("date", flat=True).first()
    first_interest = OwnerInterestPeriod.objects.order_by("effective_start").values_list("effective_start", flat=True).first()
    candidates = [day for day in (first_entry, first_interest) if day]
    return min(candidates) if candidates else None


def prior_allocation(first, last):
    """Income allocated from first through last, one calendar tax year at a time.

    Each year is allocated on its own, so proration spreads a year's income over that
    year only. Returns the same totals, owners, and warnings shape as compute_allocation.
    """
    totals, owners, warnings = {}, {}, []
    for year in range(first.year, last.year + 1):
        result = compute_allocation(max(first, date(year, 1, 1)), min(last, date(year, 12, 31)))
        for owner in result["owners"]:
            owners[owner.pk] = owner
        for owner_id, cents in result["totals"].items():
            totals[owner_id] = totals.get(owner_id, 0) + cents
        warnings += [f"{year}: {text}" for text in result["warnings"]]
    return {"totals": totals, "owners": list(owners.values()), "warnings": warnings}


def capital_account_rollforward(start, end, owner_id=None):
    """Each owner's capital from start to end: beginning, contributions, draws, allocated income or loss, other, ending.

    The beginning balance includes income allocated before start, recomputed year by
    year from the first ledger activity under the current allocation settings.
    """
    if start > end:
        raise ValidationError({"start": "start must be on or before end."})
    before_end = start - ONE_DAY
    first = earliest_activity_date()
    prior_alloc = prior_allocation(first, before_end) if first and first <= before_end else None
    current_alloc = compute_allocation(start, end)
    prior = capital_movements(None, before_end)
    current = capital_movements(start, end)

    owner_ids = set(prior) | set(current)
    owner_ids |= {owner.pk for owner in current_alloc["owners"]}
    if prior_alloc:
        owner_ids |= {owner.pk for owner in prior_alloc["owners"]}
    if owner_id is not None:
        owner_ids &= {owner_id}
        if not owner_ids and Contact.objects.filter(pk=owner_id, kind=Contact.Kind.OWNER).exists():
            owner_ids = {owner_id}
    owners = sorted(Contact.objects.filter(pk__in=owner_ids), key=lambda owner: (owner.name, owner.pk))

    empty = {"contributions": 0, "draws": 0, "other": 0}
    rows = []
    for owner in owners:
        before = prior.get(owner.pk, empty)
        during = current.get(owner.pk, empty)
        allocated_before = prior_alloc["totals"].get(owner.pk, 0) if prior_alloc else 0
        allocated = current_alloc["totals"].get(owner.pk, 0)
        beginning = before["contributions"] - before["draws"] + before["other"] + allocated_before
        ending = beginning + during["contributions"] - during["draws"] + allocated + during["other"]
        rows.append({
            "owner_id": owner.pk,
            "owner_name": owner.name,
            "beginning": money(from_cents(beginning)),
            "contributions": money(from_cents(during["contributions"])),
            "draws": money(from_cents(during["draws"])),
            "allocated_income_loss": money(from_cents(allocated)),
            "other": money(from_cents(during["other"])),
            "ending": money(from_cents(ending)),
        })
    warnings = list(current_alloc["warnings"])
    if prior_alloc:
        warnings += [f"Before {start.isoformat()}, {text}" for text in prior_alloc["warnings"]]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "method": current_alloc["method"],
        "convention": current_alloc["convention"],
        "owners": rows,
        "warnings": warnings,
    }


# -- Listing ---------------------------------------------------------------------


def owner_interest_json(period):
    return {
        "id": period.pk,
        "owner_id": period.owner_contact_id,
        "owner_name": period.owner_contact.name,
        "effective_start": iso(period.effective_start),
        "effective_end": iso(period.effective_end),
        "percentage": pct(period.percentage),
        "notes": period.notes,
        "source_document": period.source_document,
    }


def reserved_interest_json(period):
    return {
        "id": period.pk,
        "label": period.label,
        "effective_start": iso(period.effective_start),
        "effective_end": iso(period.effective_end),
        "percentage": pct(period.percentage),
        "notes": period.notes,
        "source_document": period.source_document,
    }


def settings_json(found):
    return {
        **settings_snapshot(found),
        "updated_at": found.updated_at.isoformat() if found.updated_at else None,
        "choices": {
            "allocation_method": [{"value": value, "label": label} for value, label in PartnershipSettings.Method.choices],
            "convention": [{"value": value, "label": label} for value, label in PartnershipSettings.Convention.choices],
            "unissued_allocation_policy": [{"value": value, "label": label} for value, label in PartnershipSettings.UnissuedPolicy.choices],
            "change_day_belongs_to": [{"value": value, "label": label} for value, label in PartnershipSettings.ChangeDay.choices],
        },
    }
