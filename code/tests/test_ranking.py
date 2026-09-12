"""
Unit tests for optimizer/ranking.py. Candidates are constructed directly
(not via generate_candidates) to isolate the sort-key logic itself from plan
construction, which is already covered in test_plan_generator.py.

Run with: pytest code/tests/test_ranking.py -v
"""
from datetime import date, timedelta
from decimal import Decimal

from code.config.enums import AffordabilityStatus, PaymentMethod
from code.engine.cashflow_simulator import Override
from code.optimizer.plan_generator import PlanCandidate
from code.optimizer.ranking import rank_candidates

D0 = date(2026, 1, 1)


def _candidate(**overrides_kwargs):
    base = dict(
        method=PaymentMethod.FULL_PAYMENT,
        affordability_status=AffordabilityStatus.AFFORDABLE_NOW,
        payments=((D0, Decimal("100")),),
        overrides=(),
        total_paid=Decimal("100"),
        payment_option_id=None,
    )
    base.update(overrides_kwargs)
    return PlanCandidate(**base)


def test_status_tier_dominates_every_other_axis():
    cheap_but_with_plan = _candidate(
        affordability_status=AffordabilityStatus.AFFORDABLE_WITH_PLAN, total_paid=Decimal("100"),
    )
    expensive_but_now = _candidate(
        affordability_status=AffordabilityStatus.AFFORDABLE_NOW, total_paid=Decimal("100000"),
    )
    winner = rank_candidates([cheap_but_with_plan, expensive_but_now])
    assert winner is expensive_but_now


def test_fewer_spending_changes_preferred_within_same_status():
    with_changes = _candidate(
        affordability_status=AffordabilityStatus.AFFORDABLE_WITH_PLAN,
        overrides=(Override(kind="stop", event_id="e1"), Override(kind="stop", event_id="e2")),
    )
    without_changes = _candidate(affordability_status=AffordabilityStatus.AFFORDABLE_WITH_PLAN, overrides=())
    winner = rank_candidates([with_changes, without_changes])
    assert winner is without_changes


def test_lower_total_paid_preferred():
    expensive = _candidate(total_paid=Decimal("500"))
    cheap = _candidate(total_paid=Decimal("300"))
    winner = rank_candidates([expensive, cheap])
    assert winner is cheap


def test_earlier_start_date_preferred():
    later_start = _candidate(payments=((D0 + timedelta(days=10), Decimal("100")),))
    earlier_start = _candidate(payments=((D0 + timedelta(days=5), Decimal("100")),))
    winner = rank_candidates([later_start, earlier_start])
    assert winner is earlier_start


def test_earlier_completion_date_preferred_over_later_when_start_ties():
    finishes_late = _candidate(payments=((D0, Decimal("50")), (D0 + timedelta(days=60), Decimal("50"))))
    finishes_early = _candidate(payments=((D0, Decimal("50")), (D0 + timedelta(days=30), Decimal("50"))))
    winner = rank_candidates([finishes_late, finishes_early])
    assert winner is finishes_early


def test_fewer_payments_breaks_tie_when_completion_date_matches():
    three_payments = _candidate(payments=(
        (D0, Decimal("50")), (D0 + timedelta(days=10), Decimal("50")), (D0 + timedelta(days=20), Decimal("50")),
    ))
    two_payments = _candidate(payments=((D0, Decimal("100")), (D0 + timedelta(days=20), Decimal("50"))))
    winner = rank_candidates([three_payments, two_payments])
    assert winner is two_payments


def test_method_tiebreak_when_fully_tied_on_all_prior_axes():
    partial = _candidate(method=PaymentMethod.PARTIAL_PAYMENT)
    full = _candidate(method=PaymentMethod.FULL_PAYMENT)
    winner = rank_candidates([partial, full])
    assert winner is full   # full_payment ranks ahead of partial_payment


def test_lowest_payment_option_id_breaks_installment_ties():
    option_b = _candidate(method=PaymentMethod.INSTALLMENTS, payment_option_id="payment_option_02")
    option_a = _candidate(method=PaymentMethod.INSTALLMENTS, payment_option_id="payment_option_01")
    winner = rank_candidates([option_b, option_a])
    assert winner is option_a


def test_empty_candidate_list_returns_none():
    assert rank_candidates([]) is None
