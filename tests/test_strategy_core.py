import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import strategy_core
except ModuleNotFoundError as exc:
    if exc.name != "pandas":
        raise
    strategy_core = None


def make_indicator(close, vol20, *, bonus_bias=0.0):
    return {
        "close": close,
        "sma20": close * 0.95,
        "sma60": close * 0.90,
        "sma200": close * 0.85,
        "roc20": 0.08 + bonus_bias,
        "roc60": 0.12 + bonus_bias,
        "roc120": 0.18 + bonus_bias,
        "vol20": vol20,
        "avg_quote_vol_30": 12_000_000,
        "avg_quote_vol_90": 11_000_000,
        "avg_quote_vol_180": 10_000_000,
        "trend_persist_90": 0.95,
        "age_days": 500,
    }


class StrategyCoreTests(unittest.TestCase):
    @unittest.skipIf(strategy_core is None, "pandas is not installed")
    def test_build_rotation_pool_ranking_applies_membership_bonus(self):
        btc_snapshot = {
            "btc_roc20": 0.03,
            "btc_roc60": 0.05,
            "btc_roc120": 0.08,
            "regime_on": True,
        }
        indicators_map = {
            "ETHUSDT": make_indicator(120.0, 0.08),
            "SOLUSDT": make_indicator(120.0, 0.08),
        }

        ranking = strategy_core.build_rotation_pool_ranking(
            indicators_map,
            btc_snapshot,
            previous_pool={"SOLUSDT"},
            min_history_days=365,
            min_avg_quote_vol_180=8_000_000,
            membership_bonus=0.10,
            score_weights=strategy_core.DEFAULT_POOL_SCORE_WEIGHTS,
        )

        self.assertEqual([item["symbol"] for item in ranking], ["SOLUSDT", "ETHUSDT"])
        self.assertGreater(ranking[0]["score"], ranking[1]["score"])

    @unittest.skipIf(strategy_core is None, "pandas is not installed")
    def test_select_rotation_weights_supports_equal_and_inverse_vol(self):
        btc_snapshot = {
            "btc_roc20": 0.03,
            "btc_roc60": 0.05,
            "btc_roc120": 0.08,
            "regime_on": True,
        }
        indicators_map = {
            "ETHUSDT": make_indicator(120.0, 0.05, bonus_bias=0.01),
            "SOLUSDT": make_indicator(110.0, 0.10, bonus_bias=0.02),
        }
        prices = {"ETHUSDT": 120.0, "SOLUSDT": 110.0}

        equal_weights = strategy_core.select_rotation_weights(
            indicators_map,
            prices,
            btc_snapshot,
            ["ETHUSDT", "SOLUSDT"],
            top_n=2,
            weight_mode="equal",
        )
        inverse_vol_weights = strategy_core.select_rotation_weights(
            indicators_map,
            prices,
            btc_snapshot,
            ["ETHUSDT", "SOLUSDT"],
            top_n=2,
            weight_mode="inverse_vol",
        )

        self.assertAlmostEqual(equal_weights["ETHUSDT"]["weight"], 0.5)
        self.assertAlmostEqual(equal_weights["SOLUSDT"]["weight"], 0.5)
        self.assertAlmostEqual(inverse_vol_weights["ETHUSDT"]["weight"], 2.0 / 3.0)
        self.assertAlmostEqual(inverse_vol_weights["SOLUSDT"]["weight"], 1.0 / 3.0)

    @unittest.skipIf(strategy_core is None, "pandas is not installed")
    def test_compute_allocation_budgets_matches_layer_targets(self):
        allocation = strategy_core.compute_allocation_budgets(
            total_equity=10_000.0,
            cash_usdt=2_000.0,
            trend_val=7_000.0,
            dca_val=500.0,
        )

        self.assertAlmostEqual(allocation["btc_target_ratio"], 0.14 + 0.16 * 0.6931471805599453, places=6)
        self.assertAlmostEqual(allocation["trend_target_ratio"], 1.0 - allocation["btc_target_ratio"])
        self.assertAlmostEqual(allocation["trend_usdt_pool"], 490.964517, places=4)
        self.assertAlmostEqual(allocation["dca_usdt_pool"], 1509.035483, places=4)
        self.assertAlmostEqual(allocation["trend_layer_equity"], 7490.964517, places=4)


if __name__ == "__main__":
    unittest.main()
