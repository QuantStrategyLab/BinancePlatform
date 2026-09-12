import contextlib
import io
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import ExitStack
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
from quant_platform_kit.risk.contracts import CandidateRiskIdentity


FIXTURE_TIME = datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)


def _synthetic_candidate() -> CandidateRiskIdentity:
    return CandidateRiskIdentity(
        strategy_profile="crypto_live_pool_rotation",
        account_mode="single_strategy_account_v1",
        strategy_revision="1" * 40,
        runner_revision="2" * 40,
        config_sha256="3" * 64,
        input_manifest_sha256="4" * 64,
        authority_receipt_sha256="5" * 64,
    )


def _synthetic_mandate(now: datetime, candidate: CandidateRiskIdentity) -> dict[str, object]:
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "LTCUSDT", "BCHUSDT")
    return {
        "mandate_id": "synthetic_algorithm_equivalence_only",
        "mandate_version": "test-v1",
        "authority_receipt_sha256": candidate.authority_receipt_sha256,
        "authority_scope": "RESEARCH_ONLY",
        "strategy_profile": candidate.strategy_profile,
        "account_mode": candidate.account_mode,
        "strategy_revision": candidate.strategy_revision,
        "runner_revision": candidate.runner_revision,
        "config_sha256": candidate.config_sha256,
        "input_manifest_sha256": candidate.input_manifest_sha256,
        "candidate_identity_sha256": candidate.candidate_sha256,
        "effective_at": (now - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "max_snapshot_age_seconds": 300,
        "effective_exposure_cap": 1.0,
        "loss_budget": 1_000.0,
        "product_caps": {symbol: 1.0 for symbol in symbols},
        "nominal_caps": {symbol: 1.0 for symbol in symbols},
        "product_leverage_factors": {symbol: 1 for symbol in symbols},
        "allowed_nonzero_assets": list(symbols),
        "source_revision": "6" * 40,
    }


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
        self.assertEqual(report["risk_outcome"], "REJECT")
        self.assertEqual(
            report["risk_reason_codes"],
            ["invalid_mandate", "missing_candidate_identity"],
        )
        self.assertTrue(decisions)
        for decision in decisions:
            assessment = decision.diagnostics["member_risk_assessment"]
            self.assertEqual(assessment["outcome"], "REJECT")
            self.assertEqual(
                assessment["reason_codes"],
                ("invalid_mandate", "missing_candidate_identity"),
            )
        self.assertEqual(report["redemption_subscription_intents"], [])

    def test_fixed_input_produces_deterministic_execution_report(self):
        with patch("quant_platform_kit.risk.gate._utc_now", return_value=FIXTURE_TIME):
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

    def test_bound_research_fixture_runs_full_cycle_to_mapper_without_side_effects(self):
        now = FIXTURE_TIME
        runtime, client, state_store, _ = run_cycle_replay.build_replay_runtime(
            run_id="bound-research-fixture",
            dry_run=True,
            now_utc=FIXTURE_TIME,
        )
        candidate = _synthetic_candidate()
        runtime.mandate_provenance = _synthetic_mandate(now, candidate)
        runtime.candidate_risk_identity = candidate
        decisions = []
        stages = []
        original_mapper = main.map_decision_to_allocation

        def capture_mapper(decision, *, account_metrics):
            decisions.append(decision)
            allocation = original_mapper(decision, account_metrics=account_metrics)
            self.assertTrue(allocation["execution_permitted"])
            return allocation

        def record_stage(name, original):
            def wrapped(*args, **kwargs):
                stages.append(name)
                return original(*args, **kwargs)

            return wrapped

        with ExitStack() as stack:
            stack.enter_context(
                patch("quant_platform_kit.risk.gate._utc_now", return_value=FIXTURE_TIME)
            )
            stack.enter_context(patch.object(main, "map_decision_to_allocation", side_effect=capture_mapper))
            stack.enter_context(
                patch.object(
                    main,
                    "_maybe_rebase_daily_state_for_balance_change",
                    side_effect=record_stage(
                        "daily", main._maybe_rebase_daily_state_for_balance_change
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    main,
                    "_maybe_reset_daily_state",
                    side_effect=record_stage("daily", main._maybe_reset_daily_state),
                )
            )
            stack.enter_context(
                patch.object(
                    main,
                    "_top_up_bnb_fuel",
                    side_effect=record_stage("fuel", main._top_up_bnb_fuel),
                )
            )
            stack.enter_context(
                patch.object(
                    main,
                    "_execute_trend_rotation",
                    side_effect=record_stage("trend", main._execute_trend_rotation),
                )
            )
            stack.enter_context(
                patch.object(
                    main,
                    "_execute_btc_dca_cycle",
                    side_effect=record_stage("btc", main._execute_btc_dca_cycle),
                )
            )
            stack.enter_context(
                patch.object(
                    main,
                    "manage_usdt_earn_buffer_runtime",
                    side_effect=record_stage("earn", main.manage_usdt_earn_buffer_runtime),
                )
            )
            cycle_service = sys.modules["application.cycle_service"]
            stack.enter_context(
                patch.object(
                    cycle_service,
                    "reconcile_runtime_cash_effects",
                    side_effect=record_stage(
                        "accounting", cycle_service.reconcile_runtime_cash_effects
                    ),
                )
            )
            report = main.execute_cycle(runtime)

        self.assertTrue(decisions)
        self.assertTrue(all(decision.diagnostics["member_risk_assessment"]["outcome"] == "APPROVE" for decision in decisions))
        self.assertEqual(report["status"], "ok")
        self.assertNotIn("execution_blocked_reason", report)
        self.assertEqual(report["error_summary"]["errors"], [])
        self.assertEqual(report["risk_outcome"], "APPROVE")
        self.assertEqual(report["risk_reason_codes"], [])
        self.assertTrue({"daily", "accounting", "fuel", "trend", "btc", "earn"}.issubset(stages))
        self.assertEqual(client.side_effect_calls, [])
        self.assertEqual(state_store.write_calls, [])
        self.assertTrue(report["dry_run"])
        self.assertGreater(report["side_effect_summary"]["suppressed_call_count"], 0)

    def test_bnb_fuel_position_does_not_hide_held_non_candidate_trend_stop(self):
        runtime, _client, state_store, _ = run_cycle_replay.build_replay_runtime(
            run_id="held-trend-stop-with-bnb-fuel",
            dry_run=True,
            now_utc=FIXTURE_TIME,
        )
        state_store.raw_state["ETHUSDT"].update({"is_holding": True})
        candidate = _synthetic_candidate()
        runtime.mandate_provenance = _synthetic_mandate(FIXTURE_TIME, candidate)
        runtime.candidate_risk_identity = candidate
        decisions = []
        original_mapper = main.map_decision_to_allocation

        def capture_mapper(decision, *, account_metrics):
            decisions.append(decision)
            return original_mapper(decision, account_metrics=account_metrics)

        with patch("quant_platform_kit.risk.gate._utc_now", return_value=FIXTURE_TIME):
            with patch.object(main, "map_decision_to_allocation", side_effect=capture_mapper):
                report = main.execute_cycle(runtime)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["execution_blocked_reason"], "risk_execution_not_permitted")
        self.assertEqual(len(decisions), 1)
        decision = decisions[0]
        self.assertEqual(decision.diagnostics["member_risk_assessment"]["outcome"], "APPROVE")
        self.assertEqual(decision.diagnostics["strategy_stop_evaluation"]["outcome"], "TRIGGERED")
        self.assertIn("ETHUSDT", decision.diagnostics["sell_reasons"])
        self.assertNotIn("BNBUSDT", decision.diagnostics["sell_reasons"])

    def test_fake_live_reject_with_low_bnb_and_earn_balance_submits_no_funds_actions(self):
        runtime, client, _state_store, _ = run_cycle_replay.build_replay_runtime(
            run_id="fake-live-risk-reject",
            dry_run=False,
            now_utc=FIXTURE_TIME,
        )
        runtime.research_cycle_settings = None
        runtime.state_owner_claim = lambda _owner: True
        runtime.state_owner_release = lambda _owner: True
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
            ("invalid_mandate", "missing_candidate_identity"),
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
