import contextlib
import io
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path


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
    def run_cycle(self, *, run_id):
        output_buffer = io.StringIO()
        with contextlib.redirect_stdout(output_buffer):
            return run_cycle_replay.run_replay_cycle(
                run_id=run_id,
                dry_run=True,
                now_utc=FIXTURE_TIME,
            )

    def test_dry_run_produces_no_real_side_effects(self):
        result = self.run_cycle(run_id="dry-run-regression")
        report = result["report"]

        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["dry_run"])
        self.assertEqual(result["client"].side_effect_calls, [])
        self.assertEqual(result["state_store"].write_calls, [])
        self.assertEqual(report["side_effect_summary"]["executed_call_count"], 0)
        self.assertGreater(report["side_effect_summary"]["suppressed_call_count"], 0)
        self.assertGreaterEqual(len(report["buy_sell_intents"]), 2)
        self.assertGreaterEqual(len(report["redemption_subscription_intents"]), 1)

    def test_fixed_input_produces_deterministic_execution_report(self):
        first = self.run_cycle(run_id="deterministic-report")
        second = self.run_cycle(run_id="deterministic-report")

        self.assertEqual(first["report"], second["report"])
        self.assertEqual(
            first["report"]["selected_symbols"]["active_trend_pool"],
            ["ETHUSDT", "SOLUSDT", "XRPUSDT", "LTCUSDT", "BCHUSDT"],
        )
        trend_buy_symbols = [
            intent["symbol"]
            for intent in first["report"]["buy_sell_intents"]
            if intent["category"] == "trend" and intent["action"] == "buy"
        ]
        self.assertEqual(trend_buy_symbols, ["ETHUSDT", "SOLUSDT"])
        self.assertEqual(first["report"]["btc_dca_intents"][0]["action"], "buy")
        self.assertEqual(first["report"]["redemption_subscription_intents"][0]["action"], "subscribe")
        self.assertAlmostEqual(first["report"]["redemption_subscription_intents"][0]["amount"], 71.5)

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
