"""
Finds up to 3 spending-change Overrides (stop / reduce_to) that make an
otherwise-unsafe payment plan safe, and formats them into the
spending_changes_needed output string.

Eligibility is NOT re-derived here — FinancialEvent.eligible_kinds(profile),
built in reconciliation, already applies the double-gate (the event's own
`flexibility` column intersected with the profile's `reducible_categories` /
`stoppable_categories` lists, with `protected_categories` excluded
entirely). This module only decides WHICH of the already-eligible events to
use and in what order.

Greedy, not combinatorial: candidates are ranked once by their maximum
achievable cash-flow recovery (a stop's full amount, or a reduce_to's
amount minus its minimum_allowed_amount floor — whichever is larger for
that event), then added highest-impact-first, testing real safety via the
simulator after each addition, stopping as soon as the plan is safe or 3
changes have been used. This matches "prioritized greedily by cash-flow
impact" and keeps the search boundedly cheap (one candidate list built
once, at most 3 simulate() calls to converge) rather than searching all
C(n,3) subsets of a horizon that can contain dozens of eligible recurring
occurrences per user.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

from code.config import settings
from code.domain.schemas import FinancialEvent, FinancialProfile
from code.engine.cashflow_simulator import CashflowSimulator, Override


@dataclass(frozen=True)
class _RankedChange:
    event: FinancialEvent
    kind: str          # "stop" | "reduce"
    impact: Decimal     # cash recovered if this change is applied


def _rank_eligible_events(events: list[FinancialEvent], profile: FinancialProfile) -> list[_RankedChange]:
    ranked: list[_RankedChange] = []
    seen_ids: set[str] = set()
    for event in events:
        if event.event_id in seen_ids or event.amount is None:
            continue
        kinds = event.eligible_kinds(profile)
        if not kinds:
            continue

        stop_impact = event.amount if "stop" in kinds else Decimal("0")
        reduce_impact = Decimal("0")
        if "reduce" in kinds and event.minimum_allowed_amount is not None:
            reduce_impact = max(Decimal("0"), event.amount - event.minimum_allowed_amount)

        if stop_impact >= reduce_impact and stop_impact > 0:
            best_kind, best_impact = "stop", stop_impact
        elif reduce_impact > 0:
            best_kind, best_impact = "reduce", reduce_impact
        else:
            continue   # eligible in principle but recovers nothing (e.g. already at its floor)

        ranked.append(_RankedChange(event=event, kind=best_kind, impact=best_impact))
        seen_ids.add(event.event_id)

    ranked.sort(key=lambda r: r.impact, reverse=True)
    return ranked


def _to_override(change: _RankedChange) -> Override:
    if change.kind == "stop":
        return Override(kind="stop", event_id=change.event.event_id)
    # "reduce" always targets the event's own minimum_allowed_amount floor —
    # the greedy strategy always takes the maximum allowed recovery, never a
    # partial reduction, since a smaller reduction can only ever need MORE
    # additional changes to reach safety, not fewer.
    return Override(kind="reduce_to", event_id=change.event.event_id, new_amount=change.event.minimum_allowed_amount)


def find_spending_changes_for_safety(
    simulator: CashflowSimulator,
    profile: FinancialProfile,
    payments: tuple[tuple[date, Decimal], ...],
    max_changes: int = settings.MAX_SPENDING_CHANGES,
) -> Optional[tuple[Override, ...]]:
    """Returns the smallest-effort (impact-greedy, up to max_changes) tuple
    of Overrides that makes `payments` safe, or None if no combination of up
    to max_changes eligible changes achieves safety. An empty tuple is never
    returned here (that case — already safe with zero changes — is the
    caller's responsibility to check first)."""
    ranked = _rank_eligible_events(simulator.materialized_events, profile)

    chosen: list[Override] = []
    for change in ranked:
        if len(chosen) >= max_changes:
            break
        chosen.append(_to_override(change))
        if simulator.is_plan_safe(payments, overrides=tuple(chosen)):
            return tuple(chosen)

    return None   # ran out of candidates or hit max_changes without reaching safety


def format_spending_changes(overrides: tuple[Override, ...]) -> str:
    """Renders overrides into the exact `spending_changes_needed` format:
    'stop:<id>|reduce_to:<id>:<amount>', or 'none'."""
    if not overrides:
        return settings.NONE_TOKEN
    legs = []
    for o in overrides:
        if o.kind == "stop":
            legs.append(f"stop:{o.event_id}")
        else:
            legs.append(f"reduce_to:{o.event_id}:{o.new_amount}")
    return settings.PLAN_LEG_SEP.join(legs)
