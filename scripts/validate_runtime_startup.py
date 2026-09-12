"""Load actual runtime configuration and strategy, without execution ports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


_SAFE_STARTUP_REASONS = {
    "runtime_recovery_not_active": "runtime_recovery_not_active",
    "recovery_control_state_invalid": "recovery_control_state_invalid",
    "recovery_active_binding_invalid": "recovery_active_binding_invalid",
    "STRATEGY_PROFILE does not match RUNTIME_TARGET_JSON.strategy_profile": "startup_strategy_profile_conflict",
    "BINANCE_DRY_RUN does not match RUNTIME_TARGET_JSON.dry_run_only": "startup_dry_run_conflict",
}


def validate_startup():
    if os.getenv("RUNTIME_TARGET_ENABLED") != "false":
        raise ValueError("startup_validation_requires_disabled_runtime")
    if os.getenv("BINANCE_API_KEY") or os.getenv("BINANCE_API_SECRET"):
        raise ValueError("startup_validation_broker_credentials_forbidden")
    import main

    runtime = main.build_live_runtime()
    if runtime.standard_execution_permitted:
        raise ValueError("startup_validation_execution_permitted")
    memory_writes = []
    runtime.state_writer = lambda state: memory_writes.append(dict(state)) or True
    report = main.build_execution_report(runtime)
    cycle_state = main._load_cycle_state(runtime, report, False)
    if cycle_state is None:
        raise ValueError("startup_validation_state_load_failed")
    _state, resolution, runtime_trend_universe, _allow_new = cycle_state
    candidate_count = sum(
        not meta.get("valuation_only")
        for meta in runtime_trend_universe.values()
    )
    valuation_only_count = len(runtime_trend_universe) - candidate_count
    return {"status": "passed", "strategy_profile": runtime.strategy_profile,
            "execution_permitted": False, "validation_only": True,
            "state_load_checked": True,
            "source_pool_symbol_count": len(resolution.get("symbols") or ()),
            "managed_asset_count": len(runtime_trend_universe) + 3,
            "strategy_candidate_count": candidate_count,
            "valuation_only_count": valuation_only_count}


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        print(json.dumps(validate_startup(), sort_keys=True))
    except Exception as exc:
        kind = type(exc).__name__ if type(exc) in {ValueError, KeyError, TypeError, OSError, RuntimeError} else "RuntimeError"
        # Exact application reasons only; never expose provider messages or values.
        reason = "runtime_startup_validation_failed"
        if type(exc) is ValueError:
            reason = _SAFE_STARTUP_REASONS.get(str(exc), reason)
        print(json.dumps({"status": "failed", "stage": "runtime_startup_validation", "error_type": kind, "reason_code": reason}))
        raise SystemExit(1) from None
