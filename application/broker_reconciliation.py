"""Read-only Binance evidence for recovering a frozen live baseline.

The adapter has no order, transfer, redemption, subscription, cancellation, or
state-write call.  A missing broker field is a reconciliation failure, never a
reason to guess or to allow standard execution.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from quant_platform_kit.common.broker_reconciliation import (
    BrokerReconciliationEvidence,
    BrokerReconciliationFinding,
    build_broker_reconciliation_evidence,
    calculate_broker_observation_sha256,
    evaluate_broker_reconciliation_recovery,
)


BINANCE_RECONCILIATION_EXPECTED_DIGESTS_ENV = "BINANCE_RECONCILIATION_EXPECTED_DIGESTS_JSON"
_EXPECTED_DIGEST_KEYS = (
    "positions_sha256",
    "cash_sha256",
    "open_orders_sha256",
    "recent_executions_sha256",
    "local_execution_ledger_sha256",
)
_MAX_MY_TRADES_WINDOW_MS = 24 * 60 * 60 * 1000
_MAX_MY_TRADES_PAGE_SIZE = 1000


class BinanceReconciliationReadError(RuntimeError):
    """One necessary read-only exchange or local-ledger surface is unavailable."""


def _text(value: object) -> str:
    return str(value or "").strip()


def _canonical_records(records: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    return tuple(sorted((dict(item) for item in records), key=lambda item: json.dumps(item, sort_keys=True)))


def _finite_number(value: object, *, field_name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise BinanceReconciliationReadError(f"Binance reconciliation is missing {field_name}.") from exc
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        raise BinanceReconciliationReadError(f"Binance reconciliation has non-finite {field_name}.")
    return parsed


def _normalize_balance(raw: Mapping[str, object]) -> dict[str, object]:
    asset = _text(raw.get("asset")).upper()
    if not asset:
        raise BinanceReconciliationReadError("Binance reconciliation received a balance without an asset.")
    return {
        "asset": asset,
        "free": _finite_number(raw.get("free"), field_name=f"{asset} free balance"),
        "locked": _finite_number(raw.get("locked"), field_name=f"{asset} locked balance"),
    }


def _normalize_order(raw: Mapping[str, object]) -> dict[str, object]:
    order_id = _text(raw.get("orderId"))
    symbol = _text(raw.get("symbol")).upper()
    status = _text(raw.get("status")).upper()
    if not order_id or not symbol or not status:
        raise BinanceReconciliationReadError("Binance reconciliation received an incomplete order.")
    return {
        "order_id": order_id,
        "symbol": symbol,
        "status": status,
        "side": _text(raw.get("side")).upper(),
        "type": _text(raw.get("type")).upper(),
        "orig_qty": _finite_number(raw.get("origQty", 0.0), field_name="order quantity"),
        "executed_qty": _finite_number(raw.get("executedQty", 0.0), field_name="executed quantity"),
        "update_time": _text(raw.get("updateTime")),
    }


def _normalize_trade(raw: Mapping[str, object]) -> dict[str, object]:
    trade_id = _text(raw.get("id"))
    order_id = _text(raw.get("orderId"))
    symbol = _text(raw.get("symbol")).upper()
    if not trade_id or not order_id or not symbol:
        raise BinanceReconciliationReadError("Binance reconciliation received an incomplete trade.")
    return {
        "trade_id": trade_id,
        "order_id": order_id,
        "symbol": symbol,
        "qty": _finite_number(raw.get("qty"), field_name="trade quantity"),
        "price": _finite_number(raw.get("price"), field_name="trade price"),
        "commission": _finite_number(raw.get("commission"), field_name="trade commission"),
        "commission_asset": _text(raw.get("commissionAsset")).upper(),
        "time": _text(raw.get("time")),
        "is_buyer": bool(raw.get("isBuyer")),
    }


def _collect_bounded_recent_trades(
    client: Any,
    *,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, object]]:
    """Read a finite trade history using only Binance-supported day windows.

    Binance Spot's ``myTrades`` endpoint rejects a ``startTime``/``endTime``
    span over 24 hours. A recovery candidate needs seven days of evidence, so
    partition the period into non-overlapping inclusive millisecond windows.
    A full 1,000-row response is intentionally not paginated by guessing a
    cursor; that would risk an incomplete ledger, so the candidate fails
    closed instead.
    """

    normalized_by_trade_id: dict[tuple[str, str], dict[str, object]] = {}
    window_start_ms = start_ms
    while window_start_ms <= end_ms:
        window_end_ms = min(window_start_ms + _MAX_MY_TRADES_WINDOW_MS, end_ms)
        try:
            trades = client.get_my_trades(
                symbol=symbol,
                startTime=window_start_ms,
                endTime=window_end_ms,
                limit=_MAX_MY_TRADES_PAGE_SIZE,
            )
        except Exception as exc:
            raise BinanceReconciliationReadError("Binance reconciliation could not read recent trades.") from exc
        if not isinstance(trades, list) or any(not isinstance(item, Mapping) for item in trades):
            raise BinanceReconciliationReadError("Binance reconciliation received invalid recent trades.")
        if len(trades) >= _MAX_MY_TRADES_PAGE_SIZE:
            raise BinanceReconciliationReadError("Binance reconciliation recent trades page is incomplete.")
        for raw_trade in trades:
            normalized = _normalize_trade(raw_trade)
            key = (normalized["symbol"], normalized["trade_id"])
            existing = normalized_by_trade_id.get(key)
            if existing is not None and existing != normalized:
                raise BinanceReconciliationReadError("Binance reconciliation received conflicting recent trade records.")
            normalized_by_trade_id[key] = normalized
        # The API's time ranges are inclusive. Move by exactly one
        # millisecond to avoid querying the boundary twice.
        window_start_ms = window_end_ms + 1
    return list(normalized_by_trade_id.values())


@dataclass(frozen=True)
class BinanceReconciliationObservations:
    """Sensitive in-memory observations that must not enter public artifacts."""

    account_scope: Mapping[str, object]
    account_identity_match: bool
    positions: tuple[Mapping[str, object], ...]
    cash: Mapping[str, object]
    open_orders: tuple[Mapping[str, object], ...]
    recent_executions: tuple[Mapping[str, object], ...]
    local_execution_ledger: Mapping[str, object]


@dataclass(frozen=True)
class BinanceReconciliationCandidate:
    evidence: BrokerReconciliationEvidence
    recovery_blockers: tuple[BrokerReconciliationFinding, ...]
    expected_digests_configured: bool

    @property
    def permits_active_lkg(self) -> bool:
        return not self.recovery_blockers

    def to_safe_dict(self) -> dict[str, object]:
        return {
            "schema_version": "binance_reconciliation_candidate.v1",
            "permits_active_lkg": self.permits_active_lkg,
            "expected_digests_configured": self.expected_digests_configured,
            "recovery_blockers": [finding.value for finding in self.recovery_blockers],
            "evidence": self.evidence.to_dict(),
        }


def collect_read_only_reconciliation_observations(
    client: Any,
    *,
    strategy_symbols: Sequence[str],
    local_execution_ledger: Mapping[str, object],
    now: datetime | None = None,
    lookback: timedelta = timedelta(days=7),
) -> BinanceReconciliationObservations:
    """Read exchange balances, orders and fills without mutating exchange state."""

    symbols = tuple(dict.fromkeys(_text(symbol).upper() for symbol in strategy_symbols if _text(symbol)))
    if not symbols:
        raise BinanceReconciliationReadError("Binance reconciliation requires explicit managed symbols.")
    for method_name in ("get_account", "get_open_orders", "get_my_trades"):
        if not callable(getattr(client, method_name, None)):
            raise BinanceReconciliationReadError(f"Binance reconciliation requires read-only {method_name} support.")
    account = client.get_account()
    if not isinstance(account, Mapping):
        raise BinanceReconciliationReadError("Binance reconciliation received an invalid account response.")
    # Binance exposes an account uid on the signed account response used by
    # this platform.  Without it we cannot bind a candidate to an account, so
    # the recovery path must stay closed rather than treating an API key as an
    # identity proof.
    account_uid = _text(account.get("uid"))
    if not account_uid:
        raise BinanceReconciliationReadError("Binance reconciliation account identity is unavailable.")
    balances = account.get("balances")
    if not isinstance(balances, list) or any(not isinstance(item, Mapping) for item in balances):
        raise BinanceReconciliationReadError("Binance reconciliation received invalid balances.")
    normalized_balances = _canonical_records([_normalize_balance(item) for item in balances])
    reference_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start_ms = int((reference_now - lookback).timestamp() * 1000)
    end_ms = int(reference_now.timestamp() * 1000)
    try:
        open_orders_payload = client.get_open_orders()
    except Exception as exc:
        raise BinanceReconciliationReadError("Binance reconciliation could not read open orders.") from exc
    if not isinstance(open_orders_payload, list) or any(not isinstance(item, Mapping) for item in open_orders_payload):
        raise BinanceReconciliationReadError("Binance reconciliation received invalid open orders.")
    recent_trades: list[dict[str, object]] = []
    for symbol in symbols:
        recent_trades.extend(
            _collect_bounded_recent_trades(
                client,
                symbol=symbol,
                start_ms=start_ms,
                end_ms=end_ms,
            )
        )
    return BinanceReconciliationObservations(
        account_scope={"account_uid": account_uid},
        account_identity_match=True,
        positions=normalized_balances,
        cash={"balances": list(normalized_balances)},
        open_orders=_canonical_records([_normalize_order(item) for item in open_orders_payload]),
        recent_executions=_canonical_records(recent_trades),
        local_execution_ledger=dict(local_execution_ledger),
    )


def _expected_digests(*, env_reader: Callable[[str, str | None], str | None] = os.getenv) -> Mapping[str, str] | None:
    raw = _text(env_reader(BINANCE_RECONCILIATION_EXPECTED_DIGESTS_ENV, None))
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BinanceReconciliationReadError("Binance reconciliation expected digests are invalid JSON.") from exc
    if not isinstance(value, Mapping) or set(value) != set(_EXPECTED_DIGEST_KEYS):
        raise BinanceReconciliationReadError("Binance reconciliation expected digests are incomplete.")
    return {key: _text(value[key]).lower().removeprefix("sha256:") for key in _EXPECTED_DIGEST_KEYS}


def build_reconciliation_candidate(
    *,
    observations: BinanceReconciliationObservations,
    runtime_target: Any,
    env_reader: Callable[[str, str | None], str | None] = os.getenv,
    observed_at: datetime | None = None,
) -> BinanceReconciliationCandidate:
    """Build evidence that cannot independently recover or enable execution."""

    continuity = getattr(runtime_target, "live_continuity", None)
    baseline_id = _text(getattr(continuity, "baseline_id", ""))
    baseline_target_sha256 = _text(getattr(continuity, "baseline_target_sha256", "")).lower()
    platform_id = _text(getattr(runtime_target, "platform_id", ""))
    strategy_profile = _text(getattr(runtime_target, "strategy_profile", ""))
    if continuity is None or not baseline_id or len(baseline_target_sha256) != 64 or not platform_id or not strategy_profile:
        raise BinanceReconciliationReadError("Binance reconciliation requires a complete frozen runtime target.")
    expected = _expected_digests(env_reader=env_reader)
    digests = {
        "positions_sha256": calculate_broker_observation_sha256(observations.positions),
        "cash_sha256": calculate_broker_observation_sha256(observations.cash),
        "open_orders_sha256": calculate_broker_observation_sha256(observations.open_orders),
        "recent_executions_sha256": calculate_broker_observation_sha256(observations.recent_executions),
        "local_execution_ledger_sha256": calculate_broker_observation_sha256(observations.local_execution_ledger),
    }
    timestamp = observed_at or datetime.now(timezone.utc)
    account_scope_sha256 = calculate_broker_observation_sha256(observations.account_scope)
    evidence = build_broker_reconciliation_evidence(
        platform_id=platform_id,
        strategy_profile=strategy_profile,
        account_scope_sha256=account_scope_sha256,
        baseline_id=baseline_id,
        baseline_target_sha256=baseline_target_sha256,
        runtime_target_sha256=baseline_target_sha256,
        observed_at=timestamp,
        broker_connected=True,
        account_identity_match=observations.account_identity_match,
        positions_match=expected is not None and expected["positions_sha256"] == digests["positions_sha256"],
        cash_match=expected is not None and expected["cash_sha256"] == digests["cash_sha256"],
        open_orders_match=expected is not None and expected["open_orders_sha256"] == digests["open_orders_sha256"],
        recent_executions_match=expected is not None and expected["recent_executions_sha256"] == digests["recent_executions_sha256"],
        local_execution_ledger_match=expected is not None and expected["local_execution_ledger_sha256"] == digests["local_execution_ledger_sha256"],
        **digests,
    )
    blockers = evaluate_broker_reconciliation_recovery(
        evidence,
        now=timestamp,
        expected_platform_id=platform_id,
        expected_strategy_profile=strategy_profile,
        expected_account_scope_sha256=account_scope_sha256,
        expected_baseline_id=baseline_id,
        expected_runtime_target_sha256=baseline_target_sha256,
        **{f"expected_{key}": (expected or {}).get(key) for key in _EXPECTED_DIGEST_KEYS},
    )
    return BinanceReconciliationCandidate(
        evidence=evidence,
        recovery_blockers=blockers,
        expected_digests_configured=expected is not None,
    )
