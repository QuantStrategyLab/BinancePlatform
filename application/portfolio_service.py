"""Application-level portfolio and daily-state helpers for BinancePlatform."""

from __future__ import annotations


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
    state["last_balance_snapshot"] = dict(current_balance_snapshot)

    if not isinstance(previous_snapshot, dict) or not previous_snapshot:
        return False

    changed_assets = []
    for asset in sorted(set(previous_snapshot) | set(current_balance_snapshot)):
        previous_value = float(previous_snapshot.get(asset, 0.0) or 0.0)
        current_value = float(current_balance_snapshot.get(asset, 0.0) or 0.0)
        tolerance = 1e-4 if asset == "USDT" else 1e-8
        if abs(current_value - previous_value) > tolerance:
            changed_assets.append(asset)

    if not changed_assets:
        return False

    state.update(
        {
            "daily_equity_base": total_equity,
            "daily_trend_equity_base": trend_val_equity,
            "daily_trend_pnl_basis": "trend_val",
        }
    )
    runtime_set_trade_state_fn(runtime, report, state, reason="external_balance_flow_rebase")
    append_log_fn(
        log_buffer,
        translate_fn("external_balance_flow_rebased", assets=", ".join(changed_assets)),
    )
    return True


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
    desired_basis = "trend_val"
    last_reset_date = state.get("last_reset_date")
    pnl_basis = state.get("daily_trend_pnl_basis")

    if last_reset_date != today_utc:
        state.update(
            {
                "daily_equity_base": total_equity,
                "daily_trend_equity_base": trend_val_equity,
                "daily_trend_pnl_basis": desired_basis,
                "last_reset_date": today_utc,
                "is_circuit_broken": False,
            }
        )
        runtime_set_trade_state_fn(runtime, report, state, reason="daily_reset")
        return

    if pnl_basis != desired_basis:
        state.update(
            {
                "daily_trend_equity_base": trend_val_equity,
                "daily_trend_pnl_basis": desired_basis,
            }
        )
        runtime_set_trade_state_fn(runtime, report, state, reason="trend_pnl_basis_migrate")


def compute_daily_pnls(state, total_equity, trend_equity):
    daily_pnl = (
        (total_equity - state["daily_equity_base"]) / state["daily_equity_base"]
        if state.get("daily_equity_base", 0) > 0
        else 0.0
    )
    trend_daily_pnl = (
        (trend_equity - state["daily_trend_equity_base"]) / state["daily_trend_equity_base"]
        if state.get("daily_trend_equity_base", 0) > 0
        else 0.0
    )
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
