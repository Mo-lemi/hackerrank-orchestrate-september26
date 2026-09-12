"""
Unit tests for optimizer/spending_changes.py.

Run with: pytest code/tests/test_spending_changes.py -v
"""
from datetime import date, timedelta
from decimal import Decimal

from code.config.enums import EventDirection, EventFlexibility, EventStatus, RecurrenceType
from code.engine.cashflow_simulator import CashflowSimulator
from code.optimizer.spending_changes import find_spending_changes_for_safety, format_spending_changes
from code.engine.cashflow_simulator import Override
from code.tests.fixtures.factories import make_event, make_profile

AS_OF = date(2026, 1, 1)


def test_double_gate_excludes_protected_category_even_if_flexibility_allows_stop():
    # streaming is stoppable at the profile level AND event-level flexibility
    # allows it, but it's ALSO in protected_categories — must be excluded.
    profile = make_profile(
        available_balance="1000", minimum_balance_to_keep="900",
        protected_categories=["streaming"], stoppable_categories=["streaming"],
    )
    event = make_event(category="streaming", amount="500", direction=EventDirection.DEBIT,
                        status=EventStatus.SCHEDULED, event_date=AS_OF + timedelta(days=2),
                        flexibility=EventFlexibility.STOPPABLE, recurrence=RecurrenceType.RECURRING)
    sim = CashflowSimulator(profile, [event], as_of=AS_OF)
    payments = ((AS_OF, Decimal("50")),)   # would breach without stopping the streaming debit
    result = find_spending_changes_for_safety(sim, profile, payments)
    assert result is None   # nothing eligible — protected category wins over stoppable flexibility


def test_reduce_to_uses_exactly_the_minimum_allowed_amount_floor():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="700",
                            reducible_categories=["dining"])
    event = make_event(category="dining", amount="500", direction=EventDirection.DEBIT,
                        status=EventStatus.SCHEDULED, event_date=AS_OF + timedelta(days=2),
                        flexibility=EventFlexibility.REDUCIBLE, minimum_allowed_amount="100",
                        recurrence=RecurrenceType.RECURRING)
    sim = CashflowSimulator(profile, [event], as_of=AS_OF)
    payments = ((AS_OF, Decimal("100")),)   # 1000-100=900 fine day0; day2: 900-500=400 <700 breach
    result = find_spending_changes_for_safety(sim, profile, payments)
    assert result is not None
    assert len(result) == 1
    assert result[0].kind == "reduce_to"
    assert result[0].new_amount == Decimal("100")   # exactly the floor, not some smaller cut


def test_greedy_picks_highest_impact_event_first():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="850",
                            stoppable_categories=["streaming", "gym"])
    small = make_event(category="gym", amount="30", direction=EventDirection.DEBIT,
                         status=EventStatus.SCHEDULED, event_date=AS_OF + timedelta(days=1),
                         flexibility=EventFlexibility.STOPPABLE, event_id="gym_evt",
                         recurrence=RecurrenceType.RECURRING)
    big = make_event(category="streaming", amount="200", direction=EventDirection.DEBIT,
                       status=EventStatus.SCHEDULED, event_date=AS_OF + timedelta(days=1),
                       flexibility=EventFlexibility.STOPPABLE, event_id="streaming_evt",
                       recurrence=RecurrenceType.RECURRING)
    sim = CashflowSimulator(profile, [small, big], as_of=AS_OF)
    payments = ((AS_OF, Decimal("100")),)   # day0: 900 fine; day1 with both debits: 900-30-200=670 <850
    result = find_spending_changes_for_safety(sim, profile, payments)
    assert result is not None
    assert len(result) == 1   # stopping just the bigger one is enough
    assert result[0].event_id == "streaming_evt"


def test_returns_none_when_more_than_three_changes_would_be_needed():
    profile = make_profile(available_balance="1000", minimum_balance_to_keep="990",
                            stoppable_categories=["shopping"])
    events = [
        make_event(category="shopping", amount="5", direction=EventDirection.DEBIT,
                    status=EventStatus.SCHEDULED, event_date=AS_OF + timedelta(days=1),
                    flexibility=EventFlexibility.STOPPABLE, event_id=f"shop_{i}",
                    recurrence=RecurrenceType.RECURRING)
        for i in range(5)   # 5 x $5 = $25 needed, but only 3 changes allowed = $15 max recovery
    ]
    sim = CashflowSimulator(profile, events, as_of=AS_OF)
    payments = ((AS_OF, Decimal("10")),)
    result = find_spending_changes_for_safety(sim, profile, payments, max_changes=3)
    assert result is None


def test_format_spending_changes():
    assert format_spending_changes(()) == "none"
    stop_only = (Override(kind="stop", event_id="event_1"),)
    assert format_spending_changes(stop_only) == "stop:event_1"
    mixed = (
        Override(kind="stop", event_id="event_1"),
        Override(kind="reduce_to", event_id="event_2", new_amount=Decimal("100")),
    )
    assert format_spending_changes(mixed) == "stop:event_1|reduce_to:event_2:100"
