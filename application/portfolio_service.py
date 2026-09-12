"""Application-level portfolio and daily-state helpers for BinancePlatform."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from runtime_support import ExecutionIntegrityError
from application.broker_reconciliation import collect_spot_usdt_external_cash_flows


_TREND_PNL_BASIS = "trend_mark_plus_cash_flow_v1"


def compute_portfolio_allocation(
    runtime_trend_universe,
    balances,
    prices,
    u_total,
    fuel_val,
    *,
    compute_allocation_budgets_fn,
):
    trend_val = sum(balances[symbol] * prices[symbol] for symbol in runtime_trend_universe)
    dca_val = balances["BTCUSDT"] * prices["BTCUSDT"]
    total_equity = u_total + fuel_val + trend_val + dca_val
    allocation = compute_allocation_budgets_fn(total_equity, u_total, trend_val, dca_val)
    allocation.update(
        {
            "trend_val": trend_val,
            "dca_val": dca_val,
            "total_equity": total_equity,
        }
    )
    return allocation


def build_balance_snapshot(runtime_trend_universe, balances, u_total):
    snapshot = {
        "USDT": round(float(u_total), 8),
        "BTC": round(float(balances.get("BTCUSDT", 0.0)), 8),
    }
    for symbol, config in runtime_trend_universe.items():
        snapshot[str(config["base_asset"])] = round(float(balances.get(symbol, 0.0)), 8)
    if "BNBUSDT" in balances:
        snapshot["BNB"] = round(float(balances["BNBUSDT"]), 8)
    return snapshot


def maybe_rebase_daily_state_for_balance_change(
    state,
    runtime,
    report,
    total_equity,
    trend_val_equity,
    current_balance_snapshot,
    log_buffer,
    *,
    collect_external_cash_flows_fn=collect_spot_usdt_external_cash_flows,
    runtime_set_trade_state_fn,
    append_log_fn,
    translate_fn,
):
    if "earn_accrual_checkpoint" in state:
        from application.earn_accrual import prepare_forward_earn_state, _time
        try:
            current = runtime.earn_accrual_observation
            if {a: round(float(r['quantity']), 8) for a, r in current['assets'].items()} != current_balance_snapshot:
                raise ValueError('earn_valuation_snapshot_mismatch')
            cash = collect_external_cash_flows_fn(runtime.client, now=_time(current['observed_at']),
                                                  cursor=state.get('external_cash_flow_cursor'))
            updated = prepare_forward_earn_state(state, current, cash)
        except Exception:
            raise ExecutionIntegrityError("earn_forward_accounting_unverified") from None
        runtime_set_trade_state_fn(runtime, report, updated, reason="earn_forward_accounting")
        state.clear()
        state.update(updated)
        runtime.trade_state = state
        report.setdefault("diagnostics", {})["earn_accrual"] = {"status": "reconciled"}
        return True

    previous_snapshot = state.get("last_balance_snapshot")
    initializing = not isinstance(previous_snapshot, dict) or not previous_snapshot
    if initializing:
        previous_snapshot = dict(current_balance_snapshot)

    changed_assets = []
    for asset in sorted(set(previous_snapshot) | set(current_balance_snapshot)):
        try:
            previous_value = float(previous_snapshot.get(asset, 0.0) or 0.0)
            current_value = float(current_balance_snapshot.get(asset, 0.0) or 0.0)
        except (TypeError, ValueError):
            raise ExecutionIntegrityError("balance_change_unexplained") from None
        if not math.isfinite(previous_value) or not math.isfinite(current_value):
            raise ExecutionIntegrityError("balance_change_unexplained") from None
        tolerance = 1e-4 if asset == "USDT" else 1e-8
        if abs(current_value - previous_value) > tolerance:
            changed_assets.append(asset)

    cursor = state.get("external_cash_flow_cursor")
    if initializing and cursor is not None:
        raise ExecutionIntegrityError("external_cash_flow_reconciliation_uncertain")
    if cursor is None and changed_assets:
        report.setdefault("diagnostics", {})["balance_change"] = {
            "status": "unexplained",
            "assets": changed_assets,
            "reason_code": "external_cash_flow_cursor_missing",
        }
        append_log_fn(log_buffer, translate_fn("balance_change_unexplained", assets=", ".join(changed_assets)))
        raise ExecutionIntegrityError("balance_change_unexplained")

    try:
        cash_flows = collect_external_cash_flows_fn(
            runtime.client,
            now=runtime.now_utc,
            cursor=cursor,
        )
    except ValueError as exc:
        reason_code = str(exc)
        if not reason_code.startswith("external_"):
            reason_code = "external_cash_flow_reconciliation_uncertain"
        report.setdefault("diagnostics", {})["external_cash_flow"] = {
            "status": "blocked",
            "reason_code": reason_code,
        }
        raise ExecutionIntegrityError("external_cash_flow_reconciliation_uncertain") from None

    try:
        principal = Decimal(str(cash_flows["new_deposit_principal_usdt"]))
        completed_at = [
            datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
            for value in cash_flows["new_deposit_completed_at"]
        ]
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise ExecutionIntegrityError("external_cash_flow_reconciliation_uncertain") from None
    if (
        not principal.is_finite()
        or principal < 0
        or type(cash_flows.get("new_confirmed_deposit_count")) is not int
        or type(cash_flows.get("new_unsupported_deposit_count", 0)) is not int
        or type(cash_flows.get("new_or_changed_withdrawal_count", 0)) is not int
        or cash_flows["new_confirmed_deposit_count"] != len(completed_at)
        or (principal == 0) != (not completed_at)
        or not isinstance(cash_flows.get("cursor"), dict)
    ):
        raise ExecutionIntegrityError("external_cash_flow_reconciliation_uncertain")

    if principal:
        now_utc = runtime.now_utc.astimezone(timezone.utc)
        if any(value.date() != now_utc.date() for value in completed_at):
            report.setdefault("diagnostics", {})["external_cash_flow"] = {
                "status": "blocked",
                "reason_code": "external_cash_flow_late_completion",
            }
            raise ExecutionIntegrityError("external_cash_flow_reconciliation_uncertain")
        try:
            observed_delta = Decimal(str(current_balance_snapshot.get("USDT", 0.0))) - Decimal(
                str(previous_snapshot.get("USDT", 0.0))
            )
        except (InvalidOperation, TypeError, ValueError):
            raise ExecutionIntegrityError("external_cash_flow_reconciliation_uncertain") from None
        if (
            changed_assets != ["USDT"]
            or cash_flows.get("new_unsupported_deposit_count", 0)
            or cash_flows.get("new_or_changed_withdrawal_count", 0)
            or abs(observed_delta - principal) > Decimal("0.00000001")
        ):
            report.setdefault("diagnostics", {})["external_cash_flow"] = {
                "status": "blocked",
                "reason_code": "external_cash_flow_balance_mismatch",
            }
            raise ExecutionIntegrityError("external_cash_flow_reconciliation_uncertain")
        principal_applies_to_current_day = state.get("last_reset_date") == now_utc.strftime("%Y-%m-%d")
        if principal_applies_to_current_day:
            try:
                accumulated = Decimal(str(state.get("daily_external_principal_usdt", 0.0))) + principal
            except (InvalidOperation, TypeError, ValueError):
                raise ExecutionIntegrityError("daily_accounting_unverifiable") from None
            if not accumulated.is_finite():
                raise ExecutionIntegrityError("daily_accounting_unverifiable")
            state["daily_external_principal_usdt"] = float(accumulated)
        state["last_balance_snapshot"] = dict(current_balance_snapshot)
        state["external_cash_flow_cursor"] = cash_flows["cursor"]
        report.setdefault("diagnostics", {})["external_cash_flow"] = {
            "status": "reconciled",
            "confirmed_deposit_count": cash_flows["new_confirmed_deposit_count"],
            "principal_applied_to_current_day": principal_applies_to_current_day,
        }
        runtime_set_trade_state_fn(runtime, report, state, reason="external_cash_flow_reconciliation")
        return True

    if not changed_assets:
        if initializing:
            state["last_balance_snapshot"] = dict(current_balance_snapshot)
        if initializing or cash_flows["cursor"] != cursor:
            state["external_cash_flow_cursor"] = cash_flows["cursor"]
            reason = "external_cash_flow_baseline" if initializing else "external_cash_flow_cursor"
            runtime_set_trade_state_fn(runtime, report, state, reason=reason)
        return False

    unsupported_reason = None
    if cash_flows.get("new_or_changed_withdrawal_count", 0):
        unsupported_reason = "external_withdrawal_accounting_unsupported"
    elif cash_flows.get("new_unsupported_deposit_count", 0):
        unsupported_reason = "external_cash_flow_scope_unsupported"
    if unsupported_reason:
        report.setdefault("diagnostics", {})["external_cash_flow"] = {
            "status": "blocked",
            "reason_code": unsupported_reason,
        }
    report.setdefault("diagnostics", {})["balance_change"] = {
        "status": "unexplained",
        "assets": changed_assets,
    }
    append_log_fn(
        log_buffer,
        translate_fn("balance_change_unexplained", assets=", ".join(changed_assets)),
    )
    raise ExecutionIntegrityError("balance_change_unexplained")


def maybe_reset_daily_state(
    state,
    runtime,
    report,
    today_utc,
    total_equity,
    trend_val_equity,
    *,
    runtime_set_trade_state_fn,
):
    desired_basis = _TREND_PNL_BASIS
    last_reset_date = state.get("last_reset_date")
    pnl_basis = state.get("daily_trend_pnl_basis")

    if last_reset_date != today_utc:
        state.update(
            {
                "daily_equity_base": total_equity,
                "daily_trend_equity_base": trend_val_equity,
                "daily_trend_pnl_basis": desired_basis,
                "daily_trend_cash_flow_usdt": 0.0,
                "daily_trend_net_invested_usdt": 0.0,
                "daily_trend_risk_base_usdt": max(0.0, float(trend_val_equity)),
                "daily_trend_third_fee_usdt": 0.0,
                "daily_external_principal_usdt": 0.0,
                "last_reset_date": today_utc,
                "is_circuit_broken": False,
            }
        )
        runtime_set_trade_state_fn(runtime, report, state, reason="daily_reset")
        return True

    if pnl_basis != desired_basis:
        report.setdefault("diagnostics", {})["daily_trend_accounting"] = {
            "status": "migration_unverifiable",
            "from_basis": pnl_basis,
            "to_basis": desired_basis,
        }
        raise ExecutionIntegrityError("daily_trend_accounting_migration_unverifiable")

    accounting_fields = (
        "daily_trend_cash_flow_usdt",
        "daily_trend_net_invested_usdt",
        "daily_trend_risk_base_usdt",
        "daily_trend_third_fee_usdt",
    )
    try:
        accounting_values = {field: float(state[field]) for field in accounting_fields}
    except (KeyError, TypeError, ValueError):
        raise ExecutionIntegrityError("daily_trend_accounting_unverifiable") from None
    if not all(math.isfinite(value) for value in accounting_values.values()) or accounting_values[
        "daily_trend_risk_base_usdt"
    ] < 0:
        raise ExecutionIntegrityError("daily_trend_accounting_unverifiable")
    try:
        trend_activity_values = (
            float(trend_val_equity),
            float(state.get("daily_trend_equity_base", 0.0) or 0.0),
            accounting_values["daily_trend_cash_flow_usdt"],
            accounting_values["daily_trend_net_invested_usdt"],
            accounting_values["daily_trend_third_fee_usdt"],
        )
    except (TypeError, ValueError):
        raise ExecutionIntegrityError("daily_trend_accounting_unverifiable") from None
    if not all(math.isfinite(value) for value in trend_activity_values) or (
        accounting_values["daily_trend_risk_base_usdt"] <= 0
        and any(value != 0 for value in trend_activity_values)
    ):
        raise ExecutionIntegrityError("daily_trend_accounting_unverifiable")
    return False


def compute_daily_pnls(state, total_equity, trend_equity):
    numeric_values = (
        total_equity,
        trend_equity,
        state.get("daily_equity_base", 0.0),
        state.get("daily_trend_equity_base", 0.0),
        state.get("daily_trend_cash_flow_usdt", 0.0),
        state.get("daily_trend_net_invested_usdt", 0.0),
        state.get("daily_trend_third_fee_usdt", 0.0),
        state.get("daily_trend_risk_base_usdt", state.get("daily_trend_equity_base", 0.0)),
        state.get("daily_external_principal_usdt", 0.0),
    )
    try:
        if not all(math.isfinite(float(value)) for value in numeric_values):
            raise ValueError
    except (TypeError, ValueError):
        raise ExecutionIntegrityError("daily_trend_accounting_unverifiable") from None
    daily_pnl = (
        (
            total_equity
            - state["daily_equity_base"]
            - float(state.get("daily_external_principal_usdt", 0.0) or 0.0)
        ) / state["daily_equity_base"]
        if state.get("daily_equity_base", 0) > 0
        else 0.0
    )
    trend_base = float(state.get("daily_trend_equity_base", 0.0) or 0.0)
    trend_value = (
        float(trend_equity)
        + float(state.get("daily_trend_cash_flow_usdt", 0.0) or 0.0)
        - float(state.get("daily_trend_third_fee_usdt", 0.0) or 0.0)
    )
    trend_risk_base = float(state.get("daily_trend_risk_base_usdt", trend_base) or 0.0)
    trend_activity_values = (
        trend_base,
        float(trend_equity),
        float(state.get("daily_trend_cash_flow_usdt", 0.0) or 0.0),
        float(state.get("daily_trend_net_invested_usdt", 0.0) or 0.0),
        float(state.get("daily_trend_third_fee_usdt", 0.0) or 0.0),
    )
    if trend_risk_base <= 0:
        if any(value != 0 for value in trend_activity_values):
            raise ExecutionIntegrityError("daily_trend_accounting_unverifiable")
        trend_daily_pnl = 0.0
    else:
        trend_daily_pnl = (trend_value - trend_base) / trend_risk_base
    return daily_pnl, trend_daily_pnl


def append_portfolio_report(
    log_buffer,
    allocation,
    fuel_val,
    daily_pnl,
    trend_daily_pnl,
    btc_snapshot,
    *,
    append_portfolio_report_fn,
    append_log_fn,
    translate_fn,
    separator,
):
    return append_portfolio_report_fn(
        log_buffer,
        allocation,
        fuel_val,
        daily_pnl,
        trend_daily_pnl,
        btc_snapshot,
        append_log_fn=append_log_fn,
        translate_fn=translate_fn,
        separator=separator,
    )
