from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


ACTION_RE = re.compile(r"^- \[ \] `(?P<risk>low|medium|high)` (?P<title>.+?)(?: \[(?P<flags>[^\]]+)\])?$")
SUMMARY_RE = re.compile(r"^\s+- Summary: (?P<summary>.+)$")
SOURCE_RE = re.compile(r"^\s+- Source: \[(?P<label>.+?)\]\((?P<url>[^)]+)\)$")
MARKER_PREFIX = "<!-- auto-optimization-pr:issue-"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTO_MERGE_SAFE_TERMS = (
    "readme",
    "documentation",
    "document ",
    "document and",
    "docs",
    "operator runbook",
    "workflow",
    "ci",
    "report wording",
    "report format",
    "report template",
    "telemetry",
    "diagnostic",
    "instrumentation",
    "unit test",
    "tests",
    "test coverage",
)
AUTO_MERGE_BLOCK_TERMS = {
    "CryptoSnapshotPipelines": (
        "tie-break",
        "tie break",
        "ranking",
        "rank labels",
        "rank order",
        "regime",
        "selector",
        "score",
        "universe",
        "pool membership",
        "challenger",
        "shadow build",
        "walk-forward",
        "walk forward",
    ),
    "BinancePlatform": (
        "dca",
        "rotation",
        "eligibility gate",
        "free usdt",
        "cash flow",
        "withdrawal",
        "deposit",
        "open position",
        "execution",
        "live trading",
        "threshold",
        "circuit breaker",
        "capital threshold",
        "allocation",
        "sizing",
        "liquidity",
        "spread",
        "adv",
    ),
    "CryptoStrategies": (
        "strategy",
        "signal",
        "allocation",
        "sizing",
        "ranking",
        "selector",
        "rotation",
        "dca",
        "threshold",
    ),
}
SENSITIVE_PATH_PATTERNS = {
    "CryptoSnapshotPipelines": (
        r"^src/",
        r"^config/",
    ),
    "BinancePlatform": (
        r"^application/",
        r"^infra/",
        r"^strategy/",
        r"^entrypoints/",
        r"^main\.py$",
        r"^runtime_support\.py$",
        r"^live_services\.py$",
        r"^degraded_mode_support\.py$",
        r"^market_snapshot_support\.py$",
        r"^trade_state_support\.py$",
        r"^trend_pool_support\.py$",
        r"^strategy_runtime\.py$",
        r"^decision_mapper\.py$",
        r"^strategy_loader\.py$",
        r"^strategy_registry\.py$",
    ),
    "CryptoStrategies": (
        r"^src/",
    ),
}


def parse_actions(issue_body: str) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_actions = False

    for raw_line in issue_body.splitlines():
        line = raw_line.rstrip()
        if line == "## Actions":
            in_actions = True
            continue
        if in_actions and line.startswith("## "):
            break
        if not in_actions:
            continue

        action_match = ACTION_RE.match(line)
        if action_match:
            if current is not None:
                actions.append(current)
            flags = [flag.strip() for flag in (action_match.group("flags") or "").split(",") if flag.strip()]
            current = {
                "risk_level": action_match.group("risk"),
                "title": action_match.group("title").strip(),
                "flags": flags,
            }
            continue

        if current is None:
            continue

        summary_match = SUMMARY_RE.match(line)
        if summary_match:
            current["summary"] = summary_match.group("summary").strip()
            continue

        source_match = SOURCE_RE.match(line)
        if source_match:
            current["source_label"] = source_match.group("label").strip()
            current["source_url"] = source_match.group("url").strip()

    if current is not None:
        actions.append(current)
    return actions


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _is_completed_low_risk_task(action: dict[str, Any], repo_root: Path) -> bool:
    title = str(action.get("title", "")).lower()
    repo_name = repo_root.name

    if repo_name == "CryptoSnapshotPipelines":
        if "shadow/challenger build generation" in title or "shadow build" in title:
            workflow = _read_text(repo_root / ".github" / "workflows" / "monthly_publish.yml")
            return "run_monthly_shadow_build.py" in workflow
        if "tie-break" in title or "tie break" in title:
            readme = _read_text(repo_root / "README.md")
            runbook = _read_text(repo_root / "docs" / "operator_runbook.md")
            return (
                "Monthly ranking tie-break rule for `core_major` live exports:" in readme
                and "deterministic tie-break" in runbook
            )

    if repo_name == "BinancePlatform" and (
        "zero-trade diagnostics" in title
        or "diagnostic reporting for no-trade months" in title
        or "no-trade months" in title
    ):
        monthly_report = _read_text(repo_root / "scripts" / "run_monthly_report_bundle.py")
        return (
            "No explicit gating or no-trade reasons were recorded this month." in monthly_report
            and "gating_summary" in monthly_report
        )

    return False


