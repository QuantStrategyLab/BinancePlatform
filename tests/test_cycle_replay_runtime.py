import contextlib
import io
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


def install_test_stubs():
    if "binance" not in sys.modules:
        binance_module = types.ModuleType("binance")
        client_module = types.ModuleType("binance.client")
        exceptions_module = types.ModuleType("binance.exceptions")

        class Client:
            KLINE_INTERVAL_1DAY = "1d"

            def __init__(self, *args, **kwargs):
                pass

            def ping(self):
                return None

        class BinanceAPIException(Exception):
            pass

        client_module.Client = Client
        exceptions_module.BinanceAPIException = BinanceAPIException
        binance_module.client = client_module
        binance_module.exceptions = exceptions_module
        sys.modules["binance"] = binance_module
        sys.modules["binance.client"] = client_module
        sys.modules["binance.exceptions"] = exceptions_module

    if "requests" not in sys.modules:
        requests_module = types.ModuleType("requests")
        requests_module.post = lambda *args, **kwargs: None
        sys.modules["requests"] = requests_module

    if "google" not in sys.modules:
        sys.modules["google"] = types.ModuleType("google")
    if "google.cloud" not in sys.modules:
        cloud_module = types.ModuleType("google.cloud")
        sys.modules["google.cloud"] = cloud_module
        sys.modules["google"].cloud = cloud_module
    if "google.cloud.firestore" not in sys.modules:
        firestore_module = types.ModuleType("google.cloud.firestore")

        class FirestoreClient:
            def collection(self, *args, **kwargs):
                return self

            def document(self, *args, **kwargs):
                return self

            def get(self):
                raise RuntimeError("stub Firestore client should be patched in unit tests")

            def set(self, *args, **kwargs):
                return None

        firestore_module.Client = FirestoreClient
        sys.modules["google.cloud.firestore"] = firestore_module
        sys.modules["google.cloud"].firestore = firestore_module


install_test_stubs()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
PLATFORM_KIT_SRC = PROJECT_ROOT.parent / "QuantPlatformKit" / "src"
CRYPTO_STRATEGIES_SRC = PROJECT_ROOT.parent / "CryptoStrategies" / "src"
for path in (PLATFORM_KIT_SRC, CRYPTO_STRATEGIES_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import main
import run_cycle_replay


FIXTURE_TIME = datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)


