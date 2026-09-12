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
from decimal import Decimal, DecimalException, localcontext
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
    "account_scope_sha256",
    "positions_sha256",
    "cash_sha256",
    "open_orders_sha256",
    "recent_executions_sha256",
    "local_execution_ledger_sha256",
)
_MAX_MY_TRADES_WINDOW_MS = 24 * 60 * 60 * 1000
_MAX_MY_TRADES_PAGE_SIZE = 1000
_EXTERNAL_CASH_FLOW_LOOKBACK = timedelta(days=7)
_EXTERNAL_CASH_FLOW_PAGE_SIZE = 1000
_EXTERNAL_CASH_FLOW_MAX_RECORDS = 256
_EXTERNAL_CASH_FLOW_CURSOR_VERSION = 1


class BinanceReconciliationReadError(RuntimeError):
    """One necessary read-only exchange or local-ledger surface is unavailable."""


def _text(value: object) -> str:
    return str(value or "").strip()


def _external_cash_flow_cursor(
    cursor: Mapping[str, object] | None, *, now: datetime
) -> tuple[bool, dict[str, dict[str, object]]]:
    if cursor is None:
        return True, {}
    if not isinstance(cursor, Mapping) or cursor.get("version") != _EXTERNAL_CASH_FLOW_CURSOR_VERSION:
        raise ValueError("external_cash_flow_cursor_invalid")
    observed_at = cursor.get("observed_at")
    records = cursor.get("records")
    try:
        observed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("external_cash_flow_cursor_invalid") from exc
    if (
        observed.tzinfo is None
        or observed.astimezone(timezone.utc) > now
        or not isinstance(records, Mapping)
        or len(records) > _EXTERNAL_CASH_FLOW_MAX_RECORDS
    ):
        raise ValueError("external_cash_flow_cursor_invalid")
    normalized = {}
    for identity, record in records.items():
        if (
            not isinstance(identity, str)
            or len(identity) != 64
            or any(char not in "0123456789abcdef" for char in identity)
            or not isinstance(record, Mapping)
            or record.get("kind") not in {"deposit", "withdrawal"}
            or record.get("status") not in {"pending", "final", "observed"}
            or not isinstance(record.get("payload_sha256"), str)
            or len(record["payload_sha256"]) != 64
        ):
            raise ValueError("external_cash_flow_cursor_invalid")
        normalized[identity] = dict(record)
    return False, normalized


def _external_cash_flow_rows(client: Any, *, path: str, start_ms: int, end_ms: int) -> list[Mapping[str, object]]:
    try:
        response = client._request_margin_api(
            "get",
            path,
            signed=True,
            data={"startTime": start_ms, "endTime": end_ms, "offset": 0, "limit": _EXTERNAL_CASH_FLOW_PAGE_SIZE},
        )
    except Exception:
        raise ValueError("external_cash_flow_history_read_failed") from None
    if (
        not isinstance(response, list)
        or len(response) >= _EXTERNAL_CASH_FLOW_PAGE_SIZE
        or any(not isinstance(row, Mapping) for row in response)
    ):
        raise ValueError("external_cash_flow_history_incomplete")
    return response


def _external_identity(kind: str, provider_id: str) -> str:
    return calculate_broker_observation_sha256({"kind": kind, "provider_id": provider_id})


def _external_amount(value: object) -> Decimal:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise ValueError("external_cash_flow_record_invalid")
    try:
        amount = Decimal(value)
    except DecimalException as exc:
        raise ValueError("external_cash_flow_record_invalid") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("external_cash_flow_record_invalid")
    return amount


