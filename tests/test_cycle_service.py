import builtins
import copy
import inspect
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from application.cycle_service import execute_strategy_cycle, run_live_cycle, write_execution_report
from application.execution_service import execute_trend_buys
from application.portfolio_service import (
    maybe_rebase_daily_state_for_balance_change,
    maybe_reset_daily_state,
)
from infra.binance_runtime import ensure_runtime_client
from runtime_support import (
    ExecutionIntegrityError,
    ExecutionRuntime,
    StatePersistenceError,
    append_report_error,
    build_execution_report,
    runtime_call_client,
    runtime_notify,
)


class CycleServiceTests(unittest.TestCase):
    def _run_funds_cycle(
        self,
        execution_permitted,
        *,
        post_execution_permitted=None,
        snapshot_error=False,
        state=None,
        earn_failure=False,
        runtime=None,
        ownership_events=None,
        balance_snapshot=None,
        rebase_fn=None,
        reset_fn=None,
    ):
        events = []
        state = {} if state is None else state
        post_execution_permitted = execution_permitted if post_execution_permitted is None else post_execution_permitted
        allocation_permissions = [execution_permitted, post_execution_permitted]

        def manage_earn(*_args, **_kwargs):
            events.append("earn")
            if earn_failure:
                raise ExecutionIntegrityError("order_reconciliation_uncertain")

        def send_periodic_status(*_args, **_kwargs):
            if earn_failure:
                events.append("periodic")

        runtime = runtime or SimpleNamespace(
            state_owner_claim=lambda _owner: True,
            state_owner_release=lambda _owner: True,
            dry_run=False,
            now_utc=SimpleNamespace(strftime=lambda fmt: "20260905" if "%d" in fmt else "2026-09-05"),
            standard_execution_permitted=True,
            tg_token="",
            tg_chat_id="",
        )

        ownership_events = [] if ownership_events is None else ownership_events
        def note(event, value):
            ownership_events.append(event)
            return value

        report = execute_strategy_cycle(
            runtime,
            build_execution_report=lambda _runtime: {
                "status": "ok",
                "buy_sell_intents": [],
                "btc_dca_intents": [],
                "redemption_subscription_intents": [],
                "selected_symbols": {},
                "gating_summary": {},
                "gating_events": [],
            },
            ensure_runtime_client=lambda *_args: note("client", True),
            load_cycle_execution_settings=lambda: SimpleNamespace(
                btc_status_report_interval_hours=24,
                allow_new_trend_entries_on_degraded=False,
            ),
            load_cycle_state=lambda *_args: note("state", (state, {"degraded": False}, {"ETHUSDT": {}}, True)),
            append_trend_pool_source_logs=lambda *_args: None,
            capture_market_snapshot=(
                lambda *_args: (_ for _ in ()).throw(RuntimeError("snapshot_read_failed"))
                if snapshot_error
                else note("snapshot", {
                    "u_total": 200.0,
                    "fuel_val": 0.0,
                    "dynamic_usdt_buffer": 100.0,
                    "prices": {"BTCUSDT": 50_000.0, "ETHUSDT": 100.0},
                    "balances": {"BTCUSDT": 0.0, "ETHUSDT": 0.0},
                    "btc_snapshot": {},
                    "trend_indicators": {},
                })
            ),
            top_up_bnb_fuel=lambda *_args: events.append("fuel") or (185.0, 15.0, "ready"),
            compute_portfolio_allocation=lambda *_args: {
                "total_equity": 200.0,
                "trend_val": 0.0,
                "dca_val": 0.0,
                "btc_target_ratio": 0.0,
                "dca_usdt_pool": 0.0,
                "btc_base_order_usdt": 0.0,
                "execution_permitted": allocation_permissions.pop(0)
                if allocation_permissions
                else execution_permitted,
            },
            build_balance_snapshot=lambda *_args: {} if balance_snapshot is None else balance_snapshot,
            maybe_reset_daily_state=reset_fn or (lambda *_args: events.append("state_reset")),
            maybe_rebase_daily_state_for_balance_change=rebase_fn or (lambda *_args: events.append("state_rebase")),
            compute_daily_pnls=lambda *_args: (0.0, 0.0),
            append_portfolio_report=lambda *_args: None,
            run_daily_circuit_breaker=lambda *_args: events.append("circuit_breaker") or False,
            execute_trend_rotation=lambda *_args, **_kwargs: events.append("trend") or 185.0,
            execute_btc_dca_cycle=lambda *_args: events.append("dca") or 185.0,
            manage_usdt_earn_buffer_runtime=manage_earn,
            maybe_send_periodic_btc_status_report=send_periodic_status,
            runtime_set_trade_state=lambda *_args, **_kwargs: events.append("state_write"),
            append_report_error=lambda *_args, **_kwargs: None,
            runtime_notify=lambda *_args, **_kwargs: None,
            translate_fn=lambda value, **_kwargs: value,
            traceback_module=SimpleNamespace(),
        )
        return report, events

    def test_owner_busy_prevents_client_state_and_market_reads(self):
        observed = []
        runtime = ExecutionRuntime(state_owner_claim=lambda _owner: False, state_owner_release=lambda _owner: True)
        report, events = self._run_funds_cycle(True, runtime=runtime, ownership_events=observed)
        self.assertEqual(report["execution_blocked_reason"], "state_owner_busy")
        self.assertEqual(observed, [])
        self.assertEqual(events, [])

    def test_normal_risk_rejection_releases_and_next_cycle_reads_fresh_state(self):
        owners, released, observed = [], [], []
        def claim(owner):
            if owners:
                return False
            owners.append(owner)
            return True
        def release(owner):
            self.assertEqual(owners, [owner])
            owners.clear()
            released.append(owner)
            return True
        runtime = ExecutionRuntime(state_owner_claim=claim, state_owner_release=release)
        for _ in range(2):
            report, events = self._run_funds_cycle(False, runtime=runtime, ownership_events=observed)
            self.assertEqual(report["execution_blocked_reason"], "risk_execution_not_permitted")
            self.assertEqual(events, [])
        self.assertEqual(observed, ["client", "state", "snapshot"] * 2)
        self.assertEqual(len(released), 2)
        self.assertFalse(owners)

    def test_inherited_unknown_never_uses_new_run_id_to_submit_or_release(self):
        from unittest.mock import Mock
        observed = []
        release = Mock(return_value=True)
        runtime = ExecutionRuntime(run_id="different-run", state_owner_claim=lambda _owner: True, state_owner_release=release)
        report, events = self._run_funds_cycle(True, runtime=runtime, ownership_events=observed,
            state={"order_submission": {"state": "SUBMISSION_UNKNOWN"}})
        self.assertEqual(report["status"], "error")
        self.assertEqual(observed, ["client", "state"])
        self.assertEqual(events, [])
        release.assert_not_called()

    def test_durable_funding_receipt_recovers_at_cycle_entry_without_second_post(self):
        state = {
            "order_submission": {
                "state": "SUBMISSION_UNKNOWN",
                "identity_sha256": "a" * 64,
                "method_name": "redeem_simple_earn_flexible_product",
                "funding_receipt": {
                    "id": 7,
                    "asset": "USDT",
                    "product_id": "USDT001",
                    "amount": "10",
                    "spot_before": "6",
                },
            },
            "last_balance_snapshot": {"USDT": 16.0},
        }
        persisted = {}
        release = Mock(return_value=True)
        client = Mock()
        client.get_asset_balance.return_value = {
            "asset": "USDT", "free": "16", "locked": "0",
        }
        client.get_simple_earn_flexible_product_position.return_value = {
            "total": 0, "rows": [],
        }
        client._request_margin_api.return_value = {
            "total": 1,
            "rows": [{
                "redeemId": 7,
                "projectId": "USDT001",
                "asset": "USDT",
                "amount": "10",
                "destAccount": "SPOT",
                "status": "PAID",
            }],
        }

        def write(current):
            persisted.clear()
            persisted.update(copy.deepcopy(current))
            return True

        runtime = ExecutionRuntime(
            client=client,
            state_writer=write,
            state_owner_claim=lambda _owner: True,
            state_owner_release=release,
        )

        report, events = self._run_funds_cycle(
            False,
            runtime=runtime,
            state=state,
        )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["execution_blocked_reason"], "risk_execution_not_permitted")
        self.assertEqual(events, [])
        self.assertEqual(persisted["order_submission"], {"state": "TERMINAL"})
        self.assertEqual(persisted["last_balance_snapshot"], {"USDT": 16.0})
        self.assertEqual(runtime.pending_funds, [])
        client.redeem_simple_earn_flexible_product.assert_not_called()
        client.subscribe_simple_earn_flexible_product.assert_not_called()
        client._request_margin_api.assert_called_once()
        release.assert_called_once()

    def test_rejected_or_missing_execution_permission_blocks_all_funds_actions(self):
        for execution_permitted in (False, None):
            with self.subTest(execution_permitted=execution_permitted):
                report, events = self._run_funds_cycle(execution_permitted)

                self.assertEqual(events, [])
                self.assertEqual(report["execution_blocked_reason"], "risk_execution_not_permitted")

    def test_early_cycle_returns_still_publish_one_heartbeat_without_funds_actions(self):
        for permission in (False, None):
            with self.subTest(execution_permitted=permission):
                monitor = Mock()
                with patch.object(builtins, "_qsl_health_monitor", monitor, create=True):
                    report, events = self._run_funds_cycle(permission)
                self.assertEqual(report["execution_blocked_reason"], "risk_execution_not_permitted")
                self.assertEqual(events, [])
                monitor.beat.assert_called_once_with(status="ok", error="")

    def test_heartbeat_preserves_failed_cycle_status(self):
        monitor = Mock()
        with patch.object(builtins, "_qsl_health_monitor", monitor, create=True):
            report, events = self._run_funds_cycle(True, snapshot_error=True)
        self.assertEqual(report["status"], "error")
        self.assertEqual(events, [])
        monitor.beat.assert_called_once_with(status="error", error="")

    def test_heartbeat_failure_does_not_change_risk_rejection(self):
        monitor = Mock()
        monitor.beat.side_effect = RuntimeError("synthetic heartbeat unavailable")
        with patch.object(builtins, "_qsl_health_monitor", monitor, create=True):
            report, events = self._run_funds_cycle(False)
        monitor.beat.assert_called_once()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["execution_blocked_reason"], "risk_execution_not_permitted")
        self.assertEqual(events, [])

    def test_full_cycle_publishes_heartbeat_only_once(self):
        monitor = Mock()
        with patch.object(builtins, "_qsl_health_monitor", monitor, create=True):
            report, events = self._run_funds_cycle(True)
        self.assertEqual(report["status"], "ok")
        self.assertIn("state_write", events)
        monitor.beat.assert_called_once_with(status="ok", error="")

    def test_platform_performance_record_marks_external_cash_flow_incomparable(self):
        with patch("application.cycle_service.try_record_platform_execution") as record:
            self._run_funds_cycle(True)

        record.assert_called_once()
        self.assertIn("external_cash_flow", record.call_args.args[1])
        self.assertIsNone(record.call_args.args[1]["external_cash_flow"])

    def test_approved_execution_permission_preserves_fuel_trend_dca_and_earn_actions(self):
        _report, events = self._run_funds_cycle(True)

        self.assertEqual(events, ["state_rebase", "state_reset", "circuit_breaker", "fuel", "trend", "dca", "earn", "state_write"])

    def test_new_day_cash_flow_read_failure_prevents_reset_in_real_cycle(self):
        state = {
            "last_reset_date": "2026-09-11",
            "last_balance_snapshot": {"USDT": 100.0},
            "daily_equity_base": 100.0,
            "daily_external_principal_usdt": 0.0,
            "external_cash_flow_cursor": {"version": 1, "observed_at": "2026-09-11T23:00:00+00:00", "records": {}},
        }
        runtime = ExecutionRuntime(
            now_utc=datetime(2026, 9, 12, 1, tzinfo=timezone.utc),
            client=object(),
            state_owner_claim=lambda _owner: True,
            state_owner_release=lambda _owner: True,
        )
        reset_calls = []

        def reconcile(*args):
            return maybe_rebase_daily_state_for_balance_change(
                *args,
                collect_external_cash_flows_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    ValueError("external_cash_flow_history_read_failed")
                ),
                runtime_set_trade_state_fn=lambda *_args, **_kwargs: self.fail("unsafe state write"),
                append_log_fn=lambda *_args: None,
                translate_fn=lambda key, **_kwargs: key,
            )

        report, _events = self._run_funds_cycle(
            True,
            state=state,
            runtime=runtime,
            balance_snapshot={"USDT": 200.0},
            rebase_fn=reconcile,
            reset_fn=lambda *_args: reset_calls.append("reset"),
        )

        self.assertEqual(report["status"], "error")
        self.assertEqual(reset_calls, [])
        self.assertEqual(state["daily_equity_base"], 100.0)
        self.assertEqual(state["last_reset_date"], "2026-09-11")

    def test_reconciled_new_day_state_resumes_after_reset_write_failure_without_double_count(self):
        state = {
            "last_reset_date": "2026-09-11",
            "last_balance_snapshot": {"USDT": 100.0},
            "daily_equity_base": 100.0,
            "daily_external_principal_usdt": 0.0,
            "daily_trend_pnl_basis": "trend_mark_plus_cash_flow_v1",
            "daily_trend_cash_flow_usdt": 0.0,
            "daily_trend_net_invested_usdt": 0.0,
            "daily_trend_risk_base_usdt": 0.0,
            "daily_trend_third_fee_usdt": 0.0,
            "external_cash_flow_cursor": {"version": 1, "observed_at": "2026-09-11T23:00:00+00:00", "records": {}},
        }
        now = datetime(2026, 9, 12, 1, tzinfo=timezone.utc)
        cursor = {"version": 1, "observed_at": now.isoformat(), "records": {}}
        persisted = {}

        def reconcile_deposit(*args):
            return maybe_rebase_daily_state_for_balance_change(
                *args,
                collect_external_cash_flows_fn=lambda *_args, **_kwargs: {
                    "bootstrap": False,
                    "new_deposit_principal_usdt": "100",
                    "new_confirmed_deposit_count": 1,
                    "new_deposit_completed_at": ["2026-09-12T00:30:00+00:00"],
                    "new_unsupported_deposit_count": 0,
                    "new_or_changed_withdrawal_count": 0,
                    "cursor": cursor,
                },
                runtime_set_trade_state_fn=lambda _runtime, _report, current, **_kwargs: (
                    persisted.clear(), persisted.update(copy.deepcopy(current))
                ),
                append_log_fn=lambda *_args: None,
                translate_fn=lambda key, **_kwargs: key,
            )

        def fail_reset(*args):
            return maybe_reset_daily_state(
                *args,
                runtime_set_trade_state_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    StatePersistenceError("state_persistence_failed")
                ),
            )

        runtime = ExecutionRuntime(
            now_utc=now,
            client=object(),
            state_owner_claim=lambda _owner: True,
            state_owner_release=lambda _owner: True,
        )
        report, _events = self._run_funds_cycle(
            True,
            state=state,
            runtime=runtime,
            balance_snapshot={"USDT": 200.0},
            rebase_fn=reconcile_deposit,
            reset_fn=fail_reset,
        )

        self.assertEqual(report["status"], "error")
        self.assertEqual(persisted["last_balance_snapshot"], {"USDT": 200.0})
        self.assertEqual(persisted["daily_equity_base"], 100.0)
        self.assertEqual(persisted["daily_external_principal_usdt"], 0.0)
        self.assertEqual(persisted["last_reset_date"], "2026-09-11")

        reloaded = copy.deepcopy(persisted)
        reset_persisted = {}

        def reconcile_duplicate(*args):
            return maybe_rebase_daily_state_for_balance_change(
                *args,
                collect_external_cash_flows_fn=lambda *_args, **_kwargs: {
                    "bootstrap": False,
                    "new_deposit_principal_usdt": "0",
                    "new_confirmed_deposit_count": 0,
                    "new_deposit_completed_at": [],
                    "new_unsupported_deposit_count": 0,
                    "new_or_changed_withdrawal_count": 0,
                    "cursor": cursor,
                },
                runtime_set_trade_state_fn=lambda *_args, **_kwargs: self.fail("duplicate wrote state"),
                append_log_fn=lambda *_args: None,
                translate_fn=lambda key, **_kwargs: key,
            )

        def complete_reset(*args):
            return maybe_reset_daily_state(
                *args,
                runtime_set_trade_state_fn=lambda _runtime, _report, current, **_kwargs: (
                    reset_persisted.clear(), reset_persisted.update(copy.deepcopy(current))
                ),
            )

        resumed_runtime = ExecutionRuntime(
            now_utc=now,
            client=object(),
            state_owner_claim=lambda _owner: True,
            state_owner_release=lambda _owner: True,
        )
        resumed, _events = self._run_funds_cycle(
            True,
            state=reloaded,
            runtime=resumed_runtime,
            balance_snapshot={"USDT": 200.0},
            rebase_fn=reconcile_duplicate,
            reset_fn=complete_reset,
        )

        self.assertEqual(resumed["status"], "ok")
        self.assertEqual(reset_persisted["daily_equity_base"], 200.0)
        self.assertEqual(reset_persisted["daily_external_principal_usdt"], 0.0)
        self.assertEqual(reset_persisted["last_reset_date"], "2026-09-12")

    def test_snapshot_read_failure_blocks_all_funds_actions(self):
        report, events = self._run_funds_cycle(True, snapshot_error=True)

        self.assertEqual(report["status"], "error")
        self.assertEqual(events, [])

    def test_latched_circuit_breaker_blocks_fuel_before_any_funds_action(self):
        _report, events = self._run_funds_cycle(True, state={"is_circuit_broken": True})

        self.assertNotIn("fuel", events)
        self.assertNotIn("trend", events)
        self.assertNotIn("dca", events)
        self.assertNotIn("earn", events)

    def test_post_trade_risk_veto_blocks_dca_and_earn_actions(self):
        report, events = self._run_funds_cycle(True, post_execution_permitted=False)

        self.assertEqual(report["execution_blocked_reason"], "risk_execution_not_permitted")
        self.assertIn("trend", events)
        self.assertNotIn("dca", events)
        self.assertNotIn("earn", events)

    def test_earn_integrity_error_stops_final_state_write_and_later_cycle_actions(self):
        report, events = self._run_funds_cycle(True, earn_failure=True)

        self.assertEqual(report["status"], "error")
        self.assertIn("earn", events)
        self.assertNotIn("periodic", events)
        self.assertNotIn("state_write", events)

    def test_research_cycle_settings_require_dry_run(self):
        runtime = SimpleNamespace(
            dry_run=False,
            research_cycle_settings=SimpleNamespace(
                btc_status_report_interval_hours=24,
                allow_new_trend_entries_on_degraded=False,
            ),
        )

        with self.assertRaisesRegex(ValueError, "research cycle settings require dry_run=True"):
            execute_strategy_cycle(
                runtime,
                build_execution_report=lambda _runtime: {},
                ensure_runtime_client=lambda *_args: True,
                load_cycle_execution_settings=lambda: (_ for _ in ()).throw(
                    AssertionError("live settings must not be called")
                ),
                load_cycle_state=lambda *_args: None,
                append_trend_pool_source_logs=lambda *_args: None,
                capture_market_snapshot=lambda *_args: None,
                top_up_bnb_fuel=lambda *_args: (0.0, 0.0, "ready"),
                compute_portfolio_allocation=lambda *_args: None,
                build_balance_snapshot=lambda *_args: None,
                maybe_reset_daily_state=lambda *_args: None,
                maybe_rebase_daily_state_for_balance_change=lambda *_args: None,
                compute_daily_pnls=lambda *_args: None,
                append_portfolio_report=lambda *_args: None,
                run_daily_circuit_breaker=lambda *_args: None,
                execute_trend_rotation=lambda *_args: None,
                execute_btc_dca_cycle=lambda *_args: None,
                manage_usdt_earn_buffer_runtime=lambda *_args: None,
                maybe_send_periodic_btc_status_report=lambda *_args: None,
                runtime_set_trade_state=lambda *_args: None,
                append_report_error=lambda *_args: None,
                runtime_notify=lambda *_args: None,
                translate_fn=lambda value, **_kwargs: value,
                traceback_module=SimpleNamespace(),
            )

    def test_write_execution_report_persists_json(self):
        report = {"status": "ok", "log_lines": ["hello"], "value": 1}
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = write_execution_report(report, reports_dir=tmp_dir, filename="report.json")
            with open(output_path, "r") as handle:
                payload = json.load(handle)

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["value"], 1)

    def test_run_live_cycle_writes_report_and_prints_logs(self):
        observed = {"printed": [], "built": 0}

        def fake_runtime_builder():
            observed["built"] += 1
            return object()

        def fake_execute_cycle(runtime):
            self.assertIsNotNone(runtime)
            return {"status": "ok", "log_lines": ["line-1", "line-2"]}

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.dict(
                os.environ,
                {
                    "STRATEGY_PROFILE": "crypto_live_pool_rotation",
                    "SERVICE_NAME": "binance-quant",
                },
                clear=False,
            ):
                report, output_path = run_live_cycle(
                    runtime_builder=fake_runtime_builder,
                    execute_cycle=fake_execute_cycle,
                    output_printer=lambda text: observed["printed"].append(text),
                    report_writer=lambda report: write_execution_report(
                        report,
                        reports_dir=tmp_dir,
                        filename="execution_report.json",
                    ),
                )
                with open(output_path, "r") as handle:
                    payload = json.load(handle)

        self.assertEqual(observed["built"], 1)
        self.assertEqual(len(observed["printed"]), 3)
        self.assertEqual(observed["printed"][1], "line-1\nline-2")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(payload["log_lines"], ["line-1", "line-2"])

    def test_run_live_cycle_emits_structured_runtime_events(self):
        observed = {"printed": []}

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.dict(
                os.environ,
                {
                    "STRATEGY_PROFILE": "crypto_live_pool_rotation",
                    "SERVICE_NAME": "binance-quant",
                    "LOG_DEPLOY_TARGET": "vps",
                },
                clear=False,
            ):
                report, _output_path = run_live_cycle(
                    runtime_builder=lambda: SimpleNamespace(
                        run_id="run-001",
                        dry_run=True,
                        strategy_profile="crypto_live_pool_rotation",
                        strategy_display_name="Crypto Live Pool Rotation",
                        strategy_display_name_localized="加密领涨轮动",
                    ),
                    execute_cycle=lambda _runtime: {
                        "status": "ok",
                        "log_lines": ["line-1", "line-2"],
                        "error_summary": {"errors": []},
                        "total_equity_usdt": 1000.0,
                        "trend_equity_usdt": 250.0,
                        "degraded_mode_level": None,
                        "circuit_breaker_triggered": False,
                    },
                    output_printer=lambda text: observed["printed"].append(text),
                    report_writer=lambda current_report: write_execution_report(
                        current_report,
                        reports_dir=tmp_dir,
                        filename="execution_report.json",
                    ),
                )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(len(observed["printed"]), 3)
        start_log = json.loads(observed["printed"][0])
        end_log = json.loads(observed["printed"][2])
        self.assertEqual(start_log["event"], "strategy_cycle_started")
        self.assertEqual(start_log["strategy_profile"], "crypto_live_pool_rotation")
        self.assertEqual(start_log["strategy_display_name"], "Crypto Live Pool Rotation")
        self.assertEqual(start_log["strategy_display_name_localized"], "加密领涨轮动")
        self.assertEqual(start_log["run_id"], "run-001")
        self.assertEqual(end_log["event"], "strategy_cycle_completed")
        self.assertEqual(end_log["status"], "ok")

    def test_run_live_cycle_uses_shared_runtime_report_archive(self):
        observed = {}

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = os.path.join(tmp_dir, "execution_report.json")
            with patch.dict(
                os.environ,
                {
                    "STRATEGY_PROFILE": "crypto_live_pool_rotation",
                    "SERVICE_NAME": "binance-quant",
                    "EXECUTION_REPORT_GCS_URI": "gs://demo-bucket/runtime-reports",
                    "GCP_PROJECT_ID": "demo-project",
                },
                clear=False,
            ):
                with patch(
                    "application.cycle_service.persist_runtime_report",
                    lambda report, **kwargs: observed.update(
                        {
                            "status": report["status"],
                            "kwargs": kwargs,
                        }
                    )
                    or SimpleNamespace(
                        local_path=kwargs.get("output_path"),
                        gcs_uri="gs://demo-bucket/runtime-reports/binance/crypto_live_pool_rotation/2026-04/run-001.json",
                    ),
                ):
                    report, persisted_path = run_live_cycle(
                        runtime_builder=lambda: SimpleNamespace(run_id="run-001", dry_run=False),
                        execute_cycle=lambda _runtime: {
                            "status": "ok",
                            "log_lines": [],
                            "error_summary": {"errors": []},
                        },
                        output_printer=lambda _text: None,
                        report_writer=lambda report: write_execution_report(
                            report,
                            reports_dir=tmp_dir,
                            filename="execution_report.json",
                        ),
                    )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(persisted_path, output_path)
        self.assertEqual(observed["status"], "ok")
        self.assertEqual(observed["kwargs"]["output_path"], output_path)
        self.assertEqual(observed["kwargs"]["cloud_prefix_uri"], "gs://demo-bucket/runtime-reports")
        self.assertEqual(observed["kwargs"]["project_id"], "demo-project")

    def test_run_live_cycle_calls_exit_on_error(self):
        observed = {"exit_code": None}

        def fake_execute_cycle(_runtime):
            return {"status": "error", "log_lines": []}

        def fake_exit(code):
            observed["exit_code"] = code

        with tempfile.TemporaryDirectory() as tmp_dir:
            run_live_cycle(
                runtime_builder=lambda: object(),
                execute_cycle=fake_execute_cycle,
                output_printer=lambda _text: None,
                report_writer=lambda report: write_execution_report(
                    report,
                    reports_dir=tmp_dir,
                    filename="execution_report.json",
                ),
                exit_fn=fake_exit,
            )

        self.assertEqual(observed["exit_code"], 1)

    def test_run_live_cycle_archive_failure_is_sanitized_and_fails_closed(self):
        sentinel = "SENSITIVE_PROVIDER_SENTINEL"
        observed = {"printed": [], "exit_code": None}

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch(
                "application.cycle_service.persist_runtime_report",
                side_effect=RuntimeError(sentinel),
            ):
                report, output_path = run_live_cycle(
                    runtime_builder=lambda: SimpleNamespace(run_id="run-001", dry_run=True),
                    execute_cycle=lambda _runtime: {
                        "status": "ok",
                        "log_lines": [],
                        "error_summary": {"errors": []},
                    },
                    output_printer=lambda text: observed["printed"].append(text),
                    report_writer=lambda current_report: write_execution_report(
                        current_report,
                        reports_dir=tmp_dir,
                        filename="execution_report.json",
                    ),
                    exit_fn=lambda code: observed.update(exit_code=code),
                )
            with open(output_path, "r") as handle:
                serialized_report = handle.read()

        rendered = json.dumps(report, default=str) + serialized_report + json.dumps(observed)
        self.assertEqual(report["status"], "error")
        self.assertEqual(observed["exit_code"], 1)
        self.assertNotIn(sentinel, rendered)

    def test_client_failure_is_sanitized_through_report_notification_and_logs(self):
        sentinel = "SENSITIVE_PROVIDER_SENTINEL"
        observed = {"notifications": [], "printed": [], "exit_code": None}
        runtime = ExecutionRuntime(
            dry_run=True,
            run_id="sanitized-client-failure",
            now_utc=SimpleNamespace(strftime=lambda _fmt: "20260901"),
        )
        runtime.api_key = "unused"
        runtime.api_secret = "unused"

        def execute_cycle(current_runtime):
            return execute_strategy_cycle(
                current_runtime,
                build_execution_report=build_execution_report,
                ensure_runtime_client=lambda current_runtime, report: ensure_runtime_client(
                    current_runtime,
                    report,
                    connect_client_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        RuntimeError(sentinel)
                    ),
                    append_report_error_fn=append_report_error,
                    runtime_notify_fn=lambda current_runtime, report, text: (
                        observed["notifications"].append(text),
                        runtime_notify(current_runtime, report, text),
                    )[1],
                    translate_fn=lambda key, **_kwargs: key,
                    sleep_fn=lambda *_args: None,
                    max_retries=1,
                ),
                load_cycle_execution_settings=lambda: SimpleNamespace(
                    btc_status_report_interval_hours=24,
                    allow_new_trend_entries_on_degraded=False,
                ),
                load_cycle_state=lambda *_args: self.fail("cycle must abort before state load"),
                append_trend_pool_source_logs=lambda *_args: None,
                capture_market_snapshot=lambda *_args: None,
                top_up_bnb_fuel=lambda *_args: (0.0, 0.0, "ready"),
                compute_portfolio_allocation=lambda *_args: None,
                build_balance_snapshot=lambda *_args: None,
                maybe_reset_daily_state=lambda *_args: None,
                maybe_rebase_daily_state_for_balance_change=lambda *_args: None,
                compute_daily_pnls=lambda *_args: None,
                append_portfolio_report=lambda *_args: None,
                run_daily_circuit_breaker=lambda *_args: None,
                execute_trend_rotation=lambda *_args: None,
                execute_btc_dca_cycle=lambda *_args: None,
                manage_usdt_earn_buffer_runtime=lambda *_args: None,
                maybe_send_periodic_btc_status_report=lambda *_args: None,
                runtime_set_trade_state=lambda *_args: None,
                append_report_error=append_report_error,
                runtime_notify=runtime_notify,
                translate_fn=lambda key, **_kwargs: key,
                traceback_module=SimpleNamespace(print_exc=lambda: self.fail("must not print traceback")),
            )

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch(
                "application.cycle_service.persist_runtime_report",
                return_value=SimpleNamespace(local_path=None, cloud_uri=None),
            ):
                report, output_path = run_live_cycle(
                    runtime_builder=lambda: runtime,
                    execute_cycle=execute_cycle,
                    output_printer=lambda text: observed["printed"].append(text),
                    report_writer=lambda current_report: write_execution_report(
                        current_report,
                        reports_dir=tmp_dir,
                        filename="execution_report.json",
                    ),
                    exit_fn=lambda code: observed.update(exit_code=code),
                )
            with open(output_path, "r") as handle:
                serialized_report = handle.read()

        rendered = serialized_report + json.dumps(observed, default=str)
        self.assertEqual(report["status"], "aborted")
        self.assertEqual(report["error_summary"]["errors"], [
            {"stage": "client", "message": "client_connection_failed"}
        ])
        self.assertEqual(observed["exit_code"], 1)
        self.assertNotIn(sentinel, rendered)

    def test_execute_strategy_cycle_returns_aborted_report_when_client_unavailable(self):
        runtime = SimpleNamespace(
            dry_run=True,
            print_traceback=False,
            now_utc=SimpleNamespace(strftime=lambda _fmt: "20260329"),
        )
        report = execute_strategy_cycle(
            runtime,
            build_execution_report=lambda _runtime: {"status": "ok", "log_lines": []},
            ensure_runtime_client=lambda _runtime, report: report.update(status="aborted") or False,
            load_cycle_execution_settings=lambda: SimpleNamespace(
                btc_status_report_interval_hours=24,
                allow_new_trend_entries_on_degraded=False,
            ),
            load_cycle_state=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not load state")),
            append_trend_pool_source_logs=lambda *_args, **_kwargs: None,
            capture_market_snapshot=lambda *_args, **_kwargs: None,
            top_up_bnb_fuel=lambda *_args, **_kwargs: (0.0, 0.0, "ready"),
            compute_portfolio_allocation=lambda *_args, **_kwargs: None,
            build_balance_snapshot=lambda *_args, **_kwargs: {},
            maybe_reset_daily_state=lambda *_args, **_kwargs: None,
            maybe_rebase_daily_state_for_balance_change=lambda *_args, **_kwargs: False,
            compute_daily_pnls=lambda *_args, **_kwargs: (0.0, 0.0),
            append_portfolio_report=lambda *_args, **_kwargs: None,
            run_daily_circuit_breaker=lambda *_args, **_kwargs: False,
            execute_trend_rotation=lambda *_args, **_kwargs: None,
            execute_btc_dca_cycle=lambda *_args, **_kwargs: None,
            manage_usdt_earn_buffer_runtime=lambda *_args, **_kwargs: None,
            maybe_send_periodic_btc_status_report=lambda *_args, **_kwargs: None,
            runtime_set_trade_state=lambda *_args, **_kwargs: None,
            append_report_error=lambda *_args, **_kwargs: None,
            runtime_notify=lambda *_args, **_kwargs: None,
            translate_fn=lambda key, **kwargs: key.format(**kwargs) if kwargs else key,
            traceback_module=SimpleNamespace(print_exc=lambda: None),
        )
        self.assertEqual(report["status"], "aborted")

    def test_execute_strategy_cycle_captures_unhandled_exception(self):
        runtime = SimpleNamespace(
            dry_run=True,
            print_traceback=True,
            now_utc=SimpleNamespace(strftime=lambda _fmt: "20260329"),
            tg_token="",
            tg_chat_id="",
        )
        observed = {"errors": [], "notifications": [], "tracebacks": 0}
        report = execute_strategy_cycle(
            runtime,
            build_execution_report=lambda _runtime: {"status": "ok", "log_lines": []},
            ensure_runtime_client=lambda *_args, **_kwargs: True,
            load_cycle_execution_settings=lambda: SimpleNamespace(
                btc_status_report_interval_hours=24,
                allow_new_trend_entries_on_degraded=False,
            ),
            load_cycle_state=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider-secret-cycle-error")),
            append_trend_pool_source_logs=lambda *_args, **_kwargs: None,
            capture_market_snapshot=lambda *_args, **_kwargs: None,
            top_up_bnb_fuel=lambda *_args, **_kwargs: (0.0, 0.0, "ready"),
            compute_portfolio_allocation=lambda *_args, **_kwargs: None,
            build_balance_snapshot=lambda *_args, **_kwargs: {},
            maybe_reset_daily_state=lambda *_args, **_kwargs: None,
            maybe_rebase_daily_state_for_balance_change=lambda *_args, **_kwargs: False,
            compute_daily_pnls=lambda *_args, **_kwargs: (0.0, 0.0),
            append_portfolio_report=lambda *_args, **_kwargs: None,
            run_daily_circuit_breaker=lambda *_args, **_kwargs: False,
            execute_trend_rotation=lambda *_args, **_kwargs: None,
            execute_btc_dca_cycle=lambda *_args, **_kwargs: None,
            manage_usdt_earn_buffer_runtime=lambda *_args, **_kwargs: None,
            maybe_send_periodic_btc_status_report=lambda *_args, **_kwargs: None,
            runtime_set_trade_state=lambda *_args, **_kwargs: None,
            append_report_error=lambda report, message, stage: observed["errors"].append((stage, message)),
            runtime_notify=lambda _runtime, _report, text: observed["notifications"].append(text),
            translate_fn=lambda key, **kwargs: key,
            traceback_module=SimpleNamespace(
                print_exc=lambda: observed.update(tracebacks=observed["tracebacks"] + 1)
            ),
        )
        self.assertEqual(report["status"], "error")
        self.assertEqual(observed["errors"], [("execute_cycle", "cycle_execution_failed")])
        self.assertEqual(observed["tracebacks"], 0)
        self.assertEqual(report["diagnostics"]["cycle_failure"], {"stage": "state_load", "error_type": "runtime_error"})
        self.assertEqual(observed["notifications"], ["system_crash\ncycle_execution_failed"])
        self.assertNotIn("provider-secret-cycle-error", str(report) + str(observed))

    def test_owner_failure_is_safe_and_does_not_reach_client_or_release_lock(self):
        from runtime_support import StatePersistenceError

        secret = "private-provider-message"
        custom_error = type("private-provider-class", (Exception,), {})
        cases = [
            (StatePersistenceError(secret), "state_persistence_error"),
            (TimeoutError(secret), "timeout_error"),
            (custom_error(secret), "unclassified_error"),
        ]
        for error, expected_type in cases:
            with self.subTest(expected_type=expected_type):
                runtime = SimpleNamespace(dry_run=False, state_owner_held=True)
                callbacks = {
                    name: Mock()
                    for name in inspect.signature(execute_strategy_cycle).parameters
                    if name != "runtime"
                }
                callbacks["build_execution_report"].return_value = {
                    "status": "ok", "diagnostics": {"existing": "preserved"},
                }
                callbacks["load_cycle_execution_settings"].return_value = SimpleNamespace(
                    btc_status_report_interval_hours=24,
                    allow_new_trend_entries_on_degraded=False,
                )
                callbacks["translate_fn"].return_value = "system_crash"
                with patch("application.cycle_service.acquire_runtime_state_owner", side_effect=error), patch(
                    "application.cycle_service.release_runtime_state_owner"
                ) as release:
                    report = execute_strategy_cycle(runtime, **callbacks)
                self.assertEqual(report["status"], "error")
                self.assertEqual(report["diagnostics"], {
                    "existing": "preserved",
                    "cycle_failure": {"stage": "state_owner_claim", "error_type": expected_type},
                })
                callbacks["ensure_runtime_client"].assert_not_called()
                callbacks["load_cycle_state"].assert_not_called()
                callbacks["execute_trend_rotation"].assert_not_called()
                callbacks["execute_btc_dca_cycle"].assert_not_called()
                callbacks["runtime_set_trade_state"].assert_not_called()
                release.assert_not_called()
                self.assertTrue(runtime.state_owner_held)
                self.assertIn("stage=state_owner_claim", report["log_lines"][0])
                self.assertNotIn("private-provider", str(report) + str(callbacks["runtime_notify"].call_args))

    def test_reconciliation_exhaustion_stops_second_logical_order_and_fails_report(self):
        observed = {"submissions": [], "reconciliations": []}

        class OrderNotFound(Exception):
            code = -2013

        class Client:
            def order_market_buy(self, **payload):
                observed["submissions"].append((payload["symbol"], payload["newClientOrderId"]))
                raise TimeoutError("provider-submit-secret")

            def get_order(self, *, symbol, origClientOrderId):
                observed["reconciliations"].append((symbol, origClientOrderId))
                raise OrderNotFound("provider-query-secret")

        runtime = ExecutionRuntime(
            dry_run=False,
            run_id="cycle-reconciliation-exhausted",
            state_owner_claim=lambda _owner: True,
            state_owner_release=lambda _owner: True,
            client=Client(),
            state_loader=lambda *, normalize=False: {"order_submission": {"state": "RESERVED"}},
            state_writer=lambda _state: True,
        )

        def execute_trend_rotation(current_runtime, report, state, *_args, **_kwargs):
            return execute_trend_buys(
                current_runtime,
                report,
                state,
                {
                    "ETHUSDT": {"weight": 0.5, "relative_score": 1.2},
                    "SOLUSDT": {"weight": 0.5, "relative_score": 1.1},
                },
                ["ETHUSDT", "SOLUSDT"],
                {"ETHUSDT": 100.0, "SOLUSDT": 100.0},
                {"ETHUSDT": 100.0, "SOLUSDT": 50.0},
                {"ETHUSDT": 0.0, "SOLUSDT": 0.0},
                500.0,
                [],
                "20260901",
                should_skip_duplicate_trend_action_fn=lambda *_args: False,
                append_log_fn=lambda *_args: None,
                translate_fn=lambda key, **_kwargs: key,
                format_qty_fn=lambda *_args: 1.0,
                ensure_asset_available_fn=lambda *_args: True,
                runtime_call_client_fn=lambda runtime, report, **kwargs: runtime_call_client(
                    runtime,
                    report,
                    max_retries=1,
                    retry_base_sec=0,
                    **kwargs,
                ),
                next_order_id_fn=lambda _runtime, _prefix, symbol: f"buy-{symbol}",
                set_symbol_trade_state_fn=lambda *_args: None,
                record_trend_action_fn=lambda *_args: None,
                runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
                runtime_notify_fn=lambda *_args: None,
            )

        report = execute_strategy_cycle(
            runtime,
            build_execution_report=build_execution_report,
            ensure_runtime_client=lambda *_args: True,
            load_cycle_execution_settings=lambda: SimpleNamespace(
                btc_status_report_interval_hours=24,
                allow_new_trend_entries_on_degraded=False,
            ),
            load_cycle_state=lambda *_args: (
                {},
                {"degraded": False},
                {"ETHUSDT": {}, "SOLUSDT": {}},
                True,
            ),
            append_trend_pool_source_logs=lambda *_args: None,
            capture_market_snapshot=lambda *_args: {
                "u_total": 500.0,
                "fuel_val": 0.0,
                "dynamic_usdt_buffer": 100.0,
                "prices": {"BTCUSDT": 50_000.0, "ETHUSDT": 100.0, "SOLUSDT": 50.0},
                "balances": {"BTCUSDT": 0.0, "ETHUSDT": 0.0, "SOLUSDT": 0.0},
                "btc_snapshot": {},
                "trend_indicators": {},
            },
            top_up_bnb_fuel=lambda *_args: (500.0, 0.0, "ready"),
            compute_portfolio_allocation=lambda *_args: {
                "total_equity": 500.0,
                "trend_val": 0.0,
                "execution_permitted": True,
            },
            build_balance_snapshot=lambda *_args: {},
            maybe_reset_daily_state=lambda *_args: None,
            maybe_rebase_daily_state_for_balance_change=lambda *_args: None,
            compute_daily_pnls=lambda *_args: (0.0, 0.0),
            append_portfolio_report=lambda *_args: None,
            run_daily_circuit_breaker=lambda *_args: False,
            execute_trend_rotation=execute_trend_rotation,
            execute_btc_dca_cycle=lambda *_args: None,
            manage_usdt_earn_buffer_runtime=lambda *_args, **_kwargs: None,
            maybe_send_periodic_btc_status_report=lambda *_args, **_kwargs: None,
            runtime_set_trade_state=lambda *_args, **_kwargs: None,
            append_report_error=append_report_error,
            runtime_notify=lambda *_args: None,
            translate_fn=lambda key, **_kwargs: key,
            traceback_module=SimpleNamespace(),
        )

        self.assertEqual(
            [symbol for symbol, _order_id in observed["submissions"]],
            ["ETHUSDT"],
        )
        self.assertEqual(len({order_id for _symbol, order_id in observed["submissions"]}), 1)
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["diagnostics"]["cycle_failure"], {"stage": "trend_execution", "error_type": "order_reconciliation_error"})
        self.assertEqual(
            report["error_summary"]["errors"],
            [{"stage": "execute_cycle", "message": "cycle_execution_failed"}],
        )


if __name__ == "__main__":
    unittest.main()
