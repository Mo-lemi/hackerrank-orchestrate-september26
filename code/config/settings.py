"""
Central configuration for Buy or Wait?.

Column-name constants below are now CONFIRMED by direct inspection of the
uploaded dataset/*.csv files (exchange_rates.csv, financial_events.csv,
financial_profiles.csv, images.csv, messages.csv, request_payment_options.csv)
— header row, distinct-value scan of every categorical column, and a full
arithmetic consistency check on request_payment_options.csv (0 mismatches
across 790 rows). requests.csv / sample_requests.csv / output.csv remain
CONFIRMED from problem_statement.md and the sample file, as before.

Every constant that changed from the first pass is noted inline with what it
used to be assumed as, so the diff is traceable.
"""
from __future__ import annotations
from pathlib import Path
from decimal import Decimal
import os

# ---------------------------------------------------------------------------
# Paths (resolved relative to this file, not the platform home directory)
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]      # .../repo/code/config/settings.py -> repo/
DATASET_DIR = REPO_ROOT / "dataset"
MEDIA_IMAGES_DIR = DATASET_DIR / "media" / "images"
OUTPUT_CSV_PATH = DATASET_DIR / "output.csv"
USAGE_REPORT_PATH = REPO_ROOT / "evaluation" / "usage_report.md"

REQUESTS_CSV = DATASET_DIR / "requests.csv"
SAMPLE_REQUESTS_CSV = DATASET_DIR / "sample_requests.csv"
FINANCIAL_PROFILES_CSV = DATASET_DIR / "financial_profiles.csv"
FINANCIAL_EVENTS_CSV = DATASET_DIR / "financial_events.csv"
EXCHANGE_RATES_CSV = DATASET_DIR / "exchange_rates.csv"
REQUEST_PAYMENT_OPTIONS_CSV = DATASET_DIR / "request_payment_options.csv"
MESSAGES_CSV = DATASET_DIR / "messages.csv"
IMAGES_CSV = DATASET_DIR / "images.csv"

def image_path(image_id: str) -> Path:
    """dataset/media/images/<image_id>.png — CONFIRMED pattern, problem_statement.md."""
    return MEDIA_IMAGES_DIR / f"{image_id}.png"


# ---------------------------------------------------------------------------
# Forecast / money constants
# ---------------------------------------------------------------------------
FORECAST_HORIZON_DAYS = 90          # CONFIRMED — "90-Day Safety Check"
MONEY_QUANTIZE = Decimal("0.01")    # all Decimal money rounds to cents before compare/output
MAX_SPENDING_CHANGES = 3            # CONFIRMED — spending_changes_needed allows up to 3
DATE_FORMAT = "%Y-%m-%d"            # CONFIRMED — "All dates use the YYYY-MM-DD format"
PLAN_LEG_SEP = "|"                  # CONFIRMED — payment_plan / spending_changes_needed separator
PLAN_FIELD_SEP = ":"                # CONFIRMED
NONE_TOKEN = "none"                 # CONFIRMED — "Use `none` when no ... is recommended"

VALID_HOME_CURRENCIES = {"INR", "ZAR", "IDR", "USD", "EUR"}  # CONFIRMED (also matches
                                                              # financial_profiles.csv's
                                                              # 5 distinct home_currency values)

# List-valued cells (financial_priorities, category lists,
# payment_methods_user_will_consider) use "|" — CONFIRMED by direct inspection
# of financial_profiles.csv (e.g. "education|debt_repayment").
LIST_CELL_DELIMITER = "|"

# ---------------------------------------------------------------------------
# Column-name contracts — ALL CONFIRMED against the uploaded files.
# ---------------------------------------------------------------------------

# requests.csv / sample_requests.csv — CONFIRMED, problem_statement.md + sample file
REQUESTS_COLUMNS = [
    "request_id", "user_id", "request_date", "request_type",
    "requested_amount", "desired_completion_date", "allows_partial_payment",
    "request_text",
]
SAMPLE_REQUESTS_EXTRA_COLUMNS = [
    "amount_safe_to_pay", "affordability_status", "recommended_payment_method",
    "payment_plan", "earliest_date_for_full_payment", "spending_changes_needed",
    "decision_explanation",
]

# output.csv — CONFIRMED, exact order required by problem_statement.md
OUTPUT_COLUMNS = [
    "request_id", "amount_safe_to_pay", "affordability_status",
    "recommended_payment_method", "payment_plan",
    "earliest_date_for_full_payment", "spending_changes_needed",
    "decision_explanation",
]

# financial_profiles.csv — CONFIRMED header:
#   user_id, home_currency, current_available_balance, minimum_balance_to_keep,
#   financial_priorities, expense_categories_to_protect,
#   expense_categories_user_is_willing_to_reduce,
#   expense_categories_user_is_willing_to_stop,
#   payment_methods_user_will_consider, max_installment_months
# CHANGED from first pass: "available_balance" -> "current_available_balance";
# the single assumed "flexible_categories" is actually TWO separate lists
# (reduce vs stop permissions), which can overlap for the same category.
FINANCIAL_PROFILE_COLUMNS = {
    "user_id": "user_id",
    "home_currency": "home_currency",
    "available_balance": "current_available_balance",              # renamed
    "minimum_balance_to_keep": "minimum_balance_to_keep",
    "financial_priorities": "financial_priorities",
    "protected_categories": "expense_categories_to_protect",       # renamed
    "reducible_categories": "expense_categories_user_is_willing_to_reduce",  # split out
    "stoppable_categories": "expense_categories_user_is_willing_to_stop",    # split out
    "payment_methods_user_will_consider": "payment_methods_user_will_consider",
    "max_installment_months": "max_installment_months",
}

