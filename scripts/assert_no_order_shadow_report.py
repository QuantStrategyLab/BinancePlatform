#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def validate_no_order_report(report: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(report, dict):
        return ["report must be a JSON object"]

    if report.get("status") != "ok":
        errors.append("report status must be ok")
    if report.get("dry_run") is not True:
        errors.append("report dry_run must be true")

    summary = report.get("side_effect_summary")
    if not isinstance(summary, dict):
        errors.append("side_effect_summary must be an object")
        return errors

    if summary.get("executed_call_count") != 0:
        errors.append("executed_call_count must be zero")
    suppressed = summary.get("suppressed_call_count")
    if not isinstance(suppressed, int) or isinstance(suppressed, bool) or suppressed < 1:
        errors.append("suppressed_call_count must be a positive integer")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fail unless a replay report proves no-order shadow execution.")
    parser.add_argument("report", type=Path, help="Path to the structured replay report.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    errors = validate_no_order_report(report)
    if errors:
        raise SystemExit("No-order shadow report rejected: " + "; ".join(errors))
    print("No-order shadow report accepted: dry_run=true executed_call_count=0")


if __name__ == "__main__":
    main()
