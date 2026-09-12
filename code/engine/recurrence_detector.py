"""
Deterministic recurrence detection from financial_events.csv history, and
forward projection of detected series across the 90-day forecast window.

Grounded in the real, uploaded financial_events.csv (25,342 rows / 275
users): computing per-(user, category) interval statistics up front showed
that almost every category is tightly periodic in this dataset — monthly
categories (rent, utilities, subscriptions, salary, debt_repayment, etc.)
have a day-to-day interval standard deviation under ~3 days around a ~30-day
mean, and "variable-amount" categories users can adjust (dining ~15 days,
groceries ~9 days, transport ~11.5 days) are just as regular in cadence even
though their amounts vary. Because of that, detection here does NOT force
intervals into fixed weekly/biweekly/monthly buckets (a rigid bucket check
would have rejected the real "transport" series, whose ~11.5-day cadence
fits neither); it instead accepts any interval that is *statistically
regular*, and only uses the named buckets for a human-readable label in the
audit trail.

Detected series feed forward projection: synthetic FinancialEvent rows are
materialized for the gap between a series' last known occurrence and the
end of the 90-day horizon, UNLESS a real row for the same (user, category,
direction) already exists near that date — a real record always wins over a
synthetic projection (this is conflict_resolver's level 3, "a settled event
over an estimate/forecast," applied here to forecast-vs-forecast).

TWO-TIER GROUPING — also grounded in the real dataset: grouping purely by
(user, category, direction) works for 2,642 of 2,686 real series, but fails
44 — almost all "salary" or "shopping" — because those categories sometimes
interleave two genuinely distinct recurring sub-series under one category
label (e.g. a user with "Base salary" landing on the 15th of every month
AND "Performance commission" landing on the 24th: each is individually a
clean monthly cadence, but alternating them produces a 9-day/22-day zigzag
that no single interval fits). Checking each category group's `description`
diversity confirms why a description-based key isn't used as the *primary*
grouping: dining/groceries/transport have 6-8 distinct descriptions per user
(a different specific errand each time) despite very regular cadence, so
grouping by description would fragment those into singleton, undetectable
groups. Salary/shopping/rent/utilities/insurance, by contrast, have 1-2
distinct descriptions per user.

So detection tries (user, category, direction) FIRST, and only when that
group fails the regularity gate does it retry using (user, category,
direction, description) as a fallback sub-grouping. This fallback recovered
all 44 real failures in the uploaded dataset without fragmenting any of the
already-passing 2,642 groups (the fallback only ever runs on a group that
already failed the primary check).
"""
from __future__ import annotations
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from code.config.enums import EventDirection, EventStatus, EventType, RecurrenceType
from code.domain.schemas import FinancialEvent


MIN_OCCURRENCES = 2          # below this, there's no diff to assess regularity from at all
MAX_OCCURRENCES_USED = 24    # cap for performance; real data never approaches this per series
DEDUP_WINDOW_FRACTION = 0.5  # a real row within (interval_days * this) of a projected date suppresses it
MIN_RUN_RATIO = 0.6          # the consistent run must cover this fraction of the group's occurrences

# Named only for human-readable audit labels — NOT used to gate detection.
_CADENCE_LABELS = (
    ("weekly", 5, 9),
    ("biweekly", 12, 16),
    ("monthly", 26, 33),
)


def _cadence_label(interval_days: int) -> str:
    for label, lo, hi in _CADENCE_LABELS:
        if lo <= interval_days <= hi:
            return label
    return f"every_{interval_days}_days"


@dataclass(frozen=True)
class RecurringSeries:
    user_id: str
    category: str
    direction: EventDirection
    event_type: EventType
    currency: str
    interval_days: int
    cadence_label: str
    last_occurrence_date: date
    representative_amount: Decimal
    occurrences_used: int
    low_confidence: bool          # True when detection rests on only 2 occurrences
    sample_event: FinancialEvent  # most recent real event in the series — source of
                                   # flexibility / minimum_allowed_amount / event_type for projections


