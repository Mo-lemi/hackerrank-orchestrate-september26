"""
Unit tests for optimizer/plan_generator.py. Reuses the same hand-traced
rent/salary scenario as the engine/ tests where useful (balance=1000,
min_balance=200, rent -300 on offsets 5/35/65, salary +1000 on offsets
20/50/80) so results can be cross-checked against already-verified
safety_check behavior.

Run with: pytest code/tests/test_plan_generator.py -v
"""
from datetime import date, timedelta
from decimal import Decimal

from code.config.enums import (
    AffordabilityStatus, EventDirection, EventFlexibility, EventStatus, PaymentMethod, RecurrenceType,
)
from code.engine.cashflow_simulator import CashflowSimulator
from code.engine.safety_check import SafetyCheck
from code.optimizer.plan_generator import generate_candidates
from code.tests.fixtures.factories import make_event, make_payment_option, make_profile, make_request

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


def test_full_payment_eligible_and_safe_is_affordable_now():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="200",
                            payment_methods_user_will_consider=[PaymentMethod.FULL_PAYMENT])
    request = make_request(profile, requested_amount="500")
    sim = CashflowSimulator(profile, [], as_of=AS_OF)
    safety = SafetyCheck(sim)

    candidates = generate_candidates(request, sim, safety)
    full = next(c for c in candidates if c.method == PaymentMethod.FULL_PAYMENT)
    assert full.affordability_status == AffordabilityStatus.AFFORDABLE_NOW
    assert full.payments == ((AS_OF, Decimal("500")),)
    assert full.overrides == ()


def test_full_payment_absent_when_not_in_payment_methods():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="200",
                            payment_methods_user_will_consider=[PaymentMethod.INSTALLMENTS])
    request = make_request(profile, requested_amount="500")
    sim = CashflowSimulator(profile, [], as_of=AS_OF)
    safety = SafetyCheck(sim)

    candidates = generate_candidates(request, sim, safety)
    assert all(c.method != PaymentMethod.FULL_PAYMENT for c in candidates)


def test_full_payment_becomes_with_plan_via_spending_change():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="200",
                            payment_methods_user_will_consider=[PaymentMethod.FULL_PAYMENT],
                            stoppable_categories=["shopping"])
    flexible_event = make_event(
        category="shopping", amount="200", direction=EventDirection.DEBIT,
        status=EventStatus.SCHEDULED, event_date=AS_OF + timedelta(days=5),
        flexibility=EventFlexibility.STOPPABLE, recurrence=RecurrenceType.RECURRING,
        event_id="shopping_evt",
    )
    request = make_request(profile, events=[flexible_event], requested_amount="700")
    sim = CashflowSimulator(profile, [flexible_event], as_of=AS_OF)
    safety = SafetyCheck(sim)

    candidates = generate_candidates(request, sim, safety)
    full = next(c for c in candidates if c.method == PaymentMethod.FULL_PAYMENT)
    assert full.affordability_status == AffordabilityStatus.AFFORDABLE_WITH_PLAN
    assert len(full.overrides) == 1
    assert full.overrides[0].event_id == "shopping_evt"


def test_partial_payment_requires_allows_partial_payment_flag():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="200",
                            payment_methods_user_will_consider=[PaymentMethod.PARTIAL_PAYMENT])
    request = make_request(profile, requested_amount="1000", allows_partial_payment=False)
    sim = CashflowSimulator(profile, [], as_of=AS_OF)
    safety = SafetyCheck(sim)

    candidates = generate_candidates(request, sim, safety)
    assert all(c.method != PaymentMethod.PARTIAL_PAYMENT for c in candidates)


def test_partial_payment_legs_sum_exactly_to_requested_amount_and_are_jointly_safe():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="200",
                            payment_methods_user_will_consider=[PaymentMethod.PARTIAL_PAYMENT])
    events = _rent_salary_events()
    request = make_request(profile, events=events, requested_amount="1000",
                            allows_partial_payment=True,
                            desired_completion_date=AS_OF + timedelta(days=89))
    sim = CashflowSimulator(profile, events, as_of=AS_OF)
    safety = SafetyCheck(sim)

    candidates = generate_candidates(request, sim, safety)
    partial = next(c for c in candidates if c.method == PaymentMethod.PARTIAL_PAYMENT)
    assert partial.affordability_status == AffordabilityStatus.AFFORDABLE_WITH_PLAN
    assert len(partial.payments) == 2
    leg1, leg2 = partial.payments
    assert leg1[0] == AS_OF
    assert leg1[1] + leg2[1] == Decimal("1000.00")
    # Confirms the joint-safety re-verification: both legs computed
    # independently, but the combined plan is still checked as one.
    assert sim.is_plan_safe(partial.payments)


