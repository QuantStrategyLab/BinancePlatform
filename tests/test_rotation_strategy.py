import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
QPK_SRC = PROJECT_ROOT.parent / "QuantPlatformKit" / "src"
CRYPTO_STRATEGIES_SRC = PROJECT_ROOT.parent / "CryptoStrategies" / "src"
for path in (PROJECT_ROOT, QPK_SRC, CRYPTO_STRATEGIES_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from crypto_strategies.strategies.crypto_leader_rotation.rotation import (  # noqa: E402
    get_trend_sell_reason,
    plan_trend_buys,
    refresh_rotation_pool,
)
from crypto_strategies.strategies.crypto_leader_rotation.core import allocate_trend_buy_budget  # noqa: E402


class RotationStrategyTests(unittest.TestCase):
    def test_refresh_rotation_pool_uses_fallback_when_builder_returns_empty(self):
        state = {
            "trend_pool_version": "2026-03-15-core_major",
            "trend_pool_as_of_date": "2026-03-15",
        }

        selected_pool, ranking = refresh_rotation_pool(
            state,
            indicators_map={},
            btc_snapshot={},
            trend_universe_symbols=["ETHUSDT", "SOLUSDT", "XRPUSDT"],
            trend_pool_size=2,
            build_stable_quality_pool_fn=lambda *_args: ([], []),
            now_utc=datetime(2026, 3, 15, tzinfo=timezone.utc),
        )

        self.assertEqual(selected_pool, ["ETHUSDT", "SOLUSDT"])
        self.assertEqual(ranking, [])
        self.assertEqual(state["rotation_pool_symbols"], ["ETHUSDT", "SOLUSDT"])
        self.assertEqual(state["rotation_pool_source_version"], "2026-03-15-core_major")
        self.assertEqual(state["rotation_pool_source_as_of_date"], "2026-03-15")
        self.assertEqual(state["rotation_pool_last_month"], "2026-03")

    def test_get_trend_sell_reason_updates_highest_price_and_applies_atr_stop(self):
        state = {
            "ETHUSDT": {
                "is_holding": True,
                "entry_price": 90.0,
                "highest_price": 120.0,
            }
        }

        def get_symbol_trade_state(current_state, symbol):
            return dict(current_state[symbol])

        def set_symbol_trade_state(current_state, symbol, symbol_state):
            current_state[symbol] = dict(symbol_state)

        reason = get_trend_sell_reason(
            state,
            "ETHUSDT",
            curr_price=100.0,
            indicators={"atr14": 5.0, "sma60": 90.0},
            selected_candidates={"ETHUSDT": {"weight": 1.0}},
            atr_multiplier=2.0,
            get_symbol_trade_state_fn=get_symbol_trade_state,
            set_symbol_trade_state_fn=set_symbol_trade_state,
            translate_fn=lambda key, **kwargs: (
                f"{key}:{kwargs['stop_price']:.1f}" if key == "trend_sell_reason_atr_stop" else key
            ),
        )

        self.assertEqual(reason, "trend_sell_reason_atr_stop:110.0")
        self.assertEqual(state["ETHUSDT"]["highest_price"], 120.0)

    def test_plan_trend_buys_only_allocates_symbols_that_pass_filters(self):
        state = {
            "ETHUSDT": {"is_holding": True},
            "SOLUSDT": {"is_holding": False},
            "XRPUSDT": {"is_holding": False},
        }
        observed = {}

        def get_symbol_trade_state(current_state, symbol):
            return current_state[symbol]

        def allocate_budget(selected_candidates, buyable_symbols, total_budget):
            observed["selected_candidates"] = dict(selected_candidates)
            observed["buyable_symbols"] = list(buyable_symbols)
            observed["total_budget"] = total_budget
            return allocate_trend_buy_budget(selected_candidates, buyable_symbols, total_budget)

        eligible_buy_symbols, planned_trend_buys = plan_trend_buys(
            state,
            runtime_trend_universe={
                "ETHUSDT": {"base_asset": "ETH"},
                "SOLUSDT": {"base_asset": "SOL"},
                "XRPUSDT": {"base_asset": "XRP"},
            },
            selected_candidates={
                "SOLUSDT": {"weight": 0.7},
                "XRPUSDT": {"weight": 0.3},
            },
            trend_indicators={
                "SOLUSDT": {"sma20": 100.0, "sma60": 90.0, "sma200": 80.0},
                "XRPUSDT": {"sma20": 120.0, "sma60": 110.0, "sma200": 100.0},
            },
            prices={
                "ETHUSDT": 3000.0,
                "SOLUSDT": 130.0,
                "XRPUSDT": 105.0,
            },
            available_trend_buy_budget=250.0,
            allow_new_trend_entries=True,
            get_symbol_trade_state_fn=get_symbol_trade_state,
            allocate_trend_buy_budget_fn=allocate_budget,
        )

        self.assertEqual(eligible_buy_symbols, ["SOLUSDT"])
        self.assertAlmostEqual(planned_trend_buys["SOLUSDT"], 250.0)
        self.assertEqual(observed["buyable_symbols"], ["SOLUSDT"])
        self.assertEqual(observed["total_budget"], 250.0)


if __name__ == "__main__":
    unittest.main()
