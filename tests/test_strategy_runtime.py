import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
QPK_SRC = PROJECT_ROOT.parent / "QuantPlatformKit" / "src"
CRYPTO_STRATEGIES_SRC = PROJECT_ROOT.parent / "CryptoStrategies" / "src"
for path in (PROJECT_ROOT, QPK_SRC, CRYPTO_STRATEGIES_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

if "requests" not in sys.modules:
    requests_module = types.ModuleType("requests")
    requests_module.post = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_module

from quant_platform_kit import PortfolioSnapshot
from quant_platform_kit.strategy_contracts import StrategyManifest, StrategyRuntimeAdapter


class StrategyRuntimeTests(unittest.TestCase):
    def test_load_strategy_runtime_exposes_explicit_artifact_contract(self):
        try:
            from strategy_runtime import load_strategy_runtime
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed")
            raise

        runtime = load_strategy_runtime("crypto_leader_rotation")

        self.assertEqual(runtime.profile, "crypto_leader_rotation")
        self.assertEqual(runtime.runtime_adapter.portfolio_input_name, "portfolio_snapshot")
        self.assertTrue(str(runtime.default_local_artifact_path).endswith("BinancePlatform/artifacts/live_pool_legacy.json"))
        self.assertEqual(runtime.artifact_contract["version"], "crypto_leader_rotation.live_pool.v1")
        self.assertGreaterEqual(len(runtime.local_artifact_candidates), 1)
        self.assertIn(str(runtime.default_local_artifact_path), runtime.artifact_contract["default_local_candidates"])

    def test_strategy_runtime_evaluate_returns_decision_with_buy_sell_diagnostics(self):
        try:
            from strategy_runtime import load_strategy_runtime
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed")
            raise

        runtime = load_strategy_runtime("crypto_leader_rotation")
        account_metrics = {
            "total_equity": 10000.0,
            "cash_usdt": 2500.0,
            "trend_value": 1500.0,
            "dca_value": 1200.0,
        }
        try:
            evaluation = runtime.evaluate(
                prices={"ETHUSDT": 3000.0, "SOLUSDT": 180.0, "BTCUSDT": 60000.0},
                trend_indicators={
                    "ETHUSDT": {
                        "close": 3000.0,
                        "sma20": 2800.0,
                        "sma60": 2600.0,
                        "sma200": 2200.0,
                        "roc20": 0.20,
                        "roc60": 0.35,
                        "roc120": 0.60,
                        "vol20": 0.25,
                        "avg_quote_vol_30": 60000000.0,
                        "avg_quote_vol_90": 50000000.0,
                        "avg_quote_vol_180": 45000000.0,
                        "trend_persist_90": 0.80,
                        "age_days": 500,
                        "atr14": 120.0,
                    },
                    "SOLUSDT": {
                        "close": 180.0,
                        "sma20": 170.0,
                        "sma60": 160.0,
                        "sma200": 120.0,
                        "roc20": 0.28,
                        "roc60": 0.45,
                        "roc120": 0.75,
                        "vol20": 0.30,
                        "avg_quote_vol_30": 42000000.0,
                        "avg_quote_vol_90": 39000000.0,
                        "avg_quote_vol_180": 36000000.0,
                        "trend_persist_90": 0.76,
                        "age_days": 450,
                        "atr14": 8.0,
                    },
                },
                btc_snapshot={"regime_on": True, "btc_roc20": 0.08, "btc_roc60": 0.16, "btc_roc120": 0.30},
                account_metrics=account_metrics,
                trend_universe_symbols=("ETHUSDT", "SOLUSDT"),
                state={"ETHUSDT": {"is_holding": True, "entry_price": 2800.0, "highest_price": 3000.0}},
                translator=lambda key, **_kwargs: key,
                balances={"BTCUSDT": 0.02, "ETHUSDT": 0.3, "SOLUSDT": 1.2},
                now_utc=datetime(2026, 4, 7, tzinfo=timezone.utc),
                get_symbol_trade_state_fn=lambda state, symbol: state.get(
                    symbol,
                    {"is_holding": False, "entry_price": 0.0, "highest_price": 0.0},
                ),
                set_symbol_trade_state_fn=lambda state, symbol, symbol_state: state.__setitem__(symbol, dict(symbol_state)),
            )
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed")
            raise

        diagnostics = evaluation.decision.diagnostics
        self.assertEqual(evaluation.metadata["strategy_display_name"], "Crypto Leader Rotation")
        self.assertIn("planned_trend_buys", diagnostics)
        self.assertIn("eligible_buy_symbols", diagnostics)
        self.assertIn("sell_reasons", diagnostics)
        self.assertIn("btc_base_order_usdt", diagnostics)
        self.assertGreaterEqual(diagnostics["btc_base_order_usdt"], 15.0)

    def test_load_strategy_runtime_uses_entrypoint_only(self):
        try:
            import strategy_runtime as strategy_runtime_module
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed")
            raise

        fake_entrypoint = types.SimpleNamespace(
            manifest=types.SimpleNamespace(
                profile="crypto_leader_rotation",
                default_config={
                    "trend_pool_size": 4,
                    "artifact_contract_version": "crypto_leader_rotation.live_pool.v1",
                },
            )
        )
        fake_runtime_adapter = StrategyRuntimeAdapter(
            available_inputs=frozenset(
                {
                    "market_prices",
                    "derived_indicators",
                    "benchmark_snapshot",
                    "portfolio_snapshot",
                    "universe_snapshot",
                }
            ),
            portfolio_input_name="portfolio_snapshot",
        )

        with patch.object(strategy_runtime_module, "load_strategy_entrypoint_for_profile", return_value=fake_entrypoint) as mock_entrypoint_loader, patch.object(
            strategy_runtime_module,
            "get_platform_runtime_adapter",
            return_value=fake_runtime_adapter,
        ), patch.object(
            strategy_runtime_module,
            "tp_get_default_live_pool_candidates",
            side_effect=lambda default_path: [str(default_path), "/tmp/live_pool_fallback.json"],
        ):
            runtime = strategy_runtime_module.load_strategy_runtime("crypto_leader_rotation")

        mock_entrypoint_loader.assert_called_once_with("crypto_leader_rotation")
        self.assertIs(runtime.entrypoint, fake_entrypoint)
        self.assertIs(runtime.runtime_adapter, fake_runtime_adapter)
        self.assertEqual(runtime.merged_runtime_config["trend_pool_size"], 4)
        self.assertEqual(
            tuple(str(path) for path in runtime.local_artifact_candidates),
            runtime.artifact_contract["default_local_candidates"],
        )

    def test_strategy_runtime_maps_binance_inputs_into_canonical_strategy_context(self):
        try:
            import strategy_runtime as strategy_runtime_module
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed")
            raise

        captured = {}

        class FakeEntrypoint:
            manifest = StrategyManifest(
                profile="crypto_leader_rotation",
                domain="crypto",
                display_name="Crypto Leader Rotation",
                description="test",
                required_inputs=frozenset(
                    {
                        "market_prices",
                        "derived_indicators",
                        "benchmark_snapshot",
                        "portfolio_snapshot",
                        "universe_snapshot",
                    }
                ),
                default_config={"trend_pool_size": 4, "artifact_contract_version": "crypto_leader_rotation.live_pool.v1"},
            )

            def evaluate(self, ctx):
                captured["ctx"] = ctx
                return SimpleNamespace(positions=(), budgets=(), risk_flags=(), diagnostics={})

        runtime = strategy_runtime_module.LoadedStrategyRuntime(
            entrypoint=FakeEntrypoint(),
            runtime_adapter=StrategyRuntimeAdapter(
                available_inputs=frozenset(FakeEntrypoint.manifest.required_inputs),
                portfolio_input_name="portfolio_snapshot",
            ),
            merged_runtime_config=FakeEntrypoint.manifest.default_config,
        )

        with patch.object(strategy_runtime_module, "resolve_strategy_metadata", return_value=SimpleNamespace(display_name="Crypto Leader Rotation")):
            evaluation = runtime.evaluate(
                prices={"BTCUSDT": 60000.0, "ETHUSDT": 3000.0},
                trend_indicators={"ETHUSDT": {"close": 3000.0}},
                btc_snapshot={"regime_on": True},
                account_metrics={"total_equity": 10000.0, "cash_usdt": 2000.0, "trend_value": 3000.0, "dca_value": 5000.0},
                trend_universe_symbols=("ETHUSDT",),
                balances={"BTCUSDT": 0.0833333333, "ETHUSDT": 1.0},
                state={},
                translator=lambda key, **_kwargs: key,
                now_utc=datetime(2026, 4, 7, tzinfo=timezone.utc),
            )

        ctx = captured["ctx"]
        self.assertEqual(set(ctx.market_data), {"market_prices", "derived_indicators", "benchmark_snapshot", "portfolio_snapshot", "universe_snapshot"})
        self.assertIsInstance(ctx.portfolio, PortfolioSnapshot)
        self.assertEqual(ctx.market_data["market_prices"]["ETHUSDT"], 3000.0)
        self.assertEqual(ctx.market_data["universe_snapshot"], ("ETHUSDT",))
        self.assertEqual(ctx.portfolio.metadata["account_metrics"]["cash_usdt"], 2000.0)
        self.assertEqual(evaluation.metadata["strategy_display_name"], "Crypto Leader Rotation")


if __name__ == "__main__":
    unittest.main()