def collect_spot_usdt_external_cash_flows(
    client: Any, *, now: datetime, cursor: Mapping[str, object] | None
) -> dict[str, object]:
    """Collect one bounded, deduplicated Spot-USDT deposit slice.

    Withdrawals are fingerprinted so unchanged history can be ignored. Their
    accounting amount remains unsupported because the official history contract
    does not specify whether ``amount`` includes the separate ``transactionFee``
    debit; new or changed rows are only used as evidence when a balance change
    would otherwise need an explanation.
    """
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("external_cash_flow_window_invalid")
    observed_at = now.astimezone(timezone.utc)
    bootstrap, records = _external_cash_flow_cursor(cursor, now=observed_at)
    start_ms = int((observed_at - _EXTERNAL_CASH_FLOW_LOOKBACK).timestamp() * 1000)
    end_ms = int(observed_at.timestamp() * 1000)
    deposits = _external_cash_flow_rows(
        client, path="capital/deposit/hisrec", start_ms=start_ms, end_ms=end_ms
    )
    withdrawals = _external_cash_flow_rows(
        client, path="capital/withdraw/history", start_ms=start_ms, end_ms=end_ms
    )

    new_principal = Decimal(0)
    new_completed_at: list[str] = []
    new_count = 0
    unsupported_deposit_count = 0
    withdrawal_count = 0
    observed_identities = set()
    for row in deposits:
        provider_id = row.get("id")
        status = row.get("status")
        insert_time = row.get("insertTime")
        if (
            not isinstance(provider_id, str)
            or not provider_id
            or len(provider_id) > 160
            or type(status) is not int
            or status not in {0, 1, 2, 6, 7, 8}
            or type(insert_time) is not int
            or not start_ms <= insert_time <= end_ms
        ):
            raise ValueError("external_cash_flow_record_invalid")
        amount = _external_amount(row.get("amount"))
        coin = row.get("coin")
        wallet_type = row.get("walletType")
        transfer_type = row.get("transferType")
        if (
            not isinstance(coin, str)
            or not coin.strip()
            or type(wallet_type) is not int
            or type(transfer_type) is not int
        ):
            raise ValueError("external_cash_flow_record_invalid")
        identity = _external_identity("deposit", provider_id)
        if identity in observed_identities:
            raise ValueError("external_cash_flow_history_incomplete")
        observed_identities.add(identity)
        core = {
            "kind": "deposit",
            "id": provider_id,
            "amount": format(amount, "f"),
            "coin": coin.strip().upper(),
            "wallet_type": wallet_type,
            "transfer_type": transfer_type,
            "insert_time": insert_time,
        }
        core_sha256 = calculate_broker_observation_sha256(core)
        previous = records.get(identity)
        if previous is not None and previous.get("payload_sha256") != core_sha256:
            raise ValueError("external_cash_flow_record_changed")
        if status == 1:
            complete_time = row.get("completeTime")
            tx_id = row.get("txId")
            if (
                type(complete_time) is not int
                or complete_time < insert_time
                or complete_time > end_ms
                or not isinstance(tx_id, str)
                or not tx_id
                or len(tx_id) > 256
            ):
                raise ValueError("external_cash_flow_record_invalid")
            final_sha256 = calculate_broker_observation_sha256(
                {**core, "status": status, "complete_time": complete_time, "tx_id": tx_id}
            )
            if previous is not None and previous.get("status") == "final":
                if previous.get("final_sha256") != final_sha256:
                    raise ValueError("external_cash_flow_record_changed")
                continue
            if not bootstrap:
                if core["coin"] != "USDT" or wallet_type != 0 or transfer_type != 0:
                    unsupported_deposit_count += 1
                else:
                    new_principal += amount
                    new_count += 1
                    new_completed_at.append(
                        datetime.fromtimestamp(complete_time / 1000, tz=timezone.utc).isoformat()
                    )
            records[identity] = {
                "kind": "deposit",
                "payload_sha256": core_sha256,
                "final_sha256": final_sha256,
                "status": "final",
                "source_time_ms": insert_time,
            }
        else:
            if previous is not None and previous.get("status") == "final":
                raise ValueError("external_cash_flow_record_changed")
            records[identity] = {
                "kind": "deposit",
                "payload_sha256": core_sha256,
                "status": "pending",
                "source_time_ms": insert_time,
            }

    for row in withdrawals:
        provider_id = row.get("id")
        status = row.get("status")
        if (
            not isinstance(provider_id, str)
            or not provider_id
            or len(provider_id) > 160
            or type(status) is not int
        ):
            raise ValueError("external_cash_flow_record_invalid")
        identity = _external_identity("withdrawal", provider_id)
        if identity in observed_identities:
            raise ValueError("external_cash_flow_history_incomplete")
        observed_identities.add(identity)
        payload_sha256 = calculate_broker_observation_sha256(dict(row))
        previous = records.get(identity)
        if previous is None or previous.get("payload_sha256") != payload_sha256:
            if not bootstrap:
                withdrawal_count += 1
            records[identity] = {
                "kind": "withdrawal",
                "payload_sha256": payload_sha256,
                "status": "observed",
            }

    # Final rows that have aged out of the seven-day request can be discarded;
    # pending deposits stay until they are observed final or require review.
    for identity, record in tuple(records.items()):
        source_time = record.get("source_time_ms")
        if record.get("kind") == "withdrawal" and identity not in observed_identities:
            del records[identity]
        elif record.get("status") == "final" and type(source_time) is int and source_time < start_ms:
            del records[identity]
    if len(records) > _EXTERNAL_CASH_FLOW_MAX_RECORDS:
        raise ValueError("external_cash_flow_cursor_capacity_exceeded")
    return {
        "bootstrap": bootstrap,
        "new_deposit_principal_usdt": format(new_principal, "f") if new_principal else "0",
        "new_confirmed_deposit_count": new_count,
        "new_deposit_completed_at": new_completed_at,
        "new_unsupported_deposit_count": unsupported_deposit_count,
        "new_or_changed_withdrawal_count": withdrawal_count,
        "cursor": {
            "version": _EXTERNAL_CASH_FLOW_CURSOR_VERSION,
            "observed_at": observed_at.isoformat(),
            "records": records,
        },
    }


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


