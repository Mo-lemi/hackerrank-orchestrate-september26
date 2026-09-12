"""
Unit tests for engine/cashflow_simulator.py, including the hand-traced
scenario verified arithmetically before this file was written:

    balance=1000, minimum_balance_to_keep=200
    rent: -300 on day offsets 5, 35, 65
    salary: +1000 on day offsets 20, 50, 80

Events are supplied as literal future-dated rows (not via recurrence
detection, which is tested separately in test_recurrence_detector.py) so
these tests isolate the simulator's own day-walk correctness.

Run with: pytest code/tests/test_cashflow_simulator.py -v
"""
from datetime import date, timedelta
from decimal import Decimal

from code.config.enums import EventDirection, EventFlexibility, EventStatus, RecurrenceType
from code.engine.cashflow_simulator import CashflowSimulator, Override
from code.fixtures.factories import make_event, make_profile

AS_OF = date(2026, 1, 1)


def _rent_salary_events():
    events = []
    for offset in (5, 35, 65):
        events.append(make_event(
            category="rent", amount="300", direction=EventDirection.DEBIT,
            status=EventStatus.SCHEDULED, event_date=AS_OF + timedelta(days=offset),
        ))
    for offset in (20, 50, 80):
        events.append(make_event(
            category="salary", amount="1000", direction=EventDirection.CREDIT,
            status=EventStatus.SCHEDULED, event_date=AS_OF + timedelta(days=offset),
        ))
    return events


def test_balance_walk_matches_hand_trace():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="200")
    sim = CashflowSimulator(profile, _rent_salary_events(), as_of=AS_OF)
    forecast = sim.simulate()

    assert forecast.balance_on(AS_OF) == Decimal("1000.00")
    assert forecast.balance_on(AS_OF + timedelta(days=5)) == Decimal("700.00")
    assert forecast.balance_on(AS_OF + timedelta(days=19)) == Decimal("700.00")
    assert forecast.balance_on(AS_OF + timedelta(days=20)) == Decimal("1700.00")
    assert forecast.balance_on(AS_OF + timedelta(days=35)) == Decimal("1400.00")
    assert forecast.min_balance() == Decimal("700.00")   # the lowest point across the whole horizon


def test_injected_payment_is_treated_as_a_debit_on_its_date():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="200")
    sim = CashflowSimulator(profile, _rent_salary_events(), as_of=AS_OF)
    forecast = sim.simulate(injected_payments=((AS_OF, Decimal("500")),))

    assert forecast.balance_on(AS_OF) == Decimal("500.00")
    assert forecast.balance_on(AS_OF + timedelta(days=5)) == Decimal("200.00")
    assert forecast.min_balance() == Decimal("200.00")


def test_is_plan_safe_checks_the_whole_horizon_not_just_payment_day():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="200")
    sim = CashflowSimulator(profile, _rent_salary_events(), as_of=AS_OF)

    # Paying 500 today looks fine on day 0 (1000-500=500 >= 200) but breaches
    # exactly at the day-5 rent debit (500-300=200, still OK) — try 501 to
    # push it just under.
    assert sim.is_plan_safe(((AS_OF, Decimal("500")),)) is True
    assert sim.is_plan_safe(((AS_OF, Decimal("501")),)) is False


def test_stop_override_removes_event_effect():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="200")
    events = _rent_salary_events()
    rent_event_id = events[0].event_id   # the day-5 rent row
    sim = CashflowSimulator(profile, events, as_of=AS_OF)

    forecast = sim.simulate(overrides=(Override(kind="stop", event_id=rent_event_id),))
    # With the day-5 rent stopped, balance should stay at 1000 until salary on day 20
    assert forecast.balance_on(AS_OF + timedelta(days=5)) == Decimal("1000.00")
    assert forecast.balance_on(AS_OF + timedelta(days=19)) == Decimal("1000.00")


def test_reduce_to_override_caps_amount():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="200")
    events = _rent_salary_events()
    rent_event_id = events[0].event_id
    sim = CashflowSimulator(profile, events, as_of=AS_OF)

    forecast = sim.simulate(overrides=(Override(kind="reduce_to", event_id=rent_event_id, new_amount=Decimal("100")),))
    assert forecast.balance_on(AS_OF + timedelta(days=5)) == Decimal("900.00")


def test_pending_credit_excluded_pending_debit_included():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="200")
    events = [
        make_event(category="windfall", amount="500", direction=EventDirection.CREDIT,
                    status=EventStatus.PENDING, event_date=AS_OF + timedelta(days=3)),
        make_event(category="utilities", amount="150", direction=EventDirection.DEBIT,
                    status=EventStatus.PENDING, event_date=AS_OF + timedelta(days=3)),
    ]
    sim = CashflowSimulator(profile, events, as_of=AS_OF)
    forecast = sim.simulate()
    # Pending credit ignored (unconfirmed income); pending debit reserved (counted).
    assert forecast.balance_on(AS_OF + timedelta(days=3)) == Decimal("850.00")


def test_superseded_event_is_excluded():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="200")
    events = [
        make_event(category="shopping", amount="200", direction=EventDirection.DEBIT,
                    status=EventStatus.PENDING, event_date=AS_OF + timedelta(days=2),
                    superseded_by="some_other_event"),
    ]
    sim = CashflowSimulator(profile, events, as_of=AS_OF)
    forecast = sim.simulate()
    assert forecast.balance_on(AS_OF + timedelta(days=2)) == Decimal("1000.00")
