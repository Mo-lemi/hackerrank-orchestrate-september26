"""
CSV -> Pydantic loaders for Buy or Wait?, updated against the real, uploaded
dataset/*.csv files. Column names are looked up through the *_COLUMNS maps in
code/config/settings.py (all now CONFIRMED, not assumed) — nothing here
hardcodes a header name directly.

Every loader still:
  1. fails loudly (LoaderError) if a required column is absent;
  2. parses each row defensively, collecting per-row failures (RowError)
     instead of crashing the whole file on one bad row;
  3. never defaults a blank `amount` to 0.
"""
from __future__ import annotations
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from code.config import settings
from code.config.enums import (
    RequestType,
    PaymentMethod,
    EventType,
    EventDirection,
    EventStatus,
    EventFlexibility,
    MessageSourceType,
)
from code.domain.schemas import (
    RequestRecord,
    FinancialProfile,
    FinancialEvent,
    RequestPaymentOption,
    ExchangeRate,
    MessageRecord,
    ImageRecord,
)


class LoaderError(Exception):
    """File-level failure: missing column(s). Fix by editing the matching
    *_COLUMNS map in code/config/settings.py."""


class RowError(Exception):
    """Row-level failure: collected rather than raised, so one bad row
    doesn't block loading the rest of the file."""
    def __init__(self, row_number: int, raw_row: dict, cause: Exception):
        self.row_number = row_number
        self.raw_row = raw_row
        self.cause = cause
        super().__init__(f"row {row_number}: {cause} | raw={raw_row}")


# ---------------------------------------------------------------------------
# Generic CSV plumbing
# ---------------------------------------------------------------------------

def _read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        raise LoaderError(f"missing file: {path}")
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        rows = list(reader)
    return header, rows


def _check_columns(header: list[str], expected: list[str], file_label: str) -> None:
    missing = [c for c in expected if c not in header]
    if missing:
        raise LoaderError(
            f"{file_label}: missing expected column(s) {missing}. Actual header: "
            f"{header}. If these are just named differently, update the matching "
            f"*_COLUMNS map in code/config/settings.py — nothing else needs to change."
        )


def _get(row: dict[str, str], columns: dict[str, str], key: str) -> str:
    """Look up a logical field via a settings.*_COLUMNS alias map."""
    return (row.get(columns[key], "") or "").strip()


# ---------------------------------------------------------------------------
# Scalar parsers
# ---------------------------------------------------------------------------

def _parse_bool(raw: str) -> bool:
    v = raw.strip().lower()
    if v in ("true", "1", "yes", "y"):
        return True
    if v in ("false", "0", "no", "n", ""):
        return False
    raise ValueError(f"unrecognized boolean value: {raw!r}")


def _parse_decimal(raw: str) -> Decimal:
    v = raw.strip().replace(",", "")
    if v == "":
        raise ValueError("empty decimal value")
    try:
        return Decimal(v)
    except InvalidOperation as e:
        raise ValueError(f"invalid decimal value: {raw!r}") from e


def _parse_optional_decimal(raw: str) -> Optional[Decimal]:
    if raw is None or raw.strip() == "":
        return None   # NEVER coerced to 0 — see module docstring
    return _parse_decimal(raw)


def _parse_date(raw: str) -> date:
    return date.fromisoformat(raw.strip())


def _parse_optional_date(raw: str) -> Optional[date]:
    if raw is None or raw.strip() == "":
        return None
    return _parse_date(raw)


def _parse_datetime_z(raw: str) -> datetime:
    """Parses messages.csv's sent_at, e.g. '2025-07-29T09:30:00Z'. Handled via
    an explicit 'Z' -> '+00:00' swap rather than relying on Python's own
    fromisoformat Z-support (added in 3.11), so this stays portable to older
    interpreters."""
    v = raw.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    return datetime.fromisoformat(v)


def _parse_optional_int(raw: str) -> Optional[int]:
    if raw is None or raw.strip() == "":
        return None
    return int(raw.strip())


