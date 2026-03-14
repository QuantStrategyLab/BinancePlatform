import sys
import types
import unittest
from pathlib import Path

import pandas as pd


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
                raise RuntimeError("stub Firestore client should not be used in monitor unit tests")

            def set(self, *args, **kwargs):
                return None

        firestore_module.Client = FirestoreClient
        sys.modules["google.cloud.firestore"] = firestore_module
        sys.modules["google.cloud"].firestore = firestore_module


install_test_stubs()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import run_shadow_candidate_monitor


class ShadowCandidateMonitorTests(unittest.TestCase):
    def test_validate_track_identity_accepts_expected_baseline_and_shadow_candidate(self):
        summary_table = pd.DataFrame(
            [
                {
                    "track_id": "official_baseline",
                    "track_profile": "baseline_blended_rank",
                    "source_track": "official_baseline",
                    "candidate_status": "official_reference",
                },
                {
                    "track_id": "challenger_topk_60",
                    "track_profile": "challenger_topk_60",
                    "source_track": "shadow_candidate",
                    "candidate_status": "shadow_candidate",
                },
            ]
        )

        run_shadow_candidate_monitor.validate_track_identity(summary_table)

    def test_validate_track_identity_rejects_shadow_candidate_marked_as_official(self):
        summary_table = pd.DataFrame(
            [
                {
                    "track_id": "official_baseline",
                    "track_profile": "baseline_blended_rank",
                    "source_track": "official_baseline",
                    "candidate_status": "official_reference",
                },
                {
                    "track_id": "challenger_topk_60",
                    "track_profile": "challenger_topk_60",
                    "source_track": "official_baseline",
                    "candidate_status": "official_reference",
                },
            ]
        )

        with self.assertRaises(ValueError):
            run_shadow_candidate_monitor.validate_track_identity(summary_table)

    def test_derive_sensitivity_status_flags_positive_lag_and_friction(self):
        sensitivity_summary = pd.DataFrame(
            [
                {"scenario": "lag_1", "delta_cagr": 0.10, "delta_sharpe": 0.20},
                {"scenario": "lag_3", "delta_cagr": 0.08, "delta_sharpe": 0.15},
                {"scenario": "cost_10bps", "delta_cagr": 0.06, "delta_sharpe": 0.10},
                {"scenario": "cost_20bps", "delta_cagr": 0.04, "delta_sharpe": 0.05},
            ]
        )

        status = run_shadow_candidate_monitor.derive_sensitivity_status(sensitivity_summary)

        self.assertEqual(status["lag_sensitivity_status"], "pass")
        self.assertEqual(status["friction_sensitivity_status"], "pass")
        self.assertTrue(status["gate_lag_sensitivity_ok"])
        self.assertTrue(status["gate_friction_sensitivity_ok"])

    def test_recommend_shadow_candidate_returns_continue_observation_for_mixed_recent_breadth(self):
        recommendation = run_shadow_candidate_monitor.recommend_shadow_candidate(
            {
                "challenger_cagr": 0.30,
                "baseline_cagr": 0.05,
                "challenger_sharpe": 0.80,
                "baseline_sharpe": 0.35,
                "gate_risk_off_not_worse": True,
                "gate_concentration_not_extreme": True,
                "gate_lag_sensitivity_ok": True,
                "gate_friction_sensitivity_ok": True,
                "gate_recent_12_positive": True,
                "gate_recent_6_releases_positive": True,
                "recent_12_month_outperformance_rate": 0.33,
                "recent_6_month_outperformance_rate": 0.17,
            }
        )

        self.assertEqual(recommendation, "continue observation")

    def test_recommend_shadow_candidate_returns_controlled_trial_candidate_for_strong_watchlist(self):
        recommendation = run_shadow_candidate_monitor.recommend_shadow_candidate(
            {
                "challenger_cagr": 0.30,
                "baseline_cagr": 0.05,
                "challenger_sharpe": 0.80,
                "baseline_sharpe": 0.35,
                "gate_risk_off_not_worse": True,
                "gate_concentration_not_extreme": True,
                "gate_lag_sensitivity_ok": True,
                "gate_friction_sensitivity_ok": True,
                "gate_recent_12_positive": True,
                "gate_recent_6_releases_positive": True,
                "recent_12_month_outperformance_rate": 0.67,
                "recent_6_month_outperformance_rate": 0.67,
            }
        )

        self.assertEqual(recommendation, "candidate for future controlled trial")


if __name__ == "__main__":
    unittest.main()
