import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from application.cycle_service import execute_strategy_cycle, run_live_cycle, write_execution_report


class CycleServiceTests(unittest.TestCase):
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
                    "STRATEGY_PROFILE": "crypto_leader_rotation",
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
                    "STRATEGY_PROFILE": "crypto_leader_rotation",
                    "SERVICE_NAME": "binance-quant",
                    "LOG_DEPLOY_TARGET": "vps",
                },
                clear=False,
            ):
                report, _output_path = run_live_cycle(
                    runtime_builder=lambda: SimpleNamespace(run_id="run-001", dry_run=True),
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
        self.assertEqual(start_log["strategy_profile"], "crypto_leader_rotation")
        self.assertEqual(start_log["run_id"], "run-001")
        self.assertEqual(end_log["event"], "strategy_cycle_completed")
        self.assertEqual(end_log["status"], "ok")

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
            print_traceback=False,
            now_utc=SimpleNamespace(strftime=lambda _fmt: "20260329"),
            tg_token="",
            tg_chat_id="",
        )
        observed = {"errors": []}
        report = execute_strategy_cycle(
            runtime,
            build_execution_report=lambda _runtime: {"status": "ok", "log_lines": []},
            ensure_runtime_client=lambda *_args, **_kwargs: True,
            load_cycle_execution_settings=lambda: SimpleNamespace(
                btc_status_report_interval_hours=24,
                allow_new_trend_entries_on_degraded=False,
            ),
            load_cycle_state=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
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
            runtime_notify=lambda *_args, **_kwargs: None,
            translate_fn=lambda key, **kwargs: key,
            traceback_module=SimpleNamespace(print_exc=lambda: None),
        )
        self.assertEqual(report["status"], "error")
        self.assertEqual(observed["errors"], [("execute_cycle", "boom")])


if __name__ == "__main__":
    unittest.main()
