"""
Enumerations for Buy or Wait?.

Updated against the actual uploaded dataset/*.csv files (previously only
sample_requests.csv had been reviewed). Every value below was verified by
scanning the real files' distinct values — see code/config/settings.py for
the column-name mapping each enum attaches to.
"""
from __future__ import annotations
from enum import Enum


# ---------------------------------------------------------------------------
# requests.csv / output.csv vocabulary — unchanged, verified against
# sample_requests.csv in the prior pass and again against problem_statement.md
# ---------------------------------------------------------------------------

class RequestType(str, Enum):
    PURCHASE = "purchase"
    TRAVEL = "travel"
    EDUCATION = "education"
    FAMILY_TRANSFER = "family_transfer"
    DEBT_REPAYMENT = "debt_repayment"
    INVESTMENT = "investment"
    HOUSING = "housing"
    EMERGENCY_EXPENSE = "emergency_expense"
    OTHER = "other"


class AffordabilityStatus(str, Enum):
    AFFORDABLE_NOW = "affordable_now"
    AFFORDABLE_WITH_PLAN = "affordable_with_plan"
    AFFORDABLE_LATER = "affordable_later"
    NOT_AFFORDABLE = "not_affordable"


class PaymentMethod(str, Enum):
    """Used both for output.csv's recommended_payment_method AND (a subset of
    it) for request_payment_options.csv's payment_method column, which was
    confirmed to contain only 'full_payment' and 'installments'."""
    FULL_PAYMENT = "full_payment"
    PARTIAL_PAYMENT = "partial_payment"
    INSTALLMENTS = "installments"
    WAIT = "wait"
    NOT_RECOMMENDED = "not_recommended"


class SpendingChangeKind(str, Enum):
    STOP = "stop"
    REDUCE_TO = "reduce_to"


# ---------------------------------------------------------------------------
# financial_events.csv vocabulary — CONFIRMED by scanning all 25,342 rows of
# the uploaded file (distinct-value counts noted per enum).
# ---------------------------------------------------------------------------

class EventDirection(str, Enum):
    """financial_events.csv column `direction` (3 distinct values found)."""
    CREDIT = "credit"
    DEBIT = "debit"
    NON_CASH = "non_cash"     # investment_valuation rows; always excluded from cashflow


class EventStatus(str, Enum):
    """financial_events.csv column `status` (6 distinct values found)."""
    SETTLED = "settled"
    PENDING = "pending"
    SCHEDULED = "scheduled"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNREALIZED = "unrealized"


class EventFlexibility(str, Enum):
    """financial_events.csv column `flexibility` (4 distinct values found).
    This is the authoritative per-event signal for spending_changes_needed
    eligibility; financial_profiles.csv's expense_categories_user_is_willing_to_*
    lists are a second, independent permission gate on top of this (a category
    can appear in a profile's reduce/stop lists, but if the specific event's
    flexibility is `fixed`, it still can't be touched)."""
    FIXED = "fixed"
    REDUCIBLE = "reducible"
    STOPPABLE = "stoppable"
    REDUCIBLE_OR_STOPPABLE = "reducible_or_stoppable"

    @property
    def allows_reduce(self) -> bool:
        return self in (EventFlexibility.REDUCIBLE, EventFlexibility.REDUCIBLE_OR_STOPPABLE)

    @property
    def allows_stop(self) -> bool:
        return self in (EventFlexibility.STOPPABLE, EventFlexibility.REDUCIBLE_OR_STOPPABLE)


class EventType(str, Enum):
    """financial_events.csv column `event_type` (8 distinct values found)."""
    EXPENSE = "expense"
    INCOME = "income"
    DEBT_PAYMENT = "debt_payment"
    SUBSCRIPTION = "subscription"
    REFUND = "refund"
    INVESTMENT_PURCHASE = "investment_purchase"
    INVESTMENT_SALE = "investment_sale"
    INVESTMENT_VALUATION = "investment_valuation"


class RecurrenceType(str, Enum):
    """NOT a CSV column — financial_events.csv has no recurrence field.
    Populated only by engine/recurrence_detector.py after loading, from
    history (matching category + amount tolerance + regular interval for the
    same user). Defaults to NONE until that detector runs."""
    NONE = "none"
    RECURRING = "recurring"


class MessageSourceType(str, Enum):
    """messages.csv column `source_type` (5 distinct values found). Useful
    for conflict-resolution precedence (e.g. an `employer` message about
    salary outranks a `merchant` message about the same figure)."""
    BANK = "bank"
    EMPLOYER = "employer"
    FINANCIAL_SERVICE = "financial_service"
    MERCHANT = "merchant"
    SERVICE_PROVIDER = "service_provider"


# ---------------------------------------------------------------------------
# NOTE: the previous EventFlow / CashState / FlexibilityTag (3-value) enums
# from the first pass are removed — they were placeholders for columns that
# turned out to have different names/value sets (direction, status,
# flexibility above) once the real files were inspected.
# ---------------------------------------------------------------------------
