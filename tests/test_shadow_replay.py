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
                raise RuntimeError("stub Firestore client should not be used in replay unit tests")

            def set(self, *args, **kwargs):
                return None

        firestore_module.Client = FirestoreClient
        sys.modules["google.cloud.firestore"] = firestore_module
        sys.modules["google.cloud"].firestore = firestore_module


install_test_stubs()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main
import shadow_replay


class ShadowReplayTests(unittest.TestCase):
    def test_apply_selection_meta_soft_tilt_overweights_higher_score(self):
        selected_candidates = {
            "ETHUSDT": {"weight": 0.5, "relative_score": 1.0, "abs_momentum": 0.2},
            "SOLUSDT": {"weight": 0.5, "relative_score": 0.8, "abs_momentum": 0.1},
        }
        selection_meta = {
            "ETHUSDT": {"final_score": 0.9},
            "SOLUSDT": {"final_score": 0.6},
        }

        tilted = main.apply_selection_meta_soft_tilt(
            selected_candidates,
            selection_meta,
            field="final_score",
            strength=0.2,
        )

        self.assertGreater(tilted["ETHUSDT"]["weight"], 0.5)
        self.assertLess(tilted["SOLUSDT"]["weight"], 0.5)
        self.assertAlmostEqual(
            tilted["ETHUSDT"]["weight"] + tilted["SOLUSDT"]["weight"],
            1.0,
            places=8,
        )

    def test_resolve_active_release_marks_stale_payload_as_last_known_good(self):
        releases = [
            {
                "version": "2026-01-31-core_major",
                "as_of_date": datetime(2026, 1, 31, tzinfo=timezone.utc),
                "activation_date": datetime(2026, 2, 1, tzinfo=timezone.utc),
                "payload": {
                    "symbols": ["ETHUSDT", "SOLUSDT"],
                    "symbol_map": {
                        "ETHUSDT": {"base_asset": "ETH"},
                        "SOLUSDT": {"base_asset": "SOL"},
                    },
                    "selection_meta": {},
                    "version": "2026-01-31-core_major",
                    "mode": "core_major",
                    "as_of_date": "2026-01-31",
                    "source_project": "crypto-leader-rotation",
                },
            }
        ]

        payload, source_kind = shadow_replay.resolve_active_release(
            signal_date=datetime(2026, 3, 20, tzinfo=timezone.utc),
            releases=releases,
            max_age_days=30,
        )

        self.assertEqual(source_kind, "last_known_good")
        self.assertEqual(payload["version"], "2026-01-31-core_major")


if __name__ == "__main__":
    unittest.main()
