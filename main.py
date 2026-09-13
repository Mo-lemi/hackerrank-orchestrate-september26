#!/usr/bin/env python3
"""
Buy or Wait? — entry point.

Run from the repo root:
    python main.py                     # processes dataset/requests.csv
    python main.py --sample            # processes dataset/sample_requests.csv instead
                                        # (useful here since dataset/requests.csv wasn't
                                        # part of the files reviewed while building this)
    python main.py --skip-vision       # skip Claude vision calls; blank amounts stay
                                        # unresolved (logged, excluded from simulation —
                                        # never guessed) instead of erroring
    python main.py --limit 20          # process only the first 20 requests (smoke test)

Requires ANTHROPIC_API_KEY in the environment for vision extraction unless
--skip-vision is passed. Never hardcode the key — read it from the
environment only (see multimodal/vision_extractor.py).
"""
from __future__ import annotations
import argparse
import os
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

from code.config import settings
from code.domain.schemas import OutputRow
from code.io_ import loaders
from code.io_.fx import load_fx_table
from code.io_.writers import IncrementalCsvWriter, validate_output_rows
from code.multimodal.vision_extractor import AnthropicVisionClient, VisionExtractor, VisionExtractionError
from code.pipeline.request_processor import ProcessingContext, process_request


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Buy or Wait? — run the full prediction pipeline.")
    parser.add_argument("--sample", action="store_true",
                         help="process dataset/sample_requests.csv instead of dataset/requests.csv")
    parser.add_argument("--skip-vision", action="store_true",
                         help="skip Claude vision calls; blank amounts stay unresolved")
    parser.add_argument("--limit", type=int, default=None, help="process only the first N requests")
    parser.add_argument("--output", type=Path, default=None, help="override the output CSV path")
    return parser.parse_args()


def _index_by(items, key_fn):
    index: dict = {}
    for item in items:
        index.setdefault(key_fn(item), []).append(item)
    return index


def _fallback_row(request_id: str) -> OutputRow:
    """Emitted when a single request raises an unexpected exception, so a
    bug in one row never costs the whole run its "one row per request_id"
    completeness guarantee. Deliberately the most conservative row shape:
    nothing recommended, nothing paid, nothing computed we can't stand behind.
    """
    return OutputRow(
        request_id=request_id,
        amount_safe_to_pay=0,
        affordability_status="not_affordable",
        recommended_payment_method="not_recommended",
        payment_plan=settings.NONE_TOKEN,
        earliest_date_for_full_payment=None,
        spending_changes_needed=settings.NONE_TOKEN,
        decision_explanation="Could not be evaluated due to an internal processing error.",
    )


def main() -> int:
    args = _parse_args()
    started_at = time.monotonic()

    requests_path = settings.SAMPLE_REQUESTS_CSV if args.sample else settings.REQUESTS_CSV
    output_path = args.output or settings.OUTPUT_CSV_PATH

    print(f"Loading dataset from {settings.DATASET_DIR} ...")
    try:
        requests, request_errors = loaders.load_requests(requests_path)
        profiles, profile_errors = loaders.load_financial_profiles(settings.FINANCIAL_PROFILES_CSV)
        events, event_errors = loaders.load_financial_events(settings.FINANCIAL_EVENTS_CSV)
        messages, message_errors = loaders.load_messages(settings.MESSAGES_CSV)
        images, image_errors = loaders.load_images(settings.IMAGES_CSV)
        payment_options_by_request, option_errors = loaders.load_payment_options(settings.REQUEST_PAYMENT_OPTIONS_CSV)
        fx_table = load_fx_table(settings.EXCHANGE_RATES_CSV)
    except loaders.LoaderError as e:
        print(f"FATAL — could not load dataset: {e}", file=sys.stderr)
        return 1

    for label, errs in [
        ("requests.csv", request_errors), ("financial_profiles.csv", profile_errors),
        ("financial_events.csv", event_errors), ("messages.csv", message_errors),
        ("images.csv", image_errors), ("request_payment_options.csv", option_errors),
    ]:
        if errs:
            print(f"WARNING — {len(errs)} row error(s) in {label}, first few:", file=sys.stderr)
            for err in errs[:5]:
                print(f"    {err}", file=sys.stderr)

    if args.limit is not None:
        requests = requests[: args.limit]

    events_by_user = _index_by(events, lambda e: e.user_id)
    messages_by_user = _index_by(messages, lambda m: m.user_id)
    images_by_user = _index_by(images, lambda i: i.user_id)

    vision_extractor = None
    if not args.skip_vision:
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                vision_extractor = VisionExtractor(AnthropicVisionClient())
            except VisionExtractionError as e:
                print(f"WARNING — vision extraction unavailable ({e}); blank amounts will stay unresolved.",
                      file=sys.stderr)
        else:
            print("WARNING — ANTHROPIC_API_KEY not set; blank amounts will stay unresolved. "
                  "Pass --skip-vision to silence this warning.", file=sys.stderr)

    status_counts: Counter = Counter()
    method_counts: Counter = Counter()
    error_count = 0
    output_rows: list[OutputRow] = []

    print(f"Processing {len(requests)} request(s) -> {output_path}")
    with IncrementalCsvWriter(output_path) as writer:
        for record in requests:
            try:
                profile = profiles[record.user_id]
                ctx = ProcessingContext(
                    profile=profile,
                    events=events_by_user.get(record.user_id, []),
                    messages=messages_by_user.get(record.user_id, []),
                    payment_options=payment_options_by_request.get(record.request_id, []),
                    images_by_related_event={
                        img.related_event_id: img for img in images_by_user.get(record.user_id, [])
                    },
                    fx_table=fx_table,
                    vision_extractor=vision_extractor,
                )
                row = process_request(record, ctx)
            except Exception:
                error_count += 1
                print(f"ERROR processing {record.request_id} — emitting fallback row:", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                row = _fallback_row(record.request_id)

            status_counts[row.affordability_status.value] += 1
            method_counts[row.recommended_payment_method.value] += 1
            output_rows.append(row)
            writer.write(row)

    problems = validate_output_rows(output_rows, expected_request_ids={r.request_id for r in requests})
    elapsed = time.monotonic() - started_at

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"requests processed:     {len(requests)}")
    print(f"processing errors:      {error_count}")
    print("\nby affordability_status:")
    for status, count in sorted(status_counts.items()):
        print(f"  {status:24s} {count}")
    print("\nby recommended_payment_method:")
    for method, count in sorted(method_counts.items()):
        print(f"  {method:24s} {count}")
    if problems:
        print("\nVALIDATION WARNINGS:")
        for p in problems:
            print(f"  - {p}")
    print(f"\ntotal runtime: {elapsed:.1f}s ({elapsed / max(len(requests), 1):.2f}s/request)")
    print(f"output written to: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
