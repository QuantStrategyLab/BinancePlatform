"""Characterize cycle failure reports and surrounding calls with offline services."""

import builtins
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from application import cycle_service
from infra.btc_cycle_input import BtcCycleInputError
from notifications import routing, unified_notifications
from runtime_support import (
    ClientCallError,
    ExecutionIntegrityError,
    OrderReconciliationError,
    StatePersistenceError,
)


class CustomRuntimeError(RuntimeError):
    pass


class CustomIntegrityError(ExecutionIntegrityError):
    pass


class ReasonText(str):
    pass


@pytest.fixture
def cycle_case(monkeypatch):
    """Run the public cycle with synthetic data and record its service boundary."""
    observations = []

    def run(*, failure_stage=None, error=None, diagnostics=None, validation_scope="standard",
            notify_error=False, client_available=True, execution_permitted=True):
        events = []
        state = {"order_submission": {"state": "RESERVED"}}
        runtime = SimpleNamespace(
            dry_run=False, standard_execution_permitted=True, state_owner_held=True,
            now_utc=datetime(2025, 1, 2, tzinfo=timezone.utc), strategy_profile="synthetic-profile",
            side_effect_log=[], pending_funds=(), order_sequence=0, tg_token="", tg_chat_id="",
        )
        report = {
            "status": "ok", "buy_sell_intents": [], "btc_dca_intents": [],
            "redemption_subscription_intents": [], "state_write_intents": [],
        }

        def record(name, result=None):
            def call(*_args, **_kwargs):
                events.append(name)
                if name == failure_stage:
                    if diagnostics is not None:
                        report["diagnostics"].update(deepcopy(diagnostics))
                    raise error
                return result
            return call

        def notify(_runtime, _report, message, **_kwargs):
            events.append(["notify", message])
            if notify_error:
                raise RuntimeError("synthetic-notification-detail")

        def append_error(_report, message, *, stage):
            events.append(["report_error", message, stage])

        monkeypatch.setattr(cycle_service, "acquire_runtime_state_owner", record("owner_claim", True))
        monkeypatch.setattr(cycle_service, "release_runtime_state_owner", record("owner_release"))
        monkeypatch.setattr(cycle_service, "reconcile_pending_funding_submission", record("reconcile_pending"))
        monkeypatch.setattr(cycle_service, "reconcile_runtime_cash_effects", record("reconcile_cash"))
        monkeypatch.setattr(cycle_service, "finalize_notification_delivery", record("finalize_notifications"))
        monkeypatch.setattr(cycle_service, "attach_execution_receipt_from_report", record("attach_receipt"))
        monkeypatch.setattr(cycle_service, "try_record_platform_execution", record("record_execution"))
        monkeypatch.setattr(cycle_service, "_write_lifecycle_export", record("export_lifecycle"))
        monkeypatch.setattr(unified_notifications, "bind_cycle", record("bind_notifications", "synthetic-context"))
        monkeypatch.setattr(unified_notifications, "reset_cycle", record("reset_notifications"))
        monkeypatch.setattr(routing, "daily_digest_enabled", record("digest_mode", False))
        monkeypatch.setattr(builtins, "_qsl_health_monitor", SimpleNamespace(beat=record("heartbeat")), raising=False)

        allocation = {
            "total_equity": 110.0, "trend_val": 10.0, "dca_val": 0.0,
            "btc_target_ratio": 0.0, "dca_usdt_pool": 0.0, "btc_base_order_usdt": 0.0,
            "execution_permitted": execution_permitted,
        }
        allocation_calls = 0

        def allocate(*args):
            nonlocal allocation_calls
            name = "portfolio_allocation" if allocation_calls == 0 else "post_trade_allocation"
            allocation_calls += 1
            return record(name, allocation)(*args)

        observations.append({"report": report, "events": events, "state": state})
        result = cycle_service.execute_strategy_cycle(
            runtime,
            build_execution_report=record("build_report", report),
            ensure_runtime_client=record("client_connect", client_available),
            load_cycle_execution_settings=record("settings", SimpleNamespace(
                btc_status_report_interval_hours=24, allow_new_trend_entries_on_degraded=False,
            )),
            load_cycle_state=record("state_load", (state, {"degraded": False}, {"ETHUSDT": {}}, True)),
            append_trend_pool_source_logs=record("pool_diagnostics"),
            capture_market_snapshot=record("market_snapshot", {
                "u_total": 100.0, "fuel_val": 0.0, "dynamic_usdt_buffer": 10.0,
                "prices": {"BTCUSDT": 50_000.0, "ETHUSDT": 10.0},
                "balances": {"BTCUSDT": 0.0, "ETHUSDT": 1.0},
                "btc_snapshot": {}, "trend_indicators": {},
            }),
            top_up_bnb_fuel=record("fuel_execution", (100.0, 0.0, "ready")),
            compute_portfolio_allocation=allocate,
            build_balance_snapshot=record("balance_snapshot", {}),
            maybe_reset_daily_state=record("daily_reset"),
            maybe_rebase_daily_state_for_balance_change=record("daily_state"),
            compute_daily_pnls=record("pnl", (0.0, 0.0)),
            append_portfolio_report=record("portfolio_report"),
            run_daily_circuit_breaker=record("circuit_breaker", False),
            execute_trend_rotation=record("trend_execution", 100.0),
            execute_btc_dca_cycle=record("btc_execution", 90.0),
            manage_usdt_earn_buffer_runtime=record("earn_execution"),
            maybe_send_periodic_btc_status_report=record("status_notification"),
            runtime_set_trade_state=record("state_persistence"),
            append_report_error=append_error,
            runtime_notify=notify,
            translate_fn=lambda value, **_kwargs: value,
            traceback_module=SimpleNamespace(),
            validation_scope=validation_scope,
        )
        assert result is report
        return report, events

    run.observations = observations
    return run