def _parse_list_cell(raw: str) -> list[str]:
    if raw is None or raw.strip() == "":
        return []
    return [item.strip() for item in raw.split(settings.LIST_CELL_DELIMITER) if item.strip()]


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_requests(path: Path) -> tuple[list[RequestRecord], list[RowError]]:
    header, rows = _read_rows(path)
    _check_columns(header, settings.REQUESTS_COLUMNS, path.name)
    records: list[RequestRecord] = []
    errors: list[RowError] = []
    for i, row in enumerate(rows, start=2):  # header occupies row 1
        try:
            records.append(RequestRecord(
                request_id=row["request_id"],
                user_id=row["user_id"],
                request_date=_parse_date(row["request_date"]),
                request_type=RequestType(row["request_type"]),
                requested_amount=_parse_decimal(row["requested_amount"]),
                desired_completion_date=_parse_date(row["desired_completion_date"]),
                allows_partial_payment=_parse_bool(row["allows_partial_payment"]),
                request_text=row["request_text"],
            ))
        except Exception as e:
            errors.append(RowError(i, row, e))
    return records, errors


def load_financial_profiles(path: Path) -> tuple[dict[str, FinancialProfile], list[RowError]]:
    cols = settings.FINANCIAL_PROFILE_COLUMNS
    header, rows = _read_rows(path)
    _check_columns(header, list(cols.values()), path.name)
    profiles: dict[str, FinancialProfile] = {}
    errors: list[RowError] = []
    for i, row in enumerate(rows, start=2):
        try:
            methods_raw = _parse_list_cell(_get(row, cols, "payment_methods_user_will_consider"))
            profile = FinancialProfile(
                user_id=_get(row, cols, "user_id"),
                home_currency=_get(row, cols, "home_currency"),
                available_balance=_parse_decimal(_get(row, cols, "available_balance")),
                minimum_balance_to_keep=_parse_decimal(_get(row, cols, "minimum_balance_to_keep")),
                financial_priorities=_parse_list_cell(_get(row, cols, "financial_priorities")),
                protected_categories=_parse_list_cell(_get(row, cols, "protected_categories")),
                reducible_categories=_parse_list_cell(_get(row, cols, "reducible_categories")),
                stoppable_categories=_parse_list_cell(_get(row, cols, "stoppable_categories")),
                payment_methods_user_will_consider=[PaymentMethod(m) for m in methods_raw],
                max_installment_months=_parse_optional_int(_get(row, cols, "max_installment_months")),
            )
            profiles[profile.user_id] = profile
        except Exception as e:
            errors.append(RowError(i, row, e))
    return profiles, errors


def load_financial_events(path: Path) -> tuple[list[FinancialEvent], list[RowError]]:
    cols = settings.FINANCIAL_EVENT_COLUMNS
    header, rows = _read_rows(path)
    _check_columns(header, list(cols.values()), path.name)
    events: list[FinancialEvent] = []
    errors: list[RowError] = []
    for i, row in enumerate(rows, start=2):
        try:
            flexibility_raw = _get(row, cols, "flexibility")
            flexibility = EventFlexibility(flexibility_raw) if flexibility_raw else EventFlexibility.FIXED
            events.append(FinancialEvent(
                event_id=_get(row, cols, "event_id"),
                user_id=_get(row, cols, "user_id"),
                event_type=EventType(_get(row, cols, "event_type")),
                event_date=_parse_date(_get(row, cols, "event_date")),
                settlement_date=_parse_optional_date(_get(row, cols, "settlement_date")),
                category=_get(row, cols, "category"),
                description=_get(row, cols, "description") or None,
                amount=_parse_optional_decimal(_get(row, cols, "amount")),
                currency=_get(row, cols, "currency"),
                direction=EventDirection(_get(row, cols, "direction")),
                status=EventStatus(_get(row, cols, "status")),
                flexibility=flexibility,
                minimum_allowed_amount=_parse_optional_decimal(_get(row, cols, "minimum_allowed_amount")),
                linked_event_id=_get(row, cols, "linked_event_id") or None,
                amount_source="csv" if _get(row, cols, "amount") else None,
            ))
        except Exception as e:
            errors.append(RowError(i, row, e))
    return events, errors


