"""Application-level cycle execution helpers for BinancePlatform."""

from __future__ import annotations

import json
import os

from quant_platform_kit.common.runtime_reports import persist_runtime_report
from runtime_logging import RuntimeLogContext, emit_runtime_log


def execute_strategy_cycle(
    runtime,
    *,
    build_execution_report,
    ensure_runtime_client,
    load_cycle_execution_settings,
    load_cycle_state,
    append_trend_pool_source_logs,
    capture_market_snapshot,
    compute_portfolio_allocation,
    build_balance_snapshot,
    maybe_reset_daily_state,
    maybe_rebase_daily_state_for_balance_change,
    compute_daily_pnls,
    append_portfolio_report,
    run_daily_circuit_breaker,
    execute_trend_rotation,
    execute_btc_dca_cycle,
    manage_usdt_earn_buffer_runtime,
    maybe_send_periodic_btc_status_report,
    runtime_set_trade_state,
    append_report_error,
    runtime_notify,
    translate_fn,
    traceback_module,
):
    circuit_breaker_pct = -0.05
    min_bnb_value, buy_bnb_amount = 10.0, 15.0
    cycle_settings = load_cycle_execution_settings()
    btc_status_report_interval_hours = cycle_settings.btc_status_report_interval_hours
    allow_new_trend_entries_on_degraded = cycle_settings.allow_new_trend_entries_on_degraded

    report = build_execution_report(runtime)
    log_buffer = []

    try:
        if not ensure_runtime_client(runtime, report):
            return report

        cycle_state = load_cycle_state(runtime, report, allow_new_trend_entries_on_degraded)
        if cycle_state is None:
            return report

        state, trend_pool_resolution, runtime_trend_universe, allow_new_trend_entries = cycle_state
        append_trend_pool_source_logs(log_buffer, trend_pool_resolution, allow_new_trend_entries)

        report["upstream_pool_symbols"] = list(runtime_trend_universe.keys())
        if trend_pool_resolution["degraded"]:
            report["degraded_mode_level"] = trend_pool_resolution.get("source", "unknown")

        market_snapshot = capture_market_snapshot(
            runtime,
            report,
            runtime_trend_universe,
            log_buffer,
            min_bnb_value,
            buy_bnb_amount,
        )
        u_total = market_snapshot["u_total"]
        fuel_val = market_snapshot["fuel_val"]
        dynamic_usdt_buffer = market_snapshot["dynamic_usdt_buffer"]
        prices = market_snapshot["prices"]
        balances = market_snapshot["balances"]
        btc_snapshot = market_snapshot["btc_snapshot"]
        trend_indicators = market_snapshot["trend_indicators"]

        allocation = compute_portfolio_allocation(
            runtime,
            runtime_trend_universe,
            balances,
            prices,
            u_total,
            fuel_val,
            state,
            trend_indicators,
            btc_snapshot,
        )
        total_equity = allocation["total_equity"]
        trend_val_equity = allocation["trend_val"]

        report["total_equity_usdt"] = total_equity
        report["trend_equity_usdt"] = trend_val_equity

        now_utc = runtime.now_utc
        today_utc = now_utc.strftime("%Y-%m-%d")
        today_id_str = now_utc.strftime("%Y%m%d")
        current_balance_snapshot = build_balance_snapshot(runtime_trend_universe, balances, u_total)

        maybe_reset_daily_state(state, runtime, report, today_utc, total_equity, trend_val_equity)
        maybe_rebase_daily_state_for_balance_change(
            state,
            runtime,
            report,
            total_equity,
            trend_val_equity,
            current_balance_snapshot,
            log_buffer,
        )
        daily_pnl, trend_daily_pnl = compute_daily_pnls(state, total_equity, trend_val_equity)
        append_portfolio_report(log_buffer, allocation, fuel_val, daily_pnl, trend_daily_pnl, btc_snapshot)

        if state.get("is_circuit_broken"):
            log_buffer.insert(0, translate_fn("circuit_breaker_latched_line", total_equity=total_equity))
            return report

        if run_daily_circuit_breaker(
            runtime,
            report,
            state,
            runtime_trend_universe,
            balances,
            u_total,
            prices,
            trend_daily_pnl,
            circuit_breaker_pct,
            log_buffer,
        ):
            return report

        u_total = execute_trend_rotation(
            runtime,
            report,
            state,
            runtime_trend_universe,
            trend_indicators,
            btc_snapshot,
            prices,
            balances,
            u_total,
            fuel_val,
            log_buffer,
            today_id_str,
            allow_new_trend_entries,
            allow_pool_refresh=not trend_pool_resolution["degraded"],
        )

        post_trade_allocation = compute_portfolio_allocation(
            runtime,
            runtime_trend_universe,
            balances,
            prices,
            u_total,
            fuel_val,
            state,
            trend_indicators,
            btc_snapshot,
        )
        total_equity = post_trade_allocation["total_equity"]
        trend_val_equity = post_trade_allocation["trend_val"]

        report["total_equity_usdt"] = total_equity
        report["trend_equity_usdt"] = trend_val_equity

        btc_target_ratio = post_trade_allocation["btc_target_ratio"]
        dca_usdt_pool = post_trade_allocation["dca_usdt_pool"]
        dca_val = post_trade_allocation["dca_val"]
        btc_base_order_usdt = post_trade_allocation["btc_base_order_usdt"]
        _, trend_daily_pnl = compute_daily_pnls(state, total_equity, trend_val_equity)

        u_total = execute_btc_dca_cycle(
            runtime,
            report,
            state,
            balances,
            prices,
            u_total,
            total_equity,
            dca_usdt_pool,
            dca_val,
            btc_snapshot,
            btc_target_ratio,
            btc_base_order_usdt,
            today_id_str,
            log_buffer,
        )

        manage_usdt_earn_buffer_runtime(
            runtime,
            report,
            dynamic_usdt_buffer,
            log_buffer,
            spot_free_override=u_total if runtime.dry_run else None,
        )

        maybe_send_periodic_btc_status_report(
            state,
            runtime.tg_token,
            runtime.tg_chat_id,
            now_utc,
            btc_status_report_interval_hours,
            total_equity,
            trend_val_equity,
            trend_daily_pnl,
            prices["BTCUSDT"],
            btc_snapshot,
            btc_target_ratio,
            getattr(runtime, "strategy_display_name_localized", "") or getattr(runtime, "strategy_display_name", ""),
            notifier_fn=lambda text: runtime_notify(runtime, report, text),
        )

        state["last_balance_snapshot"] = build_balance_snapshot(runtime_trend_universe, balances, u_total)
        runtime_set_trade_state(runtime, report, state, reason="cycle_complete")

    except Exception as exc:
        report["status"] = "error"
        append_report_error(report, str(exc), stage="execute_cycle")
        if runtime.print_traceback:
            traceback_module.print_exc()
        try:
            runtime_notify(runtime, report, f"{translate_fn('system_crash')}\n{str(exc)[:200]}")
        except Exception:
            pass
    finally:
        report["log_lines"] = list(log_buffer)

    return report


