"""
Unit tests for engine/recurrence_detector.py. Uses synthetic fixtures
(code/tests/fixtures/factories.py) — see that module's docstring for why:
fast, isolated, no dependency on dataset/ being present.

Run with: pytest code/tests/test_recurrence_detector.py -v
"""
from datetime import date, timedelta
from decimal import Decimal

from code.config.enums import EventDirection, EventStatus, EventType, RecurrenceType
from code.engine.recurrence_detector import detect_recurrence, materialize_recurring_events
from code.fixtures.factories import make_event


def test_detects_monthly_series():
    events = [
        make_event(category="rent", amount="5000", event_date=date(2025, 10, 2)),
        make_event(category="rent", amount="5000", event_date=date(2025, 11, 2)),
        make_event(category="rent", amount="5000", event_date=date(2025, 12, 2)),
        make_event(category="rent", amount="5000", event_date=date(2026, 1, 2)),
    ]
    series = detect_recurrence(events, as_of=date(2026, 1, 15))
    assert len(series) == 1
    s = series[0]
    assert s.category == "rent"
    assert s.cadence_label == "monthly"
    assert 28 <= s.interval_days <= 31
    assert s.representative_amount == Decimal("5000")
    assert s.low_confidence is False


def test_rejects_irregular_series():
    # Same category, but wildly inconsistent intervals — should not be
    # treated as recurring.
    events = [
        make_event(category="dining", amount="40", event_date=date(2025, 12, 1)),
        make_event(category="dining", amount="55", event_date=date(2025, 12, 3)),
        make_event(category="dining", amount="20", event_date=date(2025, 12, 28)),
        make_event(category="dining", amount="60", event_date=date(2026, 1, 1)),
    ]
    series = detect_recurrence(events, as_of=date(2026, 1, 15))
    assert series == []


def test_two_occurrences_are_low_confidence_but_still_detected():
    events = [
        make_event(category="salary", amount="3000", direction=EventDirection.CREDIT,
                    event_date=date(2025, 12, 1)),
        make_event(category="salary", amount="3000", direction=EventDirection.CREDIT,
                    event_date=date(2026, 1, 1)),
    ]
    series = detect_recurrence(events, as_of=date(2026, 1, 15))
    assert len(series) == 1
    assert series[0].low_confidence is True
    assert series[0].interval_days == 31


def test_representative_amount_is_conservative_by_direction():
    # Debit (expense): conservative = MAX historical amount (never
    # under-forecast an outflow).
    debit_events = [
        make_event(category="groceries", amount="80", event_date=date(2025, 12, 1)),
        make_event(category="groceries", amount="95", event_date=date(2025, 12, 9)),
        make_event(category="groceries", amount="70", event_date=date(2025, 12, 17)),
    ]
    series = detect_recurrence(debit_events, as_of=date(2025, 12, 20))
    assert series[0].representative_amount == Decimal("95")

    # Credit (income): conservative = MIN historical amount (never
    # over-forecast an inflow).
    credit_events = [
        make_event(category="salary", amount="3000", direction=EventDirection.CREDIT,
                    event_date=date(2025, 10, 1)),
        make_event(category="salary", amount="3100", direction=EventDirection.CREDIT,
                    event_date=date(2025, 11, 1)),
        make_event(category="salary", amount="2950", direction=EventDirection.CREDIT,
                    event_date=date(2025, 12, 1)),
    ]
    series = detect_recurrence(credit_events, as_of=date(2025, 12, 5))
    assert series[0].representative_amount == Decimal("2950")


def test_materialize_projects_future_occurrences():
    events = [
        make_event(category="rent", amount="5000", event_date=date(2025, 11, 2)),
        make_event(category="rent", amount="5000", event_date=date(2025, 12, 2)),
        make_event(category="rent", amount="5000", event_date=date(2026, 1, 2)),
    ]
    as_of = date(2026, 1, 15)
    horizon_end = as_of + timedelta(days=89)
    merged, notes = materialize_recurring_events(events, as_of=as_of, horizon_end=horizon_end)

    projected_rent = [e for e in merged if e.category == "rent" and e.amount_source == "recurrence_projection"]
    projected_dates = sorted(e.event_date for e in projected_rent)
    # Next occurrences after 2026-01-02 at ~30-31 day cadence, within the 90-day window
    assert date(2026, 2, 1) in projected_dates or date(2026, 2, 2) in projected_dates
    assert all(e.recurrence == RecurrenceType.RECURRING for e in projected_rent)
    assert all(e.status == EventStatus.SCHEDULED for e in projected_rent)


def test_materialize_dedups_against_a_real_future_row():
    events = [
        make_event(category="rent", amount="5000", event_date=date(2025, 11, 2)),
        make_event(category="rent", amount="5000", event_date=date(2025, 12, 2)),
        make_event(category="rent", amount="5000", event_date=date(2026, 1, 2)),
        # A real row already exists for the next expected occurrence —
        # projection must NOT create a duplicate near it.
        make_event(category="rent", amount="5200", event_date=date(2026, 2, 1),
                    status=EventStatus.SCHEDULED, event_id="real_future_rent"),
    ]
    as_of = date(2026, 1, 15)
    horizon_end = as_of + timedelta(days=89)
    merged, notes = materialize_recurring_events(events, as_of=as_of, horizon_end=horizon_end)

    feb_rent_rows = [e for e in merged if e.category == "rent" and e.event_date.month == 2 and e.event_date.year == 2026]
    assert len(feb_rent_rows) == 1, f"expected exactly one February rent row, got {feb_rent_rows}"
    assert feb_rent_rows[0].event_id == "real_future_rent"
