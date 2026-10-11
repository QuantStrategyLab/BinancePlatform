"""B09: RunContext isolates trend universe and strategy runtime from module globals."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main
from run_context import (
    RunContext,
    RunContextError,
    bind_run_context,
    get_active_run_context,
    reset_run_context,
    resolve_strategy_runtime,
    resolve_trend_universe,
    set_active_trend_universe,
)
from runtime_support import ExecutionRuntime


def _baseline_universe():
    return {symbol: meta.copy() for symbol, meta in main.STATIC_TREND_UNIVERSE.items()}


class RunContextUnitTests(unittest.TestCase):
    def setUp(self):
        main.TREND_UNIVERSE = _baseline_universe()

    def tearDown(self):
        main.TREND_UNIVERSE = _baseline_universe()
        self.assertIsNone(
            get_active_run_context(),
            "active RunContext leaked after test",
        )

    def test_resolve_falls_back_to_module_when_unbound(self):
        self.assertIs(resolve_trend_universe(main.TREND_UNIVERSE), main.TREND_UNIVERSE)
        self.assertIs(resolve_strategy_runtime(main.STRATEGY_RUNTIME), main.STRATEGY_RUNTIME)

    def test_unbound_resolve_without_fallback_fails_closed(self):
        with self.assertRaises(RunContextError):
            resolve_strategy_runtime()
        with self.assertRaises(RunContextError):
            resolve_trend_universe()

    def test_set_and_resolve_use_active_context(self):
        custom = {"ZZZTESTUSDT": {"base_asset": "ZZZ"}}
        ctx = RunContext(trend_universe=_baseline_universe())
        token = bind_run_context(ctx)
        try:
            set_active_trend_universe(
                custom,
                fallback_setter=lambda _value: (_ for _ in ()).throw(
                    AssertionError("fallback must not run while bound")
                ),
            )
            self.assertEqual(dict(resolve_trend_universe(main.TREND_UNIVERSE)), custom)
            self.assertEqual(dict(main.TREND_UNIVERSE), _baseline_universe())
        finally:
            reset_run_context(token)
        self.assertEqual(dict(main.TREND_UNIVERSE), _baseline_universe())
        self.assertIs(resolve_trend_universe(main.TREND_UNIVERSE), main.TREND_UNIVERSE)

    def test_sequential_binds_do_not_leak_universe(self):
        first = {"AAAISOLATEUSDT": {"base_asset": "AAA"}}
        second = {"BBBISOLATEUSDT": {"base_asset": "BBB"}}

        ctx1 = RunContext(trend_universe=_baseline_universe())
        token1 = bind_run_context(ctx1)
        try:
            set_active_trend_universe(first, fallback_setter=main._module_set_trend_universe)
            self.assertEqual(dict(resolve_trend_universe(main.TREND_UNIVERSE)), first)
        finally:
            reset_run_context(token1)

        ctx2 = RunContext(trend_universe=_baseline_universe())
        token2 = bind_run_context(ctx2)
        try:
            resolved = resolve_trend_universe(main.TREND_UNIVERSE)
            self.assertNotIn("AAAISOLATEUSDT", resolved)
            set_active_trend_universe(second, fallback_setter=main._module_set_trend_universe)
            self.assertEqual(dict(resolve_trend_universe(main.TREND_UNIVERSE)), second)
            self.assertNotIn("AAAISOLATEUSDT", resolve_trend_universe(main.TREND_UNIVERSE))
        finally:
            reset_run_context(token2)

        self.assertEqual(dict(main.TREND_UNIVERSE), _baseline_universe())

    def test_bound_resolve_ignores_poisoned_module_strategy_runtime(self):
        """PR-3: while bound, module STRATEGY_RUNTIME must not be consulted."""
        module_handle = main.STRATEGY_RUNTIME
        bound_handle = SimpleNamespace(profile="bound-profile", marker="ctx")
        poison = SimpleNamespace(profile="poison-module", marker="module")
        ctx = RunContext(trend_universe=_baseline_universe(), strategy_runtime=bound_handle)
        token = bind_run_context(ctx)
        try:
            main.STRATEGY_RUNTIME = poison
            # Fallback arg is also poisoned — bound path must ignore both.
            self.assertIs(resolve_strategy_runtime(poison), bound_handle)
            self.assertIs(resolve_strategy_runtime(module_handle), bound_handle)
            self.assertIs(main.STRATEGY_RUNTIME, poison)
        finally:
            reset_run_context(token)
            main.STRATEGY_RUNTIME = module_handle
        self.assertIs(resolve_strategy_runtime(module_handle), module_handle)

    def test_bound_without_strategy_runtime_fails_closed(self):
        ctx = RunContext(trend_universe=_baseline_universe(), strategy_runtime=None)
        token = bind_run_context(ctx)
        try:
            with self.assertRaises(RunContextError):
                resolve_strategy_runtime(main.STRATEGY_RUNTIME)
        finally:
            reset_run_context(token)

    def test_sequential_binds_do_not_leak_strategy_runtime(self):
        first = SimpleNamespace(profile="first")
        second = SimpleNamespace(profile="second")
        module_handle = main.STRATEGY_RUNTIME

        ctx1 = RunContext(trend_universe=_baseline_universe(), strategy_runtime=first)
        token1 = bind_run_context(ctx1)
        try:
            self.assertIs(resolve_strategy_runtime(module_handle), first)
        finally:
            reset_run_context(token1)

        ctx2 = RunContext(trend_universe=_baseline_universe(), strategy_runtime=second)
        token2 = bind_run_context(ctx2)
        try:
            self.assertIs(resolve_strategy_runtime(module_handle), second)
            self.assertIsNot(resolve_strategy_runtime(module_handle), first)
        finally:
            reset_run_context(token2)

        self.assertIs(resolve_strategy_runtime(module_handle), module_handle)


class ExecuteCycleUniverseIsolationTests(unittest.TestCase):
    def setUp(self):
        self._baseline = _baseline_universe()
        main.TREND_UNIVERSE = _baseline_universe()

    def tearDown(self):
        main.TREND_UNIVERSE = _baseline_universe()
        self.assertIsNone(get_active_run_context())

    def test_execute_cycle_does_not_mutate_module_universe_on_success(self):
        injected = {"ZZZTESTUSDT": {"base_asset": "ZZZ"}}

        def fake_cycle(runtime, **_kwargs):
            self.assertIsNotNone(runtime.run_context)
            main._set_runtime_trend_universe(injected)
            self.assertEqual(dict(resolve_trend_universe(main.TREND_UNIVERSE)), injected)
            self.assertEqual(dict(main.TREND_UNIVERSE), self._baseline)
            return {"status": "ok"}

        runtime = ExecutionRuntime(dry_run=True, strategy_profile="crypto_live_pool_rotation")
        with patch("main.execute_strategy_cycle", side_effect=fake_cycle):
            report = main.execute_cycle(runtime)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(dict(main.TREND_UNIVERSE), self._baseline)
        self.assertIsNone(get_active_run_context())

    def test_execute_cycle_does_not_mutate_module_universe_on_error(self):
        injected = {"ZZZTESTUSDT": {"base_asset": "ZZZ"}}

        def fake_cycle(runtime, **_kwargs):
            main._set_runtime_trend_universe(injected)
            raise RuntimeError("boom")

        runtime = ExecutionRuntime(dry_run=True, strategy_profile="crypto_live_pool_rotation")
        with patch("main.execute_strategy_cycle", side_effect=fake_cycle):
            with self.assertRaises(RuntimeError):
                main.execute_cycle(runtime)
        self.assertEqual(dict(main.TREND_UNIVERSE), self._baseline)
        self.assertIsNone(get_active_run_context())

    def test_unbound_setter_still_updates_module_for_compat(self):
        custom = {"ZZZTESTUSDT": {"base_asset": "ZZZ"}}
        self.assertIsNone(get_active_run_context())
        main._set_runtime_trend_universe(custom)
        try:
            self.assertEqual(dict(main.TREND_UNIVERSE), custom)
        finally:
            main.TREND_UNIVERSE = _baseline_universe()

    def test_execute_cycle_mounts_runtime_strategy_handle(self):
        mounted = SimpleNamespace(profile="mounted-from-runtime", marker="rt")

        def fake_cycle(runtime, **_kwargs):
            self.assertIs(runtime.run_context.strategy_runtime, mounted)
            self.assertIs(resolve_strategy_runtime(main.STRATEGY_RUNTIME), mounted)
            return {"status": "ok"}

        runtime = ExecutionRuntime(
            dry_run=True,
            strategy_profile="crypto_live_pool_rotation",
            strategy_runtime=mounted,
        )
        with patch("main.execute_strategy_cycle", side_effect=fake_cycle):
            report = main.execute_cycle(runtime)
        self.assertEqual(report["status"], "ok")
        self.assertIsNone(get_active_run_context())

    def test_execute_cycle_ignores_module_strategy_poison_and_does_not_leak(self):
        """PR-3: cycle resolve ignores module poison; module STRATEGY_RUNTIME unchanged."""
        prior = main.STRATEGY_RUNTIME
        mounted = SimpleNamespace(profile="cycle-mounted", marker="rt")
        poison = SimpleNamespace(profile="poison", marker="module")

        def fake_cycle(runtime, **_kwargs):
            main.STRATEGY_RUNTIME = poison
            self.assertIs(resolve_strategy_runtime(poison), mounted)
            self.assertIs(runtime.run_context.strategy_runtime, mounted)
            return {"status": "ok"}

        runtime = ExecutionRuntime(
            dry_run=True,
            strategy_profile="crypto_live_pool_rotation",
            strategy_runtime=mounted,
        )
        try:
            with patch("main.execute_strategy_cycle", side_effect=fake_cycle):
                report = main.execute_cycle(runtime)
            self.assertEqual(report["status"], "ok")
            # Poison applied inside cycle must not be "restored over" — we prove
            # cycle itself did not write module STRATEGY_RUNTIME; caller poison remains.
            self.assertIs(main.STRATEGY_RUNTIME, poison)
        finally:
            main.STRATEGY_RUNTIME = prior
        self.assertIsNone(get_active_run_context())

    def test_activate_while_bound_writes_context_not_module(self):
        """PR-3: activate during a bound cycle must not leak into module STRATEGY_RUNTIME."""
        prior = main.STRATEGY_RUNTIME
        prior_pool = main.TREND_POOL_SIZE
        prior_legacy = main.DEFAULT_LIVE_POOL_LEGACY_PATH
        prior_max_age = main.DEFAULT_TREND_POOL_MAX_AGE_DAYS
        prior_modes = main.DEFAULT_TREND_POOL_ACCEPTABLE_MODES
        fake = SimpleNamespace(
            profile="crypto_live_pool_rotation",
            trend_pool_size=11,
            default_local_artifact_path=prior.default_local_artifact_path,
            artifact_contract={
                "max_age_days": int(prior.artifact_contract["max_age_days"]),
                "acceptable_modes": tuple(prior.artifact_contract["acceptable_modes"]),
            },
            local_artifact_candidates=(),
        )
        ctx = RunContext(
            trend_universe=_baseline_universe(),
            strategy_runtime=SimpleNamespace(profile="seed"),
        )
        token = bind_run_context(ctx)
        try:
            with patch("main.load_strategy_runtime", return_value=fake) as loader:
                activated = main._activate_execution_strategy_runtime("crypto_live_pool_rotation")
            loader.assert_called_once_with("crypto_live_pool_rotation")
            self.assertIs(activated, fake)
            self.assertIs(ctx.strategy_runtime, fake)
            self.assertIs(main.STRATEGY_RUNTIME, prior)  # no module leakage
            self.assertIs(resolve_strategy_runtime(prior), fake)
            self.assertEqual(main.TREND_POOL_SIZE, 11)
        finally:
            reset_run_context(token)
            main.STRATEGY_RUNTIME = prior
            main.TREND_POOL_SIZE = prior_pool
            main.DEFAULT_LIVE_POOL_LEGACY_PATH = prior_legacy
            main.DEFAULT_TREND_POOL_MAX_AGE_DAYS = prior_max_age
            main.DEFAULT_TREND_POOL_ACCEPTABLE_MODES = prior_modes

    def test_activate_unbound_still_updates_module_for_compat(self):
        prior = main.STRATEGY_RUNTIME
        prior_pool = main.TREND_POOL_SIZE
        prior_legacy = main.DEFAULT_LIVE_POOL_LEGACY_PATH
        prior_max_age = main.DEFAULT_TREND_POOL_MAX_AGE_DAYS
        prior_modes = main.DEFAULT_TREND_POOL_ACCEPTABLE_MODES
        fake = SimpleNamespace(
            profile="unbound-activate",
            trend_pool_size=7,
            default_local_artifact_path=prior.default_local_artifact_path,
            artifact_contract={
                "max_age_days": int(prior.artifact_contract["max_age_days"]),
                "acceptable_modes": tuple(prior.artifact_contract["acceptable_modes"]),
            },
            local_artifact_candidates=(),
        )
        self.assertIsNone(get_active_run_context())
        try:
            with patch("main.load_strategy_runtime", return_value=fake):
                activated = main._activate_execution_strategy_runtime("crypto_live_pool_rotation")
            self.assertIs(activated, fake)
            self.assertIs(main.STRATEGY_RUNTIME, fake)
            self.assertEqual(main.TREND_POOL_SIZE, 7)
        finally:
            main.STRATEGY_RUNTIME = prior
            main.TREND_POOL_SIZE = prior_pool
            main.DEFAULT_LIVE_POOL_LEGACY_PATH = prior_legacy
            main.DEFAULT_TREND_POOL_MAX_AGE_DAYS = prior_max_age
            main.DEFAULT_TREND_POOL_ACCEPTABLE_MODES = prior_modes


class StrategyRuntimeEquivalenceTests(unittest.TestCase):
    """Bound resolve path must match direct handle for a frozen profile."""

    def tearDown(self):
        self.assertIsNone(get_active_run_context())

    def test_bound_resolve_matches_module_metrics_for_same_handle(self):
        activated = main.STRATEGY_RUNTIME
        universe = {
            "ETHUSDT": {"base_asset": "ETH"},
            "SOLUSDT": {"base_asset": "SOL", "valuation_only": True},
        }
        prices = {"BTCUSDT": 1.0, "ETHUSDT": 1.0, "SOLUSDT": 1.0, "BNBUSDT": 1.0}
        balances = {"BTCUSDT": 0.0, "ETHUSDT": 0.0, "SOLUSDT": 0.0, "BNBUSDT": 0.0}
        u_total = 100.0
        fuel_val = 0.0

        direct_metrics = activated.compute_account_metrics(
            universe, balances, prices, u_total, fuel_val
        )

        ctx = RunContext(
            trend_universe={symbol: meta.copy() for symbol, meta in universe.items()},
            strategy_runtime=activated,
        )
        token = bind_run_context(ctx)
        try:
            resolved = resolve_strategy_runtime(SimpleNamespace(profile="must-not-use"))
            self.assertIs(resolved, activated)
            bound_metrics = resolved.compute_account_metrics(
                universe, balances, prices, u_total, fuel_val
            )
        finally:
            reset_run_context(token)

        self.assertEqual(bound_metrics, direct_metrics)
        self.assertTrue(callable(activated.evaluate))
        self.assertTrue(callable(resolve_strategy_runtime(activated).evaluate))


if __name__ == "__main__":
    unittest.main()
