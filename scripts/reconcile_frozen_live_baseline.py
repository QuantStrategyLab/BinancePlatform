#!/usr/bin/env python3
"""Produce a redacted, no-order Binance recovery candidate.

The script is intentionally separate from ``main.py`` so a reconciliation run
cannot fall through into a strategy cycle.  It is suitable only for a private,
authenticated operator workflow; stdout contains no broker rows or credentials.
"""

# ruff: noqa: E402

from __future__ import annotations

import json
import os
import sys
from argparse import ArgumentParser
from pathlib import Path

# GitHub Actions invokes this file directly, which otherwise makes only the
# ``scripts/`` directory importable.  Add the reviewed repository root before
# importing application code; this does not change broker behaviour or grant
# an execution path.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from application.broker_reconciliation import (
    BinanceReconciliationReadError,
    build_reconciliation_candidate,
    collect_read_only_reconciliation_observations,
)
from infra.state_store import load_runtime_trade_state
from quant_platform_kit.binance import connect_client
from quant_platform_kit.common.runtime_target import resolve_runtime_target_from_env
from strategy_registry import BINANCE_PLATFORM
from trade_state_support import build_default_state, normalize_trade_state


_FAILURE_CLASS_BY_STAGE = {
    "runtime_target_load": "configuration",
    "local_execution_ledger_load": "ledger",
    "client_connect": "connectivity",
    "reconciliation_collect": "broker_read",
    "candidate_build": "evidence_build",
    "receipt_write": "persistence",
}


def _safe_reason_code(exc: Exception) -> str:
    """Return a stable operational code without exposing broker response text.

    The workflow log is retained as an operational artifact, so it must not
    include an exception message that might contain an exchange response or
    account metadata.  Codes let the runtime guard distinguish a transient
    broker-read problem from an intentionally closed recovery gate.
    """

    if not isinstance(exc, BinanceReconciliationReadError):
        return "unexpected_reconciliation_failure"

    message = str(exc).lower()
    known_codes = (
        ("only available for a frozen", "runtime_target_not_reconcile_only"),
        ("private api credentials", "broker_credentials_missing"),
        ("local execution ledger", "local_execution_ledger_unavailable"),
        ("explicit managed symbols", "managed_symbols_missing"),
        ("read-only get_", "broker_read_capability_missing"),
        ("account identity is unavailable", "account_identity_unavailable"),
        ("invalid account response", "account_response_invalid"),
        ("invalid balances", "balances_response_invalid"),
        ("could not read open orders", "open_orders_read_failed"),
        ("invalid open orders", "open_orders_response_invalid"),
        ("could not read recent trades", "recent_trades_read_failed"),
        ("invalid recent trades", "recent_trades_response_invalid"),
        ("expected digests are invalid", "expected_digests_invalid"),
        ("expected digests are incomplete", "expected_digests_incomplete"),
        ("complete frozen runtime target", "frozen_target_incomplete"),
    )
    for needle, code in known_codes:
        if needle in message:
            return code
    return "reconciliation_read_unavailable"


def _blocked_receipt(*, stage: str, reason_code: str) -> dict[str, str]:
    """Return the only failure fields allowed in logs and persisted receipts."""

    return {
        "status": "blocked",
        "stage": stage,
        "failure_class": _FAILURE_CLASS_BY_STAGE[stage],
        "reason_code": reason_code,
    }


def _write_receipt(output_path: Path | None, payload: dict[str, object]) -> bool:
    if output_path is None:
        return True
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def _symbols_from_env() -> tuple[str, ...]:
    raw = str(os.environ.get("BINANCE_RECONCILIATION_SYMBOLS") or "").strip()
    symbols = tuple(dict.fromkeys(item.strip().upper() for item in raw.split(",") if item.strip()))
    if not symbols:
        raise BinanceReconciliationReadError(
            "BINANCE_RECONCILIATION_SYMBOLS is required for a bounded read-only history query."
        )
    return symbols


def _parse_args(argv: list[str] | None = None):
    parser = ArgumentParser(description="Create a redacted no-order Binance reconciliation candidate")
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Require stdout-only output and reject any candidate file write.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional private/short-lived path for the redacted candidate JSON.",
    )
    args = parser.parse_args(argv)
    if args.no_persist and args.output is not None:
        parser.error("--no-persist cannot be combined with --output")
    return args


def main() -> int:
    args = _parse_args()
    stage = "runtime_target_load"
    try:
        target = resolve_runtime_target_from_env(
            env=os.environ,
            expected_platform_id=BINANCE_PLATFORM,
        )
        if str(getattr(getattr(target, "live_continuity", None), "state", "")).upper() != "RECONCILE_ONLY":
            raise BinanceReconciliationReadError(
                "Binance reconciliation is only available for a frozen baseline."
            )
        api_key = str(os.environ.get("BINANCE_API_KEY") or "").strip()
        api_secret = str(os.environ.get("BINANCE_API_SECRET") or "").strip()
        if not api_key or not api_secret:
            raise BinanceReconciliationReadError("Binance reconciliation requires the existing private API credentials.")
        stage = "local_execution_ledger_load"
        state = load_runtime_trade_state(
            normalize_fn=normalize_trade_state,
            default_state_factory=build_default_state,
            normalize=False,
        )
        if not isinstance(state, dict):
            raise BinanceReconciliationReadError("Binance reconciliation could not load the local execution ledger.")
        stage = "client_connect"
        client = connect_client(api_key, api_secret, timeout=30)
        stage = "reconciliation_collect"
        observations = collect_read_only_reconciliation_observations(
            client,
            strategy_symbols=_symbols_from_env(),
            local_execution_ledger=state,
        )
        stage = "candidate_build"
        candidate = build_reconciliation_candidate(
            observations=observations,
            runtime_target=target,
        )
        payload = candidate.to_safe_dict()
        stage = "receipt_write"
        if not _write_receipt(args.output, payload):
            print(json.dumps(_blocked_receipt(stage=stage, reason_code="receipt_write_failed"), sort_keys=True))
            return 2
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except BinanceReconciliationReadError as exc:
        payload = _blocked_receipt(stage=stage, reason_code=_safe_reason_code(exc))
    except Exception as exc:
        payload = _blocked_receipt(stage=stage, reason_code=_safe_reason_code(exc))

    if not _write_receipt(args.output, payload):
        payload = _blocked_receipt(stage="receipt_write", reason_code="receipt_write_failed")
    print(json.dumps(payload, sort_keys=True))
    return 2


if __name__ == "__main__":
    sys.exit(main())
