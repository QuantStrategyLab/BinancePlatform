import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
QPK_SRC = PROJECT_ROOT.parent / "QuantPlatformKit" / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(QPK_SRC) not in sys.path:
    sys.path.insert(0, str(QPK_SRC))

from decision_mapper import map_strategy_decision_to_allocation, map_strategy_decision_to_rotation_plan
from quant_platform_kit.strategy_contracts import BudgetIntent, PositionTarget, StrategyDecision


class DecisionMapperTests(unittest.TestCase):
    def test_map_strategy_decision_to_allocation_uses_budgets_and_diagnostics(self):
        decision = StrategyDecision(
            positions=(
                PositionTarget(symbol="BTCUSDT", target_weight=0.3),
                PositionTarget(symbol="ETHUSDT", target_weight=0.4),
            ),
            budgets=(
                BudgetIntent(name="btc_core_dca_pool", symbol="BTCUSDT", amount=250.0),
                BudgetIntent(name="trend_rotation_pool", amount=400.0),
            ),
            diagnostics={
                "btc_target_ratio": 0.3,
                "trend_target_ratio": 0.7,
                "btc_base_order_usdt": 50.0,
            },
        )

        allocation = map_strategy_decision_to_allocation(
            decision,
            account_metrics={
                "total_equity": 10000.0,
                "trend_value": 3500.0,
                "dca_value": 1800.0,
            },
        )

        self.assertEqual(allocation["total_equity"], 10000.0)
        self.assertEqual(allocation["trend_usdt_pool"], 400.0)
        self.assertEqual(allocation["dca_usdt_pool"], 250.0)
        self.assertEqual(allocation["btc_base_order_usdt"], 50.0)
        self.assertEqual(allocation["btc_target_ratio"], 0.3)
        self.assertEqual(allocation["trend_target_ratio"], 0.7)

    def test_map_strategy_decision_to_rotation_plan_uses_unified_diagnostics(self):
        decision = StrategyDecision(
            diagnostics={
                "trend_pool": ("ETHUSDT", "SOLUSDT"),
                "rotation_candidates": {
                    "ETHUSDT": {"weight": 0.6, "relative_score": 1.2, "abs_momentum": 0.3},
                },
                "eligible_buy_symbols": ("ETHUSDT",),
                "planned_trend_buys": {"ETHUSDT": 320.0},
                "sell_reasons": {"SOLUSDT": "trend_sell_reason_rotated_out"},
                "artifact_contract": {"version": "v1"},
            },
            risk_flags=("regime_off",),
        )

        plan = map_strategy_decision_to_rotation_plan(decision)

        self.assertEqual(plan["active_trend_pool"], ["ETHUSDT", "SOLUSDT"])
        self.assertEqual(plan["eligible_buy_symbols"], ["ETHUSDT"])
        self.assertEqual(plan["planned_trend_buys"], {"ETHUSDT": 320.0})
        self.assertEqual(plan["sell_reasons"], {"SOLUSDT": "trend_sell_reason_rotated_out"})
        self.assertEqual(plan["artifact_contract"], {"version": "v1"})
        self.assertEqual(plan["risk_flags"], ("regime_off",))


if __name__ == "__main__":
    unittest.main()
