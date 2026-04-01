from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def extract_latest_assistant_text(execution_log: list[dict[str, Any]]) -> str:
    for turn in reversed(execution_log):
        if turn.get("type") != "assistant":
            continue

        content_items = turn.get("message", {}).get("content", [])
        text_parts = [
            item.get("text", "").strip()
            for item in content_items
            if item.get("type") == "text" and item.get("text", "").strip()
        ]
        if text_parts:
            return "\n\n".join(text_parts).strip()

    raise ValueError("No assistant review text found in execution log")


def load_primary_review_markdown(*, execution_file: Path | None, primary_review_file: Path | None) -> str:
    if primary_review_file is not None:
        return primary_review_file.read_text(encoding="utf-8").strip()
    if execution_file is not None:
        execution_log = json.loads(execution_file.read_text(encoding="utf-8"))
        return extract_latest_assistant_text(execution_log)
    raise ValueError("Either execution_file or primary_review_file is required")


def render_secondary_review_markdown(payload: dict[str, Any]) -> str:
    lines: list[str] = [
        f"## Secondary Review ({payload.get('provider_display_name', 'GPT')})",
        "",
        f"- Verdict: `{payload['verdict']}`",
        f"- Risk Level: `{payload['risk_level']}`",
        f"- Production Recommendation: `{payload['production_recommendation']}`",
        f"- Summary: {payload['summary']}",
    ]

    findings = [item.strip() for item in payload.get("key_findings", []) if str(item).strip()]
    if findings:
        lines.extend(["", "### Key Findings"])
        lines.extend(f"- {item}" for item in findings)

    actions = payload.get("recommended_actions", [])
    if actions:
        lines.extend(["", "### Recommended Actions"])
        for action in actions:
            flags: list[str] = []
            if action.get("auto_pr_safe"):
                flags.append("auto-pr-safe")
            if action.get("experiment_only"):
                flags.append("experiment-only")
            flag_text = f" [{', '.join(flags)}]" if flags else ""
            lines.append(
                "- "
                f"{action['title']} "
                f"({action['owner_repo']}, risk={action['risk_level']}){flag_text}: {action['summary']}"
            )

    follow_up_checks = [item.strip() for item in payload.get("follow_up_checks", []) if str(item).strip()]
    if follow_up_checks:
        lines.extend(["", "### Follow-up Checks"])
        lines.extend(f"- {item}" for item in follow_up_checks)

    return "\n".join(lines).strip()


def build_full_review_markdown(
    primary_review_text: str,
    *,
    primary_title: str,
    secondary_review_payload: dict[str, Any] | None = None,
) -> str:
    lines = [f"## {primary_title}", "", primary_review_text.strip()]
    if secondary_review_payload is not None:
        lines.extend(["", "---", "", render_secondary_review_markdown(secondary_review_payload)])
    return "\n".join(lines).strip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render markdown for the monthly AI review from primary and optional secondary review outputs.",
    )
    parser.add_argument("--output-file", required=True, type=Path)
    parser.add_argument("--execution-file", type=Path)
    parser.add_argument("--primary-review-file", type=Path)
    parser.add_argument("--secondary-review-file", type=Path)
    parser.add_argument("--primary-title", default="Claude Primary Review")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    primary_review_text = load_primary_review_markdown(
        execution_file=args.execution_file,
        primary_review_file=args.primary_review_file,
    )
    secondary_review_payload = None
    if args.secondary_review_file is not None:
        secondary_review_payload = json.loads(args.secondary_review_file.read_text(encoding="utf-8"))

    markdown = build_full_review_markdown(
        primary_review_text,
        primary_title=args.primary_title,
        secondary_review_payload=secondary_review_payload,
    )
    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    args.output_file.write_text(markdown, encoding="utf-8")
    print(f"review_markdown={args.output_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
