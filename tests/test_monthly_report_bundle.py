import json
import os
import tempfile
import unittest


class TestMonthlyReportBundle(unittest.TestCase):
    def _make_report(self, run_id, status="ok", total_equity=1000.0,
                     trend_equity=200.0, circuit_breaker=False,
                     degraded_level=None, pool_symbols=None,
                     buy_sell_intents=None, btc_dca_intents=None,
                     redemption_intents=None, errors=None,
                     gating_summary=None, dry_run=False,
                     executed_calls=0, suppressed_calls=0):
        return {
            "status": status,
            "run_id": run_id,
            "dry_run": dry_run,
            "total_equity_usdt": total_equity,
            "trend_equity_usdt": trend_equity,
            "circuit_breaker_triggered": circuit_breaker,
            "degraded_mode_level": degraded_level,
            "upstream_pool_symbols": pool_symbols or ["ETHUSDT", "SOLUSDT"],
            "buy_sell_intents": buy_sell_intents or [],
            "btc_dca_intents": btc_dca_intents or [],
            "redemption_subscription_intents": redemption_intents or [],
            "error_summary": {"errors": errors or []},
            "log_lines": [],
            "selected_symbols": {
                "active_trend_pool": [],
                "selected_candidates": [],
            },
            "notifications": [],
            "state_write_intents": [],
            "gating_summary": gating_summary or {},
            "gating_events": [],
            "side_effect_summary": {
                "executed_call_count": executed_calls,
                "suppressed_call_count": suppressed_calls,
            },
        }

    def test_aggregate_basic(self):
        from scripts.run_monthly_report_bundle import aggregate_hourly_reports

        reports = {
            "2026-03-01T0000.json": self._make_report("r1", total_equity=1000.0),
            "2026-03-01T0100.json": self._make_report("r2", total_equity=1010.0),
            "2026-03-31T2300.json": self._make_report("r3", total_equity=1050.0),
        }
        with tempfile.TemporaryDirectory() as td:
            hourly_dir = os.path.join(td, "hourly", "2026-03")
            os.makedirs(hourly_dir)
            for fname, data in reports.items():
                with open(os.path.join(hourly_dir, fname), "w") as f:
                    json.dump(data, f)

            bundle = aggregate_hourly_reports(hourly_dir, "2026-03")

        self.assertEqual(bundle["report_month"], "2026-03")
        self.assertEqual(bundle["run_statistics"]["total_runs"], 3)
        self.assertEqual(bundle["run_statistics"]["successful_runs"], 3)
        self.assertEqual(bundle["run_statistics"]["failed_runs"], 0)
        self.assertEqual(bundle["run_statistics"]["dry_run_runs"], 0)
        self.assertEqual(bundle["pnl_overview"]["start_equity_usdt"], 1000.0)
        self.assertEqual(bundle["pnl_overview"]["end_equity_usdt"], 1050.0)

    def test_aggregate_with_failures(self):
        from scripts.run_monthly_report_bundle import aggregate_hourly_reports

        reports = {
            "2026-03-01T0000.json": self._make_report("r1"),
            "2026-03-01T0100.json": self._make_report("r2", status="error",
                errors=[{"stage": "client", "message": "timeout"}]),
        }
        with tempfile.TemporaryDirectory() as td:
            hourly_dir = os.path.join(td, "hourly", "2026-03")
            os.makedirs(hourly_dir)
            for fname, data in reports.items():
                with open(os.path.join(hourly_dir, fname), "w") as f:
                    json.dump(data, f)

            bundle = aggregate_hourly_reports(hourly_dir, "2026-03")

        self.assertEqual(bundle["run_statistics"]["failed_runs"], 1)
        self.assertIn("timeout", str(bundle["error_summary"]))

    def test_aggregate_circuit_breaker_events(self):
        from scripts.run_monthly_report_bundle import aggregate_hourly_reports

        reports = {
            "2026-03-07T1400.json": self._make_report("r1", circuit_breaker=True),
            "2026-03-08T0000.json": self._make_report("r2"),
        }
        with tempfile.TemporaryDirectory() as td:
            hourly_dir = os.path.join(td, "hourly", "2026-03")
            os.makedirs(hourly_dir)
            for fname, data in reports.items():
                with open(os.path.join(hourly_dir, fname), "w") as f:
                    json.dump(data, f)

            bundle = aggregate_hourly_reports(hourly_dir, "2026-03")

        self.assertEqual(len(bundle["circuit_breaker_events"]), 1)
        self.assertEqual(bundle["circuit_breaker_events"][0]["run_id"], "r1")

    def test_aggregate_pool_changes(self):
        from scripts.run_monthly_report_bundle import aggregate_hourly_reports

        reports = {
            "2026-03-01T0000.json": self._make_report("r1", pool_symbols=["ETHUSDT", "SOLUSDT"]),
            "2026-03-15T0000.json": self._make_report("r2", pool_symbols=["ETHUSDT", "NEARUSDT"]),
        }
        with tempfile.TemporaryDirectory() as td:
            hourly_dir = os.path.join(td, "hourly", "2026-03")
            os.makedirs(hourly_dir)
            for fname, data in reports.items():
                with open(os.path.join(hourly_dir, fname), "w") as f:
                    json.dump(data, f)

            bundle = aggregate_hourly_reports(hourly_dir, "2026-03")

        self.assertEqual(len(bundle["upstream_pool_changes"]), 1)
        self.assertIn("NEARUSDT", bundle["upstream_pool_changes"][0]["added"])
        self.assertIn("SOLUSDT", bundle["upstream_pool_changes"][0]["removed"])

    def test_format_review_markdown(self):
        from scripts.run_monthly_report_bundle import aggregate_hourly_reports, format_review_markdown

        reports = {
            "2026-03-01T0000.json": self._make_report("r1"),
        }
        with tempfile.TemporaryDirectory() as td:
            hourly_dir = os.path.join(td, "hourly", "2026-03")
            os.makedirs(hourly_dir)
            for fname, data in reports.items():
                with open(os.path.join(hourly_dir, fname), "w") as f:
                    json.dump(data, f)

            bundle = aggregate_hourly_reports(hourly_dir, "2026-03")
            md = format_review_markdown(bundle)

        self.assertIn("Monthly Execution Review", md)
        self.assertIn("2026-03", md)
        self.assertIn("downstream monthly execution review", md)
        self.assertIn("not a pure upstream pool publication", md)
        self.assertIn("external balance flows", md)
        self.assertIn("recorded strategy intents", md)
        self.assertIn("Execution Gating / No-Trade Reasons", md)

    def test_aggregate_gating_and_side_effects(self):
        from scripts.run_monthly_report_bundle import aggregate_hourly_reports, format_review_markdown

        reports = {
            "2026-03-01T0000.json": self._make_report(
                "r1",
                dry_run=True,
                executed_calls=1,
                suppressed_calls=3,
                gating_summary={"trend_buy_below_min_budget": 2},
            ),
            "2026-03-01T0100.json": self._make_report(
                "r2",
                executed_calls=2,
                suppressed_calls=0,
                gating_summary={"btc_dca_pool_too_small": 1, "trend_buy_below_min_budget": 1},
            ),
        }
        with tempfile.TemporaryDirectory() as td:
            hourly_dir = os.path.join(td, "hourly", "2026-03")
            os.makedirs(hourly_dir)
            for fname, data in reports.items():
                with open(os.path.join(hourly_dir, fname), "w") as f:
                    json.dump(data, f)

            bundle = aggregate_hourly_reports(hourly_dir, "2026-03")
            md = format_review_markdown(bundle)

        self.assertEqual(bundle["run_statistics"]["dry_run_runs"], 1)
        self.assertEqual(bundle["side_effect_summary"]["executed_call_count"], 3)
        self.assertEqual(bundle["side_effect_summary"]["suppressed_call_count"], 3)
        self.assertEqual(bundle["execution_gating"]["counts"]["trend_buy_below_min_budget"], 3)
        self.assertEqual(bundle["execution_gating"]["counts"]["btc_dca_pool_too_small"], 1)
        self.assertIn("trend_buy_below_min_budget", md)
        self.assertIn("Dry-run runs", md)
