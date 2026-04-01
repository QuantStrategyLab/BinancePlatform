import unittest

from runtime_support import ExecutionRuntime, build_execution_report, record_gating_event


class TestBuildExecutionReport(unittest.TestCase):
    def test_report_contains_enrichment_fields(self):
        runtime = ExecutionRuntime(dry_run=True, run_id="test-001")
        report = build_execution_report(runtime)
        self.assertIsNone(report["total_equity_usdt"])
        self.assertIsNone(report["trend_equity_usdt"])
        self.assertFalse(report["circuit_breaker_triggered"])
        self.assertIsNone(report["degraded_mode_level"])
        self.assertEqual(report["upstream_pool_symbols"], [])
        self.assertEqual(report["gating_summary"], {})
        self.assertEqual(report["gating_events"], [])

    def test_report_preserves_existing_fields(self):
        runtime = ExecutionRuntime(dry_run=False, run_id="test-002")
        report = build_execution_report(runtime)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["run_id"], "test-002")
        self.assertFalse(report["dry_run"])
        self.assertIn("buy_sell_intents", report)
        self.assertIn("log_lines", report)

    def test_record_gating_event_updates_summary_and_events(self):
        report = {}

        record_gating_event(
            report,
            gate="trend_buy_below_min_budget",
            category="trend",
            symbol="ETHUSDT",
            detail={"budget_usdt": 12.0},
        )
        record_gating_event(
            report,
            gate="trend_buy_below_min_budget",
            category="trend",
        )

        self.assertEqual(report["gating_summary"]["trend_buy_below_min_budget"], 2)
        self.assertEqual(report["gating_events"][0]["symbol"], "ETHUSDT")
        self.assertEqual(report["gating_events"][0]["detail"]["budget_usdt"], 12.0)
