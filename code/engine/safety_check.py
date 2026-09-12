"""
Built on top of CashflowSimulator; answers the two forecast-derived output
fields directly.
"""
from __future__ import annotations
from datetime import date, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from code.config import settings
from code.engine.cashflow_simulator import CashflowSimulator, Override


class SafetyCheck:
    def __init__(self, simulator: CashflowSimulator):
        self.simulator = simulator

    def amount_safe_to_pay(
        self,
        requested_amount: Decimal,
        overrides: tuple[Override, ...] = (),
    ) -> Decimal:
        """Largest amount X in [0, requested_amount] such that paying X as a
        single lump sum on request_date keeps the balance >=
        minimum_balance_to_keep on every day of the 90-day horizon.

        Binary search over integer cents, not Decimal/float bisection: a
        larger single debit today can only ever lower or match every later
        day's balance (paying more today never makes a future day safer), so
        the feasible set {X : is_safe(X)} is a contiguous prefix
        [0, X_max] of cent values — exactly the precondition binary search
        over "largest feasible" needs, and cents give an exact, deterministic
        stopping point with no floating-point drift.
        """
        requested_cents = int((requested_amount * 100).to_integral_value(rounding=ROUND_DOWN))

        def is_safe(cents: int) -> bool:
            amount = Decimal(cents) / Decimal(100)
            return self.simulator.is_plan_safe(((self.simulator.as_of, amount),), overrides)

        if not is_safe(0):
            return Decimal("0.00")   # even paying nothing breaches — floor at 0 per problem_statement.md

        lo, hi = 0, requested_cents
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if is_safe(mid):
                lo = mid
            else:
                hi = mid - 1

        return (Decimal(lo) / Decimal(100)).quantize(settings.MONEY_QUANTIZE)

    def earliest_date_for_full_payment(
        self,
        requested_amount: Decimal,
        overrides: tuple[Override, ...] = (),
        horizon_days: int = settings.FORECAST_HORIZON_DAYS,
    ) -> Optional[date]:
        """First date D in [request_date, request_date + horizon_days) such
        that paying the FULL requested_amount as a single lump sum on D
        keeps the balance safe across the whole 90-day horizon (the horizon
        is always anchored at request_date, not restarted at D — this
        matches the spec's 'first conservative projected date').

        Linear day-by-day scan rather than binary search: unlike
        amount_safe_to_pay, safety is NOT guaranteed monotonic in the
        candidate date — an intervening payday can make day 40 safe while
        day 20 isn't, so bisecting on date could skip over the true earliest
        day. 90 iterations is cheap regardless.
        """
        for offset in range(horizon_days):
            candidate = self.simulator.as_of + timedelta(days=offset)
            if self.simulator.is_plan_safe(((candidate, requested_amount),), overrides):
                return candidate
        return None