def _strict_bool(value: object, *, field_name: str) -> bool:
    if type(value) is not bool:
        raise BinanceReconciliationReadError(f"Binance reconciliation received an invalid {field_name}.")
    return value


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
        "is_buyer": _strict_bool(raw.get("isBuyer"), field_name="trade buyer flag"),
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
    account_snapshot: Mapping[str, object] | None = None,
) -> BinanceReconciliationObservations:
    """Read exchange balances, orders and fills without mutating exchange state."""

    symbols = tuple(dict.fromkeys(_text(symbol).upper() for symbol in strategy_symbols if _text(symbol)))
    if not symbols:
        raise BinanceReconciliationReadError("Binance reconciliation requires explicit managed symbols.")
    for method_name in ("get_account", "get_open_orders", "get_my_trades"):
        if not callable(getattr(client, method_name, None)):
            raise BinanceReconciliationReadError(f"Binance reconciliation requires read-only {method_name} support.")
    account = client.get_account() if account_snapshot is None else account_snapshot
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


def diagnose_balance_snapshot(
    account: Mapping[str, object], *, expected_digests: Mapping[str, str] | None,
) -> dict[str, object]:
    """Explain an exact legacy match without changing a baseline or authority.

    Old receipts contain hashes only. Removing a zero row and matching BOTH
    original hashes proves the remaining snapshot is identical. Failure to
    find such a match proves nothing about the cause of a real balance change.
    The bounded search only handles one added row (or an originally zero-free
    baseline); it must never be treated as general ledger reconciliation.
    """
    if not expected_digests or not isinstance(account, Mapping):
        raise ValueError("balance_diagnostic_input_missing")
    uid = _text(account.get("uid"))
    if not uid or calculate_broker_observation_sha256({"account_uid": uid}) != expected_digests.get("account_scope_sha256"):
        raise ValueError("account_identity_mismatch")
    raw = account.get("balances")
    if not isinstance(raw, list) or len(raw) > 5000 or any(not isinstance(row, Mapping) for row in raw):
        raise ValueError("balance_diagnostic_rows_invalid")
    rows = _canonical_records([_normalize_balance(row) for row in raw])
    if len({row["asset"] for row in rows}) != len(rows):
        raise ValueError("balance_diagnostic_duplicate_asset")
    zero_indexes = [index for index, row in enumerate(rows) if row["free"] == 0 and row["locked"] == 0]

    def matches(values):
        return (
            calculate_broker_observation_sha256(values) == expected_digests.get("positions_sha256")
            and calculate_broker_observation_sha256({"balances": list(values)}) == expected_digests.get("cash_sha256")
        )

    reason = "balance_difference_unexplained"
    if matches(rows):
        reason = "balance_snapshot_matches"
    elif zero_indexes:
        nonzero = tuple(row for row in rows if row["free"] != 0 or row["locked"] != 0)
        if matches(nonzero) or any(matches(rows[:index] + rows[index + 1:]) for index in zero_indexes):
            reason = "balance_difference_is_zero_row_only"
    return {
        "status": "diagnostic",
        "reason_code": reason,
        "balance_difference_explained": reason != "balance_difference_unexplained",
        "balance_row_count": len(rows),
        "zero_balance_row_count": len(zero_indexes),
        "positions_and_cash_share_balance_source": True,
        "baseline_rows_available": False,
        "execution_authority_granted": False,
    }


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
        expected_account_scope_sha256=(expected or {}).get("account_scope_sha256"),
        expected_baseline_id=baseline_id,
        expected_runtime_target_sha256=baseline_target_sha256,
        **{
            f"expected_{key}": (expected or {}).get(key)
            for key in _EXPECTED_DIGEST_KEYS
            if key != "account_scope_sha256"
        },
    )
    return BinanceReconciliationCandidate(
        evidence=evidence,
        recovery_blockers=blockers,
        expected_digests_configured=expected is not None,
    )


