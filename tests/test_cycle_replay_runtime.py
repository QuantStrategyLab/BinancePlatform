import contextlib
import hashlib
import io
import json
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
RISK_EVALUATION_TIME = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def canonical_digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def valid_release_identity():
    return {
        "strategy_profile": "crypto_live_pool_rotation",
        "mode": "core_major",
        "source_revision": "a" * 40,
        "input_timestamp": "2026-03-10T00:00:00Z",
        "artifact_contract": "qsl.crypto_live_pool.artifact_manifest.v1",
        "artifact_version": "2026-03-10-core_major",
        "artifacts": {
            name: {"sha256": character * 64}
            for name, character in zip(
                ("live_pool", "live_pool_legacy", "latest_ranking", "latest_universe"),
                "1234",
            )
        },
    }


def runtime_evidence_identity(report):
    aggregate = report.get("runtime_evidence_aggregate")
    return {
        "release_identity": report.get("release_identity", {}),
        "account_risk_assessment": report.get("account_risk_assessment", {}),
        "order_authorization": report.get("order_authorization", {}),
        "strategy_stop_evaluation": report.get("strategy_stop_evaluation", {}),
        "account_breaker_evaluation": report.get("account_breaker_evaluation", {}),
        "durable_v2": aggregate,
        "reconciliation": (
            aggregate.get("reconciliation", {})
            if isinstance(aggregate, dict)
            else {"status": "MISSING"}
        ),
    }


class CycleReplayRuntimeTests(unittest.TestCase):
    def run_cycle(self, *, run_id, include_release_identity=False):
        runtime, client, state_store, notifier = run_cycle_replay.build_replay_runtime(
            run_id=run_id,
            dry_run=True,
            now_utc=FIXTURE_TIME,
        )
        if include_release_identity:
            runtime.trend_pool_payload["runtime_evidence_identity"] = valid_release_identity()
        output_buffer = io.StringIO()
        with (
            patch("quant_platform_kit.risk.gate._utc_now", return_value=RISK_EVALUATION_TIME),
            contextlib.redirect_stdout(output_buffer),
        ):
            report = main.execute_cycle(runtime)
        return {
            "report": report,
            "client": client,
            "state_store": state_store,
            "notifier": notifier,
        }

    def test_dry_run_produces_no_real_side_effects(self):
        result = self.run_cycle(run_id="dry-run-regression")
        report = result["report"]

        self.assertNotEqual(report["status"], "ok")
        self.assertTrue(report["dry_run"])
        self.assertEqual(result["client"].side_effect_calls, [])
        self.assertEqual(result["state_store"].write_calls, [])
        self.assertEqual(result["notifier"].messages, [])
        self.assertEqual(report["side_effect_summary"]["executed_call_count"], 0)
        self.assertGreater(report["side_effect_summary"]["suppressed_call_count"], 0)
        self.assertEqual(report.get("positions", []), [])
        self.assertEqual(report.get("budgets", []), [])
        self.assertEqual(report["buy_sell_intents"], [])
        self.assertEqual(report["btc_dca_intents"], [])
        self.assertEqual(report["redemption_subscription_intents"], [])
        self.assertEqual(report["selected_symbols"]["active_trend_pool"], [])
        self.assertEqual(
            runtime_evidence_identity(report),
            {
                "release_identity": {},
                "account_risk_assessment": {},
                "order_authorization": {},
                "strategy_stop_evaluation": {},
                "account_breaker_evaluation": {},
                "durable_v2": None,
                "reconciliation": {"status": "MISSING"},
            },
        )

    def test_fixed_input_produces_deterministic_execution_report(self):
        first = self.run_cycle(run_id="deterministic-report", include_release_identity=True)
        second = self.run_cycle(run_id="deterministic-report", include_release_identity=True)

        self.assertEqual(first["report"], second["report"])
        self.assertEqual(canonical_digest(first["report"]), canonical_digest(second["report"]))
        self.assertEqual(
            first["report"]["selected_symbols"]["active_trend_pool"],
            ["ETHUSDT", "SOLUSDT", "XRPUSDT", "LTCUSDT", "BCHUSDT"],
        )
        self.assertEqual(first["report"]["selected_symbols"]["selected_candidates"], ["ETHUSDT", "SOLUSDT"])
        self.assertEqual(first["report"]["selected_symbols"], second["report"]["selected_symbols"])
        self.assertEqual(first["report"]["buy_sell_intents"], [])
        self.assertEqual(first["report"]["btc_dca_intents"], [])
        self.assertEqual(first["report"]["redemption_subscription_intents"], [])
        self.assertEqual(first["client"].side_effect_calls, [])
        self.assertEqual(first["state_store"].write_calls, [])
        self.assertEqual(first["report"]["side_effect_summary"]["executed_call_count"], 0)
        first_identity = runtime_evidence_identity(first["report"])
        self.assertEqual(first_identity, runtime_evidence_identity(second["report"]))
        self.assertEqual(first_identity["account_risk_assessment"]["scope"], "ACCOUNT")
        self.assertEqual(first_identity["account_risk_assessment"]["outcome"], "REJECT")
        self.assertEqual(first_identity["account_risk_assessment"]["effective_exposure_cap"], 0.0)
        self.assertEqual(first_identity["order_authorization"]["outcome"], "REJECT")
        self.assertEqual(first_identity["order_authorization"]["mandate_scope"], "RESEARCH_ONLY")
        self.assertTrue(first_identity["strategy_stop_evaluation"]["evaluated"])
        self.assertTrue(first_identity["account_breaker_evaluation"]["evaluated"])
        self.assertIsNone(first_identity["durable_v2"])
        self.assertEqual(first_identity["reconciliation"], {"status": "MISSING"})

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
