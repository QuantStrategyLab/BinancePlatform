#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VOLATILE_TOP_LEVEL_FIELDS = {
    "account_group",
    "account_region",
    "account_scope",
    "deploy_target",
    "finished_at",
    "instance_name",
    "project_id",
    "run_id",
    "run_source",
}


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


def normalize_shadow_report(report: dict[str, Any]) -> dict[str, Any]:
    """Remove deployment identity only; preserve every strategy decision and intent."""
    normalized = copy.deepcopy(report)
    for field in VOLATILE_TOP_LEVEL_FIELDS:
        normalized.pop(field, None)
    for notification in normalized.get("notifications", []):
        if isinstance(notification, dict):
            notification.pop("run_id", None)
    return normalized


def semantic_report_sha256(report: dict[str, Any]) -> str:
    canonical = json.dumps(
        normalize_shadow_report(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fail unless a replay report proves no-order shadow execution.")
    parser.add_argument("report", type=Path, help="Path to the structured replay report.")
    parser.add_argument(
        "--expected-sha256",
        default="",
        help="Optional expected semantic report digest. A mismatch fails closed.",
    )
    parser.add_argument(
        "--digest-output",
        type=Path,
        help="Optional file that receives the accepted semantic SHA-256 digest.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    errors = validate_no_order_report(report)
    if errors:
        raise SystemExit("No-order shadow report rejected: " + "; ".join(errors))
    digest = semantic_report_sha256(report)
    expected = args.expected_sha256.strip().lower()
    if expected and not SHA256_RE.fullmatch(expected):
        raise SystemExit("Expected semantic report digest must be 64 lowercase hexadecimal characters")
    if expected and digest != expected:
        raise SystemExit(f"Semantic shadow report digest mismatch: expected={expected} actual={digest}")
    if args.digest_output:
        args.digest_output.parent.mkdir(parents=True, exist_ok=True)
        args.digest_output.write_text(digest + "\n", encoding="utf-8")
    print(
        "No-order shadow report accepted: "
        f"dry_run=true executed_call_count=0 semantic_sha256={digest}"
    )


if __name__ == "__main__":
    main()