def test_installments_excluded_when_exceeding_max_installment_months():
    profile = make_profile(available_balance="100000", minimum_balance_to_keep="200",
                            payment_methods_user_will_consider=[PaymentMethod.INSTALLMENTS],
                            max_installment_months=2)
    option = make_payment_option(
        payment_method=PaymentMethod.INSTALLMENTS, payment_amount="100",
        number_of_payments=6, payment_frequency_days=30, first_payment_date=AS_OF + timedelta(days=5),
    )
    request = make_request(profile, payment_options=[option], requested_amount="600",
                            desired_completion_date=AS_OF + timedelta(days=365))
    sim = CashflowSimulator(profile, [], as_of=AS_OF)
    safety = SafetyCheck(sim)

    candidates = generate_candidates(request, sim, safety)
    assert all(c.method != PaymentMethod.INSTALLMENTS for c in candidates)


def test_installments_excluded_when_finishing_after_deadline():
    profile = make_profile(available_balance="100000", minimum_balance_to_keep="200",
                            payment_methods_user_will_consider=[PaymentMethod.INSTALLMENTS],
                            max_installment_months=12)
    option = make_payment_option(
        payment_method=PaymentMethod.INSTALLMENTS, payment_amount="100",
        number_of_payments=3, payment_frequency_days=30, first_payment_date=AS_OF + timedelta(days=5),
    )   # last payment = AS_OF + 65 days
    request = make_request(profile, payment_options=[option], requested_amount="300",
                            desired_completion_date=AS_OF + timedelta(days=30))
    sim = CashflowSimulator(profile, [], as_of=AS_OF)
    safety = SafetyCheck(sim)

    candidates = generate_candidates(request, sim, safety)
    assert all(c.method != PaymentMethod.INSTALLMENTS for c in candidates)


def test_installments_accepted_when_within_months_and_deadline():
    profile = make_profile(available_balance="100000", minimum_balance_to_keep="200",
                            payment_methods_user_will_consider=[PaymentMethod.INSTALLMENTS],
                            max_installment_months=12)
    option = make_payment_option(
        payment_method=PaymentMethod.INSTALLMENTS, payment_amount="105",
        number_of_payments=3, payment_frequency_days=30, first_payment_date=AS_OF + timedelta(days=5),
        financing_fee="15", total_payable_amount="315",
    )
    request = make_request(profile, payment_options=[option], requested_amount="300",
                            desired_completion_date=AS_OF + timedelta(days=365))
    sim = CashflowSimulator(profile, [], as_of=AS_OF)
    safety = SafetyCheck(sim)

    candidates = generate_candidates(request, sim, safety)
    installments = next(c for c in candidates if c.method == PaymentMethod.INSTALLMENTS)
    assert installments.total_paid == Decimal("315")   # fee-inclusive, penalized later by ranking
    assert installments.payment_option_id == option.payment_option_id


def test_wait_gated_on_full_payment_acceptance_not_wait_itself():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="200",
                            payment_methods_user_will_consider=[PaymentMethod.INSTALLMENTS])
    events = _rent_salary_events()
    request = make_request(profile, events=events, requested_amount="1000")
    sim = CashflowSimulator(profile, events, as_of=AS_OF)
    safety = SafetyCheck(sim)

    candidates = generate_candidates(request, sim, safety)
    assert all(c.method != PaymentMethod.WAIT for c in candidates)


def test_wait_candidate_is_always_affordable_later():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="200",
                            payment_methods_user_will_consider=[PaymentMethod.FULL_PAYMENT])
    events = _rent_salary_events()
    request = make_request(profile, events=events, requested_amount="1000")
    sim = CashflowSimulator(profile, events, as_of=AS_OF)
    safety = SafetyCheck(sim)

    candidates = generate_candidates(request, sim, safety)
    wait = next(c for c in candidates if c.method == PaymentMethod.WAIT)
    assert wait.affordability_status == AffordabilityStatus.AFFORDABLE_LATER
    assert wait.payments == ((AS_OF + timedelta(days=20), Decimal("1000")),)


def test_no_candidates_when_nothing_is_safe_or_eligible():
    profile = make_profile(available_balance="100", minimum_balance_to_keep="90",
                            payment_methods_user_will_consider=[])   # accepts nothing
    request = make_request(profile, requested_amount="5000")
    sim = CashflowSimulator(profile, [], as_of=AS_OF)
    safety = SafetyCheck(sim)

    candidates = generate_candidates(request, sim, safety)
    assert candidates == []
