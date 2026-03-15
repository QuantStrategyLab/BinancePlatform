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

import main
from degraded_mode_support import format_trend_pool_source_logs
import degraded_mode_support


def build_payload(as_of_date="2026-03-10", *, mode="core_major"):
    symbol_map = {
        "ETHUSDT": {"base_asset": "ETH"},
        "SOLUSDT": {"base_asset": "SOL"},
        "XRPUSDT": {"base_asset": "XRP"},
        "LTCUSDT": {"base_asset": "LTC"},
        "BCHUSDT": {"base_asset": "BCH"},
    }
    return {
        "as_of_date": as_of_date,
        "version": f"{as_of_date}-{mode}",
        "mode": mode,
        "pool_size": len(symbol_map),
        "symbols": list(symbol_map.keys()),
        "symbol_map": symbol_map,
        "source_project": "crypto-leader-rotation",
    }


class TrendPoolLoadingTests(unittest.TestCase):
    def test_validate_trend_pool_payload_rejects_stale_payload(self):
        payload = build_payload(as_of_date="2026-01-01")

        result = main.validate_trend_pool_payload(
            payload,
            source_label="test",
            now_utc=datetime(2026, 3, 14, tzinfo=timezone.utc),
            max_age_days=30,
            acceptable_modes=["core_major"],
            expected_pool_size=5,
            enforce_freshness=True,
        )

        self.assertFalse(result["ok"])
        self.assertIn("stale", " ".join(result["errors"]))

    def test_resolve_trend_pool_source_prefers_last_known_good_before_local_file(self):
        last_good_payload = build_payload(as_of_date="2026-02-15")
        file_result = main.validate_trend_pool_payload(
            build_payload(as_of_date="2026-03-12"),
            source_label="file:/tmp/live_pool_legacy.json",
            now_utc=datetime(2026, 3, 14, tzinfo=timezone.utc),
            acceptable_modes=["core_major"],
            expected_pool_size=5,
            enforce_freshness=True,
        )

        with patch.object(
            degraded_mode_support,
            "load_trend_pool_from_firestore",
            return_value={"ok": False, "errors": ["payload stale"], "warnings": [], "source_label": "firestore:test"},
        ), patch.object(degraded_mode_support, "load_trend_pool_from_file", return_value=file_result), patch.object(
            degraded_mode_support,
            "get_default_live_pool_candidates",
            return_value=[Path("/tmp/live_pool_legacy.json")],
        ):
            resolution = main.resolve_trend_pool_source(
                state={main.TREND_POOL_LAST_GOOD_PAYLOAD_KEY: last_good_payload},
                now_utc=datetime(2026, 3, 14, tzinfo=timezone.utc),
            )

        self.assertEqual(resolution["source_kind"], "last_known_good")
        self.assertTrue(resolution["degraded"])
        self.assertEqual(resolution["version"], "2026-02-15-core_major")

    def test_update_trend_pool_state_persists_metadata_and_last_good_payload(self):
        validated = main.validate_trend_pool_payload(
            build_payload(),
            source_label="firestore:strategy/CRYPTO_LEADER_ROTATION_LIVE_POOL",
            now_utc=datetime(2026, 3, 14, tzinfo=timezone.utc),
            acceptable_modes=["core_major"],
            expected_pool_size=5,
            enforce_freshness=True,
        )
        resolution = main.build_trend_pool_resolution(
            validated,
            source_kind="fresh_upstream",
            degraded=False,
            now_utc=datetime(2026, 3, 14, tzinfo=timezone.utc),
        )
        state = main.build_default_state()

        main.update_trend_pool_state(state, resolution)

        self.assertEqual(state["trend_pool_source"], "fresh_upstream")
        self.assertEqual(state["trend_pool_version"], "2026-03-10-core_major")
        self.assertEqual(state["trend_pool_mode"], "core_major")
        self.assertEqual(state["trend_pool_source_project"], "crypto-leader-rotation")
        self.assertEqual(
            state[main.TREND_POOL_LAST_GOOD_PAYLOAD_KEY]["version"],
            "2026-03-10-core_major",
        )

    def test_update_trend_pool_state_does_not_replace_last_good_from_local_file(self):
        validated = main.validate_trend_pool_payload(
            build_payload(),
            source_label="file:/tmp/live_pool_legacy.json",
            now_utc=datetime(2026, 3, 14, tzinfo=timezone.utc),
            acceptable_modes=["core_major"],
            expected_pool_size=5,
            enforce_freshness=True,
        )
        resolution = main.build_trend_pool_resolution(
            validated,
            source_kind="local_file",
            degraded=True,
            now_utc=datetime(2026, 3, 14, tzinfo=timezone.utc),
        )
        state = main.build_default_state()
        state[main.TREND_POOL_LAST_GOOD_PAYLOAD_KEY] = build_payload(as_of_date="2026-02-15")

        main.update_trend_pool_state(state, resolution)

        self.assertEqual(state["trend_pool_source"], "local_file")
        self.assertEqual(
            state[main.TREND_POOL_LAST_GOOD_PAYLOAD_KEY]["version"],
            "2026-02-15-core_major",
        )

    def test_get_total_balance_raises_when_spot_balance_is_unavailable(self):
        class SpotFailureClient:
            def get_asset_balance(self, *, asset):
                raise RuntimeError("spot api unavailable")

            def get_simple_earn_flexible_product_position(self, *, asset):
                return {"rows": []}

        with self.assertRaises(main.BalanceFetchError):
            main.get_total_balance(SpotFailureClient(), "USDT", log_buffer=[])

    def test_get_total_balance_keeps_spot_balance_when_earn_lookup_fails(self):
        class EarnFailureClient:
            def get_asset_balance(self, *, asset):
                return {"free": "1.5", "locked": "0.5"}

            def get_simple_earn_flexible_product_position(self, *, asset):
                raise RuntimeError("earn api unavailable")

        log_buffer = []
        total_balance = main.get_total_balance(EarnFailureClient(), "USDT", log_buffer=log_buffer)

        self.assertAlmostEqual(total_balance, 2.0)
        self.assertTrue(any("理财余额读取失败" in message for message in log_buffer))

    def test_format_trend_pool_source_logs_highlights_degraded_buy_pause(self):
        log_lines = format_trend_pool_source_logs(
            {
                "source_kind": "last_known_good",
                "mode": "core_major",
                "version": "2026-03-10-core_major",
                "as_of_date": "2026-03-10",
                "source_project": "crypto-leader-rotation",
                "messages": ["payload stale", "using cached pool"],
                "degraded": True,
            },
            allow_new_trend_entries=False,
        )

        self.assertIn("last_known_good", log_lines[0])
        self.assertTrue(any("payload stale" in line for line in log_lines))
        self.assertTrue(any("暂停新的趋势买入" in line for line in log_lines))

    def test_append_rotation_summary_separates_upstream_and_execution_pool(self):
        log_buffer = []

        main._append_rotation_summary(
            log_buffer,
            ["TRXUSDT", "ETHUSDT", "BCHUSDT", "NEARUSDT", "SOLUSDT"],
            ["TRXUSDT", "ETHUSDT", "BCHUSDT"],
            {},
        )

        self.assertEqual(log_buffer[0], "🗓️ 上游官方月度池: TRXUSDT、ETHUSDT、BCHUSDT、NEARUSDT、SOLUSDT")
        self.assertEqual(log_buffer[1], "🧭 当前月度执行池: TRXUSDT、ETHUSDT、BCHUSDT")
        self.assertEqual(log_buffer[2], "📌 当前月度执行池数量: 3")
        self.assertEqual(log_buffer[3], "🎯 当前执行目标: 无候选，保持防守")

    def test_allocate_trend_buy_budget_renormalizes_remaining_buy_candidates(self):
        selected_candidates = {
            "ETHUSDT": {"weight": 0.5},
            "SOLUSDT": {"weight": 0.3},
            "XRPUSDT": {"weight": 0.2},
        }

        full_alloc = main.allocate_trend_buy_budget(selected_candidates, ["ETHUSDT", "SOLUSDT"], 1000.0)
        single_alloc = main.allocate_trend_buy_budget(selected_candidates, ["SOLUSDT"], 1000.0)

        self.assertAlmostEqual(sum(full_alloc.values()), 1000.0)
        self.assertAlmostEqual(full_alloc["ETHUSDT"], 625.0)
        self.assertAlmostEqual(full_alloc["SOLUSDT"], 375.0)
        self.assertEqual(single_alloc, {"SOLUSDT": 1000.0})

    def test_resolve_runtime_trend_pool_rejects_invalid_injected_payload(self):
        runtime = main.ExecutionRuntime(
            dry_run=True,
            now_utc=datetime(2026, 3, 14, tzinfo=timezone.utc),
            trend_pool_payload={"version": "broken"},
        )

        with self.assertRaises(ValueError):
            main.resolve_runtime_trend_pool(runtime, raw_state={})

    def test_duplicate_trend_action_guard(self):
        state = main.build_default_state()

        self.assertFalse(main.should_skip_duplicate_trend_action(state, "ETHUSDT", "buy", "20260314"))
        main.record_trend_action(state, "ETHUSDT", "buy", "20260314")
        self.assertTrue(main.should_skip_duplicate_trend_action(state, "ETHUSDT", "buy", "20260314"))
        self.assertFalse(main.should_skip_duplicate_trend_action(state, "ETHUSDT", "sell", "20260314"))


if __name__ == "__main__":
    unittest.main()
