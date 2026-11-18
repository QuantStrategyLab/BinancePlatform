"""Read-only earn diagnosis, BNB wallet checks, cash-flow preview, and ledger audit."""

from __future__ import annotations

import copy
import json
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from urllib.request import HTTPRedirectHandler, Request, build_opener

from quant_platform_kit.common.broker_reconciliation_enrollment import (
    BrokerReconciliationBaselineCandidate,
)

from binance_platform.accounting_migration.candidate_migration import (
    read_source,
    strict_balance_snapshot,
)
from binance_platform.accounting_migration.common import (
    _BNB_DIAGNOSTIC_QUANTUM,
    _MAX_PRIVATE_SCOPE_BODY_BYTES,
    NEW_BASIS,
    PRIVATE_SCOPE_CONSOLE_URL,
    REBASE_ARCHIVE_DOCUMENT,
    MigrationApplyUncertain,
    MigrationBlocked,
    MigrationHooks,
    _diagnosis_decimal,
    _direction,
    _finite,
    _private_spot_account,
    _same_balance,
    _timestamp,
    is_prospective_archive_document,
    ledger_scope,
    resolve_hooks,
)
from binance_platform.application import earn_accrual as _earn_accrual
from binance_platform.application import rebased_recovery as _rebased_recovery
from binance_platform.application.broker_reconciliation import (
    BinanceReconciliationReadError,
)
from binance_platform.runtime_support import ExecutionIntegrityError


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, *args, **kwargs):
        return None


def _default_request_json(
    url: str, token: str, *, payload: Mapping[str, object] | None = None
) -> dict[str, object]:
    if not token:
        raise ValueError("recovery_credential_missing")
    request = Request(
        url,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "QuantStrategyLab-BinancePlatform/1.0 (accounting-migration)",
        },
        method="GET" if payload is None else "POST",
    )
    with build_opener(_NoRedirect).open(request, timeout=20) as response:
        if response.status != 200:
            raise ValueError("recovery_http_not_acknowledged")
        body = response.read(262145)
        if len(body) > 262144:
            raise ValueError("recovery_response_too_large")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("recovery_response_invalid")
    return result


def read_earn_diagnosis_source(refs) -> dict[str, object]:
    """Read the three accounting documents without treating an owner as a blocker."""
    owner = refs["owner_ref"].get(retry=None)
    ledger_snapshot = refs["ledger_ref"].get(retry=None)
    control_snapshot = refs["control_ref"].get(retry=None)
    if not ledger_snapshot.exists:
        raise MigrationBlocked("earn_diagnosis_ledger_missing")
    ledger = ledger_snapshot.to_dict()
    control = control_snapshot.to_dict() if control_snapshot.exists else None
    if control is not None and (
        not isinstance(control, Mapping)
        or control.get("state")
        not in {
            "RECONCILE_ONLY",
            "ACTIVE_LKG",
            "ROLLBACK_LKG",
            "PAUSED",
            "REDUCE_ONLY",
        }
    ):
        raise MigrationBlocked("earn_diagnosis_control_invalid")

    def marker(snapshot):
        return {
            "exists": snapshot.exists,
            "update_time": (
                _timestamp(snapshot.update_time) if snapshot.exists else None
            ),
            "value": snapshot.to_dict() if snapshot.exists else None,
        }

    return {
        "owner_exists": owner.exists,
        "owner_marker": marker(owner),
        "ledger": ledger,
        "ledger_marker": marker(ledger_snapshot),
        "control": control,
        "control_marker": marker(control_snapshot),
    }


def validate_bnb_dividend_rows(rows, *, start_ms: int, end_ms: int):
    if not isinstance(rows, list):
        return None
    row_keys = set()
    direction_values = set()
    direction_missing = False
    total = Decimal(0)
    try:
        for row in rows:
            if not isinstance(row, Mapping) or row.get("asset") != "BNB":
                return None
            div_time = row.get("divTime")
            if type(div_time) is not int or not start_ms <= div_time <= end_ms:
                return None
            row_id = row.get("id")
            tran_id = row.get("tranId")
            if (
                type(row_id) is not int
                or row_id < 0
                or type(tran_id) is not int
                or tran_id < 0
            ):
                return None
            key = (row_id, tran_id, div_time)
            if key in row_keys:
                return None
            row_keys.add(key)
            total += _diagnosis_decimal(row.get("amount"), signed=False)
            direction = row.get("direction")
            if type(direction) is int:
                direction_values.add(direction)
            else:
                direction_missing = True
    except (MigrationBlocked, TypeError, ValueError, InvalidOperation):
        return None
    return {
        "count": len(rows),
        "total": total,
        "direction_summary": {
            "integer_values": sorted(direction_values),
            "missing": direction_missing,
            "mixed": len(direction_values) > 1,
        },
    }


