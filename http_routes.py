"""CLI entry, health monitor, and cycle dispatch."""


# ruff: noqa: F403, F405
from __future__ import annotations


import os
import sys
import traceback

import notifications_runtime as nr
import runtime_wiring as rw
from runtime_wiring import *  # noqa: F403, F401
from entrypoints.cli import run_cli_entrypoint

def execute_cycle(runtime, *, validation_scope: str = "standard", btc_attribution_evidence=None):
    # B09 PR-3: cycle-scoped universe + strategy handle on RunContext.
    # Bind-time seed may copy module defaults once; while bound, resolves never
    # fall back to module STRATEGY_RUNTIME / TREND_UNIVERSE.
    mounted_strategy = getattr(runtime, "strategy_runtime", None) or STRATEGY_RUNTIME
    if mounted_strategy is None:
        raise RuntimeError("execute_cycle_missing_strategy_runtime")
    btc_validation = validation_scope == "full_cycle_no_submit" and runtime.strategy_profile == "crypto_btc_dca"
    if btc_attribution_evidence is not None and not btc_validation:
        raise ValueError("btc_validation_scope_invalid")
    if btc_validation and (runtime.dry_run is not True
                           or runtime.standard_execution_permitted is not False
                           or getattr(runtime.runtime_target, "dry_run_only", None) is not True):
        raise ValueError("btc_validation_scope_invalid")

    def record_btc_validation_proposal(
        _runtime, report, _state, _balances, _prices, u_total, _total_equity,
        dca_usdt_pool, _dca_val, _btc_snapshot, _btc_target_ratio,
        _btc_base_order_usdt, _today_id_str, _log_buffer,
    ):
        proposal = btc_cycle_inputs.get("exit_proposal")
        if proposal is not None:
            report["btc_dca_intents"].append(dict(proposal))
        elif dca_usdt_pool > 0:
            report["btc_dca_intents"].append({
                "action": "buy", "symbol": "BTCUSDT", "budget": dca_usdt_pool,
                "proposal_only": True,
            })
        return u_total

    btc_cycle_inputs = {}
    btc_balance_parts = {}
    usdt_balance_parts = {}
    if btc_attribution_evidence is not None:
        from copy import deepcopy
        btc_attribution_evidence = deepcopy(btc_attribution_evidence)

    def compute_btc_validation_allocation(*args):
        return _compute_portfolio_allocation(
            *args, validation_scope=validation_scope, btc_input_cache=btc_cycle_inputs,
            btc_attribution_evidence=btc_attribution_evidence, btc_balance_parts=btc_balance_parts,
            usdt_balance_parts=usdt_balance_parts,
        )

    def execute_btc_validation_trend(*args, **kwargs):
        return _execute_trend_rotation(*args, **kwargs, btc_input_cache=btc_cycle_inputs)

    ctx = RunContext(
        trend_universe={symbol: meta.copy() for symbol, meta in TREND_UNIVERSE.items()},
        strategy_runtime=mounted_strategy,
    )
    runtime.run_context = ctx
    token = bind_run_context(ctx)
    try:
        return execute_strategy_cycle(
            runtime,
            build_execution_report=build_execution_report,
            ensure_runtime_client=_ensure_runtime_client,
            load_cycle_execution_settings=(
                rc_load_cycle_execution_settings if validation_scope == "standard"
                else lambda: rc_load_cycle_execution_settings(validation_scope=validation_scope)
            ),
            load_cycle_state=_load_cycle_state,
            append_trend_pool_source_logs=_append_trend_pool_source_logs,
            capture_market_snapshot=(_capture_market_snapshot if validation_scope == "standard"
                                     else lambda *args: _capture_market_snapshot(
                                         *args, validation_scope=validation_scope,
                                         **({"btc_balance_parts": btc_balance_parts, "usdt_balance_parts": usdt_balance_parts}
                                            if btc_validation else {}))),
            top_up_bnb_fuel=_top_up_bnb_fuel,
            compute_portfolio_allocation=(
                compute_btc_validation_allocation if btc_validation else _compute_portfolio_allocation
            ),
            build_balance_snapshot=_build_balance_snapshot,
            maybe_reset_daily_state=_maybe_reset_daily_state,
            maybe_rebase_daily_state_for_balance_change=_maybe_rebase_daily_state_for_balance_change,
            compute_daily_pnls=_compute_daily_pnls,
            append_portfolio_report=_append_portfolio_report,
            run_daily_circuit_breaker=_run_daily_circuit_breaker,
            execute_trend_rotation=(execute_btc_validation_trend if btc_validation else _execute_trend_rotation),
            execute_btc_dca_cycle=(record_btc_validation_proposal if btc_validation else _execute_btc_dca_cycle),
            manage_usdt_earn_buffer_runtime=manage_usdt_earn_buffer_runtime,
            maybe_send_periodic_btc_status_report=maybe_send_periodic_btc_status_report,
            runtime_set_trade_state=runtime_set_trade_state,
            append_report_error=append_report_error,
            runtime_notify=runtime_notify,
            translate_fn=t,
            traceback_module=traceback,
            **({"validation_scope": validation_scope} if btc_validation else {}),
        )
    finally:
        reset_run_context(token)
def main():
    # Unified health monitor — starts HTTP server + auto-detects backend
    try:
        from quant_platform_kit.common.health import HealthMonitor
        monitor = HealthMonitor(http_port=int(os.environ.get("HEALTH_PORT", "8080")))
        monitor.start()
        import builtins
        builtins.__dict__["_qsl_health_monitor"] = monitor
        print(t("runtime_health_monitor_started"), flush=True)
    except Exception:
        pass

    try:
        return run_cli_entrypoint(
            runtime_builder=build_live_runtime,
            execute_cycle=execute_cycle,
            output_printer=print,
            exit_fn=sys.exit,
        )
    except Exception as exc:
        reason = _runtime_setup_reason(exc)
        print(t("runtime_setup_failed"))
        frames = traceback.extract_tb(exc.__traceback__)
        stage = "runtime_setup"
        for frame in frames:
            if frame.filename.endswith("runtime_config_support.py"):
                stage = "runtime_config"
            elif frame.filename.endswith(("strategy_loader.py", "strategy_runtime.py", "strategy_registry.py")):
                stage = "strategy_load"
        error_type = type(exc).__name__ if type(exc) in {ValueError, KeyError, TypeError, OSError, RuntimeError} else "RuntimeError"
        print(f"runtime_setup_failed stage={stage} error_type={error_type} reason_code={reason}")
        if _notify_runtime_error(RuntimeError(reason)):
            output_path = os.getenv("GITHUB_OUTPUT")
            if output_path:
                with open(output_path, "a", encoding="utf-8") as output:
                    output.write("runtime_failure_notified=true\n")
        raise RuntimeError("runtime_setup_failed") from None
if __name__ == "__main__":
    main()
