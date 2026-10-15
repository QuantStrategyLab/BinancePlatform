"""Causal timing and cost acceptance with synthetic data and no exchange calls."""

import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from research import backtest


class ResearchBacktestContractTests(unittest.TestCase):
    def setUp(self):
        client_patch = patch.object(backtest, "build_client", side_effect=AssertionError("offline fixtures only"))
        client_patch.start()
        self.addCleanup(client_patch.stop)
        self.times = pd.date_range("2025-01-01", periods=4, freq="h", tz="UTC")

    @staticmethod
    def daily_bars():
        times = pd.date_range("2024-01-01", periods=201, freq="D", tz="UTC")
        return pd.DataFrame({"time": times, "open": 100.0, "high": 100.0,
                             "low": 100.0, "close": 100.0, "vol": 10.0})

    def test_daily_final_close_is_only_available_at_next_utc_day(self):
        for prepare, field in ((backtest.prepare_trend_daily_indicators, "sma20"),
                               (backtest.prepare_btc_daily_indicators, "ma200")):
            with self.subTest(field=field):
                daily = self.daily_bars()
                day = daily.time.iloc[-1]
                timeline = pd.date_range(day, periods=25, freq="h")
                hourly = pd.DataFrame({"time": timeline, "open": 100.0, "close": 100.0})
                baseline = backtest.align_symbol_data(hourly, prepare(daily), timeline)
                daily.loc[daily.index[-1], "close"] = 300.0
                changed = backtest.align_symbol_data(hourly, prepare(daily), timeline)
                pd.testing.assert_series_equal(baseline[field].iloc[:24], changed[field].iloc[:24])
                self.assertNotEqual(baseline[field].iloc[24], changed[field].iloc[24])

    def test_delayed_daily_availability_is_respected(self):
        daily = self.daily_bars()
        daily["available_at"] = daily.time + pd.Timedelta(days=1, minutes=30)
        day = daily.time.iloc[-1] + pd.Timedelta(days=1)
        times = pd.date_range(day, periods=2, freq="h")
        hourly = pd.DataFrame({"time": times, "open": 100.0, "close": 100.0})
        daily.loc[daily.index[-1], "close"] = 300.0
        aligned = backtest.align_symbol_data(hourly, backtest.prepare_trend_daily_indicators(daily), times)
        self.assertEqual(aligned.sma20.iloc[0], 100.0)
        self.assertEqual(aligned.sma20.iloc[1], 110.0)

    def test_missing_quotes_can_mark_but_cannot_execute(self):
        hourly = pd.DataFrame({"time": self.times[[1, 3]], "open": [100.0, 110.0], "close": [101.0, 111.0]})
        aligned = backtest.align_symbol_data(hourly, pd.DataFrame(), self.times)
        self.assertTrue(pd.isna(aligned.close.iloc[0]))
        self.assertEqual(aligned.close.iloc[2], 101.0)
        self.assertTrue(pd.isna(aligned.open.iloc[2]))
        self.assertFalse(aligned.quote_observed.iloc[2])

    def market(self, prices):
        data = {}
        for symbol in ("BTCUSDT", "ETHUSDT"):
            data[symbol] = pd.DataFrame({
                "time": self.times[:len(prices)],
                "open": prices if symbol == "ETHUSDT" else 100.0,
                "close": prices if symbol == "ETHUSDT" else 100.0,
                "sma20": 50.0, "sma60": 50.0, "sma200": 50.0,
                "atr14": 1000.0, "ma200": 50.0, "ma200_slope": 1.0,
                "geom200": np.nan, "zscore": np.nan, "sell_trigger": np.nan,
            })
        return data

    def run_fixture(self, data, weights, *, dca=False, fee_rate=0.01, slippage_rate=0.0, start=None, end=None):
        def allocation(total, cash, trend, btc):
            return {"trend_usdt_pool": 0.0 if dca else 1000.0,
                    "trend_layer_equity": trend, "dca_usdt_pool": 1000.0 if dca else 0.0}

        with (patch.object(backtest, "ALL_SYMBOLS", list(data)),
              patch.object(backtest, "FIXED_POOL", ["ETHUSDT"]),
              patch.object(backtest, "select_rotation_weights", side_effect=weights),
              patch.object(backtest, "compute_allocation_budgets", side_effect=allocation),
              patch.object(backtest, "allocate_trend_buy_budget", side_effect=lambda active, eligible, budget: {
                  symbol: budget for symbol in eligible}),
              patch.object(backtest, "get_dynamic_btc_base_order", return_value=1000.0)):
            return backtest.simulate_window(
                {"name": "synthetic", "label": "synthetic", "pool_mode": "fixed"}, "synthetic",
                start if start is not None else data["BTCUSDT"].time.iloc[0],
                end if end is not None else data["BTCUSDT"].time.iloc[-1] + pd.Timedelta(hours=1),
                data, capture_details=True, fee_rate=fee_rate, slippage_rate=slippage_rate)

    def test_close_signal_fills_at_next_open(self):
        data = self.market([100.0, 110.0, 110.0])
        data["ETHUSDT"]["close"] = [100.0, 200.0, 200.0]
        observed = []

        def weights(strategy, rows, gate, ts, state):
            observed.append((ts, rows["ETHUSDT"]["close"]))
            return {"ETHUSDT": 1.0}

        result = self.run_fixture(data, weights, fee_rate=0.0)
        self.assertEqual(observed[0], (self.times[0], 100.0))
        trade = result["trade_ledger"][0]
        self.assertEqual(trade["signal_time"], self.times[0])
        self.assertEqual(trade["signal_available_at"], self.times[1])
        self.assertEqual(trade["fill_time"], self.times[1])
        self.assertEqual(trade["reference_price"], 110.0)
        self.assertEqual(trade["quantity"], 985.0 / 110.0)
        self.assertEqual(result["equity_series"].iloc[1], 10000.0)

    def test_trend_round_trip_deducts_cost_on_both_sides(self):
        result = self.run_fixture(self.market([100.0] * 3), [{"ETHUSDT": 1.0}, {}])
        self.assertEqual(result["trades"], 2)
        buy, sell = result["trade_ledger"]
        notional = 985.0 / 1.01
        self.assertAlmostEqual(buy["quantity"], notional / 100.0)
        self.assertAlmostEqual(result["final_equity"], 10000.0 - 2 * notional * 0.01)
        self.assertAlmostEqual(result["total_costs"], buy["fee"] + sell["fee"])
        self.assertEqual(sell["reason"], "trend_exit")

    def test_slippage_and_fee_cash_conservation(self):
        result = self.run_fixture(self.market([100.0] * 3), [{"ETHUSDT": 1.0}, {}], slippage_rate=0.02)
        buy, sell = result["trade_ledger"]
        self.assertEqual(buy["execution_price"], 102.0)
        self.assertEqual(sell["execution_price"], 98.0)
        self.assertAlmostEqual(result["final_equity"], 10000.0 + sum(t["cash_delta"] for t in result["trade_ledger"]))
        self.assertAlmostEqual(10000.0 - result["final_equity"], result["total_costs"])

    def test_no_fill_on_missing_next_open(self):
        data = self.market([100.0] * 3)
        data["ETHUSDT"].loc[1, "open"] = np.nan
        result = self.run_fixture(data, [{"ETHUSDT": 1.0}, {}])
        self.assertEqual(result["trades"], 0)
        self.assertEqual(result["final_equity"], 10000.0)

    def test_missing_exit_quote_does_not_liquidate_at_carried_price(self):
        data = self.market([100.0] * 4)
        data["ETHUSDT"].loc[2, "open"] = np.nan
        result = self.run_fixture(data, [{"ETHUSDT": 1.0}, {}, {}])
        self.assertEqual(result["trades"], 2)
        self.assertEqual(result["trade_ledger"][1]["fill_time"], self.times[3])

    def test_missing_signal_quote_cannot_create_a_buy(self):
        data = self.market([100.0] * 3)
        data["ETHUSDT"]["quote_observed"] = [False, True, True]
        result = self.run_fixture(data, [{"ETHUSDT": 1.0}, {}])
        self.assertEqual(result["trades"], 0)

    def test_dca_buy_and_partial_sell_both_pay_fees(self):
        data = self.market([100.0] * 3)
        data["BTCUSDT"]["geom200"] = 100.0
        data["BTCUSDT"]["zscore"] = [0.0, 3.0, 0.0]
        data["BTCUSDT"]["sell_trigger"] = 2.5
        result = self.run_fixture(data, [{}, {}], dca=True)
        self.assertEqual(result["trades"], 2)
        buy, sell = result["trade_ledger"]
        self.assertEqual(buy["reason"], "dca_buy")
        self.assertEqual(sell["reason"], "dca_reduce")
        self.assertAlmostEqual(sell["quantity"], buy["quantity"] * 0.1)
        self.assertAlmostEqual(result["final_equity"], 10000.0 - buy["fee"] - sell["fee"])

    def test_circuit_breaker_exit_pays_fee(self):
        data = self.market([100.0, 100.0, 100.0, 90.0])
        for frame in data.values():
            frame["time"] = pd.date_range("2025-01-01 22:00", periods=4, freq="h", tz="UTC")
        result = self.run_fixture(data, [{"ETHUSDT": 1.0}] * 3)
        exits = [t for t in result["trade_ledger"] if t["reason"] == "circuit_breaker"]
        self.assertEqual(len(exits), 1)
        self.assertGreater(exits[0]["fee"], 0.0)

    def test_initial_equity_anchor_counts_first_execution_cost(self):
        result = self.run_fixture(self.market([100.0] * 2), [{"ETHUSDT": 1.0}])
        curve = result["equity_series"]
        self.assertEqual(curve.index[0], self.times[0])
        self.assertEqual(curve.iloc[0], 10000.0)
        self.assertEqual(len(curve), 3)
        self.assertAlmostEqual(result["max_drawdown"], result["final_equity"] / 10000.0 - 1.0)
        self.assertLess(result["max_drawdown"], 0.0)

    def test_circuit_exit_waits_for_missing_open_then_liquidates(self):
        self.times = pd.date_range("2025-01-01 22:00", periods=5, freq="h", tz="UTC")
        data = self.market([100.0, 100.0, 100.0, 80.0, 80.0])
        data["SOLUSDT"] = data["ETHUSDT"].copy()
        data["SOLUSDT"]["close"] = [100.0, 100.0, 100.0, 90.0, 90.0]
        data["SOLUSDT"]["open"] = [100.0, 100.0, 100.0, np.nan, 90.0]
        result = self.run_fixture(data, [{"ETHUSDT": .5, "SOLUSDT": .5}] * 4)
        exits = [t for t in result["trade_ledger"] if t["reason"] == "circuit_breaker"]
        self.assertEqual([(t["symbol"], t["fill_time"]) for t in exits],
                         [("ETHUSDT", self.times[3]), ("SOLUSDT", self.times[4])])
        self.assertEqual(exits[1]["reference_price"], 90.0)
        self.assertGreater(exits[1]["fee"], 0.0)
        self.assertEqual(len(result["circuit_breaker_events"]), 1)
        self.assertFalse(any(t["side"] == "buy" and t["fill_time"] >= self.times[3]
                             for t in result["trade_ledger"]))

    def test_pending_circuit_exit_survives_daily_risk_reset(self):
        self.times = pd.DatetimeIndex([pd.Timestamp(t, tz="UTC") for t in (
            "2025-01-01 22:00", "2025-01-01 23:00", "2025-01-02 00:00",
            "2025-01-02 01:00", "2025-01-03 00:00")])
        data = self.market([100.0, 100.0, 100.0, 80.0, 80.0])
        data["SOLUSDT"] = data["ETHUSDT"].copy()
        data["SOLUSDT"]["close"] = [100.0, 100.0, 100.0, 90.0, 90.0]
        data["SOLUSDT"]["open"] = [100.0, 100.0, 100.0, np.nan, 90.0]
        result = self.run_fixture(data, [{"ETHUSDT": .5, "SOLUSDT": .5}] * 3 + [{}])
        exits = [t for t in result["trade_ledger"] if t["reason"] == "circuit_breaker"]
        self.assertEqual([(t["symbol"], t["fill_time"]) for t in exits],
                         [("ETHUSDT", self.times[3]), ("SOLUSDT", self.times[4])])
        self.assertEqual(len(result["circuit_breaker_events"]), 1)

    def test_future_perturbation_leaves_past_fills_unchanged(self):
        baseline = self.market([100.0] * 4)
        changed = {s: frame.copy() for s, frame in baseline.items()}
        changed["ETHUSDT"].loc[2:, ["open", "close", "sma20", "sma60"]] = 1000000.0
        def weights(strategy, rows, gate, ts, state):
            return {"ETHUSDT": 1.0} if rows["ETHUSDT"]["close"] < 500 else {}
        before = self.run_fixture(baseline, weights)
        after = self.run_fixture(changed, weights)
        pd.testing.assert_series_equal(before["equity_series"].loc[:self.times[2]],
                                       after["equity_series"].loc[:self.times[2]])
        self.assertEqual([t for t in before["trade_ledger"] if t["fill_time"] < self.times[2]],
                         [t for t in after["trade_ledger"] if t["fill_time"] < self.times[2]])

    def test_final_unfinished_hour_is_not_used(self):
        result = self.run_fixture(self.market([100.0, 100.0, 99999.0]), [{"ETHUSDT": 1.0}], end=self.times[2])
        self.assertEqual(result["equity_series"].index[-1], self.times[2])
        self.assertEqual(result["trades"], 1)

    def test_delayed_hourly_close_cannot_affect_signal_or_mark(self):
        data = self.market([100.0] * 3)
        data["ETHUSDT"]["available_at"] = data["ETHUSDT"].time + pd.Timedelta(hours=1)
        data["ETHUSDT"].loc[1, "available_at"] = self.times[3] + pd.Timedelta(days=1)
        data["ETHUSDT"].loc[1, "close"] = 1000000.0
        result = self.run_fixture(data, [{"ETHUSDT": 1.0}, {}], fee_rate=0.0)
        self.assertEqual(result["trades"], 1)
        self.assertEqual(result["final_equity"], 10000.0)

    def test_missing_signal_does_not_imply_a_trend_exit(self):
        data = self.market([100.0] * 3)
        data["ETHUSDT"]["quote_observed"] = [True, False, True]
        result = self.run_fixture(data, [{"ETHUSDT": 1.0}, {}])
        self.assertEqual(result["trades"], 1)

    def test_misaligned_symbol_timeline_is_rejected(self):
        data = self.market([100.0] * 3)
        data["ETHUSDT"]["time"] = data["ETHUSDT"].time + pd.Timedelta(hours=1)
        with self.assertRaisesRegex(ValueError, "unaligned hourly timeline"):
            self.run_fixture(data, [{"ETHUSDT": 1.0}] * 2)

    def test_unspecified_costs_remain_development_only(self):
        result = self.run_fixture(self.market([100.0] * 2), [{"ETHUSDT": 1.0}], fee_rate=None, slippage_rate=None)
        self.assertEqual(result["cost_assumption_status"], "UNSPECIFIED_ZERO_COST")
        self.assertEqual(result["research_status"], "DEVELOPMENT_ONLY")

    def test_empty_window_returns_initial_equity(self):
        result = self.run_fixture(self.market([100.0]), [], start=self.times[2], end=self.times[3])
        self.assertEqual(result["final_equity"], 10000.0)
        self.assertEqual(result["trades"], 0)

    def test_invalid_cost_rates_are_rejected(self):
        for fee, slippage in ((-0.1, 0), (0, -0.1), (np.nan, 0), (0, 1.0), (1.0, 0)):
            with self.subTest(fee=fee, slippage=slippage), self.assertRaises(ValueError):
                self.run_fixture(self.market([100.0]), [], fee_rate=fee, slippage_rate=slippage)


if __name__ == "__main__":
    unittest.main()
