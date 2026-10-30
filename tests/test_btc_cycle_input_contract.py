import ast
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from infra.btc_cycle_input import BtcCycleInputError, adapt_btc_cycle_input, cycle_failure_metadata, read_envelope

NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)


def envelope():
    return {"contract_version": "btc-cycle-input/v1", "source": "fixture", "source_version": "1",
            "symbol": "BTCUSDT", "observed_at": "2026-10-25T11:00:00Z",
            "available_at": "2026-10-25T11:05:00Z", "ahr999": 0.7}


def adapt(data=None, indicators=None, **kwargs):
    return adapt_btc_cycle_input({} if indicators is None else indicators, envelope() if data is None else data,
                                 as_of=kwargs.get("as_of", NOW), max_age_seconds=kwargs.get("max_age_seconds", 7200),
                                 price=60000, ma200=50000)


class ContractTests(unittest.TestCase):
    def test_valid_and_input_is_not_mutated(self):
        source = {"BTC-USD": {"sma200": 50000}, "ETHUSDT": {"close": 1}}
        output = adapt(indicators=source)
        self.assertEqual(output["BTCUSDT"]["ahr999"], .7)
        self.assertNotIn("ahr999", source["BTC-USD"])
        self.assertEqual(output["ETHUSDT"], source["ETHUSDT"])

    def test_fail_closed_contract_and_temporal_errors(self):
        cases = [{"contract_version": "wrong"}, {"source": " "}, {"source_version": ""},
                 {"symbol": "BTC"}, {"ahr999": True}, {"ahr999": float("nan")}, {"ahr999": 0},
                 {"ahr999": 10 ** 400}, {"ahr999": 1e309},
                 {"observed_at": "2026-10-25T11:00:00"}, {"available_at": "2026-10-25T13:00:00Z"},
                 {"observed_at": "2026-10-25T11:30:00Z"}, {"observed_at": "2026-10-25T09:00:00Z"}]
        for mutation in cases:
            with self.subTest(mutation=mutation), self.assertRaises(BtcCycleInputError):
                adapt({**envelope(), **mutation})
        for bad in (None, 0, -1, True, float("inf"), 10 ** 400, 1e309):
            with self.subTest(max_age=bad), self.assertRaises(BtcCycleInputError):
                adapt(max_age_seconds=bad)
        with self.assertRaises(BtcCycleInputError):
            adapt(as_of=NOW.replace(tzinfo=None))

    def test_conflicting_aliases_and_values(self):
        for data in ({"BTC": {}, "BTCUSDT": {}}, {"BTCUSDT": {"ahr999_gma": 0.8}},
                     {"BTCUSDT": {"sma200": 1}}, {"ahr999": 0.7},
                     {"BTCUSDT": {"ahr999": .7, " AHR999 ": .7}},
                     {"BTCUSDT": {"ahr999_sma": float("nan")}},
                     {"BTCUSDT": {"mayer_multiple": 5}},
                     {"BTCUSDT": {"estimate_price": 1}}):
            with self.subTest(data=data), self.assertRaises(BtcCycleInputError):
                adapt(indicators=data)

    def test_failure_projection_is_scoped_and_exact(self):
        error = BtcCycleInputError("btc_cycle_input_unavailable")
        kwargs = {"stage": "portfolio_allocation", "validation_scope": "full_cycle_no_submit"}
        self.assertEqual(cycle_failure_metadata(error, **kwargs)["reason_code"], "btc_cycle_input_unavailable")
        self.assertEqual(cycle_failure_metadata(error, stage="portfolio_allocation", validation_scope="standard"), {})
        self.assertEqual(cycle_failure_metadata(error, stage="market_snapshot", validation_scope="full_cycle_no_submit"), {})
        self.assertEqual(cycle_failure_metadata(ValueError("btc_cycle_input_unavailable"), **kwargs), {})
        self.assertEqual(cycle_failure_metadata(BtcCycleInputError("private"), **kwargs), {})

    def test_duplicate_json_and_unreadable_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            path.write_text('{"source":"a","source":"b"}')
            for candidate in (str(path), str(path / "missing")):
                with self.assertRaisesRegex(BtcCycleInputError, "^btc_cycle_input_unavailable$") as caught:
                    read_envelope(candidate)
                self.assertTrue(caught.exception.__suppress_context__)


