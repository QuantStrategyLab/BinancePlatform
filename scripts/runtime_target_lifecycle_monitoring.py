#!/usr/bin/env python3
"""Map Binance's read-only monitor assessments to the shared lifecycle checks."""

from __future__ import annotations

import json
import os


_CHECKS = frozenset({"pass", "attention", "not_due", "not_applicable", "unavailable"})


def _read_status(path: str | None) -> str:
    if not path:
        return "unavailable"
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return "unavailable"
    return str(payload.get("status") or "unavailable") if isinstance(payload, dict) else "unavailable"


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
    workflow_status = _read_status(os.environ.get("WORKFLOW_HEARTBEAT_PATH"))
    execution_status = _read_status(os.environ.get("EXECUTION_HEARTBEAT_PATH"))
    values = resolve_monitoring(
        configured_state=configured_state,
        configuration_guard=configuration_guard,
        workflow_status=workflow_status,
        execution_status=execution_status,
    )
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")
    print(json.dumps(values, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