def diagnose_bonus_reward_balance(
    account: Mapping[str, object], *, rewards: Sequence[Mapping[str, object]],
    expected_digests: Mapping[str, str], start: datetime, end: datetime,
) -> dict[str, object]:
    """Test one exact explanation in memory, without replacing frozen hashes.

    Flexible BONUS rewards credit Spot; REALTIME rewards accrue inside Earn.
    Subtract only documented BONUS credits using decimal arithmetic, then use
    the unchanged legacy normalizer and both original hashes. This cannot
    establish complete account reconciliation or authorize baseline migration.
    """
    diagnose_balance_snapshot(account, expected_digests=expected_digests)
    result = {
        "reason_code": "spot_bonus_rewards_do_not_explain_balance_difference",
        "historical_balance_hashes_match": False,
        "reward_type_counts": None,
        "complete_balance_reconciliation": False,
        "execution_authority_granted": False,
    }

    def amount(value):
        if not isinstance(value, str) or not value or len(value) > 80:
            raise ValueError
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            raise ValueError
        return number

    counts = dict.fromkeys(("BONUS", "REALTIME", "REWARDS"), 0)
    failure_code = "reward_page_invalid"
    try:
        if not isinstance(rewards, (list, tuple)) or len(rewards) >= 100:
            raise ValueError
        failure_code = "reward_window_invalid"
        if start.tzinfo is None or end.tzinfo is None or not start < end:
            raise ValueError
        start_ms, end_ms = int(start.timestamp()*1000), int(end.timestamp()*1000)
        seen = set()
        with localcontext() as context:
            context.prec = 100
            credits: dict[str, Decimal] = {}
            for row in rewards:
                failure_code = "reward_row_invalid"
                if not isinstance(row, Mapping):
                    raise ValueError
                asset, project, kind, timestamp = (row.get(k) for k in ("asset", "projectId", "type", "time"))
                checks = (
                    ("reward_asset_invalid", isinstance(asset, str) and bool(asset.strip())),
                    ("reward_project_invalid", project is None or isinstance(project, str)),
                    ("reward_type_invalid", isinstance(kind, str) and kind in counts),
                    ("reward_timestamp_type_invalid", type(timestamp) is int),
                    ("reward_timestamp_outside_window", type(timestamp) is int and start_ms <= timestamp <= end_ms),
                )
                for failure_code, valid in checks:
                    if not valid:
                        raise ValueError
                # The API's optional product label is not an input to balance
                # arithmetic. Keep it for duplicate detection when supplied.
                identity = (asset, project or "", kind, timestamp)
                failure_code = "reward_record_duplicate"
                if identity in seen:
                    raise ValueError
                seen.add(identity)
                failure_code = "reward_amount_invalid"
                credit = amount(row.get("rewards"))
                counts[kind] += 1
                if kind == "BONUS":
                    credits[asset.upper()] = credits.get(asset.upper(), Decimal(0)) + credit
            result["reward_type_counts"] = counts
            historical = {"uid": account["uid"], "balances": []}
            remaining = set(credits)
            for row in account["balances"]:
                failure_code = "account_balance_amount_invalid"
                asset = _text(row["asset"]).upper()
                free, locked = amount(row.get("free")), amount(row.get("locked"))
                previous_free = free - credits.get(asset, Decimal(0))
                if previous_free < 0:
                    return result
                historical["balances"].append({"asset": asset, "free": str(previous_free), "locked": str(locked)})
                remaining.discard(asset)
            if remaining or not counts["BONUS"]:
                return result
        comparison = diagnose_balance_snapshot(historical, expected_digests=expected_digests)
    except (ValueError, TypeError, KeyError, DecimalException):
        return {**result, "reason_code": "spot_bonus_reward_rows_invalid", "reward_type_counts": None,
                "validation_failure_code": failure_code}
    if comparison["balance_difference_explained"]:
        result["reason_code"] = "spot_bonus_rewards_explain_balance_difference"
        result["historical_balance_hashes_match"] = True
    return result


