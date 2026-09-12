"""
Merges messages.csv evidence into events. Only messages with a populated
related_event_id are eligible — per AGENTS.md: "related_event_id is
populated only when the message directly describes one supplied
financial-event row; a blank value means no one-to-one event row exists." A
blank-related message is never used to create or mutate a FinancialEvent
here (it may still be useful context for request-text resolution elsewhere;
that is a different module's job, not this one's).

Classification is deterministic pattern matching, not an LLM call. Every one
of the 39 related-event messages in the uploaded dataset was inspected by
hand (both English and Bahasa Indonesia templates appear) and every single
one turned out to be purely corroborating — none contradicted the CSV row it
points to. The patterns below encode that finding AND extend to genuinely
contradicting phrasings (cancellation, delay, amended amount) that don't
appear in this sample but are named directly in problem_statement.md
("clarify, amend, cancel, delay, or confirm") and may appear in the hidden
requests.csv-linked messages.

SAFETY DEFAULT: a message that doesn't match a known pattern changes
NOTHING. This is deliberately conservative on both sides — an unrecognized
claim of new/confirmed income is ignored (income stays unconfirmed), and an
unrecognized claim of cancellation is ignored (the debit stays counted) — so
an unmatched message can only ever make the 90-day forecast MORE
conservative, never less. This is also this module's defense against prompt
injection: message_text is data matched against fixed patterns, never text
handed to a model with instruction-following authority over the pipeline.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Optional

from code.config.enums import EventStatus
from code.domain.schemas import FinancialEvent, MessageRecord


class MessageAction(str, Enum):
    CONFIRM = "confirm"              # corroborates the existing row; no change
    CANCEL = "cancel"                # explicit cancellation
    DELAY = "delay"                  # explicit new settlement date
    AMEND_AMOUNT = "amend_amount"    # explicit new amount
    UNCLASSIFIED = "unclassified"    # no known pattern matched; no change applied


@dataclass(frozen=True)
class MergeNote:
    message_id: str
    event_id: str
    action: MessageAction
    detail: str


# Bilingual (English / Bahasa Indonesia), matching the two languages observed
# in messages.csv. Extend this list rather than reaching for an LLM call if a
# new template shows up in the hidden dataset — keep the classifier auditable.
_CANCEL_PATTERNS = [
    re.compile(r"\bcancel+ed\b", re.I),
    re.compile(r"\bdibatalkan\b", re.I),
    re.compile(r"\bno longer (going ahead|proceeding)\b", re.I),
]

_CONFIRM_PATTERNS = [
    re.compile(r"\bhas not reached your account yet\b", re.I),
    re.compile(r"\bbelum masuk ke rekening\b", re.I),
    re.compile(r"\bclaim is now closed\b", re.I),
    re.compile(r"\bklaim (?:sudah|telah) ditutup\b", re.I),
    re.compile(r"\bproceeds have reached your account\b", re.I),
    re.compile(r"\bstill being investigated\b", re.I),
    re.compile(r"\bmasih dalam penyelidikan\b", re.I),
    re.compile(r"\bno units have been sold\b", re.I),
    re.compile(r"\bbelum dijual\b", re.I),
    re.compile(r"\bprevious debit attempt failed\b", re.I),
    re.compile(r"\bthe sale order is complete\b", re.I),
    re.compile(r"\bsudah masuk ke rekening tunai\b", re.I),
    re.compile(r"\breceipt (has|contains) the final\b", re.I),
    re.compile(r"\bdisplayed value will continue to move\b", re.I),
    re.compile(r"\bnilai yang ditampilkan akan terus berubah\b", re.I),
]

_DELAY_RE = re.compile(
    r"\b(?:delayed|postponed|rescheduled|now expected|tertunda)\s+"
    r"(?:to|until|hingga)\s+(\d{4}-\d{2}-\d{2})",
    re.I,
)

_AMEND_AMOUNT_RE = re.compile(
    r"\b(?:corrected to|updated amount is|revised to|jumlah yang diperbarui adalah)\s+"
    r"([A-Z]{3})?\s*([\d,]+\.?\d*)",
    re.I,
)


def classify_message(message: MessageRecord) -> tuple[MessageAction, Optional[dict]]:
    text = message.message_text

    for pat in _CANCEL_PATTERNS:
        if pat.search(text):
            return MessageAction.CANCEL, None

    m = _DELAY_RE.search(text)
    if m:
        return MessageAction.DELAY, {"new_date": m.group(1)}

    m = _AMEND_AMOUNT_RE.search(text)
    if m:
        try:
            amount = Decimal(m.group(2).replace(",", ""))
            return MessageAction.AMEND_AMOUNT, {"new_amount": amount, "currency": m.group(1)}
        except InvalidOperation:
            pass   # fall through to confirm/unclassified rather than raise

    for pat in _CONFIRM_PATTERNS:
        if pat.search(text):
            return MessageAction.CONFIRM, None

    return MessageAction.UNCLASSIFIED, None


def merge_messages(
    events: list[FinancialEvent],
    messages: list[MessageRecord],
) -> tuple[list[FinancialEvent], list[MergeNote]]:
    by_id = {e.event_id: e for e in events}
    notes: list[MergeNote] = []
    updates: dict[str, dict] = {}

    eligible = [m for m in messages if m.related_event_id]
    for message in eligible:
        event = by_id.get(message.related_event_id)
        if event is None:
            continue   # message references an event_id we don't have — skip, never invent one

        action, payload = classify_message(message)

        if action == MessageAction.CANCEL:
            updates.setdefault(event.event_id, {})["status"] = EventStatus.CANCELLED
            notes.append(MergeNote(message.message_id, event.event_id, action, "explicit cancellation applied"))

        elif action == MessageAction.DELAY and payload:
            updates.setdefault(event.event_id, {})["settlement_date"] = date.fromisoformat(payload["new_date"])
            notes.append(MergeNote(message.message_id, event.event_id, action,
                                    f"settlement_date moved to {payload['new_date']}"))

        elif action == MessageAction.AMEND_AMOUNT and payload:
            updates.setdefault(event.event_id, {})["amount"] = payload["new_amount"]
            updates[event.event_id]["amount_source"] = "message_amended"
            notes.append(MergeNote(message.message_id, event.event_id, action,
                                    f"amount amended to {payload['new_amount']}"))

        elif action == MessageAction.CONFIRM:
            notes.append(MergeNote(message.message_id, event.event_id, action, "corroborates existing row — no change"))

        else:
            notes.append(MergeNote(message.message_id, event.event_id, action,
                                    "no recognized pattern — no change applied (safe default)"))

    merged = [
        e if e.event_id not in updates else e.model_copy(update=updates[e.event_id])
        for e in events
    ]
    return merged, notes
