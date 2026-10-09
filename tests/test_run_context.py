"""B09: RunContext isolates trend universe from module globals."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import main
from run_context import (
    RunContext,
    bind_run_context,
    get_active_run_context,
    reset_run_context,
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


if __name__ == "__main__":
    unittest.main()
