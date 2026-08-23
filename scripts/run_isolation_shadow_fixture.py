#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from assert_no_order_shadow_report import (  # noqa: E402
    SHA256_RE,
    semantic_report_sha256,
    validate_no_order_report,
)

import run_cycle_replay  # noqa: E402


FIXED_RUN_ID = "runtime-isolation-fixture-shadow"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the deployment-neutral fixed-input, no-order shadow fixture."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--digest-output", type=Path, required=True)
    parser.add_argument(
        "--expected-sha256",
        default=os.getenv("SHADOW_EXPECTED_REPORT_SHA256", ""),
        help="Optional semantic digest produced by another isolated target.",
    )
    return parser.parse_args()


def main() -> None:
    if os.getenv("BINANCE_DRY_RUN", "").strip().lower() != "true":
        raise SystemExit("Isolation shadow requires BINANCE_DRY_RUN=true")

    args = parse_args()
    expected = args.expected_sha256.strip().lower()
    if expected and not SHA256_RE.fullmatch(expected):
        raise SystemExit("Expected semantic report digest must be 64 lowercase hexadecimal characters")

    result = run_cycle_replay.run_replay_cycle(
        run_id=FIXED_RUN_ID,
        dry_run=True,
        now_utc=run_cycle_replay.DEFAULT_REPLAY_TIME,
    )
    report = result["report"]
    errors = validate_no_order_report(report)
    if result["client"].side_effect_calls:
        errors.append("fixture client recorded a real side-effect call")
    if result["state_store"].write_calls:
        errors.append("fixture state store recorded a real write call")
    if errors:
        raise SystemExit("Isolation shadow rejected: " + "; ".join(errors))

    digest = semantic_report_sha256(report)
    if expected and digest != expected:
        raise SystemExit(f"Semantic shadow report digest mismatch: expected={expected} actual={digest}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.digest_output.parent.mkdir(parents=True, exist_ok=True)
    args.digest_output.write_text(digest + "\n", encoding="utf-8")
    print(
        "Isolation shadow accepted: "
        f"dry_run=true executed_call_count=0 semantic_sha256={digest}"
    )


if __name__ == "__main__":
    main()