def _normalized_action_text(action: dict[str, Any]) -> str:
    title = str(action.get("title", ""))
    summary = str(action.get("summary", ""))
    return f"{title}\n{summary}".lower()


def classify_action_for_auto_merge(action: dict[str, Any], repo_root: Path | None = None) -> tuple[bool, str]:
    repo_root = repo_root or PROJECT_ROOT
    text = _normalized_action_text(action)
    if not any(term in text for term in AUTO_MERGE_SAFE_TERMS):
        return False, "task_not_in_auto_merge_allowlist"

    for term in AUTO_MERGE_BLOCK_TERMS.get(repo_root.name, ()):  # pragma: no branch - tiny tuples
        if term in text:
            return False, f"guarded_keyword:{term}"
    return True, "auto_merge_safe"


def evaluate_changed_files(changed_files: list[str], repo_root: Path | None = None) -> dict[str, Any]:
    repo_root = repo_root or PROJECT_ROOT
    blocked_files: list[str] = []
    patterns = tuple(re.compile(pattern) for pattern in SENSITIVE_PATH_PATTERNS.get(repo_root.name, ()))
    for raw_path in changed_files:
        normalized = raw_path.strip().lstrip("./")
        if not normalized:
            continue
        if any(pattern.search(normalized) for pattern in patterns):
            blocked_files.append(normalized)
    return {
        "allowed": not blocked_files,
        "blocked_files": blocked_files,
    }


def build_payload(issue_context: dict[str, Any], repo_root: Path | None = None) -> dict[str, Any]:
    repo_root = repo_root or PROJECT_ROOT
    issue_number = int(issue_context["number"])
    issue_title = str(issue_context["title"]).strip()
    issue_body = str(issue_context["body"])
    parsed_actions = parse_actions(issue_body)
    low_safe_actions = [
        action
        for action in parsed_actions
        if action["risk_level"] == "low"
        and "auto-pr-safe" in action.get("flags", [])
        and "experiment-only" not in action.get("flags", [])
    ]
    safe_actions: list[dict[str, Any]] = []
    skipped_actions: list[dict[str, Any]] = []
    for action in low_safe_actions:
        if _is_completed_low_risk_task(action, repo_root):
            skipped_actions.append({**action, "skip_reason": "already_implemented"})
        else:
            safe_actions.append(action)

    auto_merge_candidate_actions: list[dict[str, Any]] = []
    draft_only_actions: list[dict[str, Any]] = []
    for action in safe_actions:
        eligible, reason = classify_action_for_auto_merge(action, repo_root)
        if eligible:
            auto_merge_candidate_actions.append({**action, "auto_merge_reason": reason})
        else:
            draft_only_actions.append({**action, "auto_merge_blocker": reason})

    task_level_auto_merge_allowed = bool(safe_actions) and not draft_only_actions and bool(auto_merge_candidate_actions)
    return {
        "issue_number": issue_number,
        "issue_title": issue_title,
        "branch_name": f"automation/monthly-optimization-issue-{issue_number}",
        "commit_message": f"chore: address monthly optimization issue #{issue_number}",
        "pr_title": f"Auto: address monthly optimization issue #{issue_number}",
        "should_run": bool(safe_actions),
        "safe_task_count": len(safe_actions),
        "skipped_task_count": len(skipped_actions),
        "auto_merge_candidate_count": len(auto_merge_candidate_actions),
        "draft_only_task_count": len(draft_only_actions),
        "task_level_auto_merge_allowed": task_level_auto_merge_allowed,
        "safe_actions": safe_actions,
        "skipped_actions": skipped_actions,
        "auto_merge_candidate_actions": auto_merge_candidate_actions,
        "draft_only_actions": draft_only_actions,
    }