def summarize_bnb_wallet_activity(report, *, residual, start, end) -> dict[str, object]:
    """Compare private wallet rows to BNB residual without exposing source data."""
    private_rows = report.get("_private_rows") if isinstance(report, Mapping) else None
    summary = {
        "status": "CHECK_FAILED",
        "complete": False,
        "source_reason_code": None,
        "source_failed_surface": None,
        "source_failure_stage": None,
        "source_response_shape": None,
        "dividend_count": None,
        "dividend_surface_complete": False,
        "dividend_direction_summary": {
            "integer_values": [],
            "missing": False,
            "mixed": False,
        },
        "dust_record_count": None,
        "dust_bnb_detail_count": None,
        "dust_non_bnb_target_count": None,
        "dividend_residual_matches": None,
        "dust_transfer_residual_matches": None,
        "dust_after_fee_residual_matches": None,
        "combined_transfer_residual_matches": None,
        "combined_after_fee_residual_matches": None,
        "residual_within_one_eight_decimal_unit": abs(residual)
        <= _BNB_DIAGNOSTIC_QUANTUM,
        "dividend_net_semantics_verified": False,
        "dust_net_semantics_verified": False,
        "causal_reconciliation": False,
    }
    if (
        not isinstance(report, Mapping)
        or report.get("requested_surfaces_complete") is not True
    ):
        if isinstance(report, Mapping):
            counts = report.get("counts")
            if isinstance(counts, Mapping):
                if type(counts.get("bnb_dividends")) is int:
                    summary["dividend_count"] = counts["bnb_dividends"]
                if type(counts.get("spot_dust_conversions")) is int:
                    summary["dust_record_count"] = counts["spot_dust_conversions"]
            summary["source_reason_code"] = (
                report.get("reason_code")
                if report.get("reason_code") in {"bnb_wallet_history_unverified"}
                else None
            )
            summary["source_failed_surface"] = (
                report.get("failed_surface")
                if report.get("failed_surface")
                in {"bnb_dividends", "spot_dust_conversions"}
                else None
            )
            summary["source_failure_stage"] = (
                report.get("failure_stage")
                if report.get("failure_stage") in {"request", "response_validation"}
                else None
            )
            shape = report.get("response_shape")
            if isinstance(shape, Mapping):
                summary["source_response_shape"] = {
                    key: shape[key]
                    for key in (
                        "rows_present",
                        "rows_is_list",
                        "total_is_integer",
                        "total_is_decimal_string",
                        "total_is_zero",
                        "total_matches_rows",
                        "page_full",
                        "row_time_and_asset_valid",
                        "total_valid",
                        "rows_readable",
                        "visible_row_count",
                        "window_complete",
                    )
                    if type(shape.get(key)) is bool or type(shape.get(key)) is int
                }
            surface_diagnostics = report.get("surface_diagnostics")
            dividend_shape = (
                surface_diagnostics.get("bnb_dividends")
                if isinstance(surface_diagnostics, Mapping)
                else None
            )
            if (
                isinstance(dividend_shape, Mapping)
                and dividend_shape.get("window_complete") is True
                and isinstance(private_rows, Mapping)
            ):
                validated = validate_bnb_dividend_rows(
                    private_rows.get("bnb_dividends"),
                    start_ms=int(start.timestamp() * 1000),
                    end_ms=int(end.timestamp() * 1000),
                )
                if validated is not None:
                    summary["dividend_count"] = validated["count"]
                    summary["dividend_surface_complete"] = True
                    summary["dividend_residual_matches"] = validated["total"] == residual
                    summary["dividend_direction_summary"] = validated[
                        "direction_summary"
                    ]
        return summary
    summary["status"] = "UNVERIFIED"
    if (
        not isinstance(private_rows, Mapping)
        or not isinstance(private_rows.get("bnb_dividends"), list)
        or not isinstance(private_rows.get("spot_dust_conversions"), list)
    ):
        return summary
    summary["dust_bnb_detail_count"] = 0
    summary["dust_non_bnb_target_count"] = 0
    dust_diagnostics = (
        report.get("surface_diagnostics") if isinstance(report, Mapping) else None
    )
    dust_shape = (
        dust_diagnostics.get("spot_dust_conversions")
        if isinstance(dust_diagnostics, Mapping)
        else None
    )
    dust_surface_exempt = (
        isinstance(dust_shape, Mapping)
        and dust_shape.get("empty_missing_total_exempt") is True
    )

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    dust_transfer_total = Decimal(0)
    dust_after_fee_total = Decimal(0)
    dust_keys = set()
    try:
        validated_dividends = validate_bnb_dividend_rows(
            private_rows["bnb_dividends"],
            start_ms=start_ms,
            end_ms=end_ms,
        )
        if validated_dividends is None:
            return summary
        dividend_total = validated_dividends["total"]
        summary["dividend_count"] = validated_dividends["count"]
        summary["dividend_surface_complete"] = True
        summary["dividend_residual_matches"] = dividend_total == residual
        summary["dividend_direction_summary"] = validated_dividends["direction_summary"]

        for record in private_rows["spot_dust_conversions"]:
            if not isinstance(record, Mapping):
                return summary
            operate_time = record.get("operateTime")
            trans_id = record.get("transId")
            if (
                type(operate_time) is not int
                or not start_ms <= operate_time <= end_ms
                or type(trans_id) is not int
                or trans_id < 0
                or trans_id in dust_keys
            ):
                return summary
            dust_keys.add(trans_id)
            details = record.get("userAssetDribbletDetails")
            if not isinstance(details, list):
                return summary
            total_transfer = _diagnosis_decimal(
                record.get("totalTransferedAmount"), signed=False
            )
            total_fee = _diagnosis_decimal(
                record.get("totalServiceChargeAmount"), signed=False
            )
            detail_transfer_total = Decimal(0)
            detail_fee_total = Decimal(0)
            detail_keys = set()
            for detail in details:
                if not isinstance(detail, Mapping):
                    return summary
                detail_time = detail.get("operateTime")
                detail_trans_id = detail.get("transId")
                from_asset = detail.get("fromAsset")
                target_asset = detail.get("targetAsset")
                if (
                    type(detail_time) is not int
                    or not start_ms <= detail_time <= end_ms
                    or type(detail_trans_id) is not int
                    or detail_trans_id < 0
                    or not isinstance(from_asset, str)
                    or not isinstance(target_asset, str)
                ):
                    return summary
                detail_key = (detail_trans_id, detail_time, from_asset, target_asset)
                if detail_key in detail_keys:
                    return summary
                detail_keys.add(detail_key)
                transfer = _diagnosis_decimal(
                    detail.get("transferedAmount"), signed=False
                )
                fee = _diagnosis_decimal(
                    detail.get("serviceChargeAmount"), signed=False
                )
                if fee > transfer:
                    return summary
                detail_transfer_total += transfer
                detail_fee_total += fee
                if target_asset == "BNB":
                    summary["dust_bnb_detail_count"] += 1
                    dust_transfer_total += transfer
                    dust_after_fee_total += transfer - fee
                else:
                    summary["dust_non_bnb_target_count"] += 1
            if detail_transfer_total != total_transfer or detail_fee_total != total_fee:
                return summary

        summary["dust_record_count"] = len(private_rows["spot_dust_conversions"])
        if not dust_surface_exempt:
            summary["dust_transfer_residual_matches"] = dust_transfer_total == residual
            summary["dust_after_fee_residual_matches"] = dust_after_fee_total == residual
            combined_transfer = dividend_total + dust_transfer_total
            combined_after_fee = dividend_total + dust_after_fee_total
            summary["combined_transfer_residual_matches"] = combined_transfer == residual
            summary["combined_after_fee_residual_matches"] = combined_after_fee == residual
    except (MigrationBlocked, TypeError, ValueError, InvalidOperation):
        return summary
    summary["status"] = "COMPLETE"
    summary["complete"] = True
    return summary


def trade_net_diagnosis(observations, *, assets) -> tuple[dict[str, Decimal], dict[str, int]]:
    """Reconstruct signed Spot deltas from normalized, bounded myTrades rows."""
    net = {asset: Decimal(0) for asset in assets}
    counts = {asset: 0 for asset in assets}
    for trade in observations.recent_executions:
        symbol = str(trade.get("symbol") or "").upper()
        if not symbol.endswith("USDT") or len(symbol) <= 4:
            raise MigrationBlocked("earn_diagnosis_trade_symbol_invalid")
        asset = symbol[:-4]
        if asset not in assets or "USDT" not in assets:
            raise MigrationBlocked("earn_diagnosis_trade_asset_out_of_scope")
        quantity = _diagnosis_decimal(trade.get("qty"))
        price = _diagnosis_decimal(trade.get("price"))
        commission = _diagnosis_decimal(trade.get("commission"))
        commission_asset = str(trade.get("commission_asset") or "").upper()
        if not commission_asset or commission_asset not in assets:
            raise MigrationBlocked("earn_diagnosis_trade_asset_out_of_scope")
        sign = Decimal(1) if trade.get("is_buyer") is True else Decimal(-1)
        net[asset] += sign * quantity
        net["USDT"] -= sign * quantity * price
        net[commission_asset] -= commission
        counts[asset] += 1
    return net, counts


def forward_accounting_eligibility(*, result, asset_results) -> bool:
    """Return whether the stable observation may feed forward accounting.

    This is deliberately narrower than causal reconciliation and never grants
    execution authority.  It only admits a stable Earn/checkpoint interval
    with no unsupported external flow and a separately verified BNB dividend
    surface when BNB needs that explanation.
    """
    if (
        result.get("owner_exists")
        or result.get("order_state_known") is not True
        or result.get("open_order_count") != 0
        or result.get("sampling_stable") is not True
        or result.get("account_scope_verified") is not True
        or result.get("control_unchanged") is not True
        or result.get("ledger_unchanged") is not True
        or result.get("unsupported_external_flow_count") != 0
        or result.get("changed_withdrawal_count") != 0
    ):
        return False
    for asset, check in asset_results.items():
        if (
            check.get("product_status") != "STABLE"
            or check.get("trade_net_matches_persisted") is not True
            or check.get("second_sample_residual_matches_first") is not True
            or check.get("classification")
            not in {
                "residual_zero_after_realtime_counter",
                "residual_matches_bonus_records",
            }
        ):
            return False
        if asset == "USDT" and check.get("external_flow_count", 0) > 0:
            if check.get("external_flow_matches_residual") is not True:
                return False
        if asset == "BNB" and check.get("residual_matches_bonus_records") is True:
            if (
                check.get("wallet_activity_complete") is not True
                or check.get("wallet_dividend_residual_matches") is not True
            ):
                return False
    return True


def _sampling_shape(value):
    if not isinstance(value, Mapping):
        return None
    assets_value = value.get("assets")
    if not isinstance(assets_value, Mapping):
        return None
    shape = {}
    for asset, row in assets_value.items():
        if not isinstance(row, Mapping) or not isinstance(row.get("products"), Mapping):
            return None
        products = {}
        for product, position in row["products"].items():
            if not isinstance(position, Mapping):
                return None
            products[product] = (
                position.get("auto_subscribe"),
                position.get("can_redeem"),
            )
        shape[asset] = (products,)
    return shape


