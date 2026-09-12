"""
The deterministic 90-day daily balance simulation engine. No LLM calls, no
I/O beyond what's already been loaded/reconciled/FX-normalized by the
caller — same inputs always produce the same Forecast.

Composition: CashflowSimulator wraps engine/recurrence_detector.py so that
"expand recurring items across the 90 days" happens once per
EvaluationRequest (at construction, anchored to the request's own
request_date as the detection cutoff) rather than being re-derived on every
simulate() call. Each simulate() call then just walks the fixed materialized
event set, applying whatever Override/injected-payment scenario is being
tested — this is what safety_check.py calls repeatedly (binary search /
linear scan) without repeating recurrence detection each time.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from code.config import settings
from code.config.enums import EventDirection
from code.domain.schemas import FinancialEvent, FinancialProfile
from code.engine.recurrence_detector import materialize_recurring_events


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DayLedger:
    """One day's worth of settled cash movement and the resulting balance."""
    day: date
    events_applied: tuple[FinancialEvent, ...]   # events whose effective_date == day and counted today
    injected_amount: Decimal                      # sum of any injected_payments landing on this day (debit)
    net_change: Decimal
    balance_after: Decimal


@dataclass(frozen=True)
class Forecast:
    """Full daily balance trajectory starting from the simulator's as_of date."""
    start_date: date
    horizon_days: int
    ledgers: tuple[DayLedger, ...]   # len == horizon_days, ordered by day

    def balance_on(self, d: date) -> Decimal:
        for ledger in self.ledgers:
            if ledger.day == d:
                return ledger.balance_after
        raise ValueError(f"{d} is outside this forecast's horizon ({self.start_date} + {self.horizon_days}d)")

    def min_balance(self) -> Decimal:
        return min(ledger.balance_after for ledger in self.ledgers)

    def first_breach_day(self, floor: Decimal) -> Optional[date]:
        for ledger in self.ledgers:
            if ledger.balance_after < floor:
                return ledger.day
        return None


@dataclass(frozen=True)
class Override:
    """One hypothetical spending change applied before simulating — mirrors
    spending_changes_needed syntax. The caller (optimizer/spending_changes.py)
    is responsible for only proposing an event_id that is
    FinancialEvent.eligible_kinds()-permitted and, for reduce_to, a
    new_amount that respects the event's minimum_allowed_amount — this class
    and the simulator apply whatever they're given without re-deriving
    eligibility."""
    kind: str            # "stop" | "reduce_to"
    event_id: str
    new_amount: Optional[Decimal] = None   # required for "reduce_to"


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

class CashflowSimulator:
    """One instance per EvaluationRequest. `events` must already be
    reconciled (reconciliation/conflict_resolver.reconcile_events) and
    home-currency normalized (io_/fx.normalize_events_to_home_currency)."""

    def __init__(self, profile: FinancialProfile, events: list[FinancialEvent], as_of: date):
        self.profile = profile
        self.as_of = as_of
        horizon_end = as_of + timedelta(days=settings.FORECAST_HORIZON_DAYS - 1)
        self.materialized_events, self.recurrence_notes = materialize_recurring_events(
            events, as_of=as_of, horizon_end=horizon_end,
        )

    def _working_events(self, overrides: tuple[Override, ...]) -> list[FinancialEvent]:
        stops = {o.event_id for o in overrides if o.kind == "stop"}
        reductions = {o.event_id: o.new_amount for o in overrides if o.kind == "reduce_to"}
        working = []
        for event in self.materialized_events:
            if event.event_id in stops:
                continue
            if event.event_id in reductions:
                event = event.model_copy(update={"amount": reductions[event.event_id]})
            working.append(event)
        return working

    def simulate(
        self,
        horizon_days: int = settings.FORECAST_HORIZON_DAYS,
        injected_payments: tuple[tuple[date, Decimal], ...] = (),
        overrides: tuple[Override, ...] = (),
    ) -> Forecast:
        horizon_last_day = self.as_of + timedelta(days=horizon_days - 1)
        for pay_date, _ in injected_payments:
            if not (self.as_of <= pay_date <= horizon_last_day):
                raise ValueError(
                    f"injected payment on {pay_date} is outside the simulation "
                    f"horizon [{self.as_of}, {horizon_last_day}]"
                )

        working_events = self._working_events(overrides)
        events_by_day: dict[date, list[FinancialEvent]] = {}
        for event in working_events:
            eff_date = event.effective_date
            if self.as_of <= eff_date <= horizon_last_day and event.counts_toward_cashflow:
                events_by_day.setdefault(eff_date, []).append(event)

        injected_by_day: dict[date, Decimal] = {}
        for pay_date, amount in injected_payments:
            injected_by_day[pay_date] = injected_by_day.get(pay_date, Decimal("0")) + amount

        ledgers: list[DayLedger] = []
        running_balance = self.profile.available_balance
        for offset in range(horizon_days):
            day = self.as_of + timedelta(days=offset)
            todays_events = tuple(events_by_day.get(day, ()))
            event_net = sum((e.signed_amount for e in todays_events), Decimal("0"))
            injected_amount = injected_by_day.get(day, Decimal("0"))
            net_change = (event_net - injected_amount).quantize(settings.MONEY_QUANTIZE)
            running_balance = (running_balance + net_change).quantize(settings.MONEY_QUANTIZE)
            ledgers.append(DayLedger(
                day=day,
                events_applied=todays_events,
                injected_amount=injected_amount,
                net_change=net_change,
                balance_after=running_balance,
            ))

        return Forecast(start_date=self.as_of, horizon_days=horizon_days, ledgers=tuple(ledgers))

    def is_plan_safe(
        self,
        payments: tuple[tuple[date, Decimal], ...],
        overrides: tuple[Override, ...] = (),
    ) -> bool:
        """A plan is safe only if the balance never drops below
        minimum_balance_to_keep on ANY day across the full 90-day horizon —
        not just on the day(s) a payment lands."""
        forecast = self.simulate(injected_payments=payments, overrides=overrides)
        return forecast.min_balance() >= self.profile.minimum_balance_to_keep
