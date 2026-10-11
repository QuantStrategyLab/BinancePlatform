"""Offline characterization of known research limitations, not model acceptance.

See docs/research-backtest-contract.md and issue #366. These passing tests
intentionally describe today's behavior; a model fix must revise the assertions.
"""

import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from research import backtest


class ResearchBacktestContractTests(unittest.TestCase):
    def setUp(self):
        # Any accidental data loading must fail rather than reach Binance.
        self.client_patch = patch.object(
            backtest, "build_client", side_effect=AssertionError("offline fixtures only")
        )
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.times = pd.date_range("2025-01-01", periods=3, freq="h", tz="UTC")

    @staticmethod
    def daily_bars():
        times = pd.date_range("2024-01-01", periods=201, freq="D", tz="UTC")
        return pd.DataFrame({
            "time": times, "open": 100.0, "high": 100.0,
            "low": 100.0, "close": 100.0, "vol": 10.0,
        })

    def test_final_daily_close_changes_same_day_first_hour_indicator(self):
        for prepare, field, expected in (
            (backtest.prepare_trend_daily_indicators, "sma20", 110.0),
            (backtest.prepare_btc_daily_indicators, "ma200", 101.0),
        ):
            with self.subTest(field=field):
                daily = self.daily_bars()
                day = daily.time.iloc[-1]
                timeline = pd.date_range(day - pd.Timedelta(hours=1), periods=3, freq="h")
                hourly = pd.DataFrame({"time": timeline, "close": 100.0})
                baseline = backtest.align_symbol_data(hourly, prepare(daily), timeline)
                # Only the future final daily close changes; hourly history is identical.
                daily.loc[daily.index[-1], "close"] = 300.0
                changed = backtest.align_symbol_data(hourly, prepare(daily), timeline)
                self.assertEqual(changed[field].iloc[0], baseline[field].iloc[0])
                self.assertEqual(baseline[field].iloc[1], 100.0)
                self.assertEqual(changed[field].iloc[1], expected)
                self.assertEqual(changed[field].iloc[2], expected)
                # Known lookahead: D's final close is visible at D 00:00.

    def test_missing_hour_close_is_forward_filled_but_leading_gap_is_not(self):
        hourly = pd.DataFrame({"time": [self.times[1]], "close": [100.0]})
        aligned = backtest.align_symbol_data(hourly, pd.DataFrame(), self.times)
        self.assertTrue(pd.isna(aligned.close.iloc[0]))
        self.assertEqual(aligned.close.iloc[1:].tolist(), [100.0, 100.0])

    def market(self, prices):
        data = {}
        for symbol in ("BTCUSDT", "ETHUSDT"):
            data[symbol] = pd.DataFrame({
                "time": self.times[:len(prices)],
                "close": prices if symbol == "ETHUSDT" else 100.0,
                "sma20": 50.0, "sma60": 50.0, "sma200": 50.0,
                "atr14": 1000.0, "ma200": 50.0, "ma200_slope": 1.0,
                "geom200": np.nan, "zscore": np.nan, "sell_trigger": np.nan,
            })
        return data

    def run_fixture(self, data, weights, *, dca=False):
        def allocation(total, cash, trend, btc):
            return {"trend_usdt_pool": 0.0 if dca else 1000.0,
                    "trend_layer_equity": trend,
                    "dca_usdt_pool": 1000.0 if dca else 0.0}

        with (
            patch.object(backtest, "ALL_SYMBOLS", ["BTCUSDT", "ETHUSDT"]),
            patch.object(backtest, "FIXED_POOL", ["ETHUSDT"]),
            patch.object(backtest, "select_rotation_weights", side_effect=weights),
            patch.object(backtest, "compute_allocation_budgets", side_effect=allocation),
            patch.object(backtest, "allocate_trend_buy_budget",
                         side_effect=lambda active, eligible, budget: {
                             symbol: budget for symbol in eligible
                         }),
            patch.object(backtest, "get_dynamic_btc_base_order", return_value=1000.0),
        ):
            return backtest.simulate_window(
                {"name": "synthetic", "label": "synthetic", "pool_mode": "fixed"},
                "synthetic", data["BTCUSDT"].time.iloc[0],
                data["BTCUSDT"].time.iloc[-1], data, capture_details=True,
            )

    def test_same_row_signal_buys_at_that_rows_close(self):
        data = self.market([100.0, 110.0])
        observed = []

        def weights(strategy, rows, gate, ts, state):
            observed.append((ts, rows["ETHUSDT"]["close"]))
            return {"ETHUSDT": 1.0}

        result = self.run_fixture(data, weights)
        self.assertEqual(observed[0], (self.times[0], 100.0))
        self.assertEqual(result["trades"], 1)
        # 985 / first-row close = 9.85 units; a next-row fill gives no gain here.
        self.assertEqual(result["equity_series"].tolist(), [10000.0, 10098.5])

    def test_flat_trend_round_trip_deducts_no_cost(self):
        result = self.run_fixture(self.market([100.0, 100.0]), [{"ETHUSDT": 1.0}, {}])
        self.assertEqual(result["trades"], 2)
        self.assertEqual(result["final_equity"], backtest.INITIAL_CAPITAL)
        self.assertEqual(result["total_return"], 0.0)
        # The 1.5% buy budget buffer stays cash; it is not a fee.

    def test_trend_round_trip_retains_budget_buffer_as_cash(self):
        result = self.run_fixture(self.market([100.0, 110.0]), [{"ETHUSDT": 1.0}, {}])
        self.assertEqual(result["trades"], 2)
        self.assertEqual(result["final_equity"], 9015.0 + 9.85 * 110.0)

    def test_dca_budget_buffer_is_also_unspent_cash(self):
        data = self.market([100.0, 100.0])
        data["BTCUSDT"]["close"] = [100.0, 110.0]
        data["BTCUSDT"]["geom200"] = 100.0
        result = self.run_fixture(data, [{}, {}], dca=True)
        self.assertEqual(result["trades"], 1)
        self.assertEqual(result["equity_series"].tolist(), [10000.0, 10098.5])

    def test_flat_dca_buy_and_partial_sell_deduct_no_cost(self):
        data = self.market([100.0, 100.0])
        data["BTCUSDT"]["geom200"] = 100.0
        data["BTCUSDT"]["zscore"] = [0.0, 3.0]
        data["BTCUSDT"]["sell_trigger"] = 2.5
        result = self.run_fixture(data, [{}, {}], dca=True)
        self.assertEqual(result["trades"], 2)
        self.assertEqual(result["equity_series"].tolist(), [10000.0, 10000.0])

    def test_forward_filled_quote_can_be_used_for_a_new_trade(self):
        data = self.market([100.0, 100.0, 110.0])
        raw = pd.DataFrame({"time": self.times[[0, 2]], "close": [100.0, 110.0]})
        aligned = backtest.align_symbol_data(raw, pd.DataFrame(), self.times)
        data["ETHUSDT"]["close"] = aligned.close
        self.assertNotIn(self.times[1], raw.time.tolist())
        result = self.run_fixture(data, [{}, {"ETHUSDT": 1.0}, {"ETHUSDT": 1.0}])
        self.assertEqual(result["trades"], 1)
        self.assertEqual(result["equity_series"].tolist(), [10000.0, 10000.0, 10098.5])

    def test_equity_curve_has_only_post_processing_rows_no_initial_anchor(self):
        result = self.run_fixture(self.market([100.0, 90.0]), [{"ETHUSDT": 1.0}] * 2)
        curve = result["equity_series"]
        self.assertEqual(curve.index.tolist(), self.times[:2].tolist())
        self.assertEqual(len(curve), 2)
        self.assertAlmostEqual(result["max_drawdown"], 9901.5 / 10000.0 - 1.0)
        # This zero-cost fixture cannot expose a first-fill loss. The missing
        # pre-fill capital anchor remains a future cost-model acceptance case.


if __name__ == "__main__":
    unittest.main()
