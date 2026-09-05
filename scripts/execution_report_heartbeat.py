#!/usr/bin/env python3
"""Check recent Binance execution reports using read-only Cloud Storage calls."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


_SCHEMA = "qsl.execution_report_heartbeat_assessment.v1"
_ACCEPTED_STATUSES = frozenset({"ok", "skipped", "success", "completed", "no_action", "aborted"})


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _runtime_target_enabled() -> bool | None:
    raw = (os.environ.get("RUNTIME_TARGET_ENABLED") or "").strip().lower()
    if not raw:
        return True
    if raw == "true":
        return True
    if raw == "false":
        return False
    return None


def _parse_timestamp(value: object) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _month_segments(since: dt.datetime, now: dt.datetime) -> list[str]:
    cursor = dt.datetime(since.year, since.month, 1, tzinfo=dt.timezone.utc)
    end = dt.datetime(now.year, now.month, 1, tzinfo=dt.timezone.utc)
    values: list[str] = []
    while cursor <= end:
        values.append(f"{cursor.year:04d}-{cursor.month:02d}")
        if cursor.month == 12:
            cursor = dt.datetime(cursor.year + 1, 1, 1, tzinfo=dt.timezone.utc)
        else:
            cursor = dt.datetime(cursor.year, cursor.month + 1, 1, tzinfo=dt.timezone.utc)
    return values


def _run_gcloud(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=False)


def _gcloud_project_args() -> list[str]:
    project = (
        os.environ.get("RUNTIME_HEARTBEAT_GCP_PROJECT_ID")
        or os.environ.get("GCP_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or ""
    ).strip()
    return [f"--project={project}"] if project else []


def _list_reports(*, since: dt.datetime, now: dt.datetime) -> tuple[list[dict[str, Any]], str]:
    base_uri = (
        os.environ.get("RUNTIME_HEARTBEAT_GCS_URI")
        or os.environ.get("EXECUTION_REPORT_GCS_URI")
        or ""
    ).strip().rstrip("/")
    platform = (os.environ.get("RUNTIME_HEARTBEAT_REPORT_PLATFORM") or "binance").strip("/").lower()
    strategy = (os.environ.get("RUNTIME_HEARTBEAT_STRATEGY_PROFILE") or "").strip()
    if not base_uri.startswith("gs://") or not platform or not strategy:
        raise RuntimeError("execution report URI, platform, and strategy profile are required")

    entries: list[dict[str, Any]] = []
    for month in _month_segments(since, now):
        glob = f"{base_uri}/{platform}/{strategy}/**/{month}/*.json"
        result = _run_gcloud(["gcloud", "storage", "ls", "--json", glob, *_gcloud_project_args()])
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            if "matched no objects" in detail.lower() or "no urls matched" in detail.lower():
                continue
            raise RuntimeError(detail or f"gcloud storage ls failed for {glob}")
        if not result.stdout.strip():
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"gcloud storage ls returned invalid JSON: {exc}") from exc
        if isinstance(payload, list):
            entries.extend(entry for entry in payload if isinstance(entry, dict))
    return entries, base_uri


def _entry_updated_at(entry: dict[str, Any]) -> dt.datetime | None:
    metadata = entry.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return _parse_timestamp(metadata.get("updated") or metadata.get("timeCreated"))


def _entry_uri(entry: dict[str, Any]) -> str:
    return str(entry.get("url") or "").split("#", 1)[0]


def _read_report(uri: str) -> dict[str, Any] | None:
    result = _run_gcloud(["gcloud", "storage", "cat", uri, *_gcloud_project_args()])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _payload_service_name(payload: dict[str, Any]) -> str:
    target = payload.get("runtime_target")
    target_service = target.get("service_name") if isinstance(target, dict) else ""
    return str(payload.get("service_name") or target_service or "").strip()


def _payload_strategy(payload: dict[str, Any]) -> str:
    target = payload.get("runtime_target")
    target_strategy = target.get("strategy_profile") if isinstance(target, dict) else ""
    return str(payload.get("strategy_profile") or target_strategy or "").strip()


def _accepted_payload(payload: dict[str, Any]) -> tuple[bool, str]:
    expected_platform = (os.environ.get("RUNTIME_HEARTBEAT_REPORT_PLATFORM") or "binance").strip().lower()
    expected_strategy = (os.environ.get("RUNTIME_HEARTBEAT_STRATEGY_PROFILE") or "").strip()
    expected_service = (os.environ.get("RUNTIME_HEARTBEAT_SERVICE_NAME") or "").strip()
    if str(payload.get("platform") or "").strip().lower() != expected_platform:
        return False, "platform does not match"
    if expected_strategy and _payload_strategy(payload) != expected_strategy:
        return False, "strategy profile does not match"
    if expected_service and _payload_service_name(payload) != expected_service:
        return False, "service name does not match"
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        return False, "report contains errors"
    error_summary = payload.get("error_summary")
    nested_errors = error_summary.get("errors") if isinstance(error_summary, dict) else None
    if isinstance(nested_errors, list) and nested_errors:
        return False, "report error summary is non-empty"
    status = str(payload.get("status") or "").strip().lower()
    if status not in _ACCEPTED_STATUSES:
        return False, f"unaccepted status={status or '-'}"
    return True, f"status={status}"


def _deployment_observation(payload: dict[str, Any], *, now: dt.datetime) -> dict[str, Any] | None:
    """Project the last run, never the hosted monitor's current environment."""
    target = payload.get("runtime_target")
    if not isinstance(target, dict) or target.get("platform_id") != "binance":
        return None
    profile = target.get("strategy_profile")
    if not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._=-]{0,127}", profile):
        return None
    if profile != _payload_strategy(payload):
        return None
    try:
        observed = dt.datetime.fromisoformat(str(payload.get("started_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if observed.tzinfo is None or observed > now:
        return None

    mode = target.get("execution_mode")
    dry_run = payload.get("dry_run")
    if not isinstance(mode, str) or mode not in {"live", "paper", "dry_run"}:
        mode = ("dry_run" if dry_run else "live") if isinstance(dry_run, bool) else None
    elif (mode == "live" and dry_run is True) or (mode != "live" and dry_run is False):
        mode = None
    permitted = payload.get("standard_execution_permitted")
    return {
        "runtime_enabled": permitted if isinstance(permitted, bool) else None,
        "scheduler_state": "unknown",
        "strategy_profile": profile,
        "execution_mode": mode,
        "observed_at": observed.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def assess_execution_report_heartbeat(now: dt.datetime | None = None) -> dict[str, Any]:
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    name = os.environ.get("RUNTIME_HEARTBEAT_NAME") or "Binance Runtime"
    target_enabled = _runtime_target_enabled()
    if target_enabled is False:
        return {
            "schema": _SCHEMA,
            "observed_at": current.isoformat().replace("+00:00", "Z"),
            "status": "not_applicable",
            "reason": "runtime_target_disabled",
            "name": name,
        }
    if target_enabled is None:
        return {
            "schema": _SCHEMA,
            "observed_at": current.isoformat().replace("+00:00", "Z"),
            "status": "unavailable",
            "reason": "RUNTIME_TARGET_ENABLED must be true or false",
            "name": name,
        }
    try:
        lookback_hours = float(os.environ.get("RUNTIME_HEARTBEAT_LOOKBACK_HOURS") or "2.5")
        if lookback_hours <= 0:
            raise ValueError("RUNTIME_HEARTBEAT_LOOKBACK_HOURS must be positive")
        since = current - dt.timedelta(hours=lookback_hours)
        entries, _ = _list_reports(since=since, now=current)
    except (RuntimeError, ValueError) as exc:
        return {
            "schema": _SCHEMA,
            "observed_at": current.isoformat().replace("+00:00", "Z"),
            "status": "unavailable",
            "reason": str(exc),
            "name": name,
        }

    recent = [entry for entry in entries if (_entry_updated_at(entry) or current) >= since]
    for entry in sorted(recent, key=lambda item: _entry_updated_at(item) or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)[:10]:
        uri = _entry_uri(entry)
        if not uri:
            continue
        payload = _read_report(uri)
        if payload is None:
            continue
        accepted, reason = _accepted_payload(payload)
        if accepted:
            assessment = {
                "schema": _SCHEMA,
                "observed_at": current.isoformat().replace("+00:00", "Z"),
                "status": "healthy",
                "reason": reason,
                "name": name,
                "report_updated_at": (_entry_updated_at(entry) or current).isoformat().replace("+00:00", "Z"),
            }
            deployment = _deployment_observation(payload, now=current)
            if deployment is not None:
                assessment["deployment"] = deployment
            return assessment

    return {
        "schema": _SCHEMA,
        "observed_at": current.isoformat().replace("+00:00", "Z"),
        "status": "alert",
        "reason": "no_recent_accepted_execution_report",
        "name": name,
        "reports_returned": len(entries),
    }


def _write_assessment(assessment: dict[str, Any]) -> None:
    raw_path = (os.environ.get("RUNTIME_HEARTBEAT_OUTPUT_PATH") or "").strip()
    if not raw_path:
        return
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(assessment, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    assessment = assess_execution_report_heartbeat()
    _write_assessment(assessment)
    print(json.dumps(assessment, sort_keys=True))
    if assessment["status"] in {"healthy", "not_applicable"}:
        return 0
    return 1 if _env_bool("RUNTIME_HEARTBEAT_FAIL_WORKFLOW_ON_ALERT", True) else 0


if __name__ == "__main__":
    raise SystemExit(main())
