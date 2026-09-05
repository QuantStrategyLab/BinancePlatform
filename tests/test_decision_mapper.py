import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
QPK_SRC = PROJECT_ROOT.parent / "QuantPlatformKit" / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(QPK_SRC) not in sys.path:
    sys.path.insert(0, str(QPK_SRC))

from decision_mapper import map_strategy_decision_to_allocation, map_strategy_decision_to_rotation_plan
from application.execution_service import execute_trend_buys
from quant_platform_kit.common.strategy_contracts import BudgetIntent, PositionTarget, StrategyDecision


class DecisionMapperTests(unittest.TestCase):
    def test_rejected_or_missing_risk_assessment_cannot_submit_diagnostic_buy_plan(self):
        for assessment in ({"outcome": "REJECT"}, None):
            with self.subTest(assessment=assessment):
                diagnostics = {
                    "rotation_candidates": {
                        "ETHUSDT": {"weight": 0.5, "relative_score": 1.2, "abs_momentum": 0.3},
                    },
                    "eligible_buy_symbols": ("ETHUSDT",),
                    "planned_trend_buys": {"ETHUSDT": 100.0},
                }
                if assessment is not None:
                    diagnostics["member_risk_assessment"] = assessment
                plan = map_strategy_decision_to_rotation_plan(
                    StrategyDecision(diagnostics=diagnostics)
                )
                submitted = []

                execute_trend_buys(
                    SimpleNamespace(client=object()),
                    {"buy_sell_intents": [], "gating_summary": {}, "gating_events": []},
                    {},
                    plan["selected_candidates"],
                    plan["eligible_buy_symbols"],
                    plan["planned_trend_buys"],
                    {"ETHUSDT": 100.0},
                    {"ETHUSDT": 0.0},
                    500.0,
                    [],
                    "20260905",
                    should_skip_duplicate_trend_action_fn=lambda *_args: False,
                    append_log_fn=lambda *_args: None,
                    translate_fn=lambda key, **_kwargs: key,
                    format_qty_fn=lambda _client, _symbol, qty: qty,
                    ensure_asset_available_fn=lambda *_args: True,
                    runtime_call_client_fn=lambda _runtime, _report, method_name, payload, effect_type: submitted.append(
                        (method_name, payload, effect_type)
                    ),
                    next_order_id_fn=lambda _runtime, _prefix, symbol: f"buy-{symbol}",
                    set_symbol_trade_state_fn=lambda state, symbol, value: state.update({symbol: value}),
                    record_trend_action_fn=lambda *_args: None,
                    runtime_set_trade_state_fn=lambda *_args: None,
                    runtime_notify_fn=lambda *_args: None,
                )

                self.assertEqual(submitted, [])

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
                "metadata": {
                    "combo": {
                        "base_btc_weight": 0.50,
                        "base_trend_weight": 0.50,
                        "btc_weight": 0.25,
                        "trend_weight": 0.0,
                        "dynamic_regime_mode": "dual_leg",
                        "regime_tier": "hard",
                    },
                    "regime_off": True,
                    "btc_sma200_ratio": 0.94,
                    "ma200_slope": -0.01,
                    "gross_exposure": 0.25,
                },
                "rotation_candidates": {
                    "ETHUSDT": {"weight": 0.6, "relative_score": 1.2, "abs_momentum": 0.3},
                },
                "eligible_buy_symbols": ("ETHUSDT",),
                "planned_trend_buys": {"ETHUSDT": 320.0},
                "sell_reasons": {"SOLUSDT": "trend_sell_reason_rotated_out"},
                "artifact_contract": {"version": "v1"},
                "member_risk_assessment": {"outcome": "APPROVE"},
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
        self.assertEqual(
            plan["combo_diagnostics"],
            {
                "base_btc_weight": 0.50,
                "base_trend_weight": 0.50,
                "effective_btc_weight": 0.25,
                "effective_trend_weight": 0.0,
                "dynamic_regime_mode": "dual_leg",
                "regime_tier": "hard",
                "regime_off": True,
                "btc_sma200_ratio": 0.94,
                "ma200_slope": -0.01,
                "gross_exposure": 0.25,
            },
        )


    def test_legacy_live_baseline_maps_risk_gated_decision_without_new_authority(self):
        """c4b24a1 compatibility: preserve the known-good risk-gated decision bridge."""
        decision = StrategyDecision(
            positions=(
                PositionTarget(symbol="BTCUSDT", target_weight=0.3),
                PositionTarget(symbol="ETHUSDT", target_weight=0.4),
            ),
            budgets=(
                BudgetIntent(name="btc_core_dca_pool", symbol="BTCUSDT", amount=250.0),
                BudgetIntent(name="trend_rotation_pool", amount=400.0),
            ),
            diagnostics={"btc_base_order_usdt": 50.0},
        )

        allocation = map_strategy_decision_to_allocation(
            decision,
            account_metrics={"total_equity": 1_000.0, "trend_value": 400.0, "dca_value": 300.0},
        )

        self.assertEqual(allocation["btc_target_ratio"], 0.3)
        self.assertEqual(allocation["trend_target_ratio"], 0.4)
        self.assertEqual(allocation["trend_usdt_pool"], 400.0)
        self.assertEqual(allocation["dca_usdt_pool"], 250.0)
        self.assertEqual(allocation["btc_base_order_usdt"], 50.0)


if __name__ == "__main__":
    unittest.main()