class CycleReplayRuntimeTests(unittest.TestCase):
    def run_cycle(self, *, run_id, capture_decisions=False):
        output_buffer = io.StringIO()
        decisions = []

        def capture_mapper(decision, *, account_metrics):
            decisions.append(decision)
            return original_mapper(decision, account_metrics=account_metrics)

        original_mapper = main.map_decision_to_allocation
        with contextlib.redirect_stdout(output_buffer):
            with patch.object(
                main,
                "map_decision_to_allocation",
                side_effect=capture_mapper if capture_decisions else original_mapper,
            ):
                result = run_cycle_replay.run_replay_cycle(
                    run_id=run_id,
                    dry_run=True,
                    now_utc=FIXTURE_TIME,
                )
        return (result, decisions) if capture_decisions else result

    def test_dry_run_produces_no_real_side_effects(self):
        result, decisions = self.run_cycle(
            run_id="dry-run-regression",
            capture_decisions=True,
        )
        report = result["report"]

        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["dry_run"])
        self.assertEqual(result["client"].side_effect_calls, [])
        self.assertEqual(result["state_store"].write_calls, [])
        self.assertEqual(report["side_effect_summary"]["executed_call_count"], 0)
        self.assertGreater(report["side_effect_summary"]["suppressed_call_count"], 0)
        self.assertEqual(report["buy_sell_intents"], [])
        self.assertEqual(report["btc_dca_intents"], [])
        self.assertTrue(decisions)
        for decision in decisions:
            assessment = decision.diagnostics["member_risk_assessment"]
            self.assertEqual(assessment["outcome"], "REJECT")
            self.assertEqual(
                assessment["reason_codes"],
                ("invalid_mandate", "invalid_portfolio_snapshot", "missing_candidate_identity"),
            )
        self.assertEqual(report["redemption_subscription_intents"], [])

    def test_fixed_input_produces_deterministic_execution_report(self):
        first = self.run_cycle(run_id="deterministic-report")
        second = self.run_cycle(run_id="deterministic-report")

        self.assertEqual(first["report"], second["report"])
        self.assertEqual(first["report"]["selected_symbols"]["active_trend_pool"], [])
        self.assertEqual(first["report"]["execution_blocked_reason"], "risk_execution_not_permitted")
        trend_buy_symbols = [
            intent["symbol"]
            for intent in first["report"]["buy_sell_intents"]
            if intent["category"] == "trend" and intent["action"] == "buy"
        ]
        self.assertEqual(trend_buy_symbols, [])
        self.assertEqual(first["report"]["btc_dca_intents"], [])
        self.assertEqual(first["report"]["redemption_subscription_intents"], [])

    def test_fake_live_reject_with_low_bnb_and_earn_balance_submits_no_funds_actions(self):
        runtime, client, _state_store, _ = run_cycle_replay.build_replay_runtime(
            run_id="fake-live-risk-reject",
            dry_run=False,
            now_utc=FIXTURE_TIME,
        )
        runtime.research_cycle_settings = None
        original_state_writer = runtime.state_writer
        runtime.state_writer = lambda state: original_state_writer(state) or True
        client.account_snapshot["spot_balances"]["BNB"] = {"free": "0", "locked": "0"}
        client.account_snapshot["earn_positions"]["USDT"] = {
            "rows": [{"productId": "fixture-earn", "totalAmount": "50"}]
        }
        self.assertTrue(client.account_snapshot["earn_positions"]["USDT"]["rows"])
        decisions = []
        original_mapper = main.map_decision_to_allocation

        def capture_mapper(decision, *, account_metrics):
            decisions.append(decision)
            return original_mapper(decision, account_metrics=account_metrics)

        with patch.object(
            main,
            "rc_load_cycle_execution_settings",
            return_value=types.SimpleNamespace(
                btc_status_report_interval_hours=24,
                allow_new_trend_entries_on_degraded=False,
            ),
        ), patch.object(main, "map_decision_to_allocation", side_effect=capture_mapper):
            report = main.execute_cycle(runtime)

        self.assertFalse(report["dry_run"])
        self.assertEqual(report["execution_blocked_reason"], "risk_execution_not_permitted")
        self.assertEqual(client.side_effect_calls, [])
        self.assertEqual(report["buy_sell_intents"], [])
        self.assertEqual(report["btc_dca_intents"], [])
        self.assertEqual(report["redemption_subscription_intents"], [])
        self.assertTrue(decisions)
        assessment = decisions[0].diagnostics["member_risk_assessment"]
        self.assertEqual(assessment["outcome"], "REJECT")
        self.assertEqual(
            assessment["reason_codes"],
            ("invalid_mandate", "invalid_portfolio_snapshot", "missing_candidate_identity"),
        )

    def test_state_load_failure_aborts_execution_safely(self):
        runtime, client, state_store, _ = run_cycle_replay.build_replay_runtime(
            run_id="state-load-failure",
            dry_run=True,
            now_utc=FIXTURE_TIME,
        )
        runtime.state_loader = lambda *, normalize=False: None

        output_buffer = io.StringIO()
        with contextlib.redirect_stdout(output_buffer):
            report = main.execute_cycle(runtime)

        self.assertEqual(report["status"], "aborted")
        self.assertEqual(client.side_effect_calls, [])
        self.assertEqual(state_store.write_calls, [])
        self.assertEqual(report["buy_sell_intents"], [])
        self.assertTrue(
            any("Failed to load Firestore state" in error["message"] for error in report["error_summary"]["errors"])
        )


if __name__ == "__main__":
    unittest.main()
