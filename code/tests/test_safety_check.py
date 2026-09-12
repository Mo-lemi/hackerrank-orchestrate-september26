"""
Unit tests for engine/safety_check.py, reusing the same hand-traced
rent/salary scenario as test_cashflow_simulator.py:

    balance=1000, minimum_balance_to_keep=200
    rent: -300 on day offsets 5, 35, 65
    salary: +1000 on day offsets 20, 50, 80

Expected results (verified by a separate plain-Python arithmetic replica
before this file was written):
    amount_safe_to_pay(1000) == 500.00   (bound by the day-5 rent debit,
                                           not the naive day-0 balance)
    earliest_date_for_full_payment(900) == as_of + 20 days (the salary payday)

Run with: pytest code/tests/test_safety_check.py -v
"""
from datetime import date, timedelta
from decimal import Decimal

from code.config.enums import EventDirection, EventStatus
from code.engine.cashflow_simulator import CashflowSimulator
from code.engine.safety_check import SafetyCheck
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


def _safety_check():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="200")
    sim = CashflowSimulator(profile, _rent_salary_events(), as_of=AS_OF)
    return SafetyCheck(sim)


def test_amount_safe_to_pay_binds_on_worst_future_day_not_day_zero():
    safety = _safety_check()
    result = safety.amount_safe_to_pay(Decimal("1000"))
    assert result == Decimal("500.00")


def test_amount_safe_to_pay_is_zero_when_already_unsafe():
    profile = make_profile(available_balance="150", minimum_balance_to_keep="200")
    sim = CashflowSimulator(profile, [], as_of=AS_OF)
    safety = SafetyCheck(sim)
    assert safety.amount_safe_to_pay(Decimal("100")) == Decimal("0.00")


def test_amount_safe_to_pay_capped_at_requested_amount():
    # Plenty of headroom — should return exactly requested_amount, never more.
    profile = make_profile(available_balance="100000", minimum_balance_to_keep="200")
    sim = CashflowSimulator(profile, [], as_of=AS_OF)
    safety = SafetyCheck(sim)
    assert safety.amount_safe_to_pay(Decimal("250")) == Decimal("250.00")


def test_earliest_date_for_full_payment_finds_the_payday():
    safety = _safety_check()
    result = safety.earliest_date_for_full_payment(Decimal("900"))
    assert result == AS_OF + timedelta(days=20)


def test_earliest_date_for_full_payment_none_when_never_safe():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="200")
    sim = CashflowSimulator(profile, [], as_of=AS_OF)   # no income at all in the window
    safety = SafetyCheck(sim)
    result = safety.earliest_date_for_full_payment(Decimal("5000"))
    assert result is None


def test_earliest_date_for_full_payment_equals_request_date_when_already_safe():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="200")
    sim = CashflowSimulator(profile, [], as_of=AS_OF)
    safety = SafetyCheck(sim)
    result = safety.earliest_date_for_full_payment(Decimal("500"))
    assert result == AS_OF
