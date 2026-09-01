import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from application.cycle_service import execute_strategy_cycle, run_live_cycle, write_execution_report
from application.execution_service import execute_trend_buys
from infra.binance_runtime import ensure_runtime_client
from runtime_support import (
    ExecutionRuntime,
    append_report_error,
    build_execution_report,
    runtime_call_client,
    runtime_notify,
)


class CycleServiceTests(unittest.TestCase):
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
        self.assertEqual(observed["notifications"], ["system_crash\ncycle_execution_failed"])
        self.assertNotIn("provider-secret-cycle-error", str(report) + str(observed))

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

        runtime = ExecutionRuntime(dry_run=False, run_id="cycle-reconciliation-exhausted", client=Client())

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
            compute_portfolio_allocation=lambda *_args: {
                "total_equity": 500.0,
                "trend_val": 0.0,
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
        self.assertEqual(
            report["error_summary"]["errors"],
            [{"stage": "execute_cycle", "message": "cycle_execution_failed"}],
        )


if __name__ == "__main__":
    unittest.main()
