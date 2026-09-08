"""Load actual runtime configuration and strategy, without execution ports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def validate_startup():
    if os.getenv("RUNTIME_TARGET_ENABLED") != "false":
        raise ValueError("startup_validation_requires_disabled_runtime")
    if os.getenv("BINANCE_API_KEY") or os.getenv("BINANCE_API_SECRET"):
        raise ValueError("startup_validation_broker_credentials_forbidden")
    import main

    runtime = main.build_live_runtime()
    if runtime.standard_execution_permitted:
        raise ValueError("startup_validation_execution_permitted")
    return {"status": "passed", "strategy_profile": runtime.strategy_profile,
            "execution_permitted": False, "validation_only": True}


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        print(json.dumps(validate_startup(), sort_keys=True))
    except Exception as exc:
        kind = type(exc).__name__ if type(exc) in {ValueError, KeyError, TypeError, OSError, RuntimeError} else "RuntimeError"
        print(json.dumps({"status": "failed", "stage": "runtime_startup_validation", "error_type": kind}))
        raise SystemExit(1) from None
