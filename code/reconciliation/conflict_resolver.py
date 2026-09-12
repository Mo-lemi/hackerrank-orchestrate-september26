"""
Generic 4-level conflict-resolution precedence, exactly as specified in
problem_statement.md's "When records conflict, prefer" list:

    1. An explicit cancellation, settlement, or amendment
    2. A newer record from the same source
    3. A settled event over an estimate or forecast
    4. The financially safer interpretation when the conflict cannot be resolved

`resolve_candidates` is the generic, reusable primitive for weighing several
claimed values of a single fact (an event's amount, status, or date) against
each other. `reconcile_events` is the orchestrator that runs
reconciliation/event_chain.py then reconciliation/evidence_merge.py in the
order the spec implies: event_chain's structural duplicate detection is
really operating at level 3 ("settled event over an estimate"), so
evidence_merge runs SECOND and its CANCEL/AMEND_AMOUNT/DELAY actions — which
are level-1 explicit overrides — are allowed to take precedence over a prior
chain verdict on the same event.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Any, Callable, Optional, Sequence

from code.config.enums import EventStatus
from code.domain.schemas import FinancialEvent, MessageRecord
from code.reconciliation.event_chain import resolve_chains
from code.reconciliation.evidence_merge import merge_messages


class PrecedenceLevel(IntEnum):
    """Lower number = higher precedence, matching the spec's ordered list."""
    EXPLICIT_OVERRIDE = 1
    NEWER_SAME_SOURCE = 2
    SETTLED_OVER_ESTIMATE = 3
    SAFER_INTERPRETATION = 4


_STATUS_CONFIDENCE = {
    EventStatus.SETTLED: 3,
    EventStatus.SCHEDULED: 2,
    EventStatus.PENDING: 1,
    EventStatus.FAILED: 0,
    EventStatus.CANCELLED: 0,
    EventStatus.UNREALIZED: 0,
}


@dataclass(frozen=True)
class Candidate:
    """One claimed value for a single fact, from one source, to be weighed
    against other candidates for the same fact."""
    value: Any
    source_kind: str                        # "event_row" | "message" | "image"
    source_id: str
    is_explicit_override: bool = False       # level 1
    status: Optional[EventStatus] = None     # level 3
    timestamp: Optional[datetime] = None     # level 2


def resolve_candidates(
    candidates: Sequence[Candidate],
    safer_key: Callable[[Any], Any],
) -> Candidate:
    """Applies the 4-level precedence to pick one winning candidate for a
    single disputed fact.

    `safer_key` maps a candidate's value to a sort key where LOWER is safer
    (e.g. for a debit amount, safer = treating it as larger/still-owed; for a
    credit amount, safer = treating it as smaller/not-yet-confirmed). Callers
    define this per fact type since "safer" is direction-dependent — this
    module doesn't assume one."""
    if not candidates:
        raise ValueError("resolve_candidates called with no candidates")
    remaining = list(candidates)
    if len(remaining) == 1:
        return remaining[0]

    # Level 1 — explicit cancellation/settlement/amendment wins outright
    explicit = [c for c in remaining if c.is_explicit_override]
    if explicit:
        remaining = explicit
        if len(remaining) == 1:
            return remaining[0]

    # Level 2 — among same-source duplicates, the newer timestamp wins;
    # different sources are each represented by their own newest candidate
    # and carried forward to level 3.
    by_source: dict[str, list[Candidate]] = {}
    for c in remaining:
        by_source.setdefault(c.source_kind, []).append(c)
    remaining = [
        max(group, key=lambda c: c.timestamp) if any(c.timestamp for c in group) else group[0]
        for group in by_source.values()
    ]
    if len(remaining) == 1:
        return remaining[0]

    # Level 3 — a settled event outranks an estimate/forecast
    with_status = [c for c in remaining if c.status is not None]
    if with_status:
        best = max(_STATUS_CONFIDENCE.get(c.status, 0) for c in with_status)
        narrowed = [c for c in with_status if _STATUS_CONFIDENCE.get(c.status, 0) == best]
        if narrowed:
            remaining = narrowed
    if len(remaining) == 1:
        return remaining[0]

    # Level 4 — deterministic, financially conservative final tie-break
    return min(remaining, key=lambda c: safer_key(c.value))


def reconcile_events(
    events: list[FinancialEvent],
    messages: list[MessageRecord],
) -> tuple[list[FinancialEvent], list[str]]:
    """Full reconciliation pass, in precedence order: structural chain
    resolution (event_chain.py, level 3) first, then message-evidence merge
    (evidence_merge.py, which can carry level-1 explicit overrides) second so
    a message's explicit cancellation/amendment can override a chain
    verdict on the same event. Returns events ready for io_/fx.py's
    home-currency normalization and then engine/cashflow_simulator.py,
    plus a flat, human-readable audit trail."""
    chained_events, chain_notes = resolve_chains(events)
    merged_events, merge_notes = merge_messages(chained_events, messages)

    notes: list[str] = []
    notes.extend(f"[chain] {n.reason}" for n in chain_notes)
    notes.extend(
        f"[message {n.message_id}] {n.event_id}: {n.action.value} — {n.detail}"
        for n in merge_notes
    )
    return merged_events, notes
