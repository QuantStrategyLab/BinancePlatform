import unittest
from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from infra.closed_daily_bars import ClosedDailyBarsError, closed_daily_bars


class ClosedDailyBarsTest(unittest.TestCase):
    def setUp(self):
        self.as_of = datetime(2026, 1, 4, tzinfo=timezone.utc)
        self.start_ms = 1767225600000

    def bar(self, day):
        opened = self.start_ms + day * 86400000
        return [opened, "10", "12", "9", "11", "1", opened + 86399999]

    def test_excludes_unclosed_bar_at_cutoff(self):
        rows = [self.bar(0), self.bar(1), self.bar(2), self.bar(3)]
        self.assertEqual(closed_daily_bars(rows, self.as_of), rows[:3])

    def test_invalid_times_order_duplicates_and_future_fail(self):
        cases = [[self.bar(1), self.bar(0)], [self.bar(0), self.bar(0)],
                 [self.bar(0)[:6]], [self.bar(0), self.bar(4)],
                 [[True, "1", "1", "1", "1", "1", 123]],
                 [[self.start_ms, "1", "1", "1", "1", "1", self.start_ms]]]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(ClosedDailyBarsError):
                closed_daily_bars(rows, self.as_of)

    def test_missing_closed_and_invalid_clock_fail(self):
        with self.assertRaises(ClosedDailyBarsError):
            closed_daily_bars([self.bar(3)], self.as_of)
        with self.assertRaises(ClosedDailyBarsError):
            closed_daily_bars([self.bar(0)], self.as_of.replace(tzinfo=None))

    def test_missing_day_and_stale_tail_fail(self):
        with self.assertRaises(ClosedDailyBarsError):
            closed_daily_bars([self.bar(0), self.bar(2)], self.as_of)
        with self.assertRaisesRegex(ClosedDailyBarsError, "closed_daily_bars_stale"):
            closed_daily_bars([self.bar(0), self.bar(1)], self.as_of)

    def test_recent_completed_day_is_valid_during_current_day(self):
        midday = self.as_of.replace(hour=12)
        self.assertEqual(closed_daily_bars([self.bar(1), self.bar(2), self.bar(3)], midday),
                         [self.bar(1), self.bar(2)])

    def test_btc_snapshot_consumer_only_receives_closed_bars(self):
        stub = ModuleType("runtime_support")
        stub.ExecutionIntegrityError = type("ExecutionIntegrityError", (Exception,), {})
        path = Path(__file__).resolve().parents[1] / "infra" / "binance_runtime.py"
        spec = spec_from_file_location("isolated_btc_runtime", path)
        module = module_from_spec(spec)
        with patch.dict("sys.modules", {"runtime_support": stub}):
            spec.loader.exec_module(module)
        cutoff = int(self.as_of.timestamp() * 1000)
        rows = [[cutoff - (700 - i) * 86400000, 100 + i, 101 + i, 99 + i,
                 100 + i, 10, cutoff - (699 - i) * 86400000 - 1] for i in range(700)]
        rows.append([cutoff, 9999, 10000, 9998, 9999, 10, cutoff + 86399999])
        client = Mock()
        client.get_historical_klines.return_value = rows
        runtime = SimpleNamespace(client=client, now_utc=self.as_of, btc_market_snapshot=None)

        def provider(source, _price, *, log_buffer):
            self.assertEqual(source.get_historical_klines("BTCUSDT", "1d", "700 days ago UTC"), rows[:-1])
            return {"ma200": 1}

        for _ in range(2):
            self.assertEqual(module.resolve_runtime_btc_snapshot(runtime, 1, [],
                fetch_btc_market_snapshot_fn=provider, closed_bar_validation=True), {"ma200": 1})
        self.assertIsNone(runtime.btc_market_snapshot)
        self.assertEqual(client.get_historical_klines.call_count, 2)
        malformed = [row[:] for row in rows[:-1]]
        malformed[-1][6] -= 1
        invalid_close = [row[:] for row in rows[:-1]]
        invalid_close[-1][4] = float("nan")
        constant_tail = [row[:] for row in rows[:-1]]
        for row in constant_tail[-200:]:
            row[1:5] = [100, 101, 99, 100]
        for invalid in (rows[:-2], malformed, invalid_close, constant_tail):
            client.get_historical_klines.return_value = invalid
            with self.assertRaises(ClosedDailyBarsError):
                module.resolve_runtime_btc_snapshot(runtime, 1, [], fetch_btc_market_snapshot_fn=provider,
                                                    closed_bar_validation=True)
        runtime.btc_market_snapshot = {"ma200": 1}
        with self.assertRaisesRegex(ClosedDailyBarsError, "injected_btc_snapshot_forbidden"):
            module.resolve_runtime_btc_snapshot(runtime, 1, [], fetch_btc_market_snapshot_fn=provider,
                                                closed_bar_validation=True)

    def test_real_infra_fetches_closed_bars_for_btc_and_pool_without_mutation(self):
        stub = ModuleType("runtime_support")
        stub.ExecutionIntegrityError = type("ExecutionIntegrityError", (Exception,), {})
        path = Path(__file__).resolve().parents[1] / "infra" / "binance_runtime.py"
        spec = spec_from_file_location("isolated_binance_runtime", path)
        module = module_from_spec(spec)
        with patch.dict("sys.modules", {"runtime_support": stub}):
            spec.loader.exec_module(module)
        rows = [self.bar(0), self.bar(1), self.bar(2), self.bar(3)]
        client = Mock()
        client.get_historical_klines.return_value = rows
        runtime = SimpleNamespace(client=client, now_utc=self.as_of, trend_indicator_snapshots=None)
        universe = ["ETHUSDT", "SOLUSDT"]
        observed = []

        def provider(snapshot, symbol):
            bars = snapshot.get_historical_klines(symbol, "1d", "420 days ago UTC")
            observed.append((symbol, bars))
            keys = ("close", "sma20", "sma60", "sma200", "roc20", "roc60", "roc120", "vol20",
                    "atr14", "avg_quote_vol_30", "avg_quote_vol_90", "avg_quote_vol_180",
                    "trend_persist_90", "age_days")
            return dict.fromkeys(keys, 1.0)

        result = module.resolve_runtime_trend_indicators(
            runtime, universe, fetch_daily_indicators_fn=provider, closed_bar_validation=True
        )
        self.assertEqual(universe, ["ETHUSDT", "SOLUSDT"])
        self.assertEqual(list(result), ["ETHUSDT", "SOLUSDT", "BTCUSDT"])
        self.assertEqual([symbol for symbol, _ in observed], list(result))
        self.assertTrue(all(bars == rows[:3] for _, bars in observed))
        self.assertEqual(client.get_historical_klines.call_count, 3)
        with self.assertRaisesRegex(ClosedDailyBarsError, "closed_daily_indicators_unavailable"):
            module.resolve_runtime_trend_indicators(
                runtime, universe, fetch_daily_indicators_fn=lambda *_: {"close": float("inf")},
                closed_bar_validation=True,
            )

    def test_real_infra_strict_scope_rejects_injected_snapshot(self):
        stub = ModuleType("runtime_support")
        stub.ExecutionIntegrityError = type("ExecutionIntegrityError", (Exception,), {})
        path = Path(__file__).resolve().parents[1] / "infra" / "binance_runtime.py"
        spec = spec_from_file_location("isolated_binance_runtime", path)
        module = module_from_spec(spec)
        with patch.dict("sys.modules", {"runtime_support": stub}):
            spec.loader.exec_module(module)
        client = Mock()
        runtime = SimpleNamespace(client=client, now_utc=self.as_of, trend_indicator_snapshots={})
        with self.assertRaisesRegex(ClosedDailyBarsError, "injected_daily_indicators_forbidden"):
            module.resolve_runtime_trend_indicators(runtime, ["ETHUSDT"],
                fetch_daily_indicators_fn=Mock(), closed_bar_validation=True)
        client.get_historical_klines.assert_not_called()


if __name__ == "__main__":
    unittest.main()