def _sampling_components_stable(first, second) -> bool:
    if not isinstance(first, Mapping) or not isinstance(second, Mapping):
        return False
    first_assets, second_assets = first.get("assets"), second.get("assets")
    if not isinstance(first_assets, Mapping) or not isinstance(second_assets, Mapping):
        return False
    if set(first_assets) != set(second_assets):
        return False
    try:
        for asset in first_assets:
            before, after = first_assets[asset], second_assets[asset]
            if not isinstance(before, Mapping) or not isinstance(after, Mapping):
                return False
            if (
                _diagnosis_decimal(before.get("spot_free"), signed=True)
                != _diagnosis_decimal(after.get("spot_free"), signed=True)
                or _diagnosis_decimal(before.get("spot_locked"), signed=True)
                != _diagnosis_decimal(after.get("spot_locked"), signed=True)
            ):
                return False
            first_products, second_products = before.get("products"), after.get("products")
            if not isinstance(first_products, Mapping) or not isinstance(
                second_products, Mapping
            ):
                return False
            if set(first_products) != set(second_products):
                return False
            for product in first_products:
                old_row, new_row = first_products[product], second_products[product]
                if not isinstance(old_row, Mapping) or not isinstance(new_row, Mapping):
                    return False
                quantity_delta = _diagnosis_decimal(
                    new_row.get("total"), signed=True
                ) - _diagnosis_decimal(old_row.get("total"), signed=True)
                reward_delta = _diagnosis_decimal(
                    new_row.get("realtime_rewards"), signed=True
                ) - _diagnosis_decimal(old_row.get("realtime_rewards"), signed=True)
                if quantity_delta != reward_delta:
                    return False
    except (MigrationBlocked, TypeError, ValueError, InvalidOperation):
        return False
    return True


def _sample_residual_after_realtime(
    sample,
    *,
    checkpoint,
    assets: tuple[str, ...],
    stored_net: Mapping[str, Decimal],
    external_principal: Decimal,
):
    if not isinstance(sample, Mapping) or not isinstance(sample.get("assets"), Mapping):
        return None
    residuals = {}
    for asset in assets:
        before = checkpoint["assets"].get(asset)
        after = sample["assets"].get(asset)
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            return None
        before_products, after_products = before.get("products"), after.get("products")
        if not isinstance(before_products, Mapping) or not isinstance(
            after_products, Mapping
        ):
            return None
        if set(before_products) != set(after_products):
            return None
        reward = Decimal(0)
        for product in before_products:
            old_row, new_row = before["products"][product], after["products"][product]
            if (
                not isinstance(old_row, Mapping)
                or not isinstance(new_row, Mapping)
                or old_row.get("auto_subscribe") != new_row.get("auto_subscribe")
            ):
                return None
            delta = _diagnosis_decimal(
                new_row.get("realtime_rewards"), signed=True
            ) - _diagnosis_decimal(old_row.get("realtime_rewards"), signed=True)
            if delta < 0:
                return None
            reward += delta
        observed = _diagnosis_decimal(after.get("quantity"), signed=True) - _diagnosis_decimal(
            before.get("quantity"), signed=True
        )
        residuals[asset] = (
            observed
            - stored_net[asset]
            - (external_principal if asset == "USDT" else Decimal(0))
            - reward
        )
    return residuals


def _collect_earn_samples(
    client, *, assets: tuple[str, ...], now: datetime, expected_scope: str
) -> tuple[dict[str, object], dict[str, object] | None, list[dict[str, object]]]:
    sampling_requests: list[dict[str, object]] = []

    class _SamplingClient:
        def __init__(self, wrapped, sample_label: str):
            self._wrapped = wrapped
            self._sample_label = sample_label

        def _read(self, surface: str, method, **kwargs):
            started = time.monotonic()
            started_at = datetime.now(timezone.utc)
            try:
                return method(**kwargs)
            finally:
                sampling_requests.append(
                    {
                        "label": f"{surface}_{self._sample_label}",
                        "started": started,
                        "finished": time.monotonic(),
                        "started_at": started_at,
                    }
                )

        def get_account(self):
            return self._read("spot", self._wrapped.get_account)

        def get_simple_earn_flexible_product_position(self, **kwargs):
            return self._read(
                "earn",
                self._wrapped.get_simple_earn_flexible_product_position,
                **kwargs,
            )

    try:
        current = _earn_accrual.collect_earn_checkpoint(
            _SamplingClient(client, "1"),
            assets=assets,
            observed_at=now,
            expected_account_scope_sha256=expected_scope,
        )
    except Exception:
        raise MigrationBlocked("earn_diagnosis_checkpoint_unavailable") from None

    # Each checkpoint performs Spot first and Flexible Earn second. Keep both
    # samples private and compare the complete normalized shape; Firestore
    # markers below cannot prove that broker reads came from one stable window.
    try:
        second_sample = _earn_accrual.collect_earn_checkpoint(
            _SamplingClient(client, "2"),
            assets=assets,
            observed_at=now,
            expected_account_scope_sha256=expected_scope,
        )
    except Exception:
        second_sample = None

    return current, second_sample, sampling_requests


