"""
Shared, minimal-boilerplate factories for unit tests. These build synthetic,
clearly-fake fixtures — never real dataset rows — so engine/ tests are fast,
isolated, and don't depend on dataset/ being present.
"""
from __future__ import annotations
from datetime import date
from decimal import Decimal
from typing import Optional

from code.config.enums import (
    EventDirection, EventStatus, EventFlexibility, EventType, RecurrenceType,
)
from code.domain.schemas import FinancialEvent, FinancialProfile


def make_profile(
    user_id: str = "user_test",
    home_currency: str = "USD",
    available_balance: str = "1000",
    minimum_balance_to_keep: str = "200",
    **overrides,
) -> FinancialProfile:
    base = dict(
        user_id=user_id,
        home_currency=home_currency,
        available_balance=Decimal(available_balance),
        minimum_balance_to_keep=Decimal(minimum_balance_to_keep),
        financial_priorities=[],
        protected_categories=[],
        reducible_categories=["dining"],
        stoppable_categories=["streaming"],
        payment_methods_user_will_consider=[],
        max_installment_months=None,
    )
    base.update(overrides)
    return FinancialProfile(**base)


_counter = {"n": 0}


def make_event(
    user_id: str = "user_test",
    category: str = "rent",
    amount: str = "300",
    direction: EventDirection = EventDirection.DEBIT,
    status: EventStatus = EventStatus.SETTLED,
    event_date: date = date(2026, 1, 1),
    settlement_date: Optional[date] = None,
    currency: str = "USD",
    event_type: EventType = EventType.EXPENSE,
    flexibility: EventFlexibility = EventFlexibility.FIXED,
    minimum_allowed_amount: Optional[str] = None,
    recurrence: RecurrenceType = RecurrenceType.NONE,
    linked_event_id: Optional[str] = None,
    superseded_by: Optional[str] = None,
    event_id: Optional[str] = None,
) -> FinancialEvent:
    _counter["n"] += 1
    return FinancialEvent(
        event_id=event_id or f"test_event_{_counter['n']}",
        user_id=user_id,
        event_type=event_type,
        event_date=event_date,
        settlement_date=settlement_date or event_date,
        category=category,
        description=f"synthetic test event ({category})",
        amount=Decimal(amount) if amount is not None else None,
        currency=currency,
        direction=direction,
        status=status,
        flexibility=flexibility,
        minimum_allowed_amount=Decimal(minimum_allowed_amount) if minimum_allowed_amount else None,
        recurrence=recurrence,
        linked_event_id=linked_event_id,
        superseded_by=superseded_by,
    )
