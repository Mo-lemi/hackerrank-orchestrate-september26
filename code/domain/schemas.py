"""
Core data contracts for Buy or Wait?, updated against the real, uploaded
dataset/*.csv files. See code/config/settings.py for the full CONFIRMED
column-name mapping and inline notes on what changed from the first
(prose-only) pass.

All money fields are Decimal, never float. All dates are `datetime.date`
except FinancialEvent/MessageRecord timestamps that are genuinely datetimes
(messages.csv's `sent_at` carries time-of-day; every other date field in the
dataset is date-only).
"""
from __future__ import annotations
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, Field, field_validator, model_validator

from code.config.enums import (
    RequestType,
    AffordabilityStatus,
    PaymentMethod,
    EventDirection,
    EventStatus,
    EventFlexibility,
    EventType,
    RecurrenceType,
    MessageSourceType,
)


# ---------------------------------------------------------------------------
# Raw input records (one per CSV row, minimally transformed)
# ---------------------------------------------------------------------------

class RequestRecord(BaseModel):
    """One row of requests.csv (or sample_requests.csv, ignoring its extra
    completed-output columns). Unchanged — CONFIRMED against
    problem_statement.md and verified against all 25 sample_requests.csv rows."""
    request_id: str
    user_id: str
    request_date: date
    request_type: RequestType
    requested_amount: Decimal
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str

    @field_validator("requested_amount")
    @classmethod
    def positive_amount(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("requested_amount must be positive")
        return v

    @model_validator(mode="after")
    def deadline_not_before_request(self) -> "RequestRecord":
        if self.desired_completion_date < self.request_date:
            raise ValueError("desired_completion_date cannot precede request_date")
        return self


class FinancialProfile(BaseModel):
    """One row of financial_profiles.csv.

    CHANGED from the first pass: the single guessed `flexible_categories`
    list is actually two independent permission lists — a category may
    legally be reduced, stopped, both, or neither, and this is a *second*
    gate layered on top of the per-event `flexibility` column (see
    FinancialEvent.is_eligible_for_spending_change)."""
    user_id: str
    home_currency: str = Field(pattern=r"^[A-Z]{3}$")
    available_balance: Decimal                          # from current_available_balance
    minimum_balance_to_keep: Decimal
    financial_priorities: list[str] = Field(default_factory=list)
    protected_categories: list[str] = Field(default_factory=list)   # from expense_categories_to_protect
    reducible_categories: list[str] = Field(default_factory=list)   # from ..._willing_to_reduce
    stoppable_categories: list[str] = Field(default_factory=list)   # from ..._willing_to_stop
    payment_methods_user_will_consider: list[PaymentMethod] = Field(default_factory=list)
    max_installment_months: Optional[int] = None   # None => user will not consider installments

    @field_validator("minimum_balance_to_keep")
    @classmethod
    def non_negative_floor(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("minimum_balance_to_keep cannot be negative")
        return v

    def is_protected(self, category: str) -> bool:
        return category in self.protected_categories

    def permits_reduce(self, category: str) -> bool:
        return category in self.reducible_categories

    def permits_stop(self, category: str) -> bool:
        return category in self.stoppable_categories


class FinancialEvent(BaseModel):
    """One row of financial_events.csv, normalized to home currency, with
    `amount=None` preserved (never coerced to 0) until
    multimodal/vision_extractor resolves it from the linked image.

    CHANGED from the first pass:
      - `flow` -> `direction` (adds a third value, non_cash, for
        investment_valuation rows — always excluded from cashflow)
      - `cash_state` -> `status` (same 6 values, real column name)
      - `flexibility` is a real 4-value column (fixed / reducible / stoppable /
        reducible_or_stoppable), replacing the earlier 3-value guess
      - added `event_type` and `minimum_allowed_amount` (the floor for
        reduce_to on reducible/reducible_or_stoppable rows — confirmed
        populated on exactly those rows and blank everywhere else)
      - REMOVED `recurrence` / `recurrence_interval_days` as CSV-backed
        fields: no such columns exist. `recurrence` below defaults to NONE
        and is only ever set by engine/recurrence_detector.py post-load.
    """
    event_id: str
    user_id: str
    event_type: EventType
    event_date: date
    settlement_date: Optional[date] = None   # cash-movement date; falls back to event_date if absent
    category: str
    description: Optional[str] = None
    amount: Optional[Decimal] = None          # None until resolved — NEVER treated as 0
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    direction: EventDirection
    status: EventStatus
    flexibility: EventFlexibility = EventFlexibility.FIXED
    minimum_allowed_amount: Optional[Decimal] = None   # floor for reduce_to; set iff flexibility allows_reduce
    linked_event_id: Optional[str] = None

    # NOT loaded from CSV — populated later:
    recurrence: RecurrenceType = RecurrenceType.NONE          # engine/recurrence_detector.py
    amount_source: Optional[str] = None                        # reconciliation/: "csv" | "image_extracted" | "message_amended"
    superseded_by: Optional[str] = None                        # reconciliation/: event_id of an amendment/cancellation

    @model_validator(mode="after")
    def reduce_floor_only_when_reducible(self) -> "FinancialEvent":
        if self.minimum_allowed_amount is not None and not self.flexibility.allows_reduce:
            raise ValueError(
                f"{self.event_id}: minimum_allowed_amount set but flexibility "
                f"({self.flexibility}) does not allow reduce"
            )
        return self

    @property
    def effective_date(self) -> date:
        return self.settlement_date or self.event_date

    @property
    def signed_amount(self) -> Optional[Decimal]:
        if self.amount is None:
            return None
        return self.amount if self.direction == EventDirection.CREDIT else -self.amount

    @property
    def counts_toward_cashflow(self) -> bool:
        """Deterministic cash-inclusion gate per problem_statement.md's 90-Day
        Safety Check and AGENTS.md §6.3:
          - superseded by a later amendment/cancellation: never counted directly
          - non_cash direction (investment valuations) or failed/cancelled/
            unrealized status: never counted
          - pending credit (unconfirmed income, bonuses, refunds, gains): treated as 0
          - settled / scheduled (either direction), pending debit (reserved): counted
        """
        if self.superseded_by is not None:
            return False
        if self.direction == EventDirection.NON_CASH:
            return False
        if self.status in (EventStatus.FAILED, EventStatus.CANCELLED, EventStatus.UNREALIZED):
            return False
        if self.status == EventStatus.PENDING and self.direction == EventDirection.CREDIT:
            return False
        return True

    @property
    def is_eligible_for_spending_change(self) -> bool:
        """Only non-protected, flexible, RECURRING events may appear in
        spending_changes_needed. Requires recurrence to have already been
        populated by engine/recurrence_detector.py — defaults to False
        (recurrence=NONE) until that runs."""
        return (
            self.flexibility != EventFlexibility.FIXED
            and self.recurrence == RecurrenceType.RECURRING
        )

    def eligible_kinds(self, profile: FinancialProfile) -> set[str]:
        """Intersects the event's own flexibility with the profile's category
        permission lists — both must agree. Returns a subset of {"stop", "reduce"}."""
        if not self.is_eligible_for_spending_change or profile.is_protected(self.category):
            return set()
        kinds = set()
        if self.flexibility.allows_stop and profile.permits_stop(self.category):
            kinds.add("stop")
        if self.flexibility.allows_reduce and profile.permits_reduce(self.category):
            kinds.add("reduce")
        return kinds


class RequestPaymentOption(BaseModel):
    """One row of request_payment_options.csv.

    CHANGED from the first pass: per_payment_amount -> payment_amount,
    num_payments -> number_of_payments, interval_days ->
    payment_frequency_days (Optional — confirmed blank on every single-payment
    `full_payment` row, populated on every `installments` row). Added
    `payment_method`: the table carries both `full_payment` (lump sum) and
    `installments` rows for the same request_id, so a full-payment candidate
    can be sourced directly from here."""
    payment_option_id: str
    request_id: str
    payment_method: PaymentMethod
    payment_amount: Decimal
    number_of_payments: int = Field(ge=1)
    first_payment_date: date
    payment_frequency_days: Optional[int] = None   # None for single-payment options
    financing_fee: Decimal = Decimal("0")
    total_payable_amount: Decimal

    @model_validator(mode="after")
    def payments_consistent(self) -> "RequestPaymentOption":
        expected = (self.payment_amount * self.number_of_payments).quantize(Decimal("0.01"))
        if abs(expected - self.total_payable_amount) > Decimal("0.01"):
            raise ValueError(
                f"{self.payment_option_id}: payment_amount * number_of_payments "
                f"({expected}) != total_payable_amount ({self.total_payable_amount})"
            )
        return self

    @model_validator(mode="after")
    def frequency_matches_payment_count(self) -> "RequestPaymentOption":
        if self.number_of_payments > 1 and self.payment_frequency_days is None:
            raise ValueError(
                f"{self.payment_option_id}: multiple payments but no "
                f"payment_frequency_days set"
            )
        return self

    def payment_dates(self) -> list[date]:
        from datetime import timedelta
        step = self.payment_frequency_days or 0
        return [
            self.first_payment_date + timedelta(days=step * i)
            for i in range(self.number_of_payments)
        ]


class ExchangeRate(BaseModel):
    """One row of exchange_rates.csv — CONFIRMED exact match to the first
    pass's assumed shape, no changes."""
    rate_date: date
    from_currency: str = Field(pattern=r"^[A-Z]{3}$")
    to_currency: str = Field(pattern=r"^[A-Z]{3}$")
    rate: Decimal

    @field_validator("rate")
    @classmethod
    def positive_rate(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("exchange rate must be positive")
        return v


class MessageRecord(BaseModel):
    """One row of messages.csv.

    CHANGED from the first pass: `message_date` -> `sent_at`, and it is a
    full datetime (ISO-8601 with a trailing Z), not a bare date. Added
    `source_type`. user_id is always populated; request_id and
    related_event_id are frequently blank (87/215 and 176/215 respectively
    in the uploaded file) — related_event_id is populated only when the
    message maps 1:1 to a supplied financial-event row."""
    message_id: str
    user_id: str
    request_id: Optional[str] = None
    related_event_id: Optional[str] = None
    sent_at: datetime
    source_type: MessageSourceType
    message_text: str


class ImageRecord(BaseModel):
    """One row of images.csv. CONFIRMED: all 16 rows in the uploaded file have
    every field populated, and the 16 related_event_id values match the 16
    financial_events.csv rows with a blank amount exactly 1:1."""
    image_id: str
    user_id: str
    request_id: str
    related_event_id: str


# ---------------------------------------------------------------------------
# Assembled per-request context handed to engine/ and optimizer/ — unchanged
# structurally from the first pass.
# ---------------------------------------------------------------------------

class EvaluationRequest(BaseModel):
    """Fully assembled, reconciled, home-currency-normalized context for one
    request_id. Everything downstream (engine, optimizer, explainer) reads
    only from this object — never from raw CSV rows."""
    request_id: str
    user_id: str
    request_date: date
    request_type: RequestType
    requested_amount: Decimal
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str

    profile: FinancialProfile
    events: list[FinancialEvent]                       # reconciled, home-currency normalized
    payment_options: list[RequestPaymentOption]         # home-currency normalized
    resolved_notes: list[str] = Field(default_factory=list)  # audit trail for reconciliation decisions

    @model_validator(mode="after")
    def deadline_not_before_request(self) -> "EvaluationRequest":
        if self.desired_completion_date < self.request_date:
            raise ValueError("desired_completion_date cannot precede request_date")
        return self

    @classmethod
    def from_request_record(
        cls,
        record: RequestRecord,
        profile: FinancialProfile,
        events: list[FinancialEvent],
        payment_options: list[RequestPaymentOption],
        resolved_notes: Optional[list[str]] = None,
    ) -> "EvaluationRequest":
        return cls(
            request_id=record.request_id,
            user_id=record.user_id,
            request_date=record.request_date,
            request_type=record.request_type,
            requested_amount=record.requested_amount,
            desired_completion_date=record.desired_completion_date,
            allows_partial_payment=record.allows_partial_payment,
            request_text=record.request_text,
            profile=profile,
            events=events,
            payment_options=payment_options,
            resolved_notes=resolved_notes or [],
        )


# ---------------------------------------------------------------------------
# Output — unchanged from the first pass.
# ---------------------------------------------------------------------------

class OutputRow(BaseModel):
    """Exact schema for output.csv. Column order for the CSV write is enforced
    by config.settings.OUTPUT_COLUMNS at write time, not by this model's field
    order."""
    request_id: str
    amount_safe_to_pay: Decimal
    affordability_status: AffordabilityStatus
    recommended_payment_method: PaymentMethod
    payment_plan: str                                    # "date:amount|date:amount" or "none"
    earliest_date_for_full_payment: Optional[date] = None  # empty string on write if None
    spending_changes_needed: str                          # up to 3 "stop:<id>" / "reduce_to:<id>:<amt>" or "none"
    decision_explanation: str

    @field_validator("payment_plan")
    @classmethod
    def validate_plan_format(cls, v: str) -> str:
        if v == "none":
            return v
        legs = v.split("|")
        parsed_dates = []
        for leg in legs:
            d_str, amt_str = leg.split(":")
            parsed_dates.append(date.fromisoformat(d_str))
            if Decimal(amt_str) <= 0:
                raise ValueError(f"payment_plan leg amount must be positive: {leg}")
        if parsed_dates != sorted(parsed_dates):
            raise ValueError("payment_plan legs must be in chronological order")
        return v


    @field_validator("spending_changes_needed")
    @classmethod
    def validate_changes_format(cls, v: str) -> str:
        if v == "none":
            return v
        legs = v.split("|")
        if len(legs) > 3:
            raise ValueError("spending_changes_needed allows at most 3 changes")
        stopped, reduced = set(), set()
        for leg in legs:
            if leg.startswith("stop:"):
                event_id = leg[len("stop:"):]
                if not event_id:
                    raise ValueError(f"malformed spending change: {leg}")
                stopped.add(event_id)
            elif leg.startswith("reduce_to:"):
                rest = leg[len("reduce_to:"):]
                if ":" not in rest:
                    raise ValueError(f"malformed spending change: {leg}")
                event_id, amount_str = rest.rsplit(":", 1)
                if not event_id:
                    raise ValueError(f"malformed spending change: {leg}")
                try:
                    Decimal(amount_str)
                except Exception:
                    raise ValueError(f"malformed spending change: {leg}")
                reduced.add(event_id)
            else:
                raise ValueError(f"malformed spending change: {leg}")
        if stopped & reduced:
            raise ValueError("cannot both stop and reduce_to the same event_id")
        return v

    @model_validator(mode="after")
    def bounds_check(self) -> "OutputRow":
        if self.amount_safe_to_pay < 0:
            raise ValueError("amount_safe_to_pay cannot be negative")
        return self

    def to_csv_row(self, columns: list[str]) -> dict[str, str]:
        """Renders in the exact column order/format output.csv expects,
        including the empty-string convention for a None full-payment date."""
        data = self.model_dump()
        data["earliest_date_for_full_payment"] = (
            self.earliest_date_for_full_payment.isoformat()
            if self.earliest_date_for_full_payment else ""
        )
        data["affordability_status"] = self.affordability_status.value
        data["recommended_payment_method"] = self.recommended_payment_method.value
        return {col: str(data[col]) for col in columns}