def diagnose_earn_forward(
    refs,
    *,
    client,
    expected,
    now,
    configured_symbols: tuple[str, ...],
    hooks: MigrationHooks | None = None,
) -> dict[str, object]:
    """Diagnose the current checkpoint window without changing any durable state."""
    active_hooks = resolve_hooks(hooks)
    digest_fn = active_hooks.digest_fn

    source = read_earn_diagnosis_source(refs)
    ledger = source["ledger"]
    order_record = ledger.get("order_submission")
    order_state_known = isinstance(order_record, Mapping) and order_record.get("state") in {
        "RESERVED",
        "TERMINAL",
    }
    checkpoint = ledger.get("earn_accrual_checkpoint")
    if not isinstance(checkpoint, Mapping):
        raise MigrationBlocked("earn_diagnosis_checkpoint_missing")
    try:
        _earn_accrual._validate(checkpoint)
        checkpoint_at = _earn_accrual._time(checkpoint["observed_at"])
    except (TypeError, ValueError):
        raise MigrationBlocked("earn_diagnosis_checkpoint_invalid") from None
    expected_scope = expected.get("account_scope_sha256")
    if not isinstance(expected_scope, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_scope
    ):
        raise MigrationBlocked("earn_diagnosis_account_scope_missing")
    if checkpoint.get("account_scope_sha256") != expected_scope:
        raise MigrationBlocked("earn_diagnosis_checkpoint_account_scope_mismatch")
    if not isinstance(now, datetime) or now.tzinfo is None or not checkpoint_at < now:
        raise MigrationBlocked("earn_diagnosis_checkpoint_window_invalid")
    if now - checkpoint_at > timedelta(days=7):
        raise MigrationBlocked("earn_diagnosis_checkpoint_window_invalid")

    assets = tuple(sorted(checkpoint["assets"]))
    if "USDT" not in assets or not 0 < len(assets) <= 32:
        raise MigrationBlocked("earn_diagnosis_asset_scope_invalid")
    net_values = ledger.get("earn_accounted_net_changes")
    if not isinstance(net_values, Mapping) or set(net_values) != set(assets):
        raise MigrationBlocked("earn_diagnosis_net_changes_missing")
    stored_net = {
        asset: _diagnosis_decimal(net_values[asset], signed=True) for asset in assets
    }
    cursor = ledger.get("external_cash_flow_cursor")
    if not isinstance(cursor, Mapping):
        raise MigrationBlocked("earn_diagnosis_cursor_missing")
    try:
        if _earn_accrual._time(cursor.get("observed_at")) != checkpoint_at:
            raise ValueError
    except (TypeError, ValueError):
        raise MigrationBlocked("earn_diagnosis_cursor_mismatch") from None

    required_symbols = tuple(f"{asset}USDT" for asset in assets if asset != "USDT")
    configured_symbol_set = set(configured_symbols)
    if any(symbol not in configured_symbol_set for symbol in required_symbols):
        raise MigrationBlocked("earn_diagnosis_symbols_missing")

    current, second_sample, sampling_requests = _collect_earn_samples(
        client, assets=assets, now=now, expected_scope=expected_scope
    )
    sampling_shape_stable = (
        second_sample is not None
        and _sampling_shape(current) is not None
        and _sampling_shape(current) == _sampling_shape(second_sample)
    )
    sampling_components_stable = _sampling_components_stable(current, second_sample)
    expected_sampling_labels = ["spot_1", "earn_1", "spot_2", "earn_2"]
    observed_sampling_labels = [row["label"] for row in sampling_requests]
    sampling_timing_stable = (
        observed_sampling_labels == expected_sampling_labels
        and all(row["finished"] >= row["started"] for row in sampling_requests)
        and all(
            sampling_requests[index]["finished"] <= sampling_requests[index + 1]["started"]
            for index in range(len(sampling_requests) - 1)
        )
    )

    try:
        flows = active_hooks.collect_cash_flows_fn(
            client, now=now, cursor=copy.deepcopy(cursor)
        )
    except Exception:
        raise MigrationBlocked("earn_diagnosis_external_flow_unavailable") from None

    try:
        observations = active_hooks.collect_observations_fn(
            client,
            strategy_symbols=required_symbols,
            local_execution_ledger=ledger,
            now=now,
            lookback=now - checkpoint_at,
        )
    except Exception:
        raise MigrationBlocked("earn_diagnosis_trade_history_unavailable") from None
    if digest_fn(observations.account_scope) != expected_scope:
        raise MigrationBlocked("earn_diagnosis_account_scope_mismatch")
    trade_net, trade_counts = trade_net_diagnosis(observations, assets=assets)

    external_principal = _diagnosis_decimal(
        flows.get("new_deposit_principal_usdt"), signed=False
    )
    if flows.get("new_unsupported_deposit_count", 0) or flows.get(
        "new_or_changed_withdrawal_count", 0
    ):
        external_status = "UNSUPPORTED_ACTIVITY"
    else:
        external_status = "OBSERVED"

    second_residual_after_realtime = _sample_residual_after_realtime(
        second_sample,
        checkpoint=checkpoint,
        assets=assets,
        stored_net=stored_net,
        external_principal=external_principal,
    )

    observed_delta = {}
    residual_before_realtime = {}
    residual_after_realtime = {}
    product_status = {}
    realtime_delta = {}
    for asset in assets:
        before = checkpoint["assets"][asset]
        after = current["assets"].get(asset)
        if not isinstance(after, Mapping):
            raise MigrationBlocked("earn_diagnosis_checkpoint_asset_missing")
        observed_delta[asset] = _diagnosis_decimal(
            after["quantity"], signed=True
        ) - _diagnosis_decimal(before["quantity"], signed=True)
        before_products, after_products = before["products"], after["products"]
        if set(before_products) != set(after_products):
            product_status[asset] = "PRODUCT_LIFECYCLE_CHANGED"
            realtime_delta[asset] = None
        else:
            counter_delta = Decimal(0)
            status = "STABLE"
            for product in before_products:
                old_row, new_row = before_products[product], after_products[product]
                if old_row["auto_subscribe"] != new_row["auto_subscribe"]:
                    status = "PRODUCT_LIFECYCLE_CHANGED"
                    break
                delta = _diagnosis_decimal(
                    new_row["realtime_rewards"], signed=True
                ) - _diagnosis_decimal(old_row["realtime_rewards"], signed=True)
                if delta < 0:
                    status = "COUNTER_RESET"
                    break
                counter_delta += delta
            product_status[asset] = status
            realtime_delta[asset] = counter_delta if status == "STABLE" else None
        residual_before_realtime[asset] = (
            observed_delta[asset]
            - stored_net[asset]
            - (external_principal if asset == "USDT" else Decimal(0))
        )
        residual_after_realtime[asset] = residual_before_realtime[asset] - (
            realtime_delta[asset] if realtime_delta[asset] is not None else Decimal(0)
        )

    history = active_hooks.diagnose_flows_fn(
        client,
        start=checkpoint_at,
        end=now,
        now=now,
        reward_quantity_changes=residual_after_realtime,
    )
    if history.get("history_complete_for_requested_surfaces") is not True:
        reason = history.get("reason_code")
        if reason == "balance_history_reward_validation_failed":
            raise MigrationBlocked("earn_diagnosis_reward_history_invalid")
        raise MigrationBlocked("earn_diagnosis_history_incomplete")
    reward_checks = history.get("reward_quantity_checks")
    if not isinstance(reward_checks, Mapping) or set(reward_checks) != set(assets):
        raise MigrationBlocked("earn_diagnosis_reward_history_invalid")

    bnb_wallet_activity = {
        "status": "NOT_CHECKED",
        "complete": False,
        "dividend_surface_complete": False,
        "dividend_direction_summary": {
            "integer_values": [],
            "missing": False,
            "mixed": False,
        },
        "source_reason_code": None,
        "source_failed_surface": None,
        "source_failure_stage": None,
        "source_response_shape": None,
        "dividend_count": None,
        "dust_record_count": None,
        "dust_bnb_detail_count": None,
        "dust_non_bnb_target_count": None,
        "dividend_residual_matches": None,
        "dust_transfer_residual_matches": None,
        "dust_after_fee_residual_matches": None,
        "combined_transfer_residual_matches": None,
        "combined_after_fee_residual_matches": None,
        "residual_within_one_eight_decimal_unit": False,
        "dividend_net_semantics_verified": False,
        "dust_net_semantics_verified": False,
        "causal_reconciliation": False,
    }
    if "BNB" in assets:
        bnb_wallet_activity["residual_within_one_eight_decimal_unit"] = (
            abs(residual_after_realtime["BNB"]) <= _BNB_DIAGNOSTIC_QUANTUM
        )
        other_assets_normal = all(
            product_status[asset] == "STABLE"
            and residual_after_realtime[asset] == 0
            and trade_net[asset] == stored_net[asset]
            for asset in assets
            if asset != "BNB"
        )
        bnb_preconditions = (
            product_status["BNB"] == "STABLE"
            and residual_after_realtime["BNB"] > 0
            and trade_net["BNB"] == stored_net["BNB"]
            and external_status != "UNSUPPORTED_ACTIVITY"
            and not observations.open_orders
            and other_assets_normal
        )
        if bnb_preconditions:
            try:
                bnb_wallet_report = active_hooks.diagnose_bnb_wallet_fn(
                    client,
                    start=checkpoint_at,
                    end=now,
                    include_rows=True,
                )
            except Exception:
                bnb_wallet_report = {
                    "requested_surfaces_complete": False,
                    "reason_code": "bnb_wallet_history_unverified",
                    "failure_stage": "request",
                }
            bnb_wallet_activity = summarize_bnb_wallet_activity(
                bnb_wallet_report,
                residual=residual_after_realtime["BNB"],
                start=checkpoint_at,
                end=now,
            )

    asset_results = {}
    for asset in assets:
        check = reward_checks.get(asset)
        if not isinstance(check, Mapping) or not isinstance(
            check.get("reward_counts"), Mapping
        ):
            raise MigrationBlocked("earn_diagnosis_reward_history_invalid")
        counts = check["reward_counts"]
        if any(
            type(counts.get(kind)) is not int or counts[kind] < 0
            for kind in ("BONUS", "REALTIME")
        ):
            raise MigrationBlocked("earn_diagnosis_reward_history_invalid")
        if product_status[asset] == "PRODUCT_LIFECYCLE_CHANGED":
            classification = "product_lifecycle_changed"
        elif product_status[asset] == "COUNTER_RESET":
            classification = "counter_reset"
        elif trade_net[asset] != stored_net[asset]:
            classification = "trade_net_unmatched"
        elif external_status == "UNSUPPORTED_ACTIVITY" and asset == "USDT":
            classification = "external_flow_unsupported"
        elif product_status[asset] == "STABLE" and residual_after_realtime[asset] == 0:
            classification = "residual_zero_after_realtime_counter"
        elif (
            product_status[asset] == "STABLE"
            and residual_after_realtime[asset] > 0
            and counts["BONUS"] > 0
            and check.get("delta_matches_bonus")
        ):
            classification = "residual_matches_bonus_records"
        else:
            classification = "quantity_unexplained"
        realtime_status = product_status[asset]
        if realtime_delta[asset] is not None:
            realtime_status = "INCREASE" if realtime_delta[asset] > 0 else "UNCHANGED"
        external_matches = None
        if asset == "USDT" and realtime_delta[asset] is not None:
            external_matches = (
                observed_delta[asset] - stored_net[asset] - realtime_delta[asset]
                == external_principal
            )
        asset_results[asset] = {
            "quantity_direction": _direction(observed_delta[asset]),
            "residual_before_realtime_direction": _direction(
                residual_before_realtime[asset]
            ),
            "residual_direction": _direction(residual_after_realtime[asset]),
            "stored_net_change_direction": _direction(stored_net[asset]),
            "trade_net_difference_direction": _direction(
                trade_net[asset] - stored_net[asset]
            ),
            "external_flow_direction": _direction(
                external_principal if asset == "USDT" else Decimal(0)
            ),
            "realtime_reward_direction": _direction(
                realtime_delta[asset] if realtime_delta[asset] is not None else Decimal(0)
            ),
            "second_sample_residual_direction": (
                _direction(second_residual_after_realtime[asset])
                if second_residual_after_realtime is not None
                else None
            ),
            "second_sample_residual_matches_first": (
                second_residual_after_realtime is not None
                and second_residual_after_realtime[asset] == residual_after_realtime[asset]
            ),
            "product_status": product_status[asset],
            "product_count_before": len(checkpoint["assets"][asset]["products"]),
            "product_count_current": len(current["assets"][asset]["products"]),
            "realtime_counter_status": realtime_status,
            "bonus_record_count": counts["BONUS"],
            "realtime_record_count": counts["REALTIME"],
            "residual_matches_bonus_records": (
                None
                if product_status[asset] != "STABLE"
                else (
                    residual_after_realtime[asset] > 0
                    and counts["BONUS"] > 0
                    and check.get("delta_matches_bonus") is True
                )
            ),
            "trade_count": trade_counts[asset],
            "trade_net_matches_persisted": trade_net[asset] == stored_net[asset],
            "trade_net_diagnostic_only": True,
            "external_flow_matches_residual": external_matches,
            "external_flow_count": flows.get("new_confirmed_deposit_count", 0)
            if asset == "USDT"
            else 0,
            "classification": classification,
            "causal_reconciliation": False,
        }
    if "BNB" in asset_results:
        asset_results["BNB"].update(
            {
                "wallet_activity_status": bnb_wallet_activity["status"],
                "wallet_activity_complete": bnb_wallet_activity["complete"],
                "wallet_dividend_surface_complete": bnb_wallet_activity[
                    "dividend_surface_complete"
                ],
                "wallet_dividend_direction_summary": bnb_wallet_activity[
                    "dividend_direction_summary"
                ],
                "wallet_dividend_record_count": bnb_wallet_activity["dividend_count"],
                "wallet_dust_record_count": bnb_wallet_activity["dust_record_count"],
                "wallet_dust_bnb_detail_count": bnb_wallet_activity[
                    "dust_bnb_detail_count"
                ],
                "wallet_dust_non_bnb_target_count": bnb_wallet_activity[
                    "dust_non_bnb_target_count"
                ],
                "wallet_dividend_residual_matches": bnb_wallet_activity[
                    "dividend_residual_matches"
                ],
                "wallet_dust_transfer_residual_matches": bnb_wallet_activity[
                    "dust_transfer_residual_matches"
                ],
                "wallet_dust_after_fee_residual_matches": bnb_wallet_activity[
                    "dust_after_fee_residual_matches"
                ],
                "wallet_combined_transfer_residual_matches": bnb_wallet_activity[
                    "combined_transfer_residual_matches"
                ],
                "wallet_combined_after_fee_residual_matches": bnb_wallet_activity[
                    "combined_after_fee_residual_matches"
                ],
                "residual_within_one_eight_decimal_unit": bnb_wallet_activity[
                    "residual_within_one_eight_decimal_unit"
                ],
                "wallet_dividend_net_semantics_verified": bnb_wallet_activity[
                    "dividend_net_semantics_verified"
                ],
                "wallet_dust_net_semantics_verified": bnb_wallet_activity[
                    "dust_net_semantics_verified"
                ],
                "wallet_causal_reconciliation": bnb_wallet_activity[
                    "causal_reconciliation"
                ],
                "wallet_source_reason_code": bnb_wallet_activity["source_reason_code"],
                "wallet_source_failed_surface": bnb_wallet_activity[
                    "source_failed_surface"
                ],
                "wallet_source_failure_stage": bnb_wallet_activity[
                    "source_failure_stage"
                ],
                "wallet_source_response_shape": bnb_wallet_activity[
                    "source_response_shape"
                ],
            }
        )

    sampling_residual_stable = (
        second_residual_after_realtime is not None
        and all(
            second_residual_after_realtime[asset] == residual_after_realtime[asset]
            for asset in assets
        )
    )
    sampling_stable = (
        sampling_shape_stable
        and sampling_components_stable
        and sampling_timing_stable
        and sampling_residual_stable
    )

    after = read_earn_diagnosis_source(refs)
    if any(
        source[key] != after[key]
        for key in ("owner_marker", "ledger_marker", "control_marker")
    ):
        raise MigrationBlocked("earn_diagnosis_state_changed_during_read")
    result = {
        "status": "diagnosed",
        "stage": "earn_forward_accounting_diagnosis",
        "owner_exists": source["owner_exists"],
        "owner_unchanged": True,
        "ledger_unchanged": True,
        "control_unchanged": True,
        "sampling_stable": sampling_stable,
        "sampling_sequence": ["spot_1", "earn_1", "spot_2", "earn_2"],
        "sampling_second_read_available": second_sample is not None,
        "sampling_request_timing_stable": sampling_timing_stable,
        "sampling_components_stable": sampling_components_stable,
        "sampling_residual_stable": sampling_residual_stable,
        "sampling_observed_at": current.get("observed_at"),
        "account_scope_verified": True,
        "checkpoint_window_within_seven_days": True,
        "order_state_known": order_state_known,
        "diagnostic_restricted": not order_state_known,
        "assets": asset_results,
        "history_counts": history["history_counts"],
        "open_order_count": len(observations.open_orders),
        "unsupported_external_flow_count": flows.get("new_unsupported_deposit_count", 0),
        "changed_withdrawal_count": flows.get("new_or_changed_withdrawal_count", 0),
        "external_flow_status": external_status,
        "bnb_wallet_activity_checked": bnb_wallet_activity["status"] != "NOT_CHECKED",
        "bnb_wallet_activity_status": bnb_wallet_activity["status"],
        "bnb_wallet_activity_complete": bnb_wallet_activity["complete"],
        "causal_reconciliation": False,
        "activation_allowed": False,
        "no_order": True,
        "write_performed": False,
        "execution_authority_granted": False,
    }
    result["forward_accounting_eligible"] = forward_accounting_eligibility(
        result=result, asset_results=asset_results
    )
    result["forward_accounting_write_permitted"] = False
    return result


