"""
Writes the final, already-validated OutputRow instances to output.csv in
the exact column order and formatting problem_statement.md requires.

Format enforcement for payment_plan / spending_changes_needed strings
already happens twice over: format_payment_plan below always quantizes to
2 decimal places before building the string, and OutputRow's own
field_validators (domain/schemas.py) re-parse and reject anything malformed
at construction time. This module's remaining job is purely mechanical:
column order, the blank-string convention for a null
earliest_date_for_full_payment, and correct CSV quoting for free-text
fields like decision_explanation (which can contain commas/quotes).
"""
from __future__ import annotations
import csv
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

from code.config import settings
from code.config.enums import AffordabilityStatus, PaymentMethod
from code.domain.schemas import OutputRow


def format_payment_plan(payments: tuple[tuple[date, Decimal], ...]) -> str:
    """'<YYYY-MM-DD>:<amount>|...' in chronological order, or 'none'. Every
    amount is quantized to exactly 2 decimal places here regardless of how
    it arrived upstream, since this is the last stop before it becomes an
    output string."""
    if not payments:
        return settings.NONE_TOKEN
    legs = [
        f"{pay_date.isoformat()}{settings.PLAN_FIELD_SEP}{amount.quantize(settings.MONEY_QUANTIZE)}"
        for pay_date, amount in payments
    ]
    return settings.PLAN_LEG_SEP.join(legs)


def validate_output_rows(rows: list[OutputRow], expected_request_ids: set[str]) -> list[str]:
    """Pre-flight sanity check before writing. Returns a list of problem
    descriptions (empty means all clear) — never raises, since one
    missing/duplicate row shouldn't block writing everything else that's
    fine. main.py should print these, not silently swallow them."""
    problems: list[str] = []
    seen = Counter(row.request_id for row in rows)

    duplicates = [rid for rid, count in seen.items() if count > 1]
    if duplicates:
        problems.append(f"{len(duplicates)} request_id(s) appear more than once: {sorted(duplicates)[:10]}")

    missing = expected_request_ids - set(seen)
    if missing:
        problems.append(f"{len(missing)} request_id(s) never produced an output row: {sorted(missing)[:10]}")

    extra = set(seen) - expected_request_ids
    if extra:
        problems.append(f"{len(extra)} output row(s) have a request_id not in the input requests: {sorted(extra)[:10]}")

    for row in rows:
        if row.affordability_status == AffordabilityStatus.AFFORDABLE_NOW and row.earliest_date_for_full_payment is None:
            problems.append(f"{row.request_id}: affordable_now but earliest_date_for_full_payment is blank")
        if row.recommended_payment_method == PaymentMethod.NOT_RECOMMENDED and row.payment_plan != settings.NONE_TOKEN:
            problems.append(f"{row.request_id}: not_recommended but payment_plan is not 'none'")

    return problems


def write_output_csv(rows: list[OutputRow], path: Path = settings.OUTPUT_CSV_PATH) -> None:
    """Writes rows in settings.OUTPUT_COLUMNS order. OutputRow.to_csv_row
    already renders every field to its final string form (including the
    empty-string convention for a null earliest_date_for_full_payment) —
    this function's only remaining job is the file/column mechanics."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=settings.OUTPUT_COLUMNS, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.to_csv_row(settings.OUTPUT_COLUMNS))


class IncrementalCsvWriter:
    """Per-request incremental writer for main.py's loop, so a crash
    partway through a long run still leaves a valid, partially-complete
    output.csv on disk instead of nothing at all.

    Usage:
        with IncrementalCsvWriter(path) as writer:
            for record in requests:
                writer.write(process_request(record, ctx))
    """
    def __init__(self, path: Path = settings.OUTPUT_CSV_PATH):
        self.path = path

    def __enter__(self) -> "IncrementalCsvWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=settings.OUTPUT_COLUMNS, quoting=csv.QUOTE_MINIMAL)
        self._writer.writeheader()
        return self

    def write(self, row: OutputRow) -> None:
        self._writer.writerow(row.to_csv_row(settings.OUTPUT_COLUMNS))
        self._file.flush()

    def __exit__(self, exc_type, exc, tb) -> None:
        self._file.close()