def diagnose_balance_flows(
    client: Any, *, start: datetime, end: datetime, now: datetime | None = None,
    account: Mapping[str, object] | None = None, expected_digests: Mapping[str, str] | None = None,
    reward_quantity_changes: Mapping[str, Decimal] | None = None,
) -> dict[str, object]:
    """Read a bounded activity summary, never an enrollment or reconciliation.

    Only documented GET surfaces are used. Each gets one finite page; a full
    list or a total exceeding the page stops collection. No amounts, private account rows, provider text, or observed baseline hashes
    are emitted. Optional delta checks name only the supplied managed assets.
    """
    now = now or datetime.now(timezone.utc)
    if (start.tzinfo is None or end.tzinfo is None or not start < end <= now
            or end - start > timedelta(days=7) or now - start > timedelta(days=30)):
        raise ValueError("balance_history_window_invalid")
    bounds = {"startTime": int(start.timestamp() * 1000), "endTime": int(end.timestamp() * 1000)}
    surfaces = [
        ("deposits", "capital/deposit/hisrec", {"limit": 1000, "offset": 0}, True),
        ("withdrawals", "capital/withdraw/history", {"limit": 1000, "offset": 0}, True),
        ("earn_subscriptions", "simple-earn/flexible/history/subscriptionRecord", {"size": 100, "current": 1}, False),
        ("earn_redemptions", "simple-earn/flexible/history/redemptionRecord", {"size": 100, "current": 1}, False),
        ("earn_rewards", "simple-earn/flexible/history/rewardsRecord", {"size": 100, "current": 1, "type": "ALL"}, False),
    ]
    for transfer_type in (
        "MAIN_FUNDING", "FUNDING_MAIN", "MAIN_MARGIN", "MARGIN_MAIN",
        "MAIN_UMFUTURE", "UMFUTURE_MAIN", "MAIN_CMFUTURE", "CMFUTURE_MAIN",
        "MAIN_OPTION", "OPTION_MAIN", "MAIN_PORTFOLIO_MARGIN", "PORTFOLIO_MARGIN_MAIN",
    ):
        surfaces.append((f"transfer_{transfer_type.lower()}", "asset/transfer",
                         {"size": 100, "current": 1, "type": transfer_type}, False))
    counts = dict.fromkeys(name for name, *_ in surfaces)
    result = {
        "reason_code": "balance_history_activity_summary",
        "history_complete_for_requested_surfaces": False,
        "history_counts": counts,
        "automatic_spot_earn_subscriptions": None,
        "complete_balance_reconciliation": False,
        "baseline_rows_available": False,
        "execution_authority_granted": False,
    }
    reward_rows = None
    for name, path, parameters, is_list in surfaces:
        try:
            response = client._request_margin_api("get", path, signed=True, data={**bounds, **parameters})
        except Exception:
            return {**result, "reason_code": "balance_history_read_failed", "failed_surface": name}
        rows = response if is_list else response.get("rows") if isinstance(response, Mapping) else None
        total = None if is_list or not isinstance(response, Mapping) else response.get("total")
        total_valid = (type(total) is int and total >= 0) or (isinstance(total, str) and total.isdecimal())
        if not is_list and total_valid and int(total) == 0 and "rows" not in response:
            rows = []
        if (not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows)
                or len(rows) >= parameters.get("limit", parameters.get("size"))
                or (not is_list and (not total_valid or int(total) != len(rows)))):
            return {
                **result, "reason_code": "balance_history_incomplete", "failed_surface": name,
                "response_shape": {
                    "rows_is_list": isinstance(rows, list),
                    "total_valid": total_valid,
                    "total_is_zero": total_valid and int(total) == 0,
                    "page_full": isinstance(rows, list) and len(rows) >= parameters.get("limit", parameters.get("size")),
                },
            }
        counts[name] = len(rows)
        if name == "earn_rewards":
            reward_rows = rows
            if reward_quantity_changes is not None:
                try:
                    sums = {asset: {"BONUS": Decimal(0), "REALTIME": Decimal(0)} for asset in reward_quantity_changes}
                    counts_by_asset = {asset: {"BONUS": 0, "REALTIME": 0} for asset in reward_quantity_changes}
                    seen = set()
                    for row in rows:
                        asset, kind = row.get("asset"), row.get("type")
                        if asset not in sums or kind not in {"BONUS", "REALTIME"}:
                            continue
                        timestamp = row.get("time")
                        quantity = Decimal(str(row.get("rewards")))
                        identity = (asset, kind, row.get("projectId"), timestamp)
                        if (type(timestamp) is not int or not bounds["startTime"] <= timestamp <= bounds["endTime"]
                                or not quantity.is_finite() or quantity < 0 or identity in seen):
                            raise ValueError("reward_row_invalid")
                        seen.add(identity)
                        sums[asset][kind] += quantity
                        counts_by_asset[asset][kind] += 1
                    result["reward_quantity_checks"] = {
                        asset: {"delta_matches_bonus": delta == sums[asset]["BONUS"],
                                "delta_matches_realtime": delta == sums[asset]["REALTIME"],
                                "delta_matches_visible_total": delta == sum(sums[asset].values()),
                                "reward_counts": counts_by_asset[asset], "causal_reconciliation": False}
                        for asset, delta in reward_quantity_changes.items()
                    }
                except (ValueError, TypeError, DecimalException):
                    return {**result, "reason_code": "balance_history_reward_validation_failed", "failed_surface": name}
            if account is not None and expected_digests is not None:
                result["spot_bonus_reconciliation"] = diagnose_bonus_reward_balance(
                    account, rewards=reward_rows, expected_digests=expected_digests, start=start, end=end,
                )
                if result["spot_bonus_reconciliation"]["reason_code"] == "spot_bonus_reward_rows_invalid":
                    return {**result, "reason_code": "balance_history_reward_validation_failed", "failed_surface": name}
        if name == "earn_subscriptions" and all(
            all(isinstance(row.get(key), str) for key in ("type", "status", "sourceAccount")) for row in rows
        ):
            result["automatic_spot_earn_subscriptions"] = sum(
                row.get("type") == "AUTO" and row.get("status") == "SUCCESS"
                and row.get("sourceAccount") == "SPOT" for row in rows
            )
    result["history_complete_for_requested_surfaces"] = True
    return result