def validated_private_scope_source(
    refs, *, initial_source=None, hooks: MigrationHooks | None = None
) -> dict[str, object]:
    active_hooks = resolve_hooks(hooks)
    digest_fn = active_hooks.digest_fn
    read_fn = active_hooks.read_source_fn or read_source
    ledger_snapshot, ledger, control = initial_source or read_fn(refs)
    marker = ledger.get("accounting_rebase")
    archive_document = (
        marker.get("archive_document") if isinstance(marker, Mapping) else None
    )
    if archive_document == REBASE_ARCHIVE_DOCUMENT:
        validate_material = _rebased_recovery.validate_post_rebase_material
        source_kind = "post_rebase"
    elif is_prospective_archive_document(archive_document):
        validate_material = _rebased_recovery.validate_prospective_rebase_material
        source_kind = "prospective_rebase"
    else:
        raise MigrationBlocked("private_scope_source_invalid")
    archive_ref = refs["ledger_ref"].parent.document(archive_document)
    archive_snapshot = archive_ref.get(retry=None)
    if not archive_snapshot.exists:
        raise MigrationBlocked("private_scope_source_invalid")
    archive = archive_snapshot.to_dict()
    try:
        material = validate_material(ledger, archive)
        if source_kind == "prospective_rebase":
            approved = archive.get("approved_proposal")
            proposed = (
                approved.get("proposed_fields")
                if isinstance(approved, Mapping)
                else None
            )
            checkpoint = (
                proposed.get("earn_accrual_checkpoint")
                if isinstance(proposed, Mapping)
                else None
            )
            approved_account_scope = (
                checkpoint.get("account_scope_sha256")
                if isinstance(checkpoint, Mapping)
                else None
            )
        else:
            approved_account_scope = archive["recovery_control"]["source"][
                "original_evidence"
            ]["account_scope_sha256"]
        if (
            not isinstance(approved_account_scope, str)
            or len(approved_account_scope) != 64
        ):
            raise ValueError("account scope")
        opening_quantities = material.get("opening_quantities")
        if not isinstance(opening_quantities, Mapping) or not opening_quantities:
            raise ValueError("opening quantities")
    except (KeyError, TypeError, ValueError):
        raise MigrationBlocked("private_scope_source_invalid") from None
    return {
        "archive_ref": archive_ref,
        "archive_document": archive_document,
        "source_kind": source_kind,
        "material": material,
        "approved_account_scope_sha256": approved_account_scope,
        "binding": {
            "ledger_sha256": digest_fn(ledger),
            "ledger_update_time": _timestamp(ledger_snapshot.update_time),
            "control_sha256": digest_fn(control),
            "archive_sha256": digest_fn(archive),
        },
    }