class AssemblyTests(unittest.TestCase):
    def test_standard_snapshot_still_calls_original_enrichment(self):
        tree = ast.parse(Path("main.py").read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == "resolve_runtime_btc_snapshot")
        module = ast.Module(body=[function], type_ignores=[])
        seen = []
        scope = {"infra_resolve_runtime_btc_snapshot": lambda *a, **k: {"ma200": 5},
                 "fetch_btc_market_snapshot": None, "get_env_int": lambda *a: 1,
                 "BTC_MARKET_SNAPSHOT_RETRY_DELAYS": (), "time": SimpleNamespace(sleep=None),
                 "append_log": None, "t": lambda *a, **k: "",
                 "enrich_btc_snapshot_with_cycle_indicators": lambda snapshot, log: seen.append(snapshot) or snapshot}
        exec(compile(module, "main.py", "exec"), scope)
        runtime = SimpleNamespace(strategy_profile="crypto_btc_dca")
        scope["resolve_runtime_btc_snapshot"](runtime, 1, [], validation_scope="standard")
        self.assertEqual(seen, [{"ma200": 5}])
        scope["resolve_runtime_btc_snapshot"](runtime, 1, [], validation_scope="full_cycle_no_submit")
        self.assertEqual(len(seen), 1)

    def test_isolated_main_function_bodies_reach_crypto_strategy_and_same_clock(self):
        # Extract the unchanged function bodies to avoid importing the platform's
        # unavailable broker/cloud dependencies; evaluate real Crypto logic.
        from quant_platform_kit import PortfolioSnapshot
        from quant_platform_kit.common.strategy_contracts import StrategyContext
        from crypto_strategies.entrypoints import evaluate_crypto_btc_dca
        from crypto_strategies.strategies import crypto_btc_dca

        tree = ast.parse(Path("main.py").read_text())
        names = {"_resolve_strategy_evaluation", "_compute_portfolio_allocation"}
        module = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names],
                            type_ignores=[])
        class Runtime:
            effective_runtime_config = {"smart_multiplier_enabled": True, "cycle_indicator_enabled": True}
            def compute_account_metrics(self, *args):
                return {"total_equity": 1000, "cash_usdt": 1000, "dca_value": 0}
            def evaluate(self, **kwargs):
                self.seen = kwargs
                snapshot = PortfolioSnapshot(as_of=kwargs["now_utc"], total_equity=1000, buying_power=1000,
                    cash_balance=1000, metadata={"observed_effective_exposure": 0.0,
                    "btc_zscore_exit": {"route": "normal"}})
                context = StrategyContext(as_of=kwargs["now_utc"], state={}, portfolio=snapshot,
                    market_data={"market_prices": kwargs["prices"], "derived_indicators": kwargs["trend_indicators"],
                                 "portfolio_snapshot": snapshot}, runtime_config={})
                decision = evaluate_crypto_btc_dca(context)
                return SimpleNamespace(decision=decision, account_metrics=kwargs["account_metrics"], metadata={})
        strategy = Runtime()
        scope = {"resolve_strategy_runtime": lambda _: strategy, "STRATEGY_RUNTIME": None, "os": os, "Path": Path,
                 "Mapping": __import__("collections.abc", fromlist=["Mapping"]).Mapping,
                 "t": lambda key, **kwargs: key, "get_symbol_trade_state": None, "set_symbol_trade_state": None,
                 "map_decision_to_allocation": lambda *a, **k: {"execution_permitted": False},
                 "public_stoploss_observation": lambda _: None}
        exec(compile(module, "main.py", "exec"), scope)
        runtime = SimpleNamespace(now_utc=NOW, strategy_profile="crypto_btc_dca", dry_run=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cycle.json"
            path.write_text(json.dumps(envelope()))
            with patch.dict(os.environ, {"BTC_CYCLE_INDICATORS_PATH": str(path),
                                         "BTC_CYCLE_INPUT_MAX_AGE_SECONDS": "7200"}), patch.object(
                    crypto_btc_dca, "compute_signals", wraps=crypto_btc_dca.compute_signals) as compute:
                result = scope["_compute_portfolio_allocation"](runtime, {}, {"BTCUSDT": 0},
                    {"BTCUSDT": 60000}, 1000, 0, {}, {}, {"ma200": 50000, "ahr999": 9},
                    validation_scope="full_cycle_no_submit")
                cache = {}
                scope["_compute_portfolio_allocation"](runtime, {}, {"BTCUSDT": 0},
                    {"BTCUSDT": 60000}, 1000, 0, {}, {}, {"ma200": 50000},
                    validation_scope="full_cycle_no_submit", btc_input_cache=cache)
                path.unlink()
                runtime.now_utc = NOW + timedelta(hours=4)
                scope["_compute_portfolio_allocation"](runtime, {}, {"BTCUSDT": 0},
                    {"BTCUSDT": 60000}, 1000, 0, {}, {}, {"ma200": 50000},
                    validation_scope="full_cycle_no_submit", btc_input_cache=cache)
                self.assertEqual(strategy.seen["now_utc"], NOW)
                with self.assertRaises(BtcCycleInputError):
                    scope["_compute_portfolio_allocation"](runtime, {}, {"BTCUSDT": 0},
                        {"BTCUSDT": 60000}, 1000, 0, {}, {}, {"ma200": 50000},
                        validation_scope="full_cycle_no_submit", btc_input_cache={})
                runtime.now_utc = NOW
        self.assertEqual(compute.call_args.kwargs["derived_indicators"]["BTCUSDT"]["ahr999"], .7)
        self.assertEqual(compute.call_args.kwargs["as_of"], NOW)
        self.assertEqual(strategy.seen["now_utc"], NOW)
        self.assertEqual(strategy.seen["trend_indicators"]["BTCUSDT"]["ahr999"], .7)
        self.assertIn("risk_flags", result)
        with patch.dict(os.environ, {"BTC_CYCLE_INDICATORS_PATH": "/missing", "BTC_CYCLE_INPUT_MAX_AGE_SECONDS": "7200"}):
            with self.assertRaisesRegex(BtcCycleInputError, "^btc_cycle_input_unavailable$"):
                scope["_compute_portfolio_allocation"](runtime, {}, {"BTCUSDT": 0},
                    {"BTCUSDT": 60000}, 1000, 0, {}, {}, {"ma200": 50000},
                    validation_scope="full_cycle_no_submit")
            # A deliberately non-smart configuration does not need a cycle file.
            strategy.effective_runtime_config = {"smart_multiplier_enabled": False, "cycle_indicator_enabled": False}
            def ordinary(**kwargs):
                strategy.seen = kwargs
                return SimpleNamespace(decision=SimpleNamespace(diagnostics={}, risk_flags=()),
                                       account_metrics=kwargs["account_metrics"], metadata={})
            strategy.evaluate = ordinary
            scope["_compute_portfolio_allocation"](runtime, {}, {"BTCUSDT": 0},
                {"BTCUSDT": 60000}, 1000, 0, {}, {}, {"ma200": 50000},
                validation_scope="full_cycle_no_submit")
            self.assertEqual(strategy.seen["trend_indicators"], {})
            strategy.effective_runtime_config = {"smart_multiplier_enabled": True, "cycle_indicator_enabled": False}
            scope["_compute_portfolio_allocation"](runtime, {}, {"BTCUSDT": 0},
                {"BTCUSDT": 60000}, 1000, 0, {}, {}, {"ma200": 50000},
                validation_scope="full_cycle_no_submit")
            for config in ({}, {"smart_multiplier_enabled": None}, {"smart_multiplier_enabled": "true"},
                           {"cycle_indicator_enabled": "1"}):
                strategy.effective_runtime_config = config
                strategy.seen = None
                with self.subTest(config=config), self.assertRaises(BtcCycleInputError):
                    scope["_compute_portfolio_allocation"](runtime, {}, {"BTCUSDT": 0},
                        {"BTCUSDT": 60000}, 1000, 0, {}, {}, {"ma200": 50000},
                        validation_scope="full_cycle_no_submit")
                self.assertIsNone(strategy.seen)


if __name__ == "__main__":
    unittest.main()