def diagnose_bnb_wallet_activity(client, *, start: datetime, end: datetime):
    """Two audit-only wallet GETs; not a complete funding proof or recovery gate."""
    if start.tzinfo is None or end.tzinfo is None or not start < end or end-start > timedelta(days=7):
        raise ValueError("balance_history_window_invalid")
    bounds = {"startTime": int(start.timestamp()*1000), "endTime": int(end.timestamp()*1000)}
    result = {"requested_surfaces_complete": False, "counts": {},
              "complete_balance_reconciliation": False}
    for name, path, params, rows_key, time_key, limit in (
        ("bnb_dividends", "asset/assetDividend", {"asset": "BNB", "limit": 500}, "rows", "divTime", 500),
        ("spot_dust_conversions", "asset/dribblet", {"accountType": "SPOT"}, "userAssetDribblets", "operateTime", 100),
    ):
        try:
            response = client._request_margin_api("get", path, signed=True, data={**bounds, **params})
            rows, total = response.get(rows_key), response.get("total")
            if (type(total) is not int or not isinstance(rows, list) or total != len(rows) or total >= limit
                    or any(not isinstance(row, Mapping) or type(row.get(time_key)) is not int
                           or not bounds["startTime"] <= row[time_key] <= bounds["endTime"]
                           or (name == "bnb_dividends" and row.get("asset") != "BNB") for row in rows)):
                raise ValueError("history_incomplete")
            result["counts"][name] = len(rows)
        except Exception:
            return {**result, "reason_code": "bnb_wallet_history_unverified", "failed_surface": name}
    return {**result, "requested_surfaces_complete": True}