def _eligible_for_detection(event: FinancialEvent, as_of: date) -> bool:
    if event.direction == EventDirection.NON_CASH:
        return False
    if event.status not in (EventStatus.SETTLED, EventStatus.SCHEDULED):
        return False
    if event.superseded_by is not None:
        return False
    if event.amount is None:
        return False
    return event.event_date <= as_of


def _longest_consistent_run(sorted_group: list[FinancialEvent]) -> list[FinancialEvent]:
    """Finds the longest maximal run of consecutive (by date) events whose
    pairwise day-gaps are mutually consistent — each new gap must fall
    within tolerance of the running median gap for the CURRENT run. A run
    resets whenever a gap breaks that consistency (or dates repeat/go
    backwards), so a single interposed one-off event (a bonus, an amendment)
    only breaks the run it falls inside rather than poisoning the whole
    group's statistics. Ties in run length prefer the more recent run, so
    projection anchors as close to `as_of` as possible."""
    best_run = [sorted_group[0]]
    current_run = [sorted_group[0]]
    current_diffs: list[int] = []

    def _consistent(diffs: list[int], new_diff: int) -> bool:
        if not diffs:
            return True
        median = statistics.median(diffs)
        tolerance = max(3.0, 0.2 * median)
        return abs(new_diff - median) <= tolerance

    def _better(candidate: list[FinancialEvent], incumbent: list[FinancialEvent]) -> bool:
        if len(candidate) != len(incumbent):
            return len(candidate) > len(incumbent)
        return candidate[-1].event_date > incumbent[-1].event_date

    for i in range(1, len(sorted_group)):
        gap = (sorted_group[i].event_date - sorted_group[i - 1].event_date).days
        if gap > 0 and _consistent(current_diffs, gap):
            current_run.append(sorted_group[i])
            current_diffs.append(gap)
        else:
            if _better(current_run, best_run):
                best_run = current_run
            current_run = [sorted_group[i]]
            current_diffs = []
    if _better(current_run, best_run):
        best_run = current_run
    return best_run


def _detect_in_group(
    user_id: str, category: str, direction: EventDirection, group: list[FinancialEvent],
) -> Optional[RecurringSeries]:
    group = sorted(group, key=lambda e: e.event_date)[-MAX_OCCURRENCES_USED:]
    if len(group) < MIN_OCCURRENCES:
        return None

    run = _longest_consistent_run(group)
    if len(run) < MIN_OCCURRENCES or len(run) / len(group) < MIN_RUN_RATIO:
        return None

    dates = [e.event_date for e in run]
    diffs = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    median_diff = statistics.median(diffs) if diffs else float(dates[-1].toordinal())  # unreachable: len(run)>=2
    amounts = [e.amount for e in run if e.amount is not None]
    if not amounts:
        return None
    representative = max(amounts) if direction == EventDirection.DEBIT else min(amounts)

    interval_days = max(1, round(median_diff))
    sample_event = run[-1]
    return RecurringSeries(
        user_id=user_id,
        category=category,
        direction=direction,
        event_type=sample_event.event_type,
        currency=sample_event.currency,
        interval_days=interval_days,
        cadence_label=_cadence_label(interval_days),
        last_occurrence_date=dates[-1],
        representative_amount=representative,
        occurrences_used=len(run),
        low_confidence=(len(run) == MIN_OCCURRENCES),
        sample_event=sample_event,
    )


def detect_recurrence(events: list[FinancialEvent], as_of: date) -> list[RecurringSeries]:
    """Groups eligible history by (user_id, category, direction) and detects
    one RecurringSeries per group via the longest-consistent-run gate. Any
    group that fails is retried sub-grouped by description (see module
    docstring — this recovers real cases like interleaved base-salary +
    commission under one "salary" category). Detection only looks at events
    dated on or before `as_of` (the request date), never at future/projected
    data, to avoid circularity."""
    groups: dict[tuple[str, str, EventDirection], list[FinancialEvent]] = {}
    for event in events:
        if not _eligible_for_detection(event, as_of):
            continue
        key = (event.user_id, event.category, event.direction)
        groups.setdefault(key, []).append(event)

    series_list: list[RecurringSeries] = []
    for (user_id, category, direction), group in groups.items():
        primary = _detect_in_group(user_id, category, direction, group)
        if primary is not None:
            series_list.append(primary)
            continue

        by_description: dict[Optional[str], list[FinancialEvent]] = {}
        for event in group:
            by_description.setdefault(event.description, []).append(event)
        for sub_group in by_description.values():
            sub_series = _detect_in_group(user_id, category, direction, sub_group)
            if sub_series is not None:
                series_list.append(sub_series)

    return series_list


