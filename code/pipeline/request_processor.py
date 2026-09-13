"""
Orchestrates the full per-request pipeline. This is the one place that
wires together every module built across this project:

    reconciliation (linked_event_id chains, then message evidence)
      -> FX normalization to profile.home_currency
      -> CashflowSimulator (which internally runs recurrence detection)
      -> SafetyCheck (amount_safe_to_pay / earliest_date_for_full_payment)
      -> optimizer (generate_candidates -> rank_candidates)
      -> fallback to not_recommended / not_affordable if nothing qualifies
      -> deterministic decision_explanation
      -> OutputRow

NAMING NOTE: this turn's brief referred to "PurchaseRequest" and
"CashflowEvent" — this project's actual, already-built (and already
CSV-verified) types are RequestRecord and FinancialEvent respectively
(domain/schemas.py). This module uses those real names throughout rather
than introducing new ones that would just alias them.

EXPLANATION NOTE: the original architecture (this project's first design
pass) sketched an `explanation/explainer.py` seam using Claude to write
decision_explanation. That module was never requested or built. Rather than
silently invent an unreviewed LLM call inside this integration step,
_build_explanation() below is a deterministic, template-based generator —
grounded only in facts already computed (amounts, dates, minimum balance),
styled after the real decision_explanation strings observed in
sample_requests.csv (e.g. "Pay X today. This leaves at least Y available.").
If a Claude-backed explainer is wanted later, it can replace this
function's body without touching its call site or any other module.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from code.config import settings
from code.config.enums import AffordabilityStatus, PaymentMethod
from code.domain.schemas import (
    EvaluationRequest,
    FinancialEvent,
    FinancialProfile,
    ImageRecord,
    MessageRecord,
    OutputRow,
    RequestPaymentOption,
    RequestRecord,
)
from code.engine.cashflow_simulator import CashflowSimulator, Override
from code.engine.safety_check import SafetyCheck
from code.io_.fx import FxTable, normalize_events_to_home_currency
from code.io_.writers import format_payment_plan
from code.multimodal.vision_extractor import VisionExtractor
from code.optimizer.plan_generator import PlanCandidate, generate_candidates
from code.optimizer.ranking import rank_candidates
from code.optimizer.spending_changes import format_spending_changes
from code.reconciliation.conflict_resolver import reconcile_events


@dataclass
class ProcessingContext:
    """Everything process_request needs for ONE request, pre-filtered by
    the caller (main.py groups the full dataset by user once, up front) so
    this function stays pure and testable rather than re-scanning the whole
    dataset per request."""
    profile: FinancialProfile
    events: list[FinancialEvent]                        # this user's events only; NOT yet reconciled/FX-normalized
    messages: list[MessageRecord]                        # this user's messages only
    payment_options: list[RequestPaymentOption]          # THIS request's options only
    images_by_related_event: dict[str, ImageRecord]      # this user's images, keyed by related_event_id
    fx_table: FxTable
    vision_extractor: Optional[VisionExtractor] = None   # None => blank amounts stay unresolved (logged, not guessed)


def process_request(record: RequestRecord, ctx: ProcessingContext) -> OutputRow:
    notes: list[str] = []

    # 1. Multimodal seam: resolve blank amounts from linked images. Never
    #    guesses when unavailable — an unresolved event is excluded from
    #    the simulation (see CashflowSimulator.unresolved_amount_notes),
    #    not defaulted to 0.
    events = ctx.events
    if ctx.vision_extractor is not None:
        events, vision_notes = ctx.vision_extractor.resolve_blank_amounts(events, ctx.images_by_related_event)
        notes.extend(vision_notes)

    # 2. Reconciliation: linked_event_id duplicate/chain resolution, then
    #    message-evidence merge (which can override a chain verdict).
    events, reconcile_notes = reconcile_events(events, ctx.messages)
    notes.extend(reconcile_notes)

    # 3. Home-currency normalization — the only place amounts cross a
    #    currency boundary, using each event's own settlement date.
    events = normalize_events_to_home_currency(events, ctx.profile, ctx.fx_table)

    # 4. Assemble the fully-reconciled request context and run the
    #    deterministic engine. CashflowSimulator internally runs recurrence
    #    detection/projection anchored at record.request_date.
    request = EvaluationRequest.from_request_record(
        record, ctx.profile, events, ctx.payment_options, resolved_notes=notes,
    )
    simulator = CashflowSimulator(request.profile, request.events, as_of=request.request_date)
    notes.extend(simulator.recurrence_notes)
    notes.extend(simulator.unresolved_amount_notes)
    safety = SafetyCheck(simulator)

    # 5. Output-level capacity facts. These are ALWAYS the values computed
    #    directly here, independent of which candidate plan_generator/
    #    ranking eventually picks — problem_statement.md is explicit that
    #    earliest_date_for_full_payment "measures financial capacity
    #    independently of the user's payment-method preferences," and the
    #    same independence applies to amount_safe_to_pay ("before optional
    #    spending changes"). Do not substitute a winning candidate's own
    #    payment amount/date for these two fields.
    amount_safe_to_pay = safety.amount_safe_to_pay(request.requested_amount)
    earliest_date = safety.earliest_date_for_full_payment(request.requested_amount)

    # 6. Candidate generation (strict eligibility gating happens inside
    #    plan_generator.py) and ranking (the tie-break hierarchy in
    #    ranking.py).
    candidates = generate_candidates(request, simulator, safety)
    winner = rank_candidates(candidates)

    if winner is None:
        return _build_fallback_row(request, amount_safe_to_pay, earliest_date, simulator)
    return _build_winner_row(request, winner, amount_safe_to_pay, earliest_date, simulator)


def _build_winner_row(
    request: EvaluationRequest,
    winner: PlanCandidate,
    amount_safe_to_pay: Decimal,
    earliest_date: Optional[date],
    simulator: CashflowSimulator,
) -> OutputRow:
    return OutputRow(
        request_id=request.request_id,
        amount_safe_to_pay=amount_safe_to_pay,
        affordability_status=winner.affordability_status,
        recommended_payment_method=winner.method,
        payment_plan=format_payment_plan(winner.payments),
        earliest_date_for_full_payment=earliest_date,
        spending_changes_needed=format_spending_changes(winner.overrides),
        decision_explanation=_build_explanation(
            request=request,
            method=winner.method,
            payments=winner.payments,
            overrides=winner.overrides,
            amount_safe_to_pay=amount_safe_to_pay,
            simulator=simulator,
        ),
    )


def _build_fallback_row(
    request: EvaluationRequest,
    amount_safe_to_pay: Decimal,
    earliest_date: Optional[date],
    simulator: CashflowSimulator,
) -> OutputRow:
    """No eligible plan is both accepted by the user's preferences and safe
    within the 90-day horizon. amount_safe_to_pay / earliest_date are still
    reported (they're independent capacity facts, per problem_statement.md)
    even though nothing is being recommended."""
    return OutputRow(
        request_id=request.request_id,
        amount_safe_to_pay=amount_safe_to_pay,
        affordability_status=AffordabilityStatus.NOT_AFFORDABLE,
        recommended_payment_method=PaymentMethod.NOT_RECOMMENDED,
        payment_plan=settings.NONE_TOKEN,
        earliest_date_for_full_payment=earliest_date,
        spending_changes_needed=settings.NONE_TOKEN,
        decision_explanation=_build_explanation(
            request=request,
            method=PaymentMethod.NOT_RECOMMENDED,
            payments=(),
            overrides=(),
            amount_safe_to_pay=amount_safe_to_pay,
            simulator=simulator,
        ),
    )


def _build_explanation(
    request: EvaluationRequest,
    method: PaymentMethod,
    payments: tuple[tuple[date, Decimal], ...],
    overrides: tuple[Override, ...],
    amount_safe_to_pay: Decimal,
    simulator: CashflowSimulator,
) -> str:
    """Deterministic, template-based explanation — see module docstring for
    why this isn't an LLM call. Every number/date used is one already
    computed by the engine or optimizer; nothing here is invented."""
    currency = request.profile.home_currency
    min_balance_floor = request.profile.minimum_balance_to_keep
    horizon = settings.FORECAST_HORIZON_DAYS

    if method == PaymentMethod.NOT_RECOMMENDED:
        return (
            f"The full {currency} {request.requested_amount} request cannot be completed safely "
            f"within the next {horizon} days without exceeding the {currency} {min_balance_floor} "
            f"minimum. Up to {currency} {amount_safe_to_pay} is safe to pay today."
        )

    forecast = simulator.simulate(injected_payments=payments, overrides=overrides)
    min_balance = forecast.min_balance()

    if method == PaymentMethod.FULL_PAYMENT and not overrides:
        pay_amount = payments[0][1]
        return (
            f"Pay {currency} {pay_amount} today. This leaves at least {currency} {min_balance} "
            f"available over the next {horizon} days."
        )

    if method == PaymentMethod.FULL_PAYMENT and overrides:
        pay_amount = payments[0][1]
        changes = "; ".join(
            f"stopping {o.event_id}" if o.kind == "stop" else f"reducing {o.event_id} to {currency} {o.new_amount}"
            for o in overrides
        )
        return (
            f"Pay {currency} {pay_amount} today by {changes}. This leaves at least "
            f"{currency} {min_balance} available over the next {horizon} days."
        )

    if method == PaymentMethod.PARTIAL_PAYMENT:
        (first_date, first_amount), (second_date, second_amount) = payments
        return (
            f"Pay {currency} {first_amount} today, then {currency} {second_amount} on "
            f"{second_date.isoformat()}. This completes the {currency} {request.requested_amount} "
            f"request while keeping at least {currency} {min_balance} available."
        )

    if method == PaymentMethod.INSTALLMENTS:
        first_date, first_amount = payments[0]
        return (
            f"Use {len(payments)} installments of {currency} {first_amount}, starting "
            f"{first_date.isoformat()}. This leaves at least {currency} {min_balance} available."
        )

    if method == PaymentMethod.WAIT:
        wait_date, wait_amount = payments[0]
        return (
            f"Wait until {wait_date.isoformat()}, then pay {currency} {wait_amount} in full. "
            f"Paying sooner would put the {currency} {min_balance_floor} minimum at risk."
        )

    return "Recommendation computed from the 90-day cash-flow forecast."   # unreachable; defensive only