def load_exchange_rates(path: Path) -> tuple[list[ExchangeRate], list[RowError]]:
    cols = settings.EXCHANGE_RATE_COLUMNS
    header, rows = _read_rows(path)
    _check_columns(header, list(cols.values()), path.name)
    rates: list[ExchangeRate] = []
    errors: list[RowError] = []
    for i, row in enumerate(rows, start=2):
        try:
            rates.append(ExchangeRate(
                rate_date=_parse_date(_get(row, cols, "rate_date")),
                from_currency=_get(row, cols, "from_currency"),
                to_currency=_get(row, cols, "to_currency"),
                rate=_parse_decimal(_get(row, cols, "rate")),
            ))
        except Exception as e:
            errors.append(RowError(i, row, e))
    return rates, errors


def load_payment_options(path: Path) -> tuple[dict[str, list[RequestPaymentOption]], list[RowError]]:
    cols = settings.REQUEST_PAYMENT_OPTION_COLUMNS
    header, rows = _read_rows(path)
    _check_columns(header, list(cols.values()), path.name)
    by_request: dict[str, list[RequestPaymentOption]] = {}
    errors: list[RowError] = []
    for i, row in enumerate(rows, start=2):
        try:
            option = RequestPaymentOption(
                payment_option_id=_get(row, cols, "payment_option_id"),
                request_id=_get(row, cols, "request_id"),
                payment_method=PaymentMethod(_get(row, cols, "payment_method")),
                payment_amount=_parse_decimal(_get(row, cols, "payment_amount")),
                number_of_payments=int(_get(row, cols, "number_of_payments")),
                first_payment_date=_parse_date(_get(row, cols, "first_payment_date")),
                payment_frequency_days=_parse_optional_int(_get(row, cols, "payment_frequency_days")),
                financing_fee=_parse_decimal(_get(row, cols, "financing_fee") or "0"),
                total_payable_amount=_parse_decimal(_get(row, cols, "total_payable_amount")),
            )
            by_request.setdefault(option.request_id, []).append(option)
        except Exception as e:
            errors.append(RowError(i, row, e))
    return by_request, errors


def load_messages(path: Path) -> tuple[list[MessageRecord], list[RowError]]:
    cols = settings.MESSAGE_COLUMNS
    header, rows = _read_rows(path)
    _check_columns(header, list(cols.values()), path.name)
    messages: list[MessageRecord] = []
    errors: list[RowError] = []
    for i, row in enumerate(rows, start=2):
        try:
            messages.append(MessageRecord(
                message_id=_get(row, cols, "message_id"),
                user_id=_get(row, cols, "user_id"),
                request_id=_get(row, cols, "request_id") or None,
                related_event_id=_get(row, cols, "related_event_id") or None,
                sent_at=_parse_datetime_z(_get(row, cols, "sent_at")),
                source_type=MessageSourceType(_get(row, cols, "source_type")),
                message_text=_get(row, cols, "message_text"),
            ))
        except Exception as e:
            errors.append(RowError(i, row, e))
    return messages, errors


def load_images(path: Path) -> tuple[list[ImageRecord], list[RowError]]:
    cols = settings.IMAGE_COLUMNS
    header, rows = _read_rows(path)
    _check_columns(header, list(cols.values()), path.name)
    images: list[ImageRecord] = []
    errors: list[RowError] = []
    for i, row in enumerate(rows, start=2):
        try:
            images.append(ImageRecord(
                image_id=_get(row, cols, "image_id"),
                user_id=_get(row, cols, "user_id"),
                request_id=_get(row, cols, "request_id"),
                related_event_id=_get(row, cols, "related_event_id"),
            ))
        except Exception as e:
            errors.append(RowError(i, row, e))
    return images, errors
