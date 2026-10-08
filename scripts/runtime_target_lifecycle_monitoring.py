#!/usr/bin/env python3
"""Map Binance's read-only monitor assessments to the shared lifecycle checks."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime


_CHECKS = frozenset({"pass", "attention", "not_due", "not_applicable", "unavailable"})
_DEPLOYMENT_FIELDS = ("runtime_enabled", "scheduler_state", "strategy_profile", "execution_mode", "observed_at")


def _read_assessment(path: str | None) -> dict:
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _workflow_check(status: str) -> str:
    return {
        "healthy": "pass",
        "deferred": "not_due",
        "parked": "not_due",
        "alert": "attention",
        "not_applicable": "not_applicable",
        "unavailable": "unavailable",
    }.get(status, "unavailable")


def _execution_check(status: str) -> str:
    return {
        "healthy": "pass",
        "alert": "attention",
        "not_applicable": "not_applicable",
        "unavailable": "unavailable",
    }.get(status, "unavailable")


def _deployment_json(value: object) -> str | None:
    if not isinstance(value, dict) or not all(field in value for field in _DEPLOYMENT_FIELDS):
        return None
    runtime_enabled = value.get("runtime_enabled")
    profile = value.get("strategy_profile")
    scheduler_state = value.get("scheduler_state")
    execution_mode = value.get("execution_mode")
    observed_at = value.get("observed_at")
    if runtime_enabled is False or (runtime_enabled is not None and type(runtime_enabled) is not bool):
        return None
    if not isinstance(scheduler_state, str) or scheduler_state not in {"enabled", "paused", "unknown"}:
        return None
    if not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._=-]{0,127}", profile):
        return None
    if not isinstance(execution_mode, str) or execution_mode not in {"live", "paper", "dry_run"}:
        return None
    if not isinstance(observed_at, str):
        return None
    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return json.dumps({key: value[key] for key in _DEPLOYMENT_FIELDS}, sort_keys=True)


def resolve_monitoring(
    *,
    configured_state: str,
    configuration_guard: str,
    workflow_status: str,
    execution_status: str,
) -> dict[str, str]:
    """Return a contract-valid status pair without ever changing target state."""

    if configured_state not in {"enabled", "disabled"}:
        raise ValueError("configured_state must be enabled or disabled")
    if configuration_guard not in _CHECKS:
        raise ValueError("configuration_guard is unsupported")
    if configured_state == "disabled":
        return {"runtime_guard": configuration_guard, "execution_heartbeat": "not_applicable"}
    if configuration_guard != "pass":
        # Configuration must be unambiguous before process/report observations
        # can be considered.  The shared lifecycle contract will park it.
        return {"runtime_guard": configuration_guard, "execution_heartbeat": "not_due"}
    return {
        "runtime_guard": _workflow_check(workflow_status),
        "execution_heartbeat": _execution_check(execution_status),
    }


def main() -> int:
    configured_state = os.environ.get("CONFIGURED_STATE") or "enabled"
    configuration_guard = os.environ.get("CONFIGURATION_GUARD") or "attention"
    workflow = _read_assessment(os.environ.get("WORKFLOW_HEARTBEAT_PATH"))
    execution = _read_assessment(os.environ.get("EXECUTION_HEARTBEAT_PATH"))
    values = resolve_monitoring(
        configured_state=configured_state,
        configuration_guard=configuration_guard,
        workflow_status=str(workflow.get("status") or "unavailable"),
        execution_status=str(execution.get("status") or "unavailable"),
    )
    deployment = execution.get("deployment")
    deployment_json = _deployment_json(deployment)
    if deployment_json is not None:
        values["deployment_json"] = deployment_json
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")
    print(json.dumps(values, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
