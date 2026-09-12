"""
Generates every ELIGIBLE, SAFE candidate plan for one EvaluationRequest.
Eligibility gating happens FIRST and is strict — a method the user hasn't
opted into, or that violates allows_partial_payment, never produces a
candidate at all, regardless of whether it would otherwise be safe.

IMPORTANT — output-field decoupling: `amount_safe_to_pay` and
`earliest_date_for_full_payment` on the FINAL output row are always the
values SafetyCheck computes directly (with NO overrides), independent of
which candidate wins ranking — this is explicit in problem_statement.md:
"earliest_date_for_full_payment measures financial capacity independently
of the user's payment-method preferences." This module uses those same
values internally to build the partial_payment and wait candidates, but the
caller (pipeline/request_processor.py) must still source the OUTPUT row's
amount_safe_to_pay / earliest_date_for_full_payment from SafetyCheck
directly, not from whichever PlanCandidate.method ranking.py picks.

Eligibility notes worth flagging explicitly:
  - `wait` is gated on payment_methods_user_will_consider containing
    FULL_PAYMENT, not WAIT — problem_statement.md states this exactly:
    "wait is eligible when full payment becomes safe later and the user
    accepts full_payment." Confirmed against the real financial_profiles.csv:
    payment_methods_user_will_consider only ever contains full_payment /
    partial_payment / installments — WAIT and NOT_RECOMMENDED never appear
    there, since they're system fallbacks, not user preferences.
  - `wait` always maps to AFFORDABLE_LATER status, never AFFORDABLE_NOW/
    WITH_PLAN, even if its earliest date happens to land on or before
    desired_completion_date — a plain wait isn't one of the three mechanisms
    with_plan's definition enumerates (partial schedule, installments,
    spending changes). This matches the real sample_requests.csv trace done
    earlier in this project: request_03's earliest_date_for_full_payment
    equals desired_completion_date exactly, and its ground-truth status is
    still affordable_later with method wait.
  - installments must complete (last payment date) on or before
    desired_completion_date — generalizing partial_payment's explicit
    deadline requirement, since affordable_with_plan is defined as
    completing "by its deadline" for any of its three mechanisms.
  - max_installment_months is checked via number_of_payments directly
    (verified against request_payment_options.csv: 100% of installment rows
    use a 28/30/31-day payment_frequency_days, i.e. genuinely monthly
    cadence, so number_of_payments IS months-of-installments), with a
    span-based fallback for any option whose cadence isn't ~monthly.
  - Every candidate's joint safety is verified with an actual
    simulator.is_plan_safe() call on its full payment tuple — independently
    -computed values (amount_safe_to_pay, earliest_date_for_full_payment)
    are never combined into a multi-leg plan without re-verifying the
    COMBINED plan is safe, since each was computed assuming no other debit
    exists. A partial_payment plan that fails this joint check is dropped
    rather than offered unsafe.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from code.config import settings
from code.config.enums import AffordabilityStatus, PaymentMethod
from code.domain.schemas import EvaluationRequest, RequestPaymentOption
from code.engine.cashflow_simulator import CashflowSimulator, Override
from code.engine.safety_check import SafetyCheck
from code.optimizer.spending_changes import find_spending_changes_for_safety

_MONTHLY_FREQUENCY_RANGE = (25, 35)   # days; real dataset uses 28/30/31 exclusively


@dataclass(frozen=True)
class PlanCandidate:
    method: PaymentMethod
    affordability_status: AffordabilityStatus
    payments: tuple[tuple[date, Decimal], ...]   # chronological (date, amount) legs
    overrides: tuple[Override, ...] = ()
    total_paid: Decimal = Decimal("0")
    payment_option_id: Optional[str] = None       # for installments; used by ranking's final tie-break
    notes: tuple[str, ...] = field(default_factory=tuple)


def _installment_months_used(option: RequestPaymentOption) -> int:
    if _MONTHLY_FREQUENCY_RANGE[0] <= (option.payment_frequency_days or 0) <= _MONTHLY_FREQUENCY_RANGE[1]:
        return option.number_of_payments
    span_days = (option.payment_dates()[-1] - option.first_payment_date).days
    return -(-span_days // 30)   # ceil division, no non-monthly cadence observed in the real dataset


def _generate_full_payment(request: EvaluationRequest, simulator: CashflowSimulator) -> Optional[PlanCandidate]:
    if PaymentMethod.FULL_PAYMENT not in request.profile.payment_methods_user_will_consider:
        return None

    payments = ((request.request_date, request.requested_amount),)
    if simulator.is_plan_safe(payments):
        return PlanCandidate(
            method=PaymentMethod.FULL_PAYMENT,
            affordability_status=AffordabilityStatus.AFFORDABLE_NOW,
            payments=payments,
            total_paid=request.requested_amount,
        )

    overrides = find_spending_changes_for_safety(simulator, request.profile, payments)
    if overrides is not None:
        return PlanCandidate(
            method=PaymentMethod.FULL_PAYMENT,
            affordability_status=AffordabilityStatus.AFFORDABLE_WITH_PLAN,
            payments=payments,
            overrides=overrides,
            total_paid=request.requested_amount,
            notes=("full payment made safe via spending changes",),
        )
    return None


def _generate_partial_payment(
    request: EvaluationRequest, simulator: CashflowSimulator, safety: SafetyCheck,
) -> Optional[PlanCandidate]:
    if not request.allows_partial_payment:
        return None
    if PaymentMethod.PARTIAL_PAYMENT not in request.profile.payment_methods_user_will_consider:
        return None

    amount_safe = safety.amount_safe_to_pay(request.requested_amount)
    if not (0 < amount_safe < request.requested_amount):
        return None

    earliest = safety.earliest_date_for_full_payment(request.requested_amount)
    if earliest is None or earliest > request.desired_completion_date:
        return None

    remaining = (request.requested_amount - amount_safe).quantize(settings.MONEY_QUANTIZE)
    payments = ((request.request_date, amount_safe), (earliest, remaining))

    # Both legs were computed independently (neither anticipated the other's
    # debit) — verify the COMBINED plan is actually safe before offering it.
    if not simulator.is_plan_safe(payments):
        return None

    return PlanCandidate(
        method=PaymentMethod.PARTIAL_PAYMENT,
        affordability_status=AffordabilityStatus.AFFORDABLE_WITH_PLAN,
        payments=payments,
        total_paid=request.requested_amount,
    )


def _generate_installments(request: EvaluationRequest, simulator: CashflowSimulator) -> list[PlanCandidate]:
    profile = request.profile
    if PaymentMethod.INSTALLMENTS not in profile.payment_methods_user_will_consider:
        return []
    if profile.max_installment_months is None:
        return []

    candidates: list[PlanCandidate] = []
    for option in request.payment_options:
        if option.payment_method != PaymentMethod.INSTALLMENTS:
            continue
        if _installment_months_used(option) > profile.max_installment_months:
            continue

        dates = option.payment_dates()
        if dates[-1] > request.desired_completion_date:
            continue

        payments = tuple((d, option.payment_amount) for d in dates)
        if not simulator.is_plan_safe(payments):
            continue

        candidates.append(PlanCandidate(
            method=PaymentMethod.INSTALLMENTS,
            affordability_status=AffordabilityStatus.AFFORDABLE_WITH_PLAN,
            payments=payments,
            total_paid=option.total_payable_amount,   # includes financing_fee — penalized in ranking
            payment_option_id=option.payment_option_id,
        ))
    return candidates


def _generate_wait(request: EvaluationRequest, safety: SafetyCheck) -> Optional[PlanCandidate]:
    # Eligibility keys off FULL_PAYMENT acceptance, not a "wait" preference —
    # see module docstring.
    if PaymentMethod.FULL_PAYMENT not in request.profile.payment_methods_user_will_consider:
        return None

    earliest = safety.earliest_date_for_full_payment(request.requested_amount)
    if earliest is None:
        return None

    return PlanCandidate(
        method=PaymentMethod.WAIT,
        affordability_status=AffordabilityStatus.AFFORDABLE_LATER,
        payments=((earliest, request.requested_amount),),
        total_paid=request.requested_amount,
    )


def generate_candidates(
    request: EvaluationRequest,
    simulator: CashflowSimulator,
    safety: SafetyCheck,
) -> list[PlanCandidate]:
    """Returns every eligible, safe candidate for this request. Empty list
    means no safe eligible plan exists at all — the caller should then
    construct the NOT_RECOMMENDED / NOT_AFFORDABLE fallback row directly
    (there is no PlanCandidate for that fallback; it's not a "plan")."""
    candidates: list[PlanCandidate] = []

    full = _generate_full_payment(request, simulator)
    if full is not None:
        candidates.append(full)

    partial = _generate_partial_payment(request, simulator, safety)
    if partial is not None:
        candidates.append(partial)

    candidates.extend(_generate_installments(request, simulator))

    wait = _generate_wait(request, safety)
    if wait is not None:
        candidates.append(wait)

    return candidates
