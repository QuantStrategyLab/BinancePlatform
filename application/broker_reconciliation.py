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
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
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
_BNB_DIVIDEND_PAGE_SIZE = 500
_MAX_HISTORY_PAGES = 20
_REWARD_PRODUCT_CLASSIFICATION_CODES = (
    "opening_approved",
    "lifecycle_subscription_proven",
    "out_of_scope_asset",
    "lifecycle_evidence_incomplete",
    "product_mapping_ambiguous",
    "unexplained_managed_impact",
)


class BinanceReconciliationReadError(RuntimeError):
    """One necessary read-only exchange or local-ledger surface is unavailable."""


def _empty_reward_product_classification_counts() -> dict[str, int]:
    return {code: 0 for code in _REWARD_PRODUCT_CLASSIFICATION_CODES}


def _empty_lifecycle_evidence(
    approved_products: frozenset[tuple[str, str]] | None = None,
) -> dict[str, object]:
    product_assets: dict[str, str] = {}
    for asset, product_id in approved_products or ():
        prior = product_assets.get(product_id)
        if prior is not None and prior != asset:
            raise ValueError("lifecycle_product_asset_ambiguous")
        product_assets[product_id] = asset
    return {
        "subscriptions": [],
        "redemptions": [],
        "seen_subscription_ids": set(),
        "seen_redemption_ids": set(),
        "product_assets": product_assets,
    }


def _surface_row_identity(name: str, row: Mapping[str, object]) -> object | None:
    """Stable identity for cross-page / cross-chunk pagination dedupe.

    Reward rows keep their existing in-surface / cross-chunk validators; this
    helper covers deposits, withdrawals, earn lifecycle, and transfers.
    """
    if name in {"deposits", "withdrawals"}:
        identity = row.get("id")
        if identity is None or identity == "" or isinstance(identity, (dict, list, set)):
            return None
        return (name, identity)
    if name == "earn_subscriptions":
        identity = row.get("purchaseId")
        if type(identity) is not int or identity < 0:
            return None
        return (name, identity)
    if name == "earn_redemptions":
        identity = row.get("redeemId")
        if type(identity) is not int or identity < 0:
            return None
        return (name, identity)
    if name.startswith("transfer_"):
        identity = row.get("tranId")
        if type(identity) is not int or identity < 0:
            return None
        return (name, identity)
    return None


def _lifecycle_amount(value: object) -> Decimal:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise ValueError("lifecycle_row_invalid")
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("lifecycle_row_invalid") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("lifecycle_row_invalid")
    return amount


def _ingest_lifecycle_rows(
    surface: str,
    rows: Sequence[Mapping[str, object]],
    *,
    bounds: Mapping[str, int],
    managed_assets: frozenset[str] | None,
    evidence: dict[str, object],
) -> None:
    """Fail closed on every subscription/redemption row when lifecycle mode is on."""
    subscribe = surface == "earn_subscriptions"
    facts_key = "subscriptions" if subscribe else "redemptions"
    seen_key = "seen_subscription_ids" if subscribe else "seen_redemption_ids"
    id_key = "purchaseId" if subscribe else "redeemId"
    product_key = "productId" if subscribe else "projectId"
    status_ok = "SUCCESS" if subscribe else "PAID"
    account_key = "sourceAccount" if subscribe else "destAccount"
    facts = evidence[facts_key]
    seen_ids = evidence[seen_key]
    assert isinstance(facts, list) and isinstance(seen_ids, set)
    for row in rows:
        if not isinstance(row, Mapping) or not row:
            raise ValueError("lifecycle_row_invalid")
        asset = row.get("asset")
        product = row.get(product_key)
        stamp = row.get("time")
        identity = row.get(id_key)
        status = row.get("status")
        account = row.get(account_key)
        if (
            not isinstance(asset, str)
            or not asset
            or not isinstance(product, str)
            or not product
            or type(stamp) is not int
            or not bounds["startTime"] <= stamp <= bounds["endTime"]
            or status != status_ok
            or account != "SPOT"
            or type(identity) is not int
            or identity < 0
        ):
            raise ValueError("lifecycle_row_invalid")
        if managed_assets is not None and asset not in managed_assets:
            raise ValueError("lifecycle_row_invalid")
        product_assets = evidence.setdefault("product_assets", {})
        assert isinstance(product_assets, dict)
        bound = product_assets.get(product)
        if bound is not None and bound != asset:
            raise ValueError("lifecycle_row_invalid")
        product_assets[product] = asset
        amount = _lifecycle_amount(row.get("amount"))
        if identity in seen_ids:
            raise ValueError("duplicate_lifecycle_event")
        seen_ids.add(identity)
        facts.append(
            {
                "asset": asset,
                "product_id": product,
                "amount": format(amount, "f"),
                "time": stamp,
                "identity": identity,
            }
        )