def render_task_summary(payload: dict[str, Any]) -> str:
    lines = [
        "# Auto Optimization Candidate Tasks",
        "",
        f"- Issue: #{payload['issue_number']} {payload['issue_title']}",
        f"- Eligible low-risk auto-pr-safe tasks: `{payload['safe_task_count']}`",
        f"- Task-level auto-merge candidates: `{payload['auto_merge_candidate_count']}`",
        f"- Draft-only low-risk tasks: `{payload['draft_only_task_count']}`",
    ]
    if payload["skipped_actions"]:
        lines.append(f"- Skipped as already implemented: `{payload['skipped_task_count']}`")
    if not payload["safe_actions"]:
        lines.extend(["", "No eligible low-risk [auto-pr-safe] tasks remain for automation."])
        if payload["skipped_actions"]:
            lines.extend(["", "## Skipped Tasks"])
            for action in payload["skipped_actions"]:
                lines.extend(
                    [
                        f"- `{action['risk_level']}` {action['title']}",
                        f"  - Reason: {action['skip_reason']}",
                    ]
                )
        return "\n".join(lines).strip() + "\n"

    if payload["auto_merge_candidate_actions"]:
        lines.extend(["", "## Auto-Merge Candidate Tasks"])
        for action in payload["auto_merge_candidate_actions"]:
            flag_suffix = f" [{', '.join(action['flags'])}]" if action.get("flags") else ""
            lines.extend(
                [
                    f"- `{action['risk_level']}` {action['title']}{flag_suffix}",
                    f"  - Summary: {action.get('summary', 'No summary provided.')}",
                    f"  - Source: {action.get('source_label', 'Unknown source')} ({action.get('source_url', 'n/a')})",
                ]
            )

    if payload["draft_only_actions"]:
        lines.extend(["", "## Draft-Only Tasks"])
        for action in payload["draft_only_actions"]:
            flag_suffix = f" [{', '.join(action['flags'])}]" if action.get("flags") else ""
            lines.extend(
                [
                    f"- `{action['risk_level']}` {action['title']}{flag_suffix}",
                    f"  - Summary: {action.get('summary', 'No summary provided.')}",
                    f"  - Auto-merge blocker: {action.get('auto_merge_blocker', 'guarded_task')}",
                ]
            )

    if payload["skipped_actions"]:
        lines.extend(["", "## Skipped Tasks"])
        for action in payload["skipped_actions"]:
            lines.extend(
                [
                    f"- `{action['risk_level']}` {action['title']}",
                    f"  - Reason: {action['skip_reason']}",
                ]
            )

    return "\n".join(lines).strip() + "\n"


def render_pr_body(payload: dict[str, Any]) -> str:
    lines = [
        f"{MARKER_PREFIX}{payload['issue_number']} -->",
        "## Summary",
        "This PR was generated from a monthly optimization task issue.",
        "It only targets low-risk items explicitly marked `[auto-pr-safe]`.",
        "",
        "## Merge Policy",
        f"- Task-level auto-merge eligible: `{'yes' if payload['task_level_auto_merge_allowed'] else 'no'}`",
        "- High-risk guardrails remain active for selector logic, live execution, and shared strategy code paths.",
        "",
        "## Auto-selected tasks",
    ]
    for action in payload["safe_actions"]:
        lines.append(f"- {action['title']}: {action.get('summary', 'No summary provided.')}")
    if payload["draft_only_actions"]:
        lines.extend(["", "## Draft-only tasks"])
        for action in payload["draft_only_actions"]:
            lines.append(f"- {action['title']}: {action.get('auto_merge_blocker', 'guarded_task')}")
    lines.extend(["", f"Refs #{payload['issue_number']}"])
    return "\n".join(lines).strip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare metadata for auto-generated optimization PRs.")
    parser.add_argument("--issue-context-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    issue_context = json.loads(args.issue_context_file.read_text(encoding="utf-8"))
    payload = build_payload(issue_context)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    payload_file = args.output_dir / "payload.json"
    task_summary_file = args.output_dir / "task_summary.md"
    pr_body_file = args.output_dir / "pr_body.md"
    payload_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    task_summary_file.write_text(render_task_summary(payload), encoding="utf-8")
    pr_body_file.write_text(render_pr_body(payload), encoding="utf-8")
    print(f"should_run={'true' if payload['should_run'] else 'false'}")
    print(f"issue_number={payload['issue_number']}")
    print(f"branch_name={payload['branch_name']}")
    print(f"commit_message={payload['commit_message']}")
    print(f"pr_title={payload['pr_title']}")
    print(f"safe_task_count={payload['safe_task_count']}")
    print(f"auto_merge_candidate_count={payload['auto_merge_candidate_count']}")
    print(f"task_level_auto_merge_allowed={'true' if payload['task_level_auto_merge_allowed'] else 'false'}")
    print(f"payload_file={payload_file}")
    print(f"task_summary_file={task_summary_file}")
    print(f"pr_body_file={pr_body_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
