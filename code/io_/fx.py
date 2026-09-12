"""
Dated, directional currency conversion — the ONLY place amounts cross a
currency boundary. Deterministic: the same (amount, from, to, date) always
converts identically, and a missing rate raises rather than guessing or
inverting, per problem_statement.md ("Do not invent unsupported ... financial
information") and AGENTS.md's exact-direction instruction.
"""
from __future__ import annotations
from datetime import date
from decimal import Decimal
from pathlib import Path

from code.config import settings
from code.domain.schemas import ExchangeRate, FinancialEvent, FinancialProfile


class MissingExchangeRateError(Exception):
    def __init__(self, rate_date: date, from_currency: str, to_currency: str):
        self.rate_date = rate_date
        self.from_currency = from_currency
        self.to_currency = to_currency
        super().__init__(
            f"no exchange_rates.csv row for {from_currency}->{to_currency} on "
            f"{rate_date}. Not inventing a rate — either exchange_rates.csv is "
            f"missing this row, or the settlement date used for lookup is wrong "
            f"(check FinancialEvent.effective_date vs. the rate's date)."
        )


class FxTable:
    """Indexes exchange_rates.csv by (date, from_currency, to_currency) exactly
    as specified: 'use the row for its settlement date and the stated
    from_currency to to_currency direction' (AGENTS.md §6.1). No automatic
    inversion (1/rate) and no interpolation across dates — both would be
    inventing a figure the dataset didn't supply.
    """

    def __init__(self, rates: list[ExchangeRate]):
        self._index: dict[tuple[date, str, str], Decimal] = {
            (r.rate_date, r.from_currency, r.to_currency): r.rate for r in rates
        }

    def rate(self, on_date: date, from_currency: str, to_currency: str) -> Decimal:
        if from_currency == to_currency:
            return Decimal("1")
        key = (on_date, from_currency, to_currency)
        if key not in self._index:
            raise MissingExchangeRateError(on_date, from_currency, to_currency)
        return self._index[key]

    def convert(self, amount: Decimal, from_currency: str, to_currency: str, on_date: date) -> Decimal:
        rate = self.rate(on_date, from_currency, to_currency)
        return (amount * rate).quantize(settings.MONEY_QUANTIZE)


def normalize_event_to_home_currency(
    event: FinancialEvent,
    profile: FinancialProfile,
    fx_table: FxTable,
) -> FinancialEvent:
    """Returns a copy of `event` with amount/currency converted to
    profile.home_currency, using the event's effective (settlement) date for
    the lookup. A None amount (unresolved, pending image extraction) passes
    through unchanged — conversion happens after multimodal resolution, not
    before. Verified against the real dataset: all 140 currency-mismatched
    events in financial_events.csv have an exact-date rate available (no
    interpolation ever needed)."""
    if event.amount is None or event.currency == profile.home_currency:
        return event
    converted_amount = fx_table.convert(
        event.amount, event.currency, profile.home_currency, event.effective_date
    )
    return event.model_copy(update={"amount": converted_amount, "currency": profile.home_currency})


def normalize_events_to_home_currency(
    events: list[FinancialEvent],
    profile: FinancialProfile,
    fx_table: FxTable,
) -> list[FinancialEvent]:
    """Batch convenience wrapper over normalize_event_to_home_currency."""
    return [normalize_event_to_home_currency(e, profile, fx_table) for e in events]


def load_fx_table(path: Path = settings.EXCHANGE_RATES_CSV) -> FxTable:
    from code.io_.loaders import load_exchange_rates  # local import avoids a cycle at module load

    rates, errors = load_exchange_rates(path)
    if errors:
        raise ValueError(
            f"{len(errors)} malformed row(s) in {path.name}, e.g.: "
            f"{[str(e) for e in errors[:5]]}"
        )
    return FxTable(rates)
