#!/usr/bin/env python3
"""Produce a redacted, no-order Binance recovery candidate.

The script is intentionally separate from ``main.py`` so a reconciliation run
cannot fall through into a strategy cycle.  It is suitable only for a private,
authenticated operator workflow; stdout contains no broker rows or credentials.
"""

from __future__ import annotations

import json
import os
import sys
from argparse import ArgumentParser
from pathlib import Path

from application.broker_reconciliation import (
    BinanceReconciliationReadError,
    build_reconciliation_candidate,
    collect_read_only_reconciliation_observations,
)
from infra.state_store import load_runtime_trade_state
from quant_platform_kit.binance import connect_client
from runtime_config_support import load_cycle_execution_settings
from trade_state_support import build_default_state, normalize_trade_state


def _symbols_from_env() -> tuple[str, ...]:
    raw = str(os.environ.get("BINANCE_RECONCILIATION_SYMBOLS") or "").strip()
    symbols = tuple(dict.fromkeys(item.strip().upper() for item in raw.split(",") if item.strip()))
    if not symbols:
        raise BinanceReconciliationReadError(
            "BINANCE_RECONCILIATION_SYMBOLS is required for a bounded read-only history query."
        )
    return symbols


def _parse_args():
    parser = ArgumentParser(description="Create a redacted no-order Binance reconciliation candidate")
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional private/short-lived path for the redacted candidate JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        settings = load_cycle_execution_settings()
        target = settings.runtime_target
        if str(getattr(getattr(target, "live_continuity", None), "state", "")).upper() != "RECONCILE_ONLY":
            raise BinanceReconciliationReadError(
                "Binance reconciliation is only available for a frozen baseline."
            )
        api_key = str(os.environ.get("BINANCE_API_KEY") or "").strip()
        api_secret = str(os.environ.get("BINANCE_API_SECRET") or "").strip()
        if not api_key or not api_secret:
            raise BinanceReconciliationReadError("Binance reconciliation requires the existing private API credentials.")
        state = load_runtime_trade_state(
            normalize_fn=normalize_trade_state,
            default_state_factory=build_default_state,
            normalize=False,
        )
        if not isinstance(state, dict):
            raise BinanceReconciliationReadError("Binance reconciliation could not load the local execution ledger.")
        client = connect_client(api_key, api_secret, timeout=30)
        observations = collect_read_only_reconciliation_observations(
            client,
            strategy_symbols=_symbols_from_env(),
            local_execution_ledger=state,
        )
        candidate = build_reconciliation_candidate(
            observations=observations,
            runtime_target=target,
        )
        payload = json.dumps(candidate.to_safe_dict(), ensure_ascii=False, sort_keys=True)
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload + "\n", encoding="utf-8")
        print(payload)
        return 0
    except BinanceReconciliationReadError as exc:
        print(json.dumps({"status": "blocked", "reason": type(exc).__name__}, sort_keys=True))
        return 2
    except Exception as exc:
        print(json.dumps({"status": "blocked", "reason": type(exc).__name__}, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
