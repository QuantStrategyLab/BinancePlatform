import unittest
from types import SimpleNamespace

from application.execution_service import (
    execute_btc_dca_cycle,
    execute_trend_buys,
    execute_trend_rotation,
    execute_trend_sells,
    run_daily_circuit_breaker,
)


class ExecutionServiceTests(unittest.TestCase):
    def test_run_daily_circuit_breaker_liquidates_and_latches_state(self):
        runtime = SimpleNamespace(client=object())
        report = {"buy_sell_intents": []}
        state = {}
        balances = {"ETHUSDT": 2.0}
        prices = {"ETHUSDT": 100.0}
        observed = {"asset_checks": [], "client_calls": [], "state_sets": [], "persist_reasons": [], "notifications": []}

        result = run_daily_circuit_breaker(
            runtime,
            report,
            state,
            {"ETHUSDT": {"base_asset": "ETH"}},
            balances,
            50.0,
            prices,
            -0.10,
            -0.05,
            [],
            format_qty_fn=lambda _client, _symbol, qty: round(qty - 0.5, 4),
            runtime_notify_fn=lambda _runtime, _report, text: observed["notifications"].append(text),
            ensure_asset_available_fn=lambda _runtime, _report, asset, amount, _log_buffer: observed["asset_checks"].append((asset, amount)) or True,
            runtime_call_client_fn=lambda _runtime, _report, method_name, payload, effect_type: observed["client_calls"].append(
                (method_name, payload, effect_type)
            ),
            set_symbol_trade_state_fn=lambda _state, symbol, symbol_state: observed["state_sets"].append((symbol, dict(symbol_state))),
            runtime_set_trade_state_fn=lambda _runtime, _report, _state, reason: observed["persist_reasons"].append(reason),
            build_balance_snapshot_fn=lambda _universe, current_balances, u_total: {"USDT": u_total, "ETH": current_balances["ETHUSDT"]},
            translate_fn=lambda key, **kwargs: f"{key}:{kwargs}" if kwargs else key,
        )

        self.assertTrue(result)
        self.assertAlmostEqual(balances["ETHUSDT"], 0.5, places=6)
        self.assertTrue(state["is_circuit_broken"])
        self.assertEqual(state["last_balance_snapshot"], {"USDT": 200.0, "ETH": 0.5})
        self.assertTrue(report["circuit_breaker_triggered"])
        self.assertEqual(report["buy_sell_intents"][0]["reason"], "daily_circuit_breaker")
        self.assertEqual(observed["asset_checks"][0][0], "ETH")
        self.assertEqual(observed["client_calls"][0][0], "order_market_sell")
        self.assertEqual(observed["state_sets"][0][0], "ETHUSDT")
        self.assertEqual(observed["persist_reasons"], ["daily_circuit_breaker"])
        self.assertGreaterEqual(len(observed["notifications"]), 1)

    def test_execute_trend_sells_executes_sell_and_updates_runtime_state(self):
        runtime = SimpleNamespace(client=object())
        report = {"buy_sell_intents": []}
        state = {}
        balances = {"ETHUSDT": 2.0}
        prices = {"ETHUSDT": 100.0}
        observed = {
            "asset_checks": [],
            "client_calls": [],
            "state_sets": [],
            "actions": [],
            "persist_reasons": [],
            "notifications": [],
            "logs": [],
        }

        result = execute_trend_sells(
            runtime,
            report,
            state,
            {"ETHUSDT": {"base_asset": "ETH"}},
            {"SOLUSDT": {"weight": 1.0}},
            {"ETHUSDT": {"atr14": 4.0, "sma60": 90.0}},
            prices,
            balances,
            50.0,
            [],
            "20260329",
            2.5,
            get_trend_sell_reason_fn=lambda *_args: "rotated_out",
            should_skip_duplicate_trend_action_fn=lambda *_args: False,
            append_log_fn=lambda _buffer, message: observed["logs"].append(message),
            translate_fn=lambda key, **kwargs: f"{key}:{kwargs}" if kwargs else key,
            format_qty_fn=lambda _client, _symbol, _qty: 1.5,
            ensure_asset_available_fn=lambda _runtime, _report, asset, amount, _log_buffer: observed["asset_checks"].append((asset, amount)) or True,
            runtime_call_client_fn=lambda _runtime, _report, method_name, payload, effect_type: observed["client_calls"].append(
                (method_name, payload, effect_type)
            ),
            next_order_id_fn=lambda *_args: "sell-order-id",
            set_symbol_trade_state_fn=lambda _state, symbol, symbol_state: observed["state_sets"].append((symbol, dict(symbol_state))),
            record_trend_action_fn=lambda _state, symbol, action, action_date: observed["actions"].append((symbol, action, action_date)),
            runtime_set_trade_state_fn=lambda _runtime, _report, _state, reason: observed["persist_reasons"].append(reason),
            runtime_notify_fn=lambda _runtime, _report, text: observed["notifications"].append(text),
        )

        self.assertAlmostEqual(result, 200.0, places=6)
        self.assertAlmostEqual(balances["ETHUSDT"], 0.5, places=6)
        self.assertEqual(report["buy_sell_intents"][0]["action"], "sell")
        self.assertEqual(report["buy_sell_intents"][0]["reason"], "rotated_out")
        self.assertEqual(observed["asset_checks"][0][0], "ETH")
        self.assertEqual(observed["client_calls"][0][0], "order_market_sell")
        self.assertEqual(observed["actions"], [("ETHUSDT", "sell", "20260329")])
        self.assertEqual(observed["persist_reasons"], ["trend_sell:ETHUSDT"])
        self.assertGreaterEqual(len(observed["notifications"]), 1)

    def test_execute_trend_buys_executes_buy_and_updates_runtime_state(self):
        runtime = SimpleNamespace(client=object())
        report = {"buy_sell_intents": []}
        state = {}
        balances = {"ETHUSDT": 0.0}
        prices = {"ETHUSDT": 100.0}
        observed = {
            "asset_checks": [],
            "client_calls": [],
            "state_sets": [],
            "actions": [],
            "persist_reasons": [],
            "notifications": [],
            "logs": [],
        }

        result = execute_trend_buys(
            runtime,
            report,
            state,
            {"ETHUSDT": {"weight": 0.6, "relative_score": 1.2}},
            ["ETHUSDT"],
            {"ETHUSDT": 200.0},
            prices,
            balances,
            500.0,
            [],
            "20260329",
            should_skip_duplicate_trend_action_fn=lambda *_args: False,
            append_log_fn=lambda _buffer, message: observed["logs"].append(message),
            translate_fn=lambda key, **kwargs: f"{key}:{kwargs}" if kwargs else key,
            format_qty_fn=lambda _client, _symbol, qty: round(qty, 6),
            ensure_asset_available_fn=lambda _runtime, _report, asset, amount, _log_buffer: observed["asset_checks"].append((asset, amount)) or True,
            runtime_call_client_fn=lambda _runtime, _report, method_name, payload, effect_type: observed["client_calls"].append(
                (method_name, payload, effect_type)
            ),
            next_order_id_fn=lambda *_args: "buy-order-id",
            set_symbol_trade_state_fn=lambda _state, symbol, symbol_state: observed["state_sets"].append((symbol, dict(symbol_state))),
            record_trend_action_fn=lambda _state, symbol, action, action_date: observed["actions"].append((symbol, action, action_date)),
            runtime_set_trade_state_fn=lambda _runtime, _report, _state, reason: observed["persist_reasons"].append(reason),
            runtime_notify_fn=lambda _runtime, _report, text: observed["notifications"].append(text),
        )

        self.assertAlmostEqual(result, 303.0, places=6)
        self.assertAlmostEqual(balances["ETHUSDT"], 1.97, places=6)
        self.assertEqual(report["buy_sell_intents"][0]["action"], "buy")
        self.assertEqual(report["buy_sell_intents"][0]["budget"], 200.0)
        self.assertEqual(observed["asset_checks"][0][0], "USDT")
        self.assertEqual(observed["client_calls"][0][0], "order_market_buy")
        self.assertEqual(observed["actions"], [("ETHUSDT", "buy", "20260329")])
        self.assertEqual(observed["persist_reasons"], ["trend_buy:ETHUSDT"])
        self.assertGreaterEqual(len(observed["notifications"]), 1)

    def test_execute_trend_rotation_delegates_sell_buy_and_status_flow(self):
        runtime = SimpleNamespace(now_utc="2026-03-29T00:00:00Z")
        report = {"selected_symbols": {"active_trend_pool": [], "selected_candidates": []}}
        state = {}
        runtime_trend_universe = {"ETHUSDT": {"base_asset": "ETH"}}
        trend_indicators = {"ETHUSDT": {"sma20": 1.0}}
        btc_snapshot = {"ahr999": 0.6}
        prices = {"ETHUSDT": 2000.0}
        balances = {"ETHUSDT": 0.5}
        log_buffer = []
        observed = {}

        def fake_refresh_rotation_pool(*_args, **kwargs):
            observed["refresh"] = kwargs
            return ["ETHUSDT"], []

        def fake_compute_portfolio_allocation(*_args):
            observed["post_sell_budget"] = 320.0
            return {"trend_usdt_pool": 320.0}

        def fake_execute_trend_sells(*_args):
            observed["sell_called"] = True
            return 1150.0

        def fake_plan_trend_buys(*_args):
            observed["buy_budget"] = _args[5]
            return ["ETHUSDT"], {"ETHUSDT": 320.0}

        def fake_execute_trend_buys(*_args):
            observed["buy_plan"] = dict(_args[5])
            return 980.0

        result = execute_trend_rotation(
            runtime,
            report,
            state,
            runtime_trend_universe,
            trend_indicators,
            btc_snapshot,
            prices,
            balances,
            1000.0,
            15.0,
            log_buffer,
            "20260329",
            True,
            False,
            2.5,
            refresh_rotation_pool=fake_refresh_rotation_pool,
            select_rotation_weights=lambda *_args: {"ETHUSDT": {"weight": 1.0, "relative_score": 1.5}},
            append_rotation_summary=lambda *_args: observed.__setitem__("summary_called", True),
            compute_portfolio_allocation=fake_compute_portfolio_allocation,
            execute_trend_sells=fake_execute_trend_sells,
            plan_trend_buys=fake_plan_trend_buys,
            execute_trend_buys=fake_execute_trend_buys,
            append_trend_symbol_status=lambda *_args: observed.__setitem__("status_called", True),
            rotation_top_n=2,
            official_trend_pool_symbols=["ETHUSDT", "SOLUSDT"],
        )

        self.assertEqual(result, 980.0)
        self.assertEqual(
            report["selected_symbols"],
            {
                "active_trend_pool": ["ETHUSDT"],
                "selected_candidates": ["ETHUSDT"],
            },
        )
        self.assertEqual(
            observed["refresh"],
            {"allow_refresh": False, "now_utc": "2026-03-29T00:00:00Z"},
        )
        self.assertEqual(observed["buy_budget"], 320.0)
        self.assertEqual(observed["buy_plan"], {"ETHUSDT": 320.0})
        self.assertTrue(observed["summary_called"])
        self.assertTrue(observed["sell_called"])
        self.assertTrue(observed["status_called"])

    def test_execute_btc_dca_cycle_executes_buy_branch(self):
        runtime = SimpleNamespace(client=object())
        report = {"btc_dca_intents": []}
        state = {}
        balances = {"BTCUSDT": 0.1}
        prices = {"BTCUSDT": 50_000.0}
        log_buffer = []
        observed = {"asset_checks": [], "client_calls": [], "persist_reasons": [], "notifications": []}

        result = execute_btc_dca_cycle(
            runtime,
            report,
            state,
            balances,
            prices,
            1000.0,
            20_000.0,
            300.0,
            5000.0,
            {"ahr999": 0.4, "zscore": 0.0, "sell_trigger": 3.5},
            0.25,
            "20260329",
            log_buffer,
            append_log_fn=lambda buffer, message: buffer.append(message),
            translate_fn=lambda key, **_kwargs: key,
            get_dynamic_btc_base_order=lambda _total_equity: 50.0,
            format_qty_fn=lambda _client, _symbol, qty: round(qty, 6),
            ensure_asset_available_fn=lambda _runtime, _report, asset, amount, _log_buffer: observed["asset_checks"].append((asset, amount)) or True,
            runtime_call_client_fn=lambda _runtime, _report, method_name, payload, effect_type: observed["client_calls"].append(
                (method_name, payload, effect_type)
            ),
            next_order_id_fn=lambda *_args: "buy-order-id",
            runtime_notify_fn=lambda _runtime, _report, text: observed["notifications"].append(text),
            runtime_set_trade_state_fn=lambda _runtime, _report, _state, reason: observed["persist_reasons"].append(reason),
        )

        self.assertAlmostEqual(result, 753.75, places=2)
        self.assertAlmostEqual(balances["BTCUSDT"], 0.104925, places=6)
        self.assertEqual(state["dca_last_buy_date"], "20260329")
        self.assertEqual(report["btc_dca_intents"][0]["action"], "buy")
        self.assertEqual(observed["asset_checks"][0][0], "USDT")
        self.assertEqual(observed["client_calls"][0][0], "order_market_buy")
        self.assertEqual(observed["persist_reasons"], ["btc_dca_buy"])
        self.assertEqual(log_buffer, ["btc_accumulation_radar_line"])

    def test_execute_btc_dca_cycle_executes_trim_branch(self):
        runtime = SimpleNamespace(client=object())
        report = {"btc_dca_intents": []}
        state = {}
        balances = {"BTCUSDT": 1.0}
        prices = {"BTCUSDT": 10_000.0}
        log_buffer = []
        observed = {"asset_checks": [], "client_calls": [], "persist_reasons": []}

        result = execute_btc_dca_cycle(
            runtime,
            report,
            state,
            balances,
            prices,
            500.0,
            20_000.0,
            5.0,
            10_000.0,
            {"ahr999": 2.0, "zscore": 4.5, "sell_trigger": 3.5},
            0.25,
            "20260329",
            log_buffer,
            append_log_fn=lambda buffer, message: buffer.append(message),
            translate_fn=lambda key, **_kwargs: key,
            get_dynamic_btc_base_order=lambda _total_equity: 50.0,
            format_qty_fn=lambda _client, _symbol, qty: round(qty, 6),
            ensure_asset_available_fn=lambda _runtime, _report, asset, amount, _log_buffer: observed["asset_checks"].append((asset, amount)) or True,
            runtime_call_client_fn=lambda _runtime, _report, method_name, payload, effect_type: observed["client_calls"].append(
                (method_name, payload, effect_type)
            ),
            next_order_id_fn=lambda *_args: "sell-order-id",
            runtime_notify_fn=lambda *_args, **_kwargs: None,
            runtime_set_trade_state_fn=lambda _runtime, _report, _state, reason: observed["persist_reasons"].append(reason),
        )

        self.assertAlmostEqual(result, 3500.0, places=2)
        self.assertAlmostEqual(balances["BTCUSDT"], 0.7, places=6)
        self.assertEqual(state["dca_last_sell_date"], "20260329")
        self.assertEqual(report["btc_dca_intents"][0]["action"], "sell")
        self.assertEqual(report["btc_dca_intents"][0]["sell_pct"], 0.3)
        self.assertEqual(observed["asset_checks"][0][0], "BTC")
        self.assertEqual(observed["client_calls"][0][0], "order_market_sell")
        self.assertEqual(observed["persist_reasons"], ["btc_dca_sell"])


if __name__ == "__main__":
    unittest.main()