@pytest.mark.parametrize(("error_class", "error_type"), [
    (StatePersistenceError, "state_persistence_error"),
    (OrderReconciliationError, "order_reconciliation_error"),
    (ExecutionIntegrityError, "execution_integrity_error"),
    (ClientCallError, "client_call_error"),
    (TimeoutError, "timeout_error"),
    (ConnectionError, "connection_error"),
    (PermissionError, "permission_error"),
    (OSError, "io_error"),
    (ValueError, "value_error"),
    (TypeError, "type_error"),
    (KeyError, "key_error"),
    (RuntimeError, "runtime_error"),
    (Exception, "unclassified_error"),
    (CustomRuntimeError, "unclassified_error"),
    (CustomIntegrityError, "unclassified_error"),
])
def test_exact_failure_types_keep_public_report_and_finalization_order(cycle_case, error_class, error_type):
    report, events = cycle_case(failure_stage="market_snapshot", error=error_class("synthetic-provider-detail"))
    assert report["status"] == "error"
    assert report["diagnostics"]["cycle_failure"] == {"stage": "market_snapshot", "error_type": error_type}
    assert report["log_lines"] == [f"cycle_execution_failed stage=market_snapshot error_type={error_type}"]
    assert events == [
        "settings", "build_report", "bind_notifications", "owner_claim", "client_connect",
        "state_load", "reconcile_pending", "pool_diagnostics", "market_snapshot",
        ["report_error", "cycle_execution_failed", "execute_cycle"],
        ["notify", "system_crash\ncycle_execution_failed"],
        "reset_notifications", "finalize_notifications", "attach_receipt", "digest_mode",
        "record_execution", "export_lifecycle", "heartbeat",
    ]
    assert "synthetic-provider-detail" not in repr(report)
    assert "synthetic-provider-detail" not in repr(events)


@pytest.mark.parametrize(("stage", "error", "expected_reason"), [
    ("btc_execution", ExecutionIntegrityError("btc_strategy_attribution_unavailable"), "btc_strategy_attribution_unavailable"),
    ("btc_execution", CustomIntegrityError("btc_strategy_attribution_unavailable"), None),
    ("btc_execution", RuntimeError("btc_strategy_attribution_unavailable"), None),
    ("btc_execution", ExecutionIntegrityError("btc_strategy_attribution_unavailable: synthetic-detail"), None),
    ("market_snapshot", ExecutionIntegrityError("btc_strategy_attribution_unavailable"), None),
])
def test_attribution_reason_requires_exact_stage_type_and_message(cycle_case, stage, error, expected_reason):
    report, _events = cycle_case(failure_stage=stage, error=error)
    metadata = report["diagnostics"]["cycle_failure"]
    if expected_reason is None:
        assert "reason_code" not in metadata
    else:
        assert metadata["reason_code"] == expected_reason


@pytest.mark.parametrize(("diagnostics", "expected_reason"), [
    ({"earn_accrual": {"reason_code": "earn_counter_reset"},
      "external_cash_flow": {"reason_code": "external_cash_flow_history_read_failed"},
      "balance_change": {"reason_code": "external_cash_flow_cursor_missing"}}, "earn_counter_reset"),
    ({"earn_accrual": {"reason_code": "synthetic-detail"},
      "external_cash_flow": {"reason_code": "external_cash_flow_history_read_failed"},
      "balance_change": {"reason_code": "external_cash_flow_cursor_missing"}}, "external_cash_flow_history_read_failed"),
    ({"earn_accrual": [], "external_cash_flow": None,
      "balance_change": {"reason_code": "external_cash_flow_cursor_missing"}}, "external_cash_flow_cursor_missing"),
    ({"earn_accrual": {"reason_code": ReasonText("earn_counter_reset")}}, "daily_accounting_unverifiable"),
    ({"external_cash_flow": {"reason_code": ["earn_counter_reset"]}}, "daily_accounting_unverifiable"),
    ({}, "daily_accounting_unverifiable"),
])
def test_daily_diagnostic_precedence_preserves_exact_reason_types(cycle_case, diagnostics, expected_reason):
    report, _events = cycle_case(
        failure_stage="daily_state", error=ExecutionIntegrityError("daily_accounting_unverifiable"),
        diagnostics=diagnostics,
    )
    assert report["diagnostics"]["cycle_failure"] == {
        "stage": "daily_state", "error_type": "execution_integrity_error", "reason_code": expected_reason,
    }


