"""Application-level portfolio and daily-state helpers for BinancePlatform."""

from __future__ import annotations

import math

from runtime_support import ExecutionIntegrityError


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
    runtime_set_trade_state_fn,
    append_log_fn,
    translate_fn,
):
    previous_snapshot = state.get("last_balance_snapshot")
    if not isinstance(previous_snapshot, dict) or not previous_snapshot:
        state["last_balance_snapshot"] = dict(current_balance_snapshot)
        return False

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

    if not changed_assets:
        return False

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
                "last_reset_date": today_utc,
                "is_circuit_broken": False,
            }
        )
        runtime_set_trade_state_fn(runtime, report, state, reason="daily_reset")
        return

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
    )
    try:
        if not all(math.isfinite(float(value)) for value in numeric_values):
            raise ValueError
    except (TypeError, ValueError):
        raise ExecutionIntegrityError("daily_trend_accounting_unverifiable") from None
    daily_pnl = (
        (total_equity - state["daily_equity_base"]) / state["daily_equity_base"]
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
