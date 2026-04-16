from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
SUPPORTED_REVIEW_KINDS = {"upstream_selector", "execution_runtime"}

SECONDARY_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "review_kind": {"type": "string"},
        "provider": {"type": "string"},
        "provider_display_name": {"type": "string"},
        "model": {"type": "string"},
        "verdict": {"type": "string", "enum": ["agree", "partial_agree", "disagree"]},
        "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
        "production_recommendation": {
            "type": "string",
            "enum": ["keep_production_as_is", "research_only", "needs_attention"],
        },
        "summary": {"type": "string"},
        "key_findings": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 5,
        },
        "recommended_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "owner_repo": {
                        "type": "string",
                        "enum": ["CryptoSnapshotPipelines", "CryptoStrategies", "BinancePlatform"],
                    },
                    "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
                    "auto_pr_safe": {"type": "boolean"},
                    "experiment_only": {"type": "boolean"},
                    "summary": {"type": "string"},
                },
                "required": [
                    "title",
                    "owner_repo",
                    "risk_level",
                    "auto_pr_safe",
                    "experiment_only",
                    "summary",
                ],
            },
            "maxItems": 5,
        },
        "follow_up_checks": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 5,
        },
    },
    "required": [
        "review_kind",
        "provider",
        "provider_display_name",
        "model",
        "verdict",
        "risk_level",
        "production_recommendation",
        "summary",
        "key_findings",
        "recommended_actions",
        "follow_up_checks",
    ],
}


def build_system_prompt(review_kind: str) -> str:
    if review_kind == "upstream_selector":
        return (
            "You are the independent secondary reviewer for CryptoSnapshotPipelines, an upstream selector "
            "repository that publishes a monthly 5-symbol Binance Spot leader pool. Review the issue body "
            "and the Claude primary review, then return only valid JSON matching the provided schema. "
            "Do not simply echo Claude. Re-check whether release consistency, selector quality, "
            "shadow/challenger evidence, and downstream BinancePlatform impact actually support the same conclusion. "
            "Use recommended_actions for concrete next steps, and only mark auto_pr_safe=true for low-risk "
            "changes like workflow, telemetry, report wording, tests, or challenger/shadow configuration."
        )
    if review_kind == "execution_runtime":
        return (
            "You are the independent secondary reviewer for BinancePlatform, a downstream Binance Spot execution "
            "engine. Review the issue body and the Claude primary review, then return only valid JSON matching "
            "the provided schema. Do not simply echo Claude. Re-check whether execution health, gating/no-trade "
            "reasons, degraded mode, circuit breaker behavior, and cash-flow context support the same conclusion. "
            "Use recommended_actions for concrete next steps, and only mark auto_pr_safe=true for low-risk "
            "changes like workflow, telemetry, report wording, tests, or diagnostics."
        )
    raise ValueError(f"Unsupported review kind: {review_kind}")


def build_user_prompt(issue_title: str, issue_body: str, primary_review_text: str) -> str:
    return (
        "Independently review the monthly report below, then compare it against the Claude primary review. "
        "If Claude looks too strong or too weak, say so in verdict/summary/findings.\n\n"
        f"## Issue Title\n{issue_title.strip()}\n\n"
        f"## Issue Body\n{issue_body.strip()}\n\n"
        f"## Claude Primary Review\n{primary_review_text.strip()}\n"
    )


def build_request_payload(
    *,
    model: str,
    review_kind: str,
    issue_title: str,
    issue_body: str,
    primary_review_text: str,
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": build_system_prompt(review_kind)},
            {
                "role": "user",
                "content": build_user_prompt(issue_title, issue_body, primary_review_text),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "secondary_monthly_review",
                "strict": True,
                "schema": SECONDARY_REVIEW_SCHEMA,
            },
        },
    }


def extract_completion_content(response_payload: dict[str, Any]) -> str:
    choices = response_payload.get("choices") or []
    if not choices:
        raise ValueError("OpenAI response did not include choices")

    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("OpenAI response did not include text content")
    return content.strip()


def call_openai(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(
        OPENAI_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        charset = response.headers.get_content_charset("utf-8")
        return json.loads(response.read().decode(charset))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an OpenAI secondary review for the monthly AI review workflow.",
    )
    parser.add_argument("--review-kind", required=True, choices=sorted(SUPPORTED_REVIEW_KINDS))
    parser.add_argument("--issue-context-file", required=True, type=Path)
    parser.add_argument("--primary-review-file", required=True, type=Path)
    parser.add_argument("--output-file", required=True, type=Path)
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-5.4-mini"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY is required", file=sys.stderr)
        return 1

    issue_context = json.loads(args.issue_context_file.read_text(encoding="utf-8"))
    primary_review_text = args.primary_review_file.read_text(encoding="utf-8")
    payload = build_request_payload(
        model=args.model,
        review_kind=args.review_kind,
        issue_title=str(issue_context.get("title", "")),
        issue_body=str(issue_context.get("body", "")),
        primary_review_text=primary_review_text,
    )

    try:
        response_payload = call_openai(payload, api_key)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"OpenAI API request failed: {exc.code} {detail}", file=sys.stderr)
        return 1

    review_payload = json.loads(extract_completion_content(response_payload))
    review_payload["review_kind"] = args.review_kind
    review_payload["provider"] = "openai"
    review_payload["provider_display_name"] = "GPT Secondary Review"
    review_payload["model"] = args.model

    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    args.output_file.write_text(json.dumps(review_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"secondary_review={args.output_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