@pytest.mark.parametrize("reason", [
    "earn_checkpoint_required_for_receipt", "earn_forward_accounting_unverified",
    "balance_change_unexplained", "external_cash_flow_reconciliation_uncertain",
    "daily_accounting_unverifiable", "daily_trend_accounting_migration_unverifiable",
    "daily_trend_accounting_unverifiable",
])
def test_daily_failure_preserves_fixed_fallback_reason_codes(cycle_case, reason):
    report, _events = cycle_case(failure_stage="daily_state", error=ExecutionIntegrityError(reason))
    assert report["diagnostics"]["cycle_failure"]["reason_code"] == reason


@pytest.mark.parametrize("error", [
    CustomIntegrityError("daily_accounting_unverifiable"),
    RuntimeError("daily_accounting_unverifiable"),
    ExecutionIntegrityError("synthetic-provider-detail"),
])
def test_daily_failure_does_not_promote_unknown_errors_to_known_reasons(cycle_case, error):
    report, _events = cycle_case(failure_stage="daily_state", error=error)
    assert "reason_code" not in report["diagnostics"]["cycle_failure"]


@pytest.mark.parametrize(("scope", "stage", "message", "expected"), [
    ("full_cycle_no_submit", "portfolio_allocation", "btc_cycle_input_unavailable", True),
    ("standard", "portfolio_allocation", "btc_cycle_input_unavailable", False),
    ("full_cycle_no_submit", "market_snapshot", "btc_cycle_input_unavailable", False),
    ("full_cycle_no_submit", "portfolio_allocation", "synthetic-provider-detail", False),
])
def test_no_submit_metadata_keeps_original_log_classification(cycle_case, scope, stage, message, expected):
    report, _events = cycle_case(failure_stage=stage, error=BtcCycleInputError(message), validation_scope=scope)
    metadata = {"stage": stage, "error_type": "unclassified_error"}
    if expected:
        metadata.update(error_type="btc_cycle_input_error", reason_code="btc_cycle_input_unavailable")
    assert report["diagnostics"]["cycle_failure"] == metadata
    assert report["log_lines"] == [f"cycle_execution_failed stage={stage} error_type=unclassified_error"]


def test_notification_failure_does_not_replace_original_failure_or_skip_finalization(cycle_case):
    report, events = cycle_case(failure_stage="market_snapshot", error=TimeoutError("synthetic-provider-detail"), notify_error=True)
    assert report["diagnostics"]["cycle_failure"] == {"stage": "market_snapshot", "error_type": "timeout_error"}
    assert events[-7:] == [
        "reset_notifications", "finalize_notifications", "attach_receipt", "digest_mode",
        "record_execution", "export_lifecycle", "heartbeat",
    ]
    assert "synthetic-notification-detail" not in repr(report)


def test_unhashable_earn_reason_preserves_existing_finalization_error(cycle_case):
    with pytest.raises(TypeError, match="^unhashable type: 'list'$"):
        cycle_case(
            failure_stage="daily_state", error=ExecutionIntegrityError("daily_accounting_unverifiable"),
            diagnostics={"earn_accrual": {"reason_code": ["earn_counter_reset"]}},
        )
    observation = cycle_case.observations[0]
    assert observation["report"]["diagnostics"]["cycle_failure"] == {
        "stage": "daily_state", "error_type": "execution_integrity_error",
        "reason_code": "daily_accounting_unverifiable",
    }
    assert observation["events"][-3:] == [
        ["report_error", "cycle_execution_failed", "execute_cycle"],
        ["notify", "system_crash\ncycle_execution_failed"],
        "reset_notifications",
    ]
    assert "record_execution" not in observation["events"]
    assert "heartbeat" not in observation["events"]


@pytest.mark.parametrize(("client_available", "execution_permitted", "blocked"), [
    (True, True, None),
    (False, True, None),
    (True, False, "risk_execution_not_permitted"),
])
def test_success_and_early_returns_do_not_create_failure_metadata(cycle_case, client_available, execution_permitted, blocked):
    report, events = cycle_case(client_available=client_available, execution_permitted=execution_permitted)
    assert report["status"] == "ok"
    assert "cycle_failure" not in report["diagnostics"]
    assert report.get("execution_blocked_reason") == blocked
    assert not any(isinstance(event, list) for event in events)
    assert events[-6:] == [
        "finalize_notifications", "attach_receipt", "digest_mode", "record_execution", "export_lifecycle", "heartbeat",
    ]