def collect_private_spot_scope_preview(
    refs,
    *,
    client,
    expected,
    observed_at,
    source_sha,
    initial_source=None,
    hooks: MigrationHooks | None = None,
) -> dict[str, object]:
    """Collect an encrypted-output-only view of non-managed Spot balances."""
    active_hooks = resolve_hooks(hooks)
    scope_source_fn = active_hooks.validated_private_scope_source_fn or (
        lambda r, *, initial_source=None: validated_private_scope_source(
            r, initial_source=initial_source, hooks=active_hooks
        )
    )
    before = scope_source_fn(refs, initial_source=initial_source)
    account_scope = expected.get("account_scope_sha256")
    if (
        not isinstance(account_scope, str)
        or len(account_scope) != 64
        or account_scope != before["approved_account_scope_sha256"]
    ):
        raise MigrationBlocked("account_scope_unverified")
    private_spot_fn = active_hooks.private_spot_account_fn or (
        lambda acc, *, expected_account_scope_sha256: _private_spot_account(
            acc,
            expected_account_scope_sha256=expected_account_scope_sha256,
            digest_fn=active_hooks.digest_fn,
        )
    )
    first = private_spot_fn(
        client.get_account(), expected_account_scope_sha256=account_scope
    )
    second = private_spot_fn(
        client.get_account(), expected_account_scope_sha256=account_scope
    )
    if first != second:
        raise MigrationBlocked("private_scope_snapshot_changed")

    after = scope_source_fn(refs)
    if before["binding"] != after["binding"]:
        raise MigrationBlocked("private_scope_source_changed")

    managed_assets = set(before["material"]["opening_quantities"])
    non_managed = [
        {"asset": asset, "free": str(free), "locked": str(locked)}
        for asset, (free, locked) in first[1]
        if asset not in managed_assets and (free != 0 or locked != 0)
    ]
    return {
        "status": "observed",
        "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
        "source_kind": before["source_kind"],
        "source": {
            "source_sha": source_sha,
            "archive_document": before["archive_document"],
            "archive_sha256": before["binding"]["archive_sha256"],
            "ledger_sha256": before["binding"]["ledger_sha256"],
            "account_scope_sha256": account_scope,
        },
        "scope": "non_managed_nonzero_spot_assets",
        "non_managed_nonzero_spot_assets": non_managed,
        "historical_difference_unresolved": True,
        "no_order": True,
        "write_performed": False,
        "execution_authority_granted": False,
    }


def build_private_spot_scope_payload(
    preview, *, source_run_id, source_sha
) -> dict[str, object]:
    """Build the one private console request without adding account surfaces."""
    if (
        type(source_run_id) is not str
        or not re.fullmatch(r"[1-9][0-9]{0,19}", source_run_id)
        or type(source_sha) is not str
        or not re.fullmatch(r"[0-9a-f]{40}", source_sha)
    ):
        raise MigrationBlocked("private_scope_payload_invalid")
    try:
        assets = [
            {
                "asset": row["asset"],
                "free": format(Decimal(row["free"]), "f"),
                "locked": format(Decimal(row["locked"]), "f"),
            }
            for row in preview["non_managed_nonzero_spot_assets"]
        ]
        payload = {
            "platform": "binance",
            "observed_at": preview["observed_at"],
            "source_run_id": source_run_id,
            "source_sha": source_sha,
            "account_scope_sha256": preview["source"]["account_scope_sha256"],
            "assets": assets,
            "historical_difference_unresolved": True,
            "no_order": True,
            "execution_authority_granted": False,
        }
        encoded = json.dumps(payload).encode()
    except (InvalidOperation, KeyError, TypeError, ValueError):
        raise MigrationBlocked("private_scope_payload_invalid") from None
    if len(encoded) > _MAX_PRIVATE_SCOPE_BODY_BYTES:
        raise MigrationBlocked("private_scope_payload_too_large")
    return payload


def publish_private_spot_scope_preview(
    preview,
    *,
    token,
    source_run_id,
    source_sha,
    request_json_fn: Callable[..., Mapping[str, object]] | None = None,
) -> None:
    """POST one bounded private report and require its exact acknowledgement."""
    requester = request_json_fn or _default_request_json
    payload = build_private_spot_scope_payload(
        preview, source_run_id=source_run_id, source_sha=source_sha
    )
    try:
        acknowledgement = requester(
            PRIVATE_SCOPE_CONSOLE_URL, token, payload=payload
        )
        expected_keys = {"ok", "observed_at", "source_run_id", "asset_count"}
        if (
            set(acknowledgement) != expected_keys
            or acknowledgement.get("ok") is not True
            or type(acknowledgement.get("observed_at")) is not str
            or acknowledgement["observed_at"] != payload["observed_at"]
            or type(acknowledgement.get("source_run_id")) is not str
            or acknowledgement["source_run_id"] != source_run_id
            or type(acknowledgement.get("asset_count")) is not int
            or acknowledgement["asset_count"] != len(payload["assets"])
        ):
            raise ValueError("private scope acknowledgement mismatch")
    except Exception:
        raise MigrationApplyUncertain("private_scope_publication_unknown") from None


