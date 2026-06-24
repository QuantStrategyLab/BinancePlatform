#!/usr/bin/env python3
"""Verify that the Binance Runtime workflow completed successfully recently."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from typing import Any


def _split_values(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.replace(";", ",").replace("\n", ",").split(",") if part.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    value = (os.environ.get(name) or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _github_request(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _workflow_paths(workflow: str) -> set[str]:
    workflow = workflow.strip()
    paths = {workflow}
    if "/" not in workflow:
        paths.add(f".github/workflows/{workflow}")
    if workflow.startswith(".github/workflows/"):
        paths.add(workflow.rsplit("/", 1)[-1])
    return paths


def _dedupe_and_sort_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for run in runs:
        key = str(run.get("id") or run.get("run_number") or run.get("html_url") or len(unique))
        unique[key] = run

    def created_at(run: dict[str, Any]) -> dt.datetime:
        minimum = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        return _parse_timestamp(run.get("created_at")) or minimum

    return sorted(
        unique.values(),
        key=created_at,
        reverse=True,
    )


def _list_workflow_runs(
    *,
    repository: str,
    workflow: str,
    token: str,
    branch: str,
    per_page: int,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "branch": branch,
            "event": "workflow_dispatch",
            "per_page": str(per_page),
        }
    )
    url = f"https://api.github.com/repos/{repository}/actions/workflows/{workflow}/runs?{query}"
    payload = _github_request(url, token)
    runs = payload.get("workflow_runs")
    return runs if isinstance(runs, list) else []


def _list_repository_workflow_runs(
    *,
    repository: str,
    workflow: str,
    token: str,
    branch: str,
    per_page: int,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "branch": branch,
            "event": "workflow_dispatch",
            "per_page": str(per_page),
        }
    )
    url = f"https://api.github.com/repos/{repository}/actions/runs?{query}"
    payload = _github_request(url, token)
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        return []
    expected_paths = _workflow_paths(workflow)
    return [run for run in runs if str(run.get("path") or "") in expected_paths]


def _list_runtime_runs(
    *,
    repository: str,
    workflow: str,
    token: str,
    branch: str,
    per_page: int,
) -> list[dict[str, Any]]:
    workflow_runs = _list_workflow_runs(
        repository=repository,
        workflow=workflow,
        token=token,
        branch=branch,
        per_page=per_page,
    )
    try:
        repository_runs = _list_repository_workflow_runs(
            repository=repository,
            workflow=workflow,
            token=token,
            branch=branch,
            per_page=per_page,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Repository-level workflow run lookup skipped: {exc}", file=sys.stderr)
        repository_runs = []
    return _dedupe_and_sort_runs([*workflow_runs, *repository_runs])


def _send_telegram(message: str) -> bool:
    token = os.environ.get("TG_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    chats = _split_values(os.environ.get("GLOBAL_TELEGRAM_CHAT_ID"))
    if not token or not chats:
        print("Telegram heartbeat notification skipped: target is not configured.", file=sys.stderr)
        return False
    ok = True
    for chat_id in chats:
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                ok = ok and response.status < 400
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"Telegram send failed: {exc}", file=sys.stderr)
    return ok


def main() -> int:
    repository = os.environ.get("GITHUB_REPOSITORY") or "QuantStrategyLab/BinancePlatform"
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN is required")
    workflow = os.environ.get("RUNTIME_HEARTBEAT_WORKFLOW") or "main.yml"
    branch = os.environ.get("RUNTIME_HEARTBEAT_BRANCH") or "main"
    name = os.environ.get("RUNTIME_HEARTBEAT_NAME") or "Binance Runtime"
    lookback_hours = float(os.environ.get("RUNTIME_HEARTBEAT_LOOKBACK_HOURS") or "2.5")
    per_page = int(os.environ.get("RUNTIME_HEARTBEAT_RUNS_TO_SCAN") or "30")
    fail_workflow = _env_bool("RUNTIME_HEARTBEAT_FAIL_WORKFLOW_ON_ALERT", True)

    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=lookback_hours)
    runs = _list_runtime_runs(
        repository=repository,
        workflow=workflow,
        token=token,
        branch=branch,
        per_page=per_page,
    )
    recent_runs = []
    for run in runs:
        created_at = _parse_timestamp(run.get("created_at"))
        if created_at and created_at >= since:
            recent_runs.append(run)

    successful_runs = [run for run in recent_runs if run.get("status") == "completed" and run.get("conclusion") == "success"]
    latest_run = recent_runs[0] if recent_runs else None
    issues = []
    if not recent_runs:
        issues.append(f"no Runtime workflow_dispatch run found in the last {lookback_hours:g} hours")
    if not successful_runs:
        issues.append(f"no successful Runtime workflow run found in the last {lookback_hours:g} hours")
    if latest_run and latest_run.get("status") == "completed" and latest_run.get("conclusion") != "success":
        issues.append(
            f"latest Runtime run completed with conclusion={latest_run.get('conclusion') or '<none>'}"
        )

    if not issues:
        latest_success = successful_runs[0]
        print(
            "Runtime workflow heartbeat OK: "
            f"run={latest_success.get('run_number')} "
            f"created_at={latest_success.get('created_at')} "
            f"url={latest_success.get('html_url')}"
        )
        return 0

    lines = [
        f"[Runtime Workflow Heartbeat] {name}",
        f"Lookback: {lookback_hours:g} hours",
        "Issues:",
        *[f"- {issue}" for issue in issues],
    ]
    if latest_run:
        lines.extend(
            [
                "Latest Runtime run:",
                f"- run: #{latest_run.get('run_number')} status={latest_run.get('status')} conclusion={latest_run.get('conclusion')}",
                f"- created_at: {latest_run.get('created_at')}",
                f"- url: {latest_run.get('html_url')}",
            ]
        )
    if os.environ.get("GITHUB_SERVER_URL") and os.environ.get("GITHUB_RUN_ID"):
        lines.append(f"Heartbeat workflow: {os.environ['GITHUB_SERVER_URL']}/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    message = "\n".join(lines)
    print(message)
    _send_telegram(message[:3900])
    return 1 if fail_workflow else 0


if __name__ == "__main__":
    raise SystemExit(main())