# financial_events.csv — CONFIRMED header:
#   event_id, user_id, event_type, description, category, direction, amount,
#   currency, event_date, settlement_date, status, linked_event_id,
#   flexibility, minimum_allowed_amount
# CHANGED from first pass: "flow" -> "direction" (3 values, incl. non_cash);
# "cash_state" -> "status" (same 6 values as originally assumed, different
# column name); "flexibility" is a real 4-value column, not our 3-value guess;
# added "event_type" and "minimum_allowed_amount"; REMOVED "recurrence" /
# "recurrence_interval_days" — no such columns exist, recurrence must be
# detected from history in engine/recurrence_detector.py.
FINANCIAL_EVENT_COLUMNS = {
    "event_id": "event_id",
    "user_id": "user_id",
    "event_type": "event_type",                       # new
    "event_date": "event_date",
    "settlement_date": "settlement_date",
    "category": "category",
    "description": "description",
    "amount": "amount",                                 # blank => resolve via image, never 0
    "currency": "currency",
    "direction": "direction",                            # renamed from "flow"
    "status": "status",                                    # renamed from "cash_state"
    "flexibility": "flexibility",                            # real 4-value column now
    "minimum_allowed_amount": "minimum_allowed_amount",        # new — floor for reduce_to
    "linked_event_id": "linked_event_id",
}

# exchange_rates.csv — CONFIRMED exact match to first-pass assumption.
EXCHANGE_RATE_COLUMNS = {
    "rate_date": "rate_date",
    "from_currency": "from_currency",
    "to_currency": "to_currency",
    "rate": "rate",
}

# request_payment_options.csv — CONFIRMED header:
#   payment_option_id, request_id, payment_method, payment_amount,
#   number_of_payments, first_payment_date, payment_frequency_days,
#   financing_fee, total_payable_amount
# CHANGED from first pass: "per_payment_amount" -> "payment_amount";
# "num_payments" -> "number_of_payments"; "interval_days" ->
# "payment_frequency_days" (blank for the single-payment "full_payment" rows,
# confirmed blank on all 275 such rows, populated on all 515 "installments"
# rows); added "payment_method" column (values: full_payment, installments —
# so a lump-sum option can itself appear in this table, not only as a
# synthesized full_payment candidate).
REQUEST_PAYMENT_OPTION_COLUMNS = {
    "payment_option_id": "payment_option_id",
    "request_id": "request_id",
    "payment_method": "payment_method",                     # new
    "payment_amount": "payment_amount",                       # renamed
    "number_of_payments": "number_of_payments",                 # renamed
    "first_payment_date": "first_payment_date",
    "payment_frequency_days": "payment_frequency_days",           # renamed, optional
    "financing_fee": "financing_fee",
    "total_payable_amount": "total_payable_amount",
}

# messages.csv — CONFIRMED header:
#   message_id, user_id, request_id, related_event_id, sent_at, source_type,
#   message_text
# CHANGED from first pass: "message_date" -> "sent_at", and it's a full
# ISO-8601 datetime with a trailing "Z" (e.g. 2025-07-29T09:30:00Z), not a
# bare date; added "source_type" (5 values). user_id is always populated (0
# blanks / 215); request_id is blank on 87/215 rows; related_event_id is
# blank on 176/215 rows (populated only when the message maps 1:1 to a
# supplied financial-event row, per problem_statement.md).
MESSAGE_COLUMNS = {
    "message_id": "message_id",
    "user_id": "user_id",
    "request_id": "request_id",
    "related_event_id": "related_event_id",
    "sent_at": "sent_at",                    # renamed, now a datetime not a date
    "source_type": "source_type",              # new
    "message_text": "message_text",
}

# images.csv — CONFIRMED header: image_id, user_id, request_id,
# related_event_id. All 16 rows have every field populated, and the 16
# related_event_id values match the 16 financial_events.csv rows with a blank
# amount exactly 1:1.
IMAGE_COLUMNS = {
    "image_id": "image_id",
    "user_id": "user_id",
    "request_id": "request_id",
    "related_event_id": "related_event_id",
}

# ---------------------------------------------------------------------------
# Multimodal / LLM seam config — never imported by engine/ or optimizer/
# ---------------------------------------------------------------------------
VISION_MODEL = os.environ.get("BUY_OR_WAIT_VISION_MODEL", "claude-sonnet-5")
TEXT_RESOLVER_MODEL = os.environ.get("BUY_OR_WAIT_TEXT_MODEL", "claude-sonnet-5")
EXPLAINER_MODEL = os.environ.get("BUY_OR_WAIT_EXPLAIN_MODEL", "claude-haiku-4-5-20251001")