def _text(value: object) -> str:
    return str(value or "").strip()


def _reward_sum_prec(*values: Decimal) -> int:
    """Significant digits so addition does not round away smaller finite addends."""
    parts = [value for value in values if isinstance(value, Decimal) and value.is_finite()]
    nonzero = [value for value in parts if value != 0]
    if not nonzero:
        return 50
    max_msd = max(value.adjusted() for value in nonzero)
    min_lsd = min(value.as_tuple().exponent for value in nonzero)
    span = max_msd - min_lsd + 1
    return max(50, min(span + 8, 1000))


def _add_reward_quantity(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _reward_sum_prec(left, right)
        return left + right


def _format_reward_quantity(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = _reward_sum_prec(value)
        return format(value, "f")


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


def collect_bnb_dividend_quantity(
    client: Any, *, start: datetime, end: datetime
) -> dict[str, object]:
    """Read one complete BNB dividend window for Earn quantity conservation.

    This is a narrow platform contract: only positive BNB rows with integer
    ``direction == 1`` are accepted.  The rows stay in memory and the returned
    identities are only for same-window stability and duplicate detection.
    """
    if (
        not isinstance(start, datetime)
        or not isinstance(end, datetime)
        or start.tzinfo is None
        or end.tzinfo is None
        or not start < end
    ):
        raise ValueError("bnb_dividend_window_invalid")
    start_ms = int(start.astimezone(timezone.utc).timestamp() * 1000)
    end_ms = int(end.astimezone(timezone.utc).timestamp() * 1000)
    try:
        response = client._request_margin_api(
            "get",
            "asset/assetDividend",
            signed=True,
            data={"asset": "BNB", "startTime": start_ms, "endTime": end_ms,
                  "limit": _BNB_DIVIDEND_PAGE_SIZE},
        )
    except Exception:
        raise ValueError("bnb_dividend_read_failed") from None
    if not isinstance(response, Mapping):
        raise ValueError("bnb_dividend_response_invalid")
    rows = response.get("rows")
    total = response.get("total")
    total_is_decimal_string = (
        isinstance(total, str) and total.isascii() and total.isdecimal() and len(total) <= 10
    )
    count = int(total) if type(total) is int or total_is_decimal_string else None
    if (
        not isinstance(rows, list)
        or count is None
        or count < 0
        or count != len(rows)
        or count >= _BNB_DIVIDEND_PAGE_SIZE
    ):
        raise ValueError("bnb_dividend_history_incomplete")
    quantity = Decimal(0)
    identities = []
    direction_values = set()
    with localcontext() as context:
        context.prec = 100
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("bnb_dividend_row_invalid")
            identity = (row.get("id"), row.get("tranId"), row.get("divTime"))
            if (
                type(identity[0]) is not int
                or identity[0] < 0
                or type(identity[1]) is not int
                or identity[1] < 0
                or type(identity[2]) is not int
                or not start_ms < identity[2] <= end_ms
                or row.get("asset") != "BNB"
                or type(row.get("direction")) is not int
                or row.get("direction") != 1
                or identity in identities
            ):
                raise ValueError("bnb_dividend_row_invalid")
            amount = row.get("amount")
            if not isinstance(amount, str) or not amount or len(amount) > 80:
                raise ValueError("bnb_dividend_row_invalid")
            try:
                decimal_amount = Decimal(amount)
            except DecimalException:
                raise ValueError("bnb_dividend_row_invalid") from None
            if (
                not decimal_amount.is_finite()
                or decimal_amount <= 0
                or abs(decimal_amount) > Decimal("1e30")
                or decimal_amount.as_tuple().exponent < -30
            ):
                raise ValueError("bnb_dividend_row_invalid")
            quantity += decimal_amount
            identities.append(identity)
            direction_values.add(1)
    return {
        "quantity": quantity,
        "record_count": len(identities),
        "identities": tuple(identities),
        "direction_values": tuple(sorted(direction_values)),
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
    _seen_reward_identities: set[tuple[object, ...]] | None = None,
    _reward_sum_sink: dict[str, dict[str, Decimal]] | None = None,
    _approved_reward_products: frozenset[tuple[str, str]] | None = None,
    _lifecycle_evidence: dict[str, object] | None = None,
    _reward_product_classification_counts: dict[str, int] | None = None,
    _managed_reward_assets: frozenset[str] | None = None,
    _seen_surface_identities: dict[str, set[object]] | None = None,
) -> dict[str, object]:
    """Read a bounded activity summary, never an enrollment or reconciliation.

    Only documented GET surfaces are used. Pages continue until a short page or
    an exact total match; a full page with an unverifiable remainder fails
    closed. No amounts, private account rows, provider text, or observed
    baseline hashes are emitted. Optional delta checks name only the supplied
    managed assets. Opening reward products or complete subscription lifecycle
    evidence on managed assets may admit a reward product.
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
    classification_counts = (
        _reward_product_classification_counts
        if _reward_product_classification_counts is not None
        else (
            _empty_reward_product_classification_counts()
            if _approved_reward_products is not None
            else None
        )
    )
    result = {
        "reason_code": "balance_history_activity_summary",
        "history_complete_for_requested_surfaces": False,
        "history_counts": counts,
        "automatic_spot_earn_subscriptions": None,
        "complete_balance_reconciliation": False,
        "baseline_rows_available": False,
        "execution_authority_granted": False,
    }
    if classification_counts is not None:
        result["reward_product_classification_counts"] = classification_counts
    reward_rows = None
    managed_assets = _managed_reward_assets
    if managed_assets is None and _reward_sum_sink is not None:
        managed_assets = frozenset(_reward_sum_sink)
    if managed_assets is None and reward_quantity_changes is not None:
        managed_assets = frozenset(reward_quantity_changes)

    def _incomplete(name, *, page_full, rows_is_list, total_valid, total):
        return {
            **result,
            "reason_code": "balance_history_incomplete",
            "failed_surface": name,
            "response_shape": {
                "rows_is_list": rows_is_list,
                "total_valid": total_valid,
                "total_is_zero": bool(total_valid and int(total) == 0),
                "page_full": page_full,
            },
        }

    def _fetch_surface(name, path, parameters, is_list):
        page_size = int(parameters.get("limit", parameters.get("size")))
        collected: list[Mapping[str, object]] = []
        locked_total = None
        total_valid = is_list
        page_seen = (
            _seen_surface_identities.setdefault(name, set())
            if _seen_surface_identities is not None
            else set()
        )
        for page_index in range(1, _MAX_HISTORY_PAGES + 1):
            page_params = dict(parameters)
            if is_list:
                page_params["offset"] = len(collected)
            else:
                page_params["current"] = page_index
            try:
                response = client._request_margin_api(
                    "get", path, signed=True, data={**bounds, **page_params}
                )
            except Exception:
                return None, {
                    **result,
                    "reason_code": "balance_history_read_failed",
                    "failed_surface": name,
                }
            if is_list:
                rows = response
                total = None
            else:
                rows = response.get("rows") if isinstance(response, Mapping) else None
                total = response.get("total") if isinstance(response, Mapping) else None
                total_valid = (type(total) is int and total >= 0) or (
                    isinstance(total, str) and total.isdecimal()
                )
                if (
                    total_valid
                    and int(total) == 0
                    and isinstance(response, Mapping)
                    and "rows" not in response
                ):
                    rows = []
                if not total_valid:
                    return None, _incomplete(
                        name,
                        page_full=isinstance(rows, list) and len(rows) >= page_size,
                        rows_is_list=isinstance(rows, list),
                        total_valid=False,
                        total=total,
                    )
                page_total = int(total)
                if locked_total is None:
                    locked_total = page_total
                elif page_total != locked_total:
                    return None, _incomplete(
                        name,
                        page_full=isinstance(rows, list) and len(rows) >= page_size,
                        rows_is_list=isinstance(rows, list),
                        total_valid=True,
                        total=locked_total,
                    )
            if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
                return None, _incomplete(
                    name,
                    page_full=isinstance(rows, list) and len(rows) >= page_size,
                    rows_is_list=isinstance(rows, list),
                    total_valid=total_valid,
                    total=0 if is_list else (locked_total if locked_total is not None else total),
                )
            for row in rows:
                identity = _surface_row_identity(name, row)
                if identity is None:
                    if name.startswith("transfer_"):
                        return None, _incomplete(
                            name,
                            page_full=len(rows) >= page_size,
                            rows_is_list=True,
                            total_valid=total_valid,
                            total=(
                                0
                                if is_list
                                else (locked_total if locked_total is not None else total)
                            ),
                        )
                    continue
                if identity in page_seen:
                    return None, _incomplete(
                        name,
                        page_full=len(rows) >= page_size,
                        rows_is_list=True,
                        total_valid=total_valid,
                        total=0 if is_list else (locked_total if locked_total is not None else total),
                    )
                page_seen.add(identity)
            page_full = len(rows) >= page_size
            if is_list:
                collected.extend(rows)
                if not page_full:
                    return collected, None
                continue
            if len(rows) == 0:
                if len(collected) == locked_total:
                    return collected, None
                return None, _incomplete(
                    name,
                    page_full=False,
                    rows_is_list=True,
                    total_valid=True,
                    total=locked_total,
                )
            collected.extend(rows)
            if locked_total < len(collected):
                return None, _incomplete(
                    name,
                    page_full=page_full,
                    rows_is_list=True,
                    total_valid=True,
                    total=locked_total,
                )
            if len(collected) == locked_total:
                return collected, None
            if not page_full:
                return None, _incomplete(
                    name,
                    page_full=False,
                    rows_is_list=True,
                    total_valid=True,
                    total=locked_total,
                )
        return None, _incomplete(
            name,
            page_full=True,
            rows_is_list=True,
            total_valid=total_valid,
            total=locked_total or 0,
        )

    for name, path, parameters, is_list in surfaces:
        rows, failure = _fetch_surface(name, path, parameters, is_list)
        if failure is not None:
            return failure
        assert rows is not None
        counts[name] = len(rows)
        if name in {"earn_subscriptions", "earn_redemptions"} and _lifecycle_evidence is not None:
            try:
                _ingest_lifecycle_rows(
                    name,
                    rows,
                    bounds=bounds,
                    managed_assets=managed_assets,
                    evidence=_lifecycle_evidence,
                )
            except ValueError as exc:
                code = str(exc)
                if code == "duplicate_lifecycle_event":
                    return {
                        **result,
                        "reason_code": "balance_history_duplicate_event",
                        "failed_surface": name,
                        "history_complete_for_requested_surfaces": False,
                    }
                return {
                    **result,
                    "reason_code": "balance_history_incomplete",
                    "failed_surface": name,
                    "history_complete_for_requested_surfaces": False,
                }
        if name == "earn_subscriptions":
            if all(
                all(isinstance(row.get(key), str) for key in ("type", "status", "sourceAccount"))
                for row in rows
            ):
                result["automatic_spot_earn_subscriptions"] = sum(
                    row.get("type") == "AUTO"
                    and row.get("status") == "SUCCESS"
                    and row.get("sourceAccount") == "SPOT"
                    for row in rows
                )
        if name != "earn_rewards":
            continue
        reward_rows = rows
        if (
            reward_quantity_changes is not None
            or _seen_reward_identities is not None
            or _reward_sum_sink is not None
            or _approved_reward_products is not None
        ):
            try:
                seen = set()
                sums = (
                    {asset: {"BONUS": Decimal(0), "REALTIME": Decimal(0)} for asset in reward_quantity_changes}
                    if reward_quantity_changes is not None else None
                )
                counts_by_asset = (
                    {asset: {"BONUS": 0, "REALTIME": 0} for asset in reward_quantity_changes}
                    if reward_quantity_changes is not None else None
                )
                strict_rows = (
                    _seen_reward_identities is not None
                    or _reward_sum_sink is not None
                    or _approved_reward_products is not None
                )
                opening_products = _approved_reward_products or frozenset()
                subscription_facts = (
                    list(_lifecycle_evidence.get("subscriptions") or [])
                    if isinstance(_lifecycle_evidence, Mapping)
                    else []
                )
                for row in rows:
                    asset, kind = row.get("asset"), row.get("type")
                    timestamp = row.get("time")
                    project = row.get("projectId")
                    identity = (asset, kind, project, timestamp)
                    # Cross-chunk duplicates must be detected before window checks:
                    # a later chunk may re-emit an earlier identity outside its bounds.
                    if _seen_reward_identities is not None and identity in _seen_reward_identities:
                        return {
                            **result,
                            "reason_code": "balance_history_duplicate_event",
                            "failed_surface": name,
                            "history_complete_for_requested_surfaces": False,
                        }
                    if (
                        type(timestamp) is not int
                        or not bounds["startTime"] <= timestamp <= bounds["endTime"]
                    ):
                        if reward_quantity_changes is not None or strict_rows:
                            raise ValueError("reward_row_invalid")
                        continue
                    if kind not in {"BONUS", "REALTIME"}:
                        if strict_rows:
                            raise ValueError("reward_row_invalid")
                        continue
                    if _approved_reward_products is not None:
                        if not isinstance(asset, str) or not isinstance(project, str) or not project:
                            if classification_counts is not None:
                                classification_counts["lifecycle_evidence_incomplete"] += 1
                            raise ValueError("unapproved_reward_product")
                        if managed_assets is not None and asset not in managed_assets:
                            if classification_counts is not None:
                                classification_counts["out_of_scope_asset"] += 1
                            raise ValueError("unapproved_reward_product")
                        earlier_subs = [
                            fact
                            for fact in subscription_facts
                            if (
                                fact.get("product_id") == project
                                and type(fact.get("time")) is int
                                and fact["time"] < timestamp
                            )
                        ]
                        if (asset, project) in opening_products:
                            if classification_counts is not None:
                                classification_counts["opening_approved"] += 1
                        elif any(
                            fact.get("asset") == asset and fact.get("product_id") == project
                            for fact in earlier_subs
                        ):
                            if any(fact.get("asset") != asset for fact in earlier_subs):
                                if classification_counts is not None:
                                    classification_counts["product_mapping_ambiguous"] += 1
                                raise ValueError("unapproved_reward_product")
                            if classification_counts is not None:
                                classification_counts["lifecycle_subscription_proven"] += 1
                        elif earlier_subs:
                            if classification_counts is not None:
                                classification_counts["product_mapping_ambiguous"] += 1
                            raise ValueError("unapproved_reward_product")
                        elif managed_assets is not None and asset in managed_assets:
                            if classification_counts is not None:
                                classification_counts["lifecycle_evidence_incomplete"] += 1
                            raise ValueError("unapproved_reward_product")
                        else:
                            if classification_counts is not None:
                                classification_counts["unexplained_managed_impact"] += 1
                            raise ValueError("unapproved_reward_product")
                    if identity in seen:
                        if reward_quantity_changes is not None or strict_rows:
                            raise ValueError("reward_row_invalid")
                        return {
                            **result,
                            "reason_code": "balance_history_duplicate_event",
                            "failed_surface": name,
                            "history_complete_for_requested_surfaces": False,
                        }
                    seen.add(identity)
                    if _seen_reward_identities is not None:
                        _seen_reward_identities.add(identity)
                    try:
                        quantity = Decimal(str(row.get("rewards")))
                    except (InvalidOperation, TypeError, ValueError):
                        raise ValueError("reward_row_invalid") from None
                    if not quantity.is_finite() or quantity < 0:
                        raise ValueError("reward_row_invalid")
                    if _reward_sum_sink is not None:
                        if asset not in _reward_sum_sink:
                            raise ValueError("reward_row_invalid")
                        _reward_sum_sink[asset][kind] = _add_reward_quantity(
                            _reward_sum_sink[asset][kind], quantity
                        )
                    if sums is None or asset not in sums:
                        continue
                    sums[asset][kind] = _add_reward_quantity(sums[asset][kind], quantity)
                    counts_by_asset[asset][kind] += 1
                if reward_quantity_changes is not None:
                    result["reward_quantity_checks"] = {
                        asset: {
                            "delta_matches_bonus": delta == sums[asset]["BONUS"],
                            "delta_matches_realtime": delta == sums[asset]["REALTIME"],
                            "delta_matches_visible_total": delta == _add_reward_quantity(
                                sums[asset]["BONUS"], sums[asset]["REALTIME"]
                            ),
                            "reward_counts": counts_by_asset[asset],
                            "causal_reconciliation": False,
                        }
                        for asset, delta in reward_quantity_changes.items()
                    }
            except ValueError as exc:
                if str(exc) == "unapproved_reward_product":
                    return {
                        **result,
                        "reason_code": "balance_history_unapproved_reward_product",
                        "failed_surface": name,
                        "history_complete_for_requested_surfaces": False,
                    }
                return {
                    **result,
                    "reason_code": "balance_history_reward_validation_failed",
                    "failed_surface": name,
                }
            except (TypeError, DecimalException):
                return {
                    **result,
                    "reason_code": "balance_history_reward_validation_failed",
                    "failed_surface": name,
                }
        if account is not None and expected_digests is not None:
            result["spot_bonus_reconciliation"] = diagnose_bonus_reward_balance(
                account, rewards=reward_rows, expected_digests=expected_digests, start=start, end=end,
            )
            if result["spot_bonus_reconciliation"]["reason_code"] == "spot_bonus_reward_rows_invalid":
                return {
                    **result,
                    "reason_code": "balance_history_reward_validation_failed",
                    "failed_surface": name,
                }
    result["history_complete_for_requested_surfaces"] = True
    return result


def diagnose_chunked_balance_flows(
    client: Any,
    *,
    start: datetime,
    end: datetime,
    now: datetime | None = None,
    account: Mapping[str, object] | None = None,
    expected_digests: Mapping[str, str] | None = None,
    max_chunk: timedelta = timedelta(days=7),
    managed_reward_assets: Sequence[str] | None = None,
    approved_reward_products: set[tuple[str, str]] | frozenset[tuple[str, str]] | None = None,
) -> dict[str, object]:
    """Aggregate abutting <=max_chunk windows; never enlarges a single-window call.

    Explicit start/end required. Chunk boundaries advance by one millisecond so
    inclusive API ranges do not overlap. When the remaining inclusive endpoint
    would otherwise be skipped (span == N*max_chunk + 1ms), the final window
    overlaps the prior boundary by 1ms and cross-chunk duplicates still fail
    closed. Reward identities are deduplicated across chunks; a duplicate fails
    closed. When managed_reward_assets is set, BONUS/REALTIME reward quantities
    are summed from the first validated pass only — never by re-fetching the
    rewards surface. When approved_reward_products is set, opening products or
    complete subscription lifecycle evidence on managed assets may admit a
    reward product; diagnostics expose only fixed classification counts.
    """
    now = now or datetime.now(timezone.utc)
    if (
        start.tzinfo is None
        or end.tzinfo is None
        or not start < end <= now
        or max_chunk <= timedelta(0)
        or max_chunk > timedelta(days=7)
    ):
        raise ValueError("balance_history_window_invalid")

    counts: dict[str, int | None] = {}
    chunk_count = 0
    seen_rewards: set[tuple[object, ...]] = set()
    seen_surface_identities: dict[str, set[object]] = {}
    reward_assets = tuple(
        dict.fromkeys(str(asset).upper() for asset in (managed_reward_assets or ()) if str(asset).strip())
    )
    reward_sums = {
        asset: {"BONUS": Decimal(0), "REALTIME": Decimal(0)} for asset in reward_assets
    }
    approved_products = (
        frozenset(approved_reward_products) if approved_reward_products is not None else None
    )
    try:
        lifecycle_evidence = (
            _empty_lifecycle_evidence(approved_products) if approved_products is not None else None
        )
    except ValueError:
        return {
            "reason_code": "balance_history_incomplete",
            "history_complete_for_requested_surfaces": False,
            "history_counts": {},
            "chunk_count": 0,
            "complete_balance_reconciliation": False,
            "baseline_rows_available": False,
            "execution_authority_granted": False,
        }
    classification_counts = (
        _empty_reward_product_classification_counts() if approved_products is not None else None
    )
    managed_asset_set = frozenset(reward_assets) if reward_assets else None
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + max_chunk, end)
        chunk = diagnose_balance_flows(
            client,
            start=cursor,
            end=chunk_end,
            now=chunk_end,
            account=account if chunk_count == 0 else None,
            expected_digests=expected_digests if chunk_count == 0 else None,
            _seen_reward_identities=seen_rewards,
            _reward_sum_sink=reward_sums if reward_assets else None,
            _approved_reward_products=approved_products,
            _lifecycle_evidence=lifecycle_evidence,
            _reward_product_classification_counts=classification_counts,
            _managed_reward_assets=managed_asset_set,
            _seen_surface_identities=seen_surface_identities,
        )
        chunk_count += 1
        if chunk.get("history_complete_for_requested_surfaces") is not True:
            failure = {
                **chunk,
                "chunk_count": chunk_count,
                "history_complete_for_requested_surfaces": False,
            }
            if classification_counts is not None:
                failure["reward_product_classification_counts"] = dict(classification_counts)
            return failure
        chunk_counts = chunk.get("history_counts")
        if not isinstance(chunk_counts, Mapping):
            return {
                "reason_code": "balance_history_incomplete",
                "history_complete_for_requested_surfaces": False,
                "history_counts": counts,
                "chunk_count": chunk_count,
                "complete_balance_reconciliation": False,
                "baseline_rows_available": False,
                "execution_authority_granted": False,
            }
        for name, value in chunk_counts.items():
            if type(value) is not int or value < 0:
                return {
                    "reason_code": "balance_history_incomplete",
                    "history_complete_for_requested_surfaces": False,
                    "history_counts": counts,
                    "chunk_count": chunk_count,
                    "complete_balance_reconciliation": False,
                    "baseline_rows_available": False,
                    "execution_authority_granted": False,
                }
            counts[name] = int(counts.get(name) or 0) + value
        if chunk_end == end:
            break
        next_cursor = chunk_end + timedelta(milliseconds=1)
        if next_cursor < end:
            cursor = next_cursor
        elif next_cursor == end:
            # Cover the inclusive final millisecond via a 1ms overlapping window.
            cursor = chunk_end
        else:
            break

    result = {
        "reason_code": "balance_history_activity_summary",
        "history_complete_for_requested_surfaces": True,
        "history_counts": counts,
        "chunk_count": chunk_count,
        "automatic_spot_earn_subscriptions": None,
        "complete_balance_reconciliation": False,
        "baseline_rows_available": False,
        "execution_authority_granted": False,
    }
    if classification_counts is not None:
        result["reward_product_classification_counts"] = dict(classification_counts)
    if lifecycle_evidence is not None:
        # Private continuity-only facts; never part of public diagnostic emission.
        result["_private_lifecycle_subscription_facts"] = list(
            lifecycle_evidence["subscriptions"]
        )
        result["_private_lifecycle_redemption_facts"] = list(
            lifecycle_evidence["redemptions"]
        )
    if reward_assets:
        result["reward_quantity_totals"] = {
            asset: {
                "BONUS": _format_reward_quantity(values["BONUS"]),
                "REALTIME": _format_reward_quantity(values["REALTIME"]),
                "total": _format_reward_quantity(
                    _add_reward_quantity(values["BONUS"], values["REALTIME"])
                ),
            }
            for asset, values in reward_sums.items()
        }
    return result


def diagnose_bnb_wallet_activity(
    client, *, start: datetime, end: datetime, include_rows: bool = False
):
    """Two audit-only wallet GETs; not a complete funding proof or recovery gate."""
    if start.tzinfo is None or end.tzinfo is None or not start < end or end-start > timedelta(days=7):
        raise ValueError("balance_history_window_invalid")
    bounds = {"startTime": int(start.timestamp()*1000), "endTime": int(end.timestamp()*1000)}
    result = {
        "requested_surfaces_complete": False,
        "counts": {},
        "complete_balance_reconciliation": False,
        "surface_diagnostics": {},
    }
    for name, path, params, rows_key, time_key, limit in (
        ("bnb_dividends", "asset/assetDividend", {"asset": "BNB", "limit": 500}, "rows", "divTime", 500),
        ("spot_dust_conversions", "asset/dribblet", {"accountType": "SPOT"}, "userAssetDribblets", "operateTime", 100),
    ):
        try:
            response = client._request_margin_api("get", path, signed=True, data={**bounds, **params})
        except Exception as exc:
            failure = {**result, "reason_code": "bnb_wallet_history_unverified", "failed_surface": name,
                       "failure_stage": "request"}
            status = getattr(exc, "status_code", None)
            code = getattr(exc, "code", None)
            if type(status) is int and 100 <= status <= 599:
                failure["http_status"] = status
            if type(code) is int and -4999 <= code <= -1000:
                failure["provider_error_code"] = code
            return failure
        rows = response.get(rows_key) if isinstance(response, Mapping) else None
        total = response.get("total") if isinstance(response, Mapping) else None
        # Match the existing history reader: Binance may encode counts as strings.
        total_is_decimal_string = isinstance(total, str) and total.isascii() and total.isdecimal() and len(total) <= 10
        count = int(total) if type(total) is int or total_is_decimal_string else None
        shape = {
            "rows_present": isinstance(response, Mapping) and rows_key in response,
            "rows_is_list": isinstance(rows, list),
            "total_is_integer": type(total) is int,
            "total_is_decimal_string": total_is_decimal_string,
            "total_is_zero": count == 0,
            "total_matches_rows": isinstance(rows, list) and count == len(rows),
            "page_full": isinstance(rows, list) and len(rows) >= limit,
        }
        rows_readable = isinstance(rows, list) and all(isinstance(row, Mapping) for row in rows)
        visible_row_count = len(rows) if isinstance(rows, list) else 0
        row_keys = set()
        valid_rows = rows_readable
        if valid_rows:
            for row in rows:
                timestamp = row.get(time_key)
                if (
                    type(timestamp) is not int
                    or not bounds["startTime"] <= timestamp <= bounds["endTime"]
                    or (name == "bnb_dividends" and row.get("asset") != "BNB")
                ):
                    valid_rows = False
                    break
                if name == "bnb_dividends":
                    identity = (row.get("id"), row.get("tranId"), timestamp)
                    if (
                        type(identity[0]) is not int
                        or identity[0] < 0
                        or type(identity[1]) is not int
                        or identity[1] < 0
                        or identity in row_keys
                    ):
                        valid_rows = False
                        break
                else:
                    identity = row.get("transId")
                    if type(identity) is not int or identity < 0 or identity in row_keys:
                        valid_rows = False
                        break
                row_keys.add(identity)
        shape.update({
            "total_valid": count is not None and count >= 0,
            "rows_readable": rows_readable,
            "visible_row_count": visible_row_count,
            "empty_missing_total_exempt": (
                name == "spot_dust_conversions"
                and isinstance(response, Mapping)
                and "total" not in response
                and rows == []
            ),
            "window_complete": (
                (
                    name == "spot_dust_conversions"
                    and isinstance(response, Mapping)
                    and "total" not in response
                    and rows == []
                )
                or (
                    count is not None and count >= 0 and shape["total_matches_rows"]
                    and count < limit and valid_rows
                )
            ),
        })
        shape["row_time_and_asset_valid"] = valid_rows
        result["surface_diagnostics"][name] = shape
        if isinstance(rows, list) and rows_readable:
            result["counts"][name] = visible_row_count
            if include_rows:
                result.setdefault("_private_rows", {})[name] = rows
        if not shape["window_complete"]:
            return {**result, "reason_code": "bnb_wallet_history_unverified", "failed_surface": name,
                    "failure_stage": "response_validation", "response_shape": shape}
    return {**result, "requested_surfaces_complete": True}