def audit_ledger(
    refs,
    *,
    client,
    expected,
    now,
    configured_symbols: tuple[str, ...],
    hooks: MigrationHooks | None = None,
) -> dict[str, object]:
    """Compare two distinct balance bases without granting migration authority."""
    active_hooks = resolve_hooks(hooks)
    digest_fn = active_hooks.digest_fn
    read_fn = active_hooks.read_source_fn or read_source
    ledger_snapshot, ledger, control = read_fn(refs)
    # Reuse the exact reviewed control, allowing only its approved downgrade.
    if (
        not isinstance(control, Mapping)
        or digest_fn({**control, "state": "ACTIVE_LKG"})
        != active_hooks.quiesce_control_sha256
    ):
        raise MigrationBlocked("audit_recovery_source_changed")
    try:
        source = control["source"]
        original = source["original_evidence"]
        baseline = BrokerReconciliationBaselineCandidate.from_dict(
            control["candidate"]
        ).expected_digests
        recovered_expected = {
            **baseline,
            "account_scope_sha256": expected["account_scope_sha256"],
        }
        if (
            source["frozen_expected_sha256"] != digest_fn(expected)
            or original["account_scope_sha256"] != expected["account_scope_sha256"]
            or any(
                baseline[key] != original[key]
                for key in ("positions_sha256", "cash_sha256")
            )
        ):
            raise ValueError("source binding")
        start = active_hooks.datetime_cls.fromisoformat(
            original["observed_at"].replace("Z", "+00:00")
        )
        if now.tzinfo is None or start.tzinfo is None:
            raise ValueError("window")
        old = ledger["last_balance_snapshot"]
        if not isinstance(old, Mapping) or not old:
            raise ValueError("snapshot")
        assets, symbols = ledger_scope(ledger, configured_symbols=configured_symbols)
        if any(
            not isinstance(asset, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", asset)
            for asset in assets
        ):
            raise ValueError("asset")
        reset_date = (
            active_hooks.datetime_cls.strptime(ledger["last_reset_date"], "%Y-%m-%d")
            .date()
            .isoformat()
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        raise MigrationBlocked("audit_source_invalid") from None
    post_rebase_source = None
    if "accounting_rebase" in ledger:
        scope_source_fn = active_hooks.validated_private_scope_source_fn or (
            lambda r, *, initial_source=None: validated_private_scope_source(
                r, initial_source=initial_source, hooks=active_hooks
            )
        )
        post_rebase_source = scope_source_fn(
            refs, initial_source=(ledger_snapshot, ledger, control)
        )
        start = post_rebase_source["material"]["opening_balance_observed_at"]
    if not start < now or now - start > timedelta(days=7):
        raise MigrationBlocked("audit_source_invalid")
    account = client.get_account()
    observations = active_hooks.collect_observations_fn(
        client,
        strategy_symbols=symbols,
        local_execution_ledger=ledger,
        now=now,
        lookback=now - start,
        account_snapshot=account,
    )
    if digest_fn(observations.account_scope) != expected["account_scope_sha256"]:
        raise MigrationBlocked("account_scope_unverified")
    balances = strict_balance_snapshot(client, account["balances"], assets)
    comparison = {
        asset: "MISSING_IN_LEDGER"
        if asset not in old
        else "MATCH"
        if _same_balance(old[asset], balances[asset], asset=asset)
        else "MISMATCH"
        for asset in sorted(assets)
    }
    execution_counts = {}
    for trade in observations.recent_executions:
        symbol = trade.get("symbol")
        if symbol not in symbols:
            raise MigrationBlocked("audit_execution_symbol_invalid")
        execution_counts[symbol] = execution_counts.get(symbol, 0) + 1
    btc_spot = next(
        float(Decimal(row["free"]) + Decimal(row["locked"]))
        for row in account["balances"]
        if row["asset"] == "BTC"
    )
    btc_change = "UNKNOWN"
    if "BTC" in old:
        btc_change = (
            "UNCHANGED"
            if _same_balance(old["BTC"], balances["BTC"], asset="BTC")
            else "INCREASE"
            if balances["BTC"] > _finite(old["BTC"])
            else "DECREASE"
        )
    history = active_hooks.diagnose_flows_fn(
        client,
        start=start,
        end=now,
        now=now,
        account=account,
        expected_digests=recovered_expected,
        reward_quantity_changes={
            asset: Decimal(str(balances[asset])) - Decimal(str(old[asset]))
            for asset in assets
            if asset in old
        },
    )
    if history.get("history_complete_for_requested_surfaces") is not True:
        raise MigrationBlocked("audit_history_incomplete")
    bnb_wallet_activity = None
    if comparison.get("BNB") == "MISMATCH":
        bnb_wallet_activity = active_hooks.diagnose_bnb_wallet_fn(
            client, start=start, end=now
        )
    after_account = client.get_account()
    after_balances = strict_balance_snapshot(client, after_account["balances"], assets)
    after_snapshot, after_ledger, after_control = read_fn(refs)
    if (
        digest_fn(account["balances"]) != digest_fn(after_account["balances"])
        or any(not _same_balance(balances[a], after_balances[a], asset=a) for a in assets)
        or digest_fn(after_ledger) != digest_fn(ledger)
        or _timestamp(after_snapshot.update_time)
        != _timestamp(ledger_snapshot.update_time)
        or digest_fn(after_control) != digest_fn(control)
    ):
        raise MigrationBlocked("audit_state_changed_during_read")
    if post_rebase_source is not None:
        scope_source_fn = active_hooks.validated_private_scope_source_fn or (
            lambda r, *, initial_source=None: validated_private_scope_source(
                r, initial_source=initial_source, hooks=active_hooks
            )
        )
        if scope_source_fn(refs)["binding"] != post_rebase_source["binding"]:
            raise MigrationBlocked("audit_state_changed_during_read")
    return {
        "status": "audited",
        "stage": "accounting_ledger_audit",
        "observed_at": now.isoformat(),
        "ledger_update_time": _timestamp(ledger_snapshot.update_time),
        "ledger_last_reset_date": reset_date,
        "ledger_uses_new_accounting_basis": ledger.get("daily_trend_pnl_basis")
        == NEW_BASIS,
        "circuit_breaker_latched": ledger.get("is_circuit_broken") is True,
        "ledger_balance_scope": "managed_assets_spot_plus_flexible_earn",
        "ledger_balance_comparison": comparison,
        # Legacy snapshot stores quantities only. Document update/reset times
        # cannot establish when those quantities were observed.
        "ledger_snapshot_observation_time_available": post_rebase_source is not None,
        "missing_ledger_nonzero_assets": [
            a for a in sorted(assets) if a not in old and balances[a] > 0
        ],
        "btc_total_balance_change": btc_change,
        "btc_ledger_matches_current_spot_only": "BTC" in old
        and _same_balance(old["BTC"], btc_spot, asset="BTC"),
        "btc_has_flexible_earn_balance": balances["BTC"] > btc_spot + 1e-8,
        "bnb_has_flexible_earn_balance": balances.get("BNB", 0)
        > next(
            (
                float(Decimal(row["free"]) + Decimal(row["locked"]))
                for row in account["balances"]
                if row["asset"] == "BNB"
            ),
            0.0,
        )
        + 1e-8,
        "bnb_quantity_change": (
            "UNKNOWN"
            if "BNB" not in old
            else "INCREASE"
            if balances["BNB"] > old["BNB"]
            else "DECREASE"
            if balances["BNB"] < old["BNB"]
            else "UNCHANGED"
        ),
        "bnb_wallet_activity": bnb_wallet_activity,
        "realtime_reward_interval_alignment": "UNVERIFIED_DAILY_HISTORY_VS_INTRADAY_BALANCES",
        "history_window_basis": "approved_opening"
        if post_rebase_source
        else "legacy_recovery",
        "recent_execution_window_start": start.isoformat(),
        "recent_execution_count": len(observations.recent_executions),
        "recent_execution_counts_by_symbol": execution_counts,
        "open_order_count": len(observations.open_orders),
        "recovered_spot_window_start": start.isoformat(),
        "recovered_spot_history": history,
        "complete_balance_reconciliation": False,
        "ledger_unchanged": True,
        "no_order": True,
        "write_performed": False,
        "execution_authority_granted": False,
    }


def classify_activity_evidence(
    *, history_counts, recent_execution_count, flow_summary, earn_forward_eligible=None
) -> dict[str, object]:
    """Summarize activity by accounting category without inferring causality."""
    counts = history_counts if isinstance(history_counts, Mapping) else {}
    flows = flow_summary if isinstance(flow_summary, Mapping) else None

    def _nonnegative_int(value):
        return type(value) is int and value >= 0

    trade_count = (
        recent_execution_count if _nonnegative_int(recent_execution_count) else None
    )
    if flows is None:
        deposit_count = withdrawal_count = None
        deposit_supported = withdrawal_supported = False
        deposit_status = withdrawal_status = "unverified"
    else:
        deposit_count = flows.get("new_confirmed_deposit_count")
        withdrawal_count = flows.get("new_or_changed_withdrawal_count")
        deposit_count = deposit_count if _nonnegative_int(deposit_count) else None
        withdrawal_count = (
            withdrawal_count if _nonnegative_int(withdrawal_count) else None
        )
        unsupported = flows.get("new_unsupported_deposit_count")
        deposit_supported = deposit_count is not None and unsupported == 0
        withdrawal_supported = withdrawal_count == 0
        deposit_status = "observed" if deposit_count is not None else "unverified"
        withdrawal_status = "observed" if withdrawal_count is not None else "unverified"

    transfer_count = (
        sum(
            value
            for key, value in counts.items()
            if isinstance(key, str)
            and key.startswith("transfer_")
            and _nonnegative_int(value)
        )
        if counts
        else None
    )
    earn_count = (
        sum(
            value
            for key, value in counts.items()
            if key in {"earn_rewards", "earn_subscriptions", "earn_redemptions"}
            and _nonnegative_int(value)
        )
        if counts
        else None
    )
    categories = {
        "trade": {
            "count": trade_count,
            "status": "observed" if trade_count is not None else "unverified",
            "supported": trade_count == 0,
        },
        "deposit": {
            "count": deposit_count,
            "status": deposit_status,
            "supported": deposit_supported,
        },
        "withdrawal": {
            "count": withdrawal_count,
            "status": withdrawal_status,
            "supported": withdrawal_supported,
        },
        "internal_transfer": {
            "count": transfer_count,
            "status": "observed" if transfer_count is not None else "unverified",
            "supported": transfer_count == 0,
        },
        "earn": {
            "count": earn_count,
            "status": (
                "forward_eligible"
                if earn_count and earn_forward_eligible is True
                else "observed"
                if earn_count is not None
                else "unverified"
            ),
            "supported": earn_count == 0
            or (earn_count is not None and earn_forward_eligible is True),
        },
    }
    return {
        **categories,
        "all_supported": all(item["supported"] for item in categories.values()),
    }


def preview_external_cash_flow(
    refs,
    *,
    client,
    expected,
    now,
    configured_symbols: tuple[str, ...],
    initial_source=None,
    hooks: MigrationHooks | None = None,
) -> dict[str, object]:
    """Replay the production deposit consumer on a copy, without ledger writes."""
    active_hooks = resolve_hooks(hooks)
    read_fn = active_hooks.read_source_fn or read_source
    snapshot, ledger, control = initial_source or read_fn(refs)
    assets, _ = ledger_scope(ledger, configured_symbols=configured_symbols)
    if len(assets) > 32 or any(
        not isinstance(a, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", a) for a in assets
    ):
        raise MigrationBlocked("cash_flow_preview_scope_invalid")
    account = client.get_account()
    if active_hooks.private_spot_account_fn is not None:
        _, spot = active_hooks.private_spot_account_fn(
            account, expected_account_scope_sha256=expected["account_scope_sha256"]
        )
    else:
        _, spot = _private_spot_account(
            account,
            expected_account_scope_sha256=expected["account_scope_sha256"],
            digest_fn=active_hooks.digest_fn,
        )
    balances = strict_balance_snapshot(client, account["balances"], assets)
    try:
        flows = active_hooks.collect_cash_flows_fn(
            client, now=now, cursor=copy.deepcopy(ledger.get("external_cash_flow_cursor"))
        )
    except ValueError as exc:
        safe_codes = {
            "external_cash_flow_cursor_invalid",
            "external_cash_flow_history_read_failed",
            "external_cash_flow_history_incomplete",
            "external_cash_flow_record_invalid",
            "external_cash_flow_record_changed",
            "external_cash_flow_cursor_capacity_exceeded",
            "external_cash_flow_window_invalid",
        }
        reason = str(exc) if str(exc) in safe_codes else "cash_flow_preview_read_failed"
        raise MigrationBlocked(reason) from None
    history = active_hooks.diagnose_flows_fn(
        client,
        start=now - timedelta(days=7),
        end=now,
        now=now,
        account=account,
        expected_digests=expected,
    )
    recent_execution_count = None
    try:
        observations = active_hooks.collect_observations_fn(
            client,
            strategy_symbols=configured_symbols,
            local_execution_ledger=ledger,
            now=now,
            lookback=timedelta(days=7),
            account_snapshot=account,
        )
        recent_execution_count = len(observations.recent_executions)
    except (BinanceReconciliationReadError, ValueError, TypeError, KeyError):
        # The category remains explicitly unverified; do not infer zero trades.
        pass
    earn_diagnosis = None
    try:
        earn_diagnosis = diagnose_earn_forward(
            refs,
            client=client,
            expected=expected,
            now=now,
            configured_symbols=configured_symbols,
            hooks=active_hooks,
        )
    except (MigrationBlocked, ValueError, TypeError, KeyError):
        # Earn remains observed but unsupported unless its dedicated proof passes.
        pass
    activity = classify_activity_evidence(
        history_counts=history.get("history_counts")
        if isinstance(history, Mapping)
        else None,
        recent_execution_count=recent_execution_count,
        flow_summary=flows,
        earn_forward_eligible=(
            earn_diagnosis.get("forward_accounting_eligible")
            if isinstance(earn_diagnosis, Mapping)
            else None
        ),
    )
    after_account = client.get_account()
    if active_hooks.private_spot_account_fn is not None:
        _, after_spot = active_hooks.private_spot_account_fn(
            after_account, expected_account_scope_sha256=expected["account_scope_sha256"]
        )
    else:
        _, after_spot = _private_spot_account(
            after_account,
            expected_account_scope_sha256=expected["account_scope_sha256"],
            digest_fn=active_hooks.digest_fn,
        )
    after_balances = strict_balance_snapshot(client, after_account["balances"], assets)
    after_snapshot, after_ledger, after_control = read_fn(refs)
    if (
        spot != after_spot
        or balances != after_balances
        or ledger != after_ledger
        or control != after_control
        or snapshot.update_time != after_snapshot.update_time
    ):
        raise MigrationBlocked("cash_flow_preview_state_changed")
    result = {
        "stage": "external_cash_flow_preview",
        "observed_at": now.isoformat(),
        "cash_flow_history_read": True,
        "cursor_present": ledger.get("external_cash_flow_cursor") is not None,
        "new_confirmed_deposit_count": flows["new_confirmed_deposit_count"],
        "activity_by_type": activity,
        "activity_history_complete": history.get("history_complete_for_requested_surfaces")
        is True,
        "activity_history_reason_code": history.get("reason_code"),
        "earn_forward_accounting_eligible": (
            earn_diagnosis.get("forward_accounting_eligible")
            if isinstance(earn_diagnosis, Mapping)
            else False
        ),
        "unmanaged_spot_asset_count": sum(
            1 for a, (free, locked) in spot if a not in assets and free + locked > 0
        ),
        "complete_balance_reconciliation": False,
        "write_performed": False,
        "ledger_unchanged": True,
        "no_order": True,
        "execution_authority_granted": False,
    }
    state, report = copy.deepcopy(ledger), {}
    try:
        reconciled = active_hooks.maybe_rebase_daily_state_fn(
            state,
            SimpleNamespace(client=client, now_utc=now),
            report,
            None,
            None,
            balances,
            [],
            collect_external_cash_flows_fn=lambda *_args, **_kwargs: copy.deepcopy(flows),
            runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
            append_log_fn=lambda *_args: None,
            translate_fn=lambda key, **_kwargs: key,
        )
    except ExecutionIntegrityError:
        diagnostics = report.get("diagnostics", {})
        reason = diagnostics.get("external_cash_flow", {}).get(
            "reason_code"
        ) or diagnostics.get("balance_change", {}).get("reason_code")
        safe_codes = {
            "external_cash_flow_cursor_missing",
            "external_cash_flow_late_completion",
            "external_cash_flow_balance_mismatch",
            "external_withdrawal_accounting_unsupported",
            "external_cash_flow_scope_unsupported",
        }
        return {
            **result,
            "status": "blocked",
            "reason_code": reason
            if reason in safe_codes
            else "cash_flow_preview_unverifiable",
        }
    return {
        **result,
        "status": "reconciled_preview"
        if reconciled
        else "baseline_preview"
        if flows["bootstrap"]
        else "no_new_deposit",
    }
