#!/usr/bin/env python3
"""Resolve Binance's bounded lifecycle metadata without execution authority.

This adapter deliberately owns only the platform-specific edge: the Binance
runtime is a GitHub Actions workflow, not a Cloud Run service.  It validates
the canonical target declaration and the explicit enabled/disabled control,
then emits only the fields consumed by the shared runtime lifecycle contract.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$")
_VALID_STATES = frozenset({"true", "false"})
_VALID_MODES = frozenset({"dry_run", "paper", "live"})


@dataclass(frozen=True)
class LifecycleMetadata:
    configured_state: str
    execution_mode: str
    runtime_guard: str
    target_id: str
    error: str = ""


def _value_as_bool(raw: object) -> bool | None:
    if isinstance(raw, bool):
        return raw
    value = str(raw or "").strip().lower()
    if value not in _VALID_STATES:
        return None
    return value == "true"


def _fallback(error: str) -> LifecycleMetadata:
    # The central contract requires a valid lane even for a malformed local
    # declaration.  `dry_run` is a display-safe fallback and `attention`
    # parks the target; it never changes the actual runner configuration.
    return LifecycleMetadata(
        configured_state="enabled",
        execution_mode="dry_run",
        runtime_guard="attention",
        target_id="binance.unresolved",
        error=error,
    )


def resolve_lifecycle_metadata(
    runtime_target_json: str | None,
    runtime_target_enabled: str | None,
) -> LifecycleMetadata:
    """Validate only the non-sensitive target metadata required for monitoring."""

    try:
        payload = json.loads(runtime_target_json or "")
    except json.JSONDecodeError:
        return _fallback("RUNTIME_TARGET_JSON is not valid JSON")
    if not isinstance(payload, dict):
        return _fallback("RUNTIME_TARGET_JSON must be a JSON object")
    if str(payload.get("platform_id") or "").strip().lower() != "binance":
        return _fallback("RUNTIME_TARGET_JSON.platform_id must be binance")

    strategy = str(payload.get("strategy_profile") or "").strip()
    if not _IDENTIFIER.fullmatch(strategy):
        return _fallback("RUNTIME_TARGET_JSON.strategy_profile must be a stable identifier")

    execution_mode = str(payload.get("execution_mode") or "").strip()
    if execution_mode not in _VALID_MODES:
        return _fallback("RUNTIME_TARGET_JSON.execution_mode is unsupported")

    raw_enabled = runtime_target_enabled
    if raw_enabled is None or not str(raw_enabled).strip():
        raw_enabled = payload.get("runtime_target_enabled", "false")
    enabled = _value_as_bool(raw_enabled)
    if enabled is None:
        return _fallback("RUNTIME_TARGET_ENABLED must be true or false")

    dry_run_only = payload.get("dry_run_only")
    if not isinstance(dry_run_only, bool):
        return _fallback("RUNTIME_TARGET_JSON.dry_run_only must be boolean")
    if dry_run_only and execution_mode == "live":
        return _fallback("dry_run_only target cannot declare live execution_mode")

    return LifecycleMetadata(
        configured_state="enabled" if enabled else "disabled",
        execution_mode=execution_mode,
        runtime_guard="pass",
        target_id=f"binance.{strategy}",
    )


def main() -> int:
    result = resolve_lifecycle_metadata(
        os.environ.get("RUNTIME_TARGET_JSON"),
        os.environ.get("RUNTIME_TARGET_ENABLED"),
    )
    fields: dict[str, Any] = {
        "configured_state": result.configured_state,
        "execution_mode": result.execution_mode,
        "runtime_guard": result.runtime_guard,
        "target_id": result.target_id,
        "error": result.error,
    }
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            for key, value in fields.items():
                handle.write(f"{key}={value}\n")
    print(json.dumps(fields, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
