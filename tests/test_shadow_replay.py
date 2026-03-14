import sys
import types
import unittest
from datetime import datetime, timezone
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

import shadow_replay


class ShadowReplayTests(unittest.TestCase):
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

    def test_override_activation_lag_uses_trading_days(self):
        index_table = pd.DataFrame(
            {
                "as_of_date": pd.to_datetime(["2026-01-31", "2026-02-28"]),
                "activation_date": pd.to_datetime(["2026-02-01", "2026-03-01"]),
            }
        )
        trading_dates = pd.DatetimeIndex(
            pd.to_datetime(["2026-01-31", "2026-02-01", "2026-02-02", "2026-02-28", "2026-03-01", "2026-03-02"])
        )

        adjusted = shadow_replay.override_activation_lag(index_table, trading_dates, lag_days=2)

        self.assertEqual(str(adjusted.iloc[0]["activation_date"].date()), "2026-02-02")
        self.assertEqual(str(adjusted.iloc[1]["activation_date"].date()), "2026-03-02")

    def test_drop_release_rows_is_deterministic(self):
        index_table = pd.DataFrame({"version": [f"v{i}" for i in range(6)]})

        filtered = shadow_replay.drop_release_rows(index_table, every_nth=3, phase=2)

        self.assertEqual(filtered["version"].tolist(), ["v0", "v1", "v3", "v4"])

    def test_apply_turnover_costs_scales_with_turnover(self):
        gross_returns = pd.Series([0.02, 0.01], index=pd.to_datetime(["2026-01-01", "2026-01-02"]))
        turnover = pd.Series([0.50, 0.25], index=gross_returns.index)

        net_returns, transaction_cost = shadow_replay.apply_turnover_costs(
            gross_returns,
            turnover,
            cost_bps=10.0,
        )

        self.assertAlmostEqual(float(transaction_cost.iloc[0]), 0.0005)
        self.assertAlmostEqual(float(transaction_cost.iloc[1]), 0.00025)
        self.assertAlmostEqual(float(net_returns.iloc[0]), 0.0195)
        self.assertAlmostEqual(float(net_returns.iloc[1]), 0.00975)


if __name__ == "__main__":
    unittest.main()
