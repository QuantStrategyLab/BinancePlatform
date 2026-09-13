"""Application-level cycle execution helpers for BinancePlatform."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
import tempfile

from quant_platform_kit.common.runtime_reports import persist_runtime_report
from quant_platform_kit.strategy_lifecycle.performance_monitor import (
    resolve_lifecycle_stream_id,
    try_record_platform_execution,
)
from application.execution_receipt_adapter import attach_execution_receipt_from_report
from application.portfolio_service import EARN_FORWARD_REASON_CODES
from runtime_logging import RuntimeLogContext, emit_runtime_log
from runtime_support import (
    append_report_error, finalize_notification_delivery, acquire_runtime_state_owner,
    release_runtime_state_owner, reconcile_pending_funding_submission,
    reconcile_runtime_cash_effects, ExecutionIntegrityError, StatePersistenceError,
    OrderReconciliationError, ClientCallError,
)


def _record_risk_diagnostics(report, allocation):
    assessment = allocation.get("risk_assessment")
    if not isinstance(assessment, Mapping):
        return
    report["risk_assessment"] = dict(assessment)
    report["risk_outcome"] = assessment.get("outcome")
    report["risk_reason_codes"] = list(assessment.get("reason_codes") or ())
    report["risk_flags"] = list(allocation.get("risk_flags") or ())


def _settled_order_state(state):
    if not isinstance(state, Mapping):
        return None
    record = state.get("order_submission")
    if not isinstance(record, Mapping):
        return None
    status = record.get("state")
    return status if status in {"RESERVED", "TERMINAL"} else None


def _build_platform_execution_result(report, *, state_healthy, state_owner_release_uncertain):
    return {
        "platform": "binance",
        "status": report.get("status"),
        "total_equity_usdt": report.get("total_equity_usdt"),
        "trend_equity_usdt": report.get("trend_equity_usdt"),
        # The local daily-loss state excludes supported deposits, but this
        # per-cycle record has no exactly-once external-flow delivery.
        "external_cash_flow": None,
        "external_cash_flow_interval": (
            report.get("external_cash_flow_interval")
            if report.get("status") == "ok"
            and state_healthy
            and not state_owner_release_uncertain
            else None
        ),
        "degraded_mode_level": report.get("degraded_mode_level"),
        # Preserve the existing recorder payload exactly; the separate export
        # applies its own safe-field allowlist and omits this field.
        "error": report.get("error"),
    }


def _write_lifecycle_export(profile_id, execution_result):
    """Best-effort, redacted export of the current recorder payload.

    The normal PerformanceStore path remains authoritative. This optional
    file is a short-lived transport handoff for the monitor and must never
    change the cycle result or expose report details and provider errors.
    """
    raw_path = str(os.environ.get("BINANCE_LIFECYCLE_EXPORT_PATH") or "").strip()
    if not raw_path:
        return
    path = Path(raw_path)
    try:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return
        if path.parent.is_symlink():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return
        profile = str(profile_id or "").strip()
        if not profile:
            return
        stream_id = resolve_lifecycle_stream_id(execution_result=execution_result)
        safe_result = {
            key: execution_result.get(key)
            for key in (
                "platform",
                "status",
                "total_equity_usdt",
                "trend_equity_usdt",
                "external_cash_flow",
                "external_cash_flow_interval",
                "degraded_mode_level",
            )
        }
        if safe_result.get("status") != "ok":
            safe_result["error_code"] = "cycle_failed"
        payload = {
            "strategy_profile": profile,
            "domain": "crypto",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "record_kind": "execution",
            "execution_result": safe_result,
            "lifecycle_stream_id": stream_id,
            "schema_version": "strategy_lifecycle.v1",
        }
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            os.chmod(path, 0o600)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
    except Exception:
        # Monitoring transport is deliberately non-blocking for the trading
        # cycle; the authoritative recorder call remains unchanged.
        return


def execute_strategy_cycle(
    runtime,
    *,
    build_execution_report,
    ensure_runtime_client,
    load_cycle_execution_settings,
    load_cycle_state,
    append_trend_pool_source_logs,
    capture_market_snapshot,
    top_up_bnb_fuel,
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
    cycle_settings = getattr(runtime, "research_cycle_settings", None)
    if cycle_settings is not None:
        if not bool(getattr(runtime, "dry_run", False)):
            raise ValueError("research cycle settings require dry_run=True")
    else:
        cycle_settings = load_cycle_execution_settings()
    btc_status_report_interval_hours = cycle_settings.btc_status_report_interval_hours
    allow_new_trend_entries_on_degraded = cycle_settings.allow_new_trend_entries_on_degraded

    report = build_execution_report(runtime)
    log_buffer = []
    if not getattr(runtime, "standard_execution_permitted", True):
        report["execution_blocked_reason"] = "runtime_target_disabled"
        log_buffer.append(
            "Runtime target disables standard execution; monitoring continues and all order/state-write calls are suppressed."
        )

    state_healthy = False
    state_owner_release_uncertain = False
    failure_stage = "state_owner_claim"
    owner_claimed_this_cycle = False
    initial_order_state = None
    daily_state_write_intents_start = None
    daily_funding_submission_start = None
    daily_funding_side_effect_start = None
    daily_funding_intent_lengths = None
    daily_order_sequence_start = None
    cycle_funding_submission_start = None
    cycle_funding_side_effect_start = None
    cycle_order_sequence_start = None
    try:
        if not acquire_runtime_state_owner(runtime):
            report["execution_blocked_reason"] = "state_owner_busy"
            return report
        owner_claimed_this_cycle = (
            not getattr(runtime, "dry_run", False)
            and getattr(runtime, "standard_execution_permitted", True)
            and getattr(runtime, "state_owner_held", False)
        )
        receipt_observation = report.get("execution_receipt_observation", {})
        cycle_funding_submission_start = (
            receipt_observation.get("submission_attempted_count", 0)
            if isinstance(receipt_observation, Mapping)
            else 0
        )
        cycle_funding_side_effect_start = len(getattr(runtime, "side_effect_log", ()))
        cycle_order_sequence_start = getattr(runtime, "order_sequence", 0)
        failure_stage = "client_connect"
        if not ensure_runtime_client(runtime, report):
            return report

        failure_stage = "state_load"
        cycle_state = load_cycle_state(runtime, report, allow_new_trend_entries_on_degraded)
        if cycle_state is None:
            return report

        state, trend_pool_resolution, runtime_trend_universe, allow_new_trend_entries = cycle_state
        runtime.trade_state = state
        initial_order_state = _settled_order_state(state)
        failure_stage = "funding_reconciliation"
        reconcile_pending_funding_submission(runtime)
        submission_state = state.get("order_submission", {}).get("state", "RESERVED")
        if submission_state == "SUBMISSION_UNKNOWN":
            raise ExecutionIntegrityError("order_reconciliation_uncertain")
        if submission_state == "FILLED_ACCOUNTING_PENDING":
            raise ExecutionIntegrityError("filled_order_accounting_unverifiable")
        failure_stage = "pool_diagnostics"
        append_trend_pool_source_logs(log_buffer, trend_pool_resolution, allow_new_trend_entries)

        report["upstream_pool_symbols"] = list(
            trend_pool_resolution.get("symbols") or runtime_trend_universe
        )
        if trend_pool_resolution["degraded"]:
            report["degraded_mode_level"] = trend_pool_resolution.get("source_kind", "unknown")

        failure_stage = "market_snapshot"
        market_snapshot = capture_market_snapshot(
            runtime,
            report,
            runtime_trend_universe,
            log_buffer,
        )
        u_total = market_snapshot["u_total"]
        fuel_val = market_snapshot["fuel_val"]
        dynamic_usdt_buffer = market_snapshot["dynamic_usdt_buffer"]
        prices = market_snapshot["prices"]
        balances = market_snapshot["balances"]
        btc_snapshot = market_snapshot["btc_snapshot"]
        trend_indicators = market_snapshot["trend_indicators"]

        state_healthy = True
        failure_stage = "portfolio_allocation"
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
        _record_risk_diagnostics(report, allocation)

        if not allocation.get("execution_permitted", False):
            report["execution_blocked_reason"] = "risk_execution_not_permitted"
            return report

        failure_stage = "daily_state"
        daily_state_write_intents_start = len(report.get("state_write_intents", ()))
        receipt_observation = report.get("execution_receipt_observation", {})
        daily_funding_submission_start = (
            receipt_observation.get("submission_attempted_count", 0)
            if isinstance(receipt_observation, Mapping)
            else 0
        )
        daily_funding_side_effect_start = len(getattr(runtime, "side_effect_log", ()))
        daily_order_sequence_start = getattr(runtime, "order_sequence", 0)
        daily_funding_intent_lengths = tuple(
            len(report.get(key, ())) if isinstance(report.get(key, ()), list) else None
            for key in ("buy_sell_intents", "btc_dca_intents", "redemption_subscription_intents")
        )
        now_utc = runtime.now_utc
        today_utc = now_utc.strftime("%Y-%m-%d")
        today_id_str = now_utc.strftime("%Y%m%d")
        current_balance_snapshot = build_balance_snapshot(runtime_trend_universe, balances, u_total)

        maybe_rebase_daily_state_for_balance_change(
            state,
            runtime,
            report,
            total_equity,
            trend_val_equity,
            current_balance_snapshot,
            log_buffer,
        )
        maybe_reset_daily_state(state, runtime, report, today_utc, total_equity, trend_val_equity)
        daily_pnl, trend_daily_pnl = compute_daily_pnls(state, total_equity, trend_val_equity)
        append_portfolio_report(log_buffer, allocation, fuel_val, daily_pnl, trend_daily_pnl, btc_snapshot)

        if state.get("is_circuit_broken"):
            log_buffer.insert(0, translate_fn("circuit_breaker_latched_line", total_equity=total_equity))
            return report

        failure_stage = "circuit_breaker"
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

        failure_stage = "fuel_execution"
        _u_total, _fuel_val, fuel_status = top_up_bnb_fuel(
            runtime,
            report,
            u_total,
            fuel_val,
            log_buffer,
            min_bnb_value,
            buy_bnb_amount,
        )
        if fuel_status != "ready":
            report["execution_blocked_reason"] = f"bnb_fuel_{fuel_status}"
            if fuel_status == "filled_pending_snapshot":
                reconcile_runtime_cash_effects(runtime, state)
                runtime_set_trade_state(runtime, report, state, reason="cash_reconciliation")
            else:
                state_healthy = False
            return report

        failure_stage = "trend_execution"
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

        failure_stage = "post_trade_allocation"
        fuel_symbol = str(getattr(runtime, "fuel_symbol", "BNBUSDT") or "BNBUSDT")
        if fuel_symbol in balances and fuel_symbol in prices:
            fuel_val = balances[fuel_symbol] * prices[fuel_symbol]
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
        _record_risk_diagnostics(report, post_trade_allocation)

        if not post_trade_allocation.get("execution_permitted", False):
            report["execution_blocked_reason"] = "risk_execution_not_permitted"
            return report

        btc_target_ratio = post_trade_allocation["btc_target_ratio"]
        dca_usdt_pool = post_trade_allocation["dca_usdt_pool"]
        dca_val = post_trade_allocation["dca_val"]
        btc_base_order_usdt = post_trade_allocation["btc_base_order_usdt"]
        _, trend_daily_pnl = compute_daily_pnls(state, total_equity, trend_val_equity)

        failure_stage = "btc_execution"
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
        if fuel_symbol in balances and fuel_symbol in prices:
            fuel_val = balances[fuel_symbol] * prices[fuel_symbol]
        trend_val_equity = sum(
            balances[symbol] * prices[symbol] for symbol in runtime_trend_universe
        )
        total_equity = (
            u_total
            + fuel_val
            + trend_val_equity
            + balances["BTCUSDT"] * prices["BTCUSDT"]
        )
        report["total_equity_usdt"] = total_equity
        report["trend_equity_usdt"] = trend_val_equity

        failure_stage = "earn_execution"
        manage_usdt_earn_buffer_runtime(
            runtime,
            report,
            dynamic_usdt_buffer,
            log_buffer,
            spot_free_override=u_total if runtime.dry_run else None,
        )

        failure_stage = "status_notification"
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

        failure_stage = "state_persistence"
        state["last_balance_snapshot"] = build_balance_snapshot(runtime_trend_universe, balances, u_total)
        reconcile_runtime_cash_effects(runtime, state)
        runtime_set_trade_state(runtime, report, state, reason="cycle_complete")

    except Exception as exc:
        state_healthy = False
        report["status"] = "error"
        # Exact types only: provider messages and custom exception names may
        # contain credentials or account data. Unknown types stay unclassified.
        error_type = {
            StatePersistenceError: "state_persistence_error",
            OrderReconciliationError: "order_reconciliation_error",
            ExecutionIntegrityError: "execution_integrity_error",
            ClientCallError: "client_call_error",
            TimeoutError: "timeout_error",
            ConnectionError: "connection_error",
            PermissionError: "permission_error",
            OSError: "io_error",
            ValueError: "value_error",
            TypeError: "type_error",
            KeyError: "key_error",
            RuntimeError: "runtime_error",
        }.get(type(exc), "unclassified_error")
        failure_metadata = {"stage": failure_stage, "error_type": error_type}
        if failure_stage == "daily_state":
            earn_diagnostics = report.get("diagnostics", {}).get("earn_accrual", {})
            reason_code = earn_diagnostics.get("reason_code") if isinstance(earn_diagnostics, Mapping) else None
            if reason_code in EARN_FORWARD_REASON_CODES:
                failure_metadata["reason_code"] = reason_code
        report.setdefault("diagnostics", {})["cycle_failure"] = failure_metadata
        log_buffer.append(f"cycle_execution_failed stage={failure_stage} error_type={error_type}")
        append_report_error(report, "cycle_execution_failed", stage="execute_cycle")
        try:
            runtime_notify(runtime, report, f"{translate_fn('system_crash')}\ncycle_execution_failed")
        except Exception:
            pass
    finally:
        reason_code = None
        earn_diagnostics = report.get("diagnostics", {}).get("earn_accrual", {})
        if isinstance(earn_diagnostics, Mapping):
            reason_code = earn_diagnostics.get("reason_code")
        current_order_state = _settled_order_state(getattr(runtime, "trade_state", None) or locals().get("state"))
        current_receipt_observation = report.get("execution_receipt_observation", {})
        funding_submission_unchanged = (
            isinstance(current_receipt_observation, Mapping)
            and current_receipt_observation.get("submission_attempted_count", 0)
            == daily_funding_submission_start
        )
        funding_submission_unchanged_full_cycle = (
            isinstance(current_receipt_observation, Mapping)
            and current_receipt_observation.get("submission_attempted_count", 0)
            == cycle_funding_submission_start
        )
        current_intent_lengths = tuple(
            len(report.get(key, ())) if isinstance(report.get(key, ()), list) else None
            for key in ("buy_sell_intents", "btc_dca_intents", "redemption_subscription_intents")
        )
        funding_intents_unchanged = current_intent_lengths == daily_funding_intent_lengths
        funding_order_sequence_unchanged = getattr(runtime, "order_sequence", 0) == daily_order_sequence_start
        funding_order_sequence_unchanged_full_cycle = (
            getattr(runtime, "order_sequence", 0) == cycle_order_sequence_start
        )
        full_cycle_side_effects = list(getattr(runtime, "side_effect_log", ()))[cycle_funding_side_effect_start or 0:]
        full_cycle_funding_side_effects_absent = all(
            not str(entry.get("effect_type", "")).startswith(("order_", "earn_"))
            for entry in full_cycle_side_effects
            if isinstance(entry, Mapping)
        )
        new_side_effects = list(getattr(runtime, "side_effect_log", ()))[daily_funding_side_effect_start or 0:]
        funding_side_effects_absent = all(
            not str(entry.get("effect_type", "")).startswith(("order_", "earn_"))
            for entry in new_side_effects
            if isinstance(entry, Mapping)
        )
        persistent_side_effects_absent = all(
            not (
                str(entry.get("target", "")) == "firestore"
                or str(entry.get("effect_type", "")).startswith("state_")
            )
            for entry in new_side_effects
            if isinstance(entry, Mapping)
        )
        pure_daily_state_failure = (
            not state_healthy
            and failure_stage == "daily_state"
            and reason_code in EARN_FORWARD_REASON_CODES
            and owner_claimed_this_cycle
            and initial_order_state in {"RESERVED", "TERMINAL"}
            and current_order_state in {"RESERVED", "TERMINAL"}
            and not getattr(runtime, "pending_funds", ())
            and funding_submission_unchanged
            and funding_submission_unchanged_full_cycle
            and funding_intents_unchanged
            and funding_order_sequence_unchanged
            and funding_order_sequence_unchanged_full_cycle
            and funding_side_effects_absent
            and full_cycle_funding_side_effects_absent
            and persistent_side_effects_absent
            and isinstance(report.get("state_write_intents"), list)
            and daily_state_write_intents_start is not None
            and len(report["state_write_intents"]) == daily_state_write_intents_start
        )
        if pure_daily_state_failure:
            try:
                release_runtime_state_owner(runtime)
            except ExecutionIntegrityError:
                state_owner_release_uncertain = True
                report["status"] = "error"
                append_report_error(report, "state_owner_release_uncertain", stage="state_release")
        elif state_healthy and getattr(runtime, "state_owner_held", False):
            try:
                release_runtime_state_owner(runtime)
            except ExecutionIntegrityError:
                state_owner_release_uncertain = True
                report["status"] = "error"
                append_report_error(report, "state_owner_release_uncertain", stage="state_release")
        report["log_lines"] = list(log_buffer)
        finalize_notification_delivery(report)
        attach_execution_receipt_from_report(report)
        if not getattr(runtime, "dry_run", False):
            execution_result = _build_platform_execution_result(
                report,
                state_healthy=state_healthy,
                state_owner_release_uncertain=state_owner_release_uncertain,
            )
            try_record_platform_execution(
                str(getattr(runtime, "strategy_profile", "") or ""),
                execution_result,
                domain="crypto",
            )
            _write_lifecycle_export(
                str(getattr(runtime, "strategy_profile", "") or ""),
                execution_result,
            )

        # Early returns (including risk rejection) also complete a cycle.
        try:
            import builtins
            monitor = builtins.__dict__.get("_qsl_health_monitor")
            if monitor is not None:
                monitor.beat(
                    status=report.get("status", "ok"),
                    error=report.get("error", ""),
                )
        except Exception:
            pass

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
    runtime_target = getattr(runtime, "runtime_target", None)
    log_context = RuntimeLogContext(
        platform="binance",
        deploy_target=os.getenv("LOG_DEPLOY_TARGET", "vps"),
        service_name=(
            getattr(runtime_target, "service_name", None)
            or os.getenv("SERVICE_NAME")
            or "binance-platform"
        ),
        strategy_profile=str(getattr(runtime, "strategy_profile", "") or os.getenv("STRATEGY_PROFILE", "crypto_live_pool_rotation")),
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
    persisted_cloud_uri = None
    try:
        persisted = persist_runtime_report(
            report,
            output_path=report_path,
            cloud_prefix_uri=os.getenv("EXECUTION_REPORT_CLOUD_URI") or os.getenv("EXECUTION_REPORT_GCS_URI"),
            project_id=os.getenv("CLOUD_PROJECT_ID") or os.getenv("GCP_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT"),
        )
        persisted_local_path = persisted.local_path or report_path
        if hasattr(persisted, "cloud_uri"):
            persisted_cloud_uri = persisted.cloud_uri
        else:
            persisted_cloud_uri = getattr(persisted, "gcs_uri", None)
    except Exception:
        report["status"] = "error"
        append_report_error(report, "report_persistence_failed", stage="report_persistence")
        output_printer("failed to persist archived execution report: report_persistence_failed")
        persisted_local_path = report_writer(report)
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
        report_cloud_uri=persisted_cloud_uri,
        total_equity_usdt=report.get("total_equity_usdt"),
        trend_equity_usdt=report.get("trend_equity_usdt"),
        degraded_mode_level=report.get("degraded_mode_level"),
        circuit_breaker_triggered=report.get("circuit_breaker_triggered"),
        error_count=len(report.get("error_summary", {}).get("errors", [])),
    )

    if report.get("status") != "ok" and exit_fn is not None:
        exit_fn(1)

    return report, persisted_local_path
