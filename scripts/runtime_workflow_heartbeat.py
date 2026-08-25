#!/usr/bin/env python3
"""Verify that the Binance Runtime workflow completed successfully recently."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


_GITHUB_API_MAX_ATTEMPTS = 4
_GITHUB_API_MAX_RETRY_DELAY_SECONDS = 30.0
_RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
_ASSESSMENT_SCHEMA = "qsl.runtime_heartbeat_assessment.v1"


def _split_values(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.replace(";", ",").replace("\n", ",").split(",") if part.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    value = (os.environ.get(name) or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


def _positive_float_from_env(name: str, default: float) -> float:
    raw_value = (os.environ.get(name) or "").strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a positive number") from exc
    if value <= 0:
        raise SystemExit(f"{name} must be a positive number")
    return value


def _positive_int_from_env(name: str, default: int) -> int:
    raw_value = (os.environ.get(name) or "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise SystemExit(f"{name} must be a positive integer")
    return value


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


def _github_retry_delay(exc: urllib.error.HTTPError | urllib.error.URLError, attempt: int) -> float:
    retry_after = exc.headers.get("Retry-After") if isinstance(exc, urllib.error.HTTPError) else None
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), _GITHUB_API_MAX_RETRY_DELAY_SECONDS)
        except ValueError:
            pass
    return min(float(2 ** (attempt - 1)), _GITHUB_API_MAX_RETRY_DELAY_SECONDS)


def _github_request(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    for attempt in range(1, _GITHUB_API_MAX_ATTEMPTS + 1):
        retry_error: urllib.error.HTTPError | urllib.error.URLError | None = None
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            retryable = exc.code in _RETRYABLE_HTTP_STATUSES
            if not retryable or attempt == _GITHUB_API_MAX_ATTEMPTS:
                raise
            retry_error = exc
        except urllib.error.URLError as exc:
            if attempt == _GITHUB_API_MAX_ATTEMPTS:
                raise
            retry_error = exc

        assert retry_error is not None
        delay = _github_retry_delay(retry_error, attempt)
        print(
            "GitHub API request failed "
            f"({retry_error}); retrying in {delay:g}s "
            f"(attempt {attempt + 1}/{_GITHUB_API_MAX_ATTEMPTS})",
            file=sys.stderr,
        )
        time.sleep(delay)

    raise RuntimeError("unreachable")


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


def _run_summary(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "run_number": run.get("run_number"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "html_url": run.get("html_url"),
    }


def _assess_runtime_heartbeat(
    *,
    runs: list[dict[str, Any]],
    now: dt.datetime,
    lookback_hours: float,
    expected_interval_hours: float,
    max_consecutive_misses: int,
) -> dict[str, Any]:
    """Classify a runtime observation without treating one late dispatch as failed.

    A completed runtime failure remains an immediate alert.  An absent new
    dispatch is only escalated after the last successful run has missed the
    configured number of expected cadence intervals.  The returned record is
    intentionally JSON-safe so the workflow log is an audit trail of both the
    query and the state transition.
    """

    normalized_now = now.astimezone(dt.timezone.utc)
    recent_since = normalized_now - dt.timedelta(hours=lookback_hours)
    sorted_runs = _dedupe_and_sort_runs(runs)
    latest_run = sorted_runs[0] if sorted_runs else None
    latest_success = next(
        (
            run
            for run in sorted_runs
            if run.get("status") == "completed" and run.get("conclusion") == "success"
        ),
        None,
    )
    recent_runs = [
        run
        for run in sorted_runs
        if (created_at := _parse_timestamp(run.get("created_at"))) and created_at >= recent_since
    ]
    recent_success = [
        run
        for run in recent_runs
        if run.get("status") == "completed" and run.get("conclusion") == "success"
    ]
    latest_created_at = _parse_timestamp(latest_run.get("created_at")) if latest_run else None
    latest_success_at = _parse_timestamp(latest_success.get("created_at")) if latest_success else None

    assessment: dict[str, Any] = {
        "schema": _ASSESSMENT_SCHEMA,
        "observed_at": normalized_now.isoformat().replace("+00:00", "Z"),
        "status": "healthy",
        "reason": "recent_success",
        "query": {
            "lookback_hours": lookback_hours,
            "expected_interval_hours": expected_interval_hours,
            "max_consecutive_misses": max_consecutive_misses,
            "runs_returned": len(sorted_runs),
            "recent_runs": len(recent_runs),
        },
        "latest_run": _run_summary(latest_run),
        "latest_success": _run_summary(latest_success),
        "consecutive_misses": 0,
    }

    if (
        latest_run
        and latest_created_at
        and latest_created_at >= recent_since
        and latest_run.get("status") == "completed"
        and latest_run.get("conclusion") != "success"
    ):
        assessment.update(status="alert", reason="latest_runtime_completed_unsuccessfully")
        return assessment

    if recent_success:
        return assessment

    if (
        latest_run
        and latest_created_at
        and latest_created_at >= recent_since
        and latest_run.get("status") != "completed"
    ):
        assessment.update(status="parked", reason="runtime_dispatch_pending")
        return assessment

    if latest_success_at is None:
        assessment.update(status="alert", reason="no_successful_runtime_run_observed")
        return assessment

    elapsed_hours = max(0.0, (normalized_now - latest_success_at).total_seconds() / 3600)
    consecutive_misses = int(elapsed_hours // expected_interval_hours)
    assessment["consecutive_misses"] = consecutive_misses
    if consecutive_misses < max_consecutive_misses:
        assessment.update(status="deferred", reason="awaiting_dispatch_confirmation")
        return assessment

    assessment.update(status="alert", reason="consecutive_runtime_dispatches_missing")
    return assessment


def _send_telegram(message: str) -> bool:
    token = os.environ.get("TG_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    chats = _split_values(os.environ.get("QSL_GLOBAL_TELEGRAM_CHAT_ID") or os.environ.get("GLOBAL_TELEGRAM_CHAT_ID"))
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
                try:
                    payload = json.loads(response.read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = {}
                ok = (
                    ok
                    and response.status < 400
                    and isinstance(payload, dict)
                    and payload.get("ok") is True
                )
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
    lookback_hours = _positive_float_from_env("RUNTIME_HEARTBEAT_LOOKBACK_HOURS", 2.5)
    expected_interval_hours = _positive_float_from_env("RUNTIME_HEARTBEAT_EXPECTED_INTERVAL_HOURS", 1.0)
    max_consecutive_misses = _positive_int_from_env("RUNTIME_HEARTBEAT_MAX_CONSECUTIVE_MISSES", 2)
    per_page = _positive_int_from_env("RUNTIME_HEARTBEAT_RUNS_TO_SCAN", 30)
    fail_workflow = _env_bool("RUNTIME_HEARTBEAT_FAIL_WORKFLOW_ON_ALERT", True)

    now = dt.datetime.now(dt.timezone.utc)
    runs = _list_runtime_runs(
        repository=repository,
        workflow=workflow,
        token=token,
        branch=branch,
        per_page=per_page,
    )
    assessment = _assess_runtime_heartbeat(
        runs=runs,
        now=now,
        lookback_hours=lookback_hours,
        expected_interval_hours=expected_interval_hours,
        max_consecutive_misses=max_consecutive_misses,
    )
    print(json.dumps(assessment, sort_keys=True))

    if assessment["status"] == "healthy":
        latest_success = assessment["latest_success"] or {}
        print(
            "Runtime workflow heartbeat OK: "
            f"run={latest_success.get('run_number')} "
            f"created_at={latest_success.get('created_at')} "
            f"url={latest_success.get('html_url')}"
        )
        return 0

    if assessment["status"] in {"parked", "deferred"}:
        print(
            "Runtime workflow heartbeat "
            f"{assessment['status'].upper()}: reason={assessment['reason']} "
            f"consecutive_misses={assessment['consecutive_misses']}"
        )
        return 0

    latest_run = assessment["latest_run"]
    issues = []
    if assessment["reason"] == "latest_runtime_completed_unsuccessfully":
        issues.append(
            "latest Runtime run completed with "
            f"conclusion={latest_run.get('conclusion') or '<none>'}"
        )
    elif assessment["reason"] == "no_successful_runtime_run_observed":
        issues.append(
            "no successful Runtime workflow run was returned by the auditable "
            "GitHub Actions query"
        )
    else:
        issues.append(
            "Runtime workflow dispatches have been missing for "
            f"{assessment['consecutive_misses']} consecutive expected intervals "
            f"(threshold={max_consecutive_misses})"
        )
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
