import sys
import types
import unittest
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
            now_utc=datetime(2026, 4, 7, tzinfo=timezone.utc),
            get_symbol_trade_state_fn=lambda state, symbol: state.get(
                symbol,
                {"is_holding": False, "entry_price": 0.0, "highest_price": 0.0},
            ),
            set_symbol_trade_state_fn=lambda state, symbol, symbol_state: state.__setitem__(symbol, dict(symbol_state)),
        )

        diagnostics = evaluation.decision.diagnostics
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

        with patch.object(strategy_runtime_module, "load_strategy_entrypoint_for_profile", return_value=fake_entrypoint) as mock_entrypoint_loader, patch.object(
            strategy_runtime_module,
            "tp_get_default_live_pool_candidates",
            side_effect=lambda default_path: [str(default_path), "/tmp/live_pool_fallback.json"],
        ):
            runtime = strategy_runtime_module.load_strategy_runtime("crypto_leader_rotation")

        mock_entrypoint_loader.assert_called_once_with("crypto_leader_rotation")
        self.assertIs(runtime.entrypoint, fake_entrypoint)
        self.assertEqual(runtime.merged_runtime_config["trend_pool_size"], 4)
        self.assertEqual(
            tuple(str(path) for path in runtime.local_artifact_candidates),
            runtime.artifact_contract["default_local_candidates"],
        )


if __name__ == "__main__":
    unittest.main()