def project_occurrence_dates(series: RecurringSeries, start_date: date, horizon_end: date) -> list[date]:
    """Dates strictly after the series' last real occurrence, from
    `start_date` (inclusive) through `horizon_end` (inclusive)."""
    dates: list[date] = []
    candidate = series.last_occurrence_date + timedelta(days=series.interval_days)
    while candidate <= horizon_end:
        if candidate >= start_date:
            dates.append(candidate)
        candidate += timedelta(days=series.interval_days)
    return dates


def _has_nearby_real_event(
    series: RecurringSeries,
    candidate_date: date,
    real_events_by_key: dict[tuple[str, str, EventDirection], list[FinancialEvent]],
) -> bool:
    key = (series.user_id, series.category, series.direction)
    window = max(1, round(series.interval_days * DEDUP_WINDOW_FRACTION))
    for event in real_events_by_key.get(key, []):
        if abs((event.effective_date - candidate_date).days) <= window:
            return True
    return False


def materialize_recurring_events(
    events: list[FinancialEvent],
    as_of: date,
    horizon_end: date,
) -> tuple[list[FinancialEvent], list[str]]:
    """Detects recurring series from history (<= as_of), projects each
    forward through horizon_end, and returns the ORIGINAL events plus
    synthetic SCHEDULED rows for projected occurrences that don't already
    have a real row nearby. Never mutates or removes an existing event."""
    series_list = detect_recurrence(events, as_of)
    real_events_by_key: dict[tuple[str, str, EventDirection], list[FinancialEvent]] = {}
    for event in events:
        real_events_by_key.setdefault((event.user_id, event.category, event.direction), []).append(event)

    synthetic: list[FinancialEvent] = []
    notes: list[str] = []
    for series in series_list:
        confidence_note = " (low confidence: only 2 historical occurrences)" if series.low_confidence else ""
        notes.append(
            f"{series.user_id}/{series.category}/{series.direction.value}: detected "
            f"{series.cadence_label} recurrence (every {series.interval_days}d, "
            f"{series.occurrences_used} occurrences used, representative amount "
            f"{series.representative_amount} {series.currency}){confidence_note}"
        )
        for occurrence_date in project_occurrence_dates(series, as_of, horizon_end):
            if _has_nearby_real_event(series, occurrence_date, real_events_by_key):
                notes.append(
                    f"  skipped projecting {series.category} on {occurrence_date}: "
                    f"a real event already exists nearby"
                )
                continue
            sample = series.sample_event
            synthetic.append(sample.model_copy(update={
                "event_id": f"proj:{sample.event_id}:{occurrence_date.isoformat()}",
                "event_date": occurrence_date,
                "settlement_date": occurrence_date,
                "amount": series.representative_amount,
                "status": EventStatus.SCHEDULED,
                "recurrence": RecurrenceType.RECURRING,
                "linked_event_id": None,
                "amount_source": "recurrence_projection",
                "superseded_by": None,
            }))

    # Mark the real events that anchored a detected series as RECURRING too,
    # so is_eligible_for_spending_change is consistent for real future rows
    # that happen to belong to the same series (not just synthetic ones).
    recurring_keys = {(s.user_id, s.category, s.direction) for s in series_list}
    annotated = [
        e.model_copy(update={"recurrence": RecurrenceType.RECURRING})
        if (e.user_id, e.category, e.direction) in recurring_keys and e.recurrence != RecurrenceType.RECURRING
        else e
        for e in events
    ]
    return annotated + synthetic, notes
