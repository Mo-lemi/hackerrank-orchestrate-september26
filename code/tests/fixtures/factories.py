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
    EventDirection, EventStatus, EventFlexibility, EventType, RecurrenceType, PaymentMethod, RequestType,
)
from code.domain.schemas import EvaluationRequest, FinancialEvent, FinancialProfile, RequestPaymentOption


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


def make_payment_option(
    request_id: str = "request_test",
    payment_method: PaymentMethod = PaymentMethod.INSTALLMENTS,
    payment_amount: str = "100",
    number_of_payments: int = 3,
    first_payment_date: date = date(2026, 1, 5),
    payment_frequency_days: Optional[int] = 30,
    financing_fee: str = "0",
    total_payable_amount: Optional[str] = None,
    payment_option_id: Optional[str] = None,
) -> RequestPaymentOption:
    _counter["n"] += 1
    total = Decimal(total_payable_amount) if total_payable_amount is not None else (
        Decimal(payment_amount) * number_of_payments
    )
    return RequestPaymentOption(
        payment_option_id=payment_option_id or f"test_option_{_counter['n']}",
        request_id=request_id,
        payment_method=payment_method,
        payment_amount=Decimal(payment_amount),
        number_of_payments=number_of_payments,
        first_payment_date=first_payment_date,
        payment_frequency_days=payment_frequency_days,
        financing_fee=Decimal(financing_fee),
        total_payable_amount=total,
    )


def make_request(
    profile: FinancialProfile,
    events: Optional[list[FinancialEvent]] = None,
    payment_options: Optional[list[RequestPaymentOption]] = None,
    request_id: str = "request_test",
    request_date: date = date(2026, 1, 1),
    requested_amount: str = "1000",
    desired_completion_date: Optional[date] = None,
    allows_partial_payment: bool = True,
    request_type: RequestType = RequestType.PURCHASE,
    request_text: str = "Can I afford this?",
) -> EvaluationRequest:
    return EvaluationRequest(
        request_id=request_id,
        user_id=profile.user_id,
        request_date=request_date,
        request_type=request_type,
        requested_amount=Decimal(requested_amount),
        desired_completion_date=desired_completion_date or (request_date.replace(year=request_date.year + 1)),
        allows_partial_payment=allows_partial_payment,
        request_text=request_text,
        profile=profile,
        events=events or [],
        payment_options=payment_options or [],
    )