def write_execution_report(report, *, reports_dir="reports", filename="execution_report.json"):
    os.makedirs(reports_dir, exist_ok=True)
    output_path = os.path.join(reports_dir, filename)
    with open(output_path, "w") as handle:
        json.dump(report, handle, indent=2, default=str)
    return output_path


def run_live_cycle(
    *,
    runtime_builder,
    execute_cycle,
    output_printer=print,
    report_writer=write_execution_report,
    exit_fn=None,
):
    runtime = runtime_builder()
    log_context = RuntimeLogContext(
        platform="binance",
        deploy_target=os.getenv("LOG_DEPLOY_TARGET", "vps"),
        service_name=os.getenv("SERVICE_NAME", "binance-platform"),
        strategy_profile=str(getattr(runtime, "strategy_profile", "") or os.getenv("STRATEGY_PROFILE", "crypto_leader_rotation")),
        run_id=str(getattr(runtime, "run_id", "") or ""),
        extra_fields={
            "dry_run": bool(getattr(runtime, "dry_run", False)),
            "strategy_display_name": str(getattr(runtime, "strategy_display_name", "") or ""),
            "strategy_display_name_localized": str(getattr(runtime, "strategy_display_name_localized", "") or ""),
        },
    )
    emit_runtime_log(
        log_context,
        "strategy_cycle_started",
        message="Starting strategy execution",
        printer=output_printer,
    )
    report = execute_cycle(runtime)
    output_printer("\n".join(report.get("log_lines", [])))
    report_path = report_writer(report)
    persisted_local_path = report_path
    persisted_gcs_uri = None
    try:
        persisted = persist_runtime_report(
            report,
            output_path=report_path,
            gcs_prefix_uri=os.getenv("EXECUTION_REPORT_GCS_URI"),
            gcp_project_id=os.getenv("GCP_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT"),
        )
        persisted_local_path = persisted.local_path or report_path
        persisted_gcs_uri = persisted.gcs_uri
    except Exception as persist_exc:
        output_printer(f"failed to persist archived execution report: {persist_exc}")
    report_status = str(report.get("status", "unknown"))
    status_event = {
        "ok": "strategy_cycle_completed",
        "aborted": "strategy_cycle_aborted",
    }.get(report_status, "strategy_cycle_failed")
    emit_runtime_log(
        log_context,
        status_event,
        message="Strategy execution finished",
        severity="INFO" if report_status in {"ok", "aborted"} else "ERROR",
        printer=output_printer,
        status=report_status,
        report_path=persisted_local_path,
        report_gcs_uri=persisted_gcs_uri,
        total_equity_usdt=report.get("total_equity_usdt"),
        trend_equity_usdt=report.get("trend_equity_usdt"),
        degraded_mode_level=report.get("degraded_mode_level"),
        circuit_breaker_triggered=report.get("circuit_breaker_triggered"),
        error_count=len(report.get("error_summary", {}).get("errors", [])),
    )

    if report.get("status") != "ok" and exit_fn is not None:
        exit_fn(1)

    return report, persisted_local_path
