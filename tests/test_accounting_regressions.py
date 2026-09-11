import unittest
import copy
from types import SimpleNamespace

from application.execution_service import execute_trend_buys, execute_trend_sells
from application.portfolio_service import (
    compute_daily_pnls,
    maybe_rebase_daily_state_for_balance_change,
    maybe_reset_daily_state,
)
from runtime_support import ExecutionIntegrityError
from runtime_support import (
    build_execution_report,
    reconcile_runtime_cash_effects,
    release_runtime_state_owner,
    runtime_call_client,
    runtime_set_trade_state,
)
from market_snapshot_support import top_up_bnb_fuel
from tests.test_runtime_support import owned_runtime


def _filled(*, symbol, side, executed, quote, fills):
    return {
        "status": "FILLED",
        "symbol": symbol,
        "side": side,
        "clientOrderId": f"{side.lower()}-{symbol}",
        "executedQty": str(executed),
        "cummulativeQuoteQty": str(quote),
        "fills": fills,
    }


def _fill(price, qty, commission="0", asset="USDT"):
    return {
        "price": str(price),
        "qty": str(qty),
        "commission": str(commission),
        "commissionAsset": asset,
    }


class AccountingRegressionTests(unittest.TestCase):
    def _buy(self, *, state, balances, prices, response, symbols=("ETHUSDT",)):
        calls = []
        self.last_calls = calls

        def call_client(*_args, **kwargs):
            calls.append(kwargs["payload"]["symbol"])
            return response(kwargs["payload"]["symbol"]) if callable(response) else response

        cash = execute_trend_buys(
            SimpleNamespace(client=object(), dry_run=False),
            {"buy_sell_intents": [], "gating_summary": {}, "gating_events": []},
            state,
            {symbol: {"weight": 1 / len(symbols), "relative_score": 1.0} for symbol in symbols},
            list(symbols),
            {symbol: 200.0 for symbol in symbols},
            prices,
            balances,
            200.0,
            [],
            "20260911",
            should_skip_duplicate_trend_action_fn=lambda *_args: False,
            append_log_fn=lambda *_args: None,
            translate_fn=lambda key, **_kwargs: key,
            format_qty_fn=lambda *_args: 1.0,
            ensure_asset_available_fn=lambda *_args: True,
            runtime_call_client_fn=call_client,
            next_order_id_fn=lambda _runtime, prefix, symbol: f"{prefix}-{symbol}",
            set_symbol_trade_state_fn=lambda current, symbol, value: current.update({symbol: value}),
            record_trend_action_fn=lambda current, symbol, action, date: current.setdefault(
                "trend_action_history", {}
            ).update({symbol: {"action": action, "date": date}}),
            runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
            runtime_notify_fn=lambda *_args: None,
        )
        return cash, calls

    def test_actual_buy_uses_quote_quantity_base_fee_and_execution_price(self):
        state = {
            "daily_trend_equity_base": 0.0,
            "daily_trend_cash_flow_usdt": 0.0,
            "daily_trend_net_invested_usdt": 0.0,
            "daily_trend_risk_base_usdt": 0.0,
            "daily_trend_third_fee_usdt": 0.0,
        }
        balances = {"ETHUSDT": 0.0}
        cash, calls = self._buy(
            state=state,
            balances=balances,
            prices={"ETHUSDT": 100.0},
            response=_filled(
                symbol="ETHUSDT",
                side="BUY",
                executed=1,
                quote=105,
                fills=[_fill(105, 1, "0.001", "ETH")],
            ),
        )

        self.assertEqual(calls, ["ETHUSDT"])
        self.assertAlmostEqual(cash, 95.0)
        self.assertAlmostEqual(balances["ETHUSDT"], 0.999)
        self.assertAlmostEqual(state["ETHUSDT"]["entry_price"], 105.0)
        self.assertAlmostEqual(state["daily_trend_cash_flow_usdt"], -105.0)
        self.assertAlmostEqual(state["daily_trend_risk_base_usdt"], 105.0)

    def test_multipart_quote_fee_sell_preserves_zero_loss_rotation_value(self):
        state = {"last_reset_date": "2026-09-10"}
        maybe_reset_daily_state(
            state,
            SimpleNamespace(),
            {},
            "2026-09-11",
            1000.0,
            1000.0,
            runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
        )
        balances = {"ETHUSDT": 2.0, "SOLUSDT": 8.0}
        response = _filled(
            symbol="ETHUSDT",
            side="SELL",
            executed=2,
            quote=200,
            fills=[_fill(100, 0.75, "0", "USDT"), _fill(100, 1.25, "0", "USDT")],
        )
        cash = execute_trend_sells(
            SimpleNamespace(client=object(), dry_run=False),
            {"buy_sell_intents": []},
            state,
            {"ETHUSDT": {"base_asset": "ETH"}, "SOLUSDT": {"base_asset": "SOL"}},
            {"ETHUSDT": "rotation"},
            {"ETHUSDT": 100.0, "SOLUSDT": 100.0},
            balances,
            0.0,
            [],
            "20260911",
            should_skip_duplicate_trend_action_fn=lambda *_args: False,
            append_log_fn=lambda *_args: None,
            translate_fn=lambda key, **_kwargs: key,
            format_qty_fn=lambda *_args: 2.0,
            ensure_asset_available_fn=lambda *_args: True,
            runtime_call_client_fn=lambda *_args, **_kwargs: response,
            next_order_id_fn=lambda *_args: "sell-ETHUSDT",
            set_symbol_trade_state_fn=lambda *_args: None,
            record_trend_action_fn=lambda *_args: None,
            runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
            runtime_notify_fn=lambda *_args: None,
        )

        self.assertEqual(cash, 200.0)
        self.assertEqual(balances["ETHUSDT"], 0.0)
        self.assertEqual(compute_daily_pnls(state, 1000.0, 800.0), (0.0, 0.0))

    def test_bnb_fee_reduces_trend_pnl_and_account_equity(self):
        state = {
            "daily_trend_equity_base": 0.0,
            "daily_trend_cash_flow_usdt": 0.0,
            "daily_trend_net_invested_usdt": 0.0,
            "daily_trend_risk_base_usdt": 0.0,
            "daily_trend_third_fee_usdt": 0.0,
        }
        balances = {"ETHUSDT": 0.0, "BNBUSDT": 1.0}
        cash, _ = self._buy(
            state=state,
            balances=balances,
            prices={"ETHUSDT": 100.0, "BNBUSDT": 300.0},
            response=_filled(
                symbol="ETHUSDT",
                side="BUY",
                executed=1,
                quote=100,
                fills=[_fill(100, 1, "0.01", "BNB")],
            ),
        )

        self.assertEqual(cash, 100.0)
        self.assertAlmostEqual(balances["BNBUSDT"], 0.99)
        self.assertAlmostEqual(compute_daily_pnls(state, 397.0, 100.0)[1], -0.03)

    def test_missing_or_nan_fill_data_preserves_intent_and_stops_next_order(self):
        for response in (
            _filled(symbol="ETHUSDT", side="BUY", executed="nan", quote=100, fills=[_fill(100, 1)]),
            _filled(symbol="ETHUSDT", side="BUY", executed=1, quote=100, fills=[]),
            _filled(symbol="ETHUSDT", side="BUY", executed=1, quote=100, fills=[_fill(100, 1, ".01", "BNB")]),
        ):
            with self.subTest(response=response):
                state = {}
                balances = {"ETHUSDT": 0.0, "SOLUSDT": 0.0}
                with self.assertRaises(ExecutionIntegrityError):
                    _cash, calls = self._buy(
                        state=state,
                        balances=balances,
                        prices={"ETHUSDT": 100.0, "SOLUSDT": 100.0},
                        response=lambda symbol: response if symbol == "ETHUSDT" else _filled(
                            symbol="SOLUSDT", side="BUY", executed=1, quote=100, fills=[_fill(100, 1)]
                        ),
                        symbols=("ETHUSDT", "SOLUSDT"),
                    )
                self.assertEqual(self.last_calls, ["ETHUSDT"])

    def test_zero_opening_sleeve_uses_first_net_investment_as_risk_denominator(self):
        state = {
            "daily_trend_equity_base": 0.0,
            "daily_trend_cash_flow_usdt": 0.0,
            "daily_trend_net_invested_usdt": 0.0,
            "daily_trend_risk_base_usdt": 0.0,
            "daily_trend_third_fee_usdt": 0.0,
        }
        balances = {"ETHUSDT": 0.0}
        self._buy(
            state=state,
            balances=balances,
            prices={"ETHUSDT": 100.0},
            response=_filled(symbol="ETHUSDT", side="BUY", executed=1, quote=100, fills=[_fill(100, 1)]),
        )

        self.assertAlmostEqual(compute_daily_pnls(state, 194.0, 94.0)[1], -0.06)

    def test_nonempty_sleeve_with_zero_risk_base_blocks_pnl_evaluation(self):
        state = {
            "last_reset_date": "2026-09-11",
            "daily_equity_base": 1000.0,
            "daily_trend_equity_base": 0.0,
            "daily_trend_pnl_basis": "trend_mark_plus_cash_flow_v1",
            "daily_trend_cash_flow_usdt": -100.0,
            "daily_trend_net_invested_usdt": 100.0,
            "daily_trend_risk_base_usdt": 0.0,
            "daily_trend_third_fee_usdt": 0.0,
        }

        with self.assertRaisesRegex(
            ExecutionIntegrityError,
            "daily_trend_accounting_unverifiable",
        ):
            maybe_reset_daily_state(
                state,
                SimpleNamespace(),
                {},
                "2026-09-11",
                980.0,
                80.0,
                runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
            )

        with self.assertRaisesRegex(
            ExecutionIntegrityError,
            "daily_trend_accounting_unverifiable",
        ):
            compute_daily_pnls(state, 980.0, 80.0)

    def test_unexplained_balance_change_and_same_day_basis_migration_block_without_reset(self):
        original = {
            "last_balance_snapshot": {"USDT": 0.0, "ETH": 10.0},
            "daily_equity_base": 1000.0,
            "daily_trend_equity_base": 1000.0,
            "daily_trend_pnl_basis": "trend_val",
            "last_reset_date": "2026-09-11",
            "is_circuit_broken": True,
        }
        state = dict(original)
        with self.assertRaises(ExecutionIntegrityError):
            maybe_rebase_daily_state_for_balance_change(
                state,
                SimpleNamespace(),
                {},
                900.01,
                900.0,
                {"USDT": 0.01, "ETH": 10.0},
                [],
                runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
                append_log_fn=lambda *_args: None,
                translate_fn=lambda key, **_kwargs: key,
            )
        self.assertEqual(state, original)

        with self.assertRaises(ExecutionIntegrityError):
            maybe_reset_daily_state(
                state,
                SimpleNamespace(),
                {},
                "2026-09-11",
                900.01,
                900.0,
                runtime_set_trade_state_fn=lambda *_args, **_kwargs: None,
            )
        self.assertEqual(state, original)

    def test_actual_runtime_fill_is_pending_until_accounting_state_is_persisted(self):
        persisted = []
        state = {
            "order_submission": {"state": "RESERVED"},
            "daily_trend_equity_base": 0.0,
            "daily_trend_cash_flow_usdt": 0.0,
            "daily_trend_net_invested_usdt": 0.0,
            "daily_trend_risk_base_usdt": 0.0,
            "daily_trend_third_fee_usdt": 0.0,
        }

        class Client:
            def order_market_buy(self, **payload):
                return _filled(
                    symbol="ETHUSDT",
                    side="BUY",
                    executed=1,
                    quote=105,
                    fills=[_fill(105, 1, "0.001", "ETH")],
                ) | {"clientOrderId": payload["newClientOrderId"]}

        def write(current):
            persisted.append(copy.deepcopy(current))
            return True

        runtime = owned_runtime(
            dry_run=False,
            client=Client(),
            trade_state=state,
            state_loader=lambda *, normalize=False: copy.deepcopy(state),
            state_writer=write,
        )
        balances = {"ETHUSDT": 0.0}
        cash = execute_trend_buys(
            runtime,
            build_execution_report(runtime),
            state,
            {"ETHUSDT": {"weight": 1.0, "relative_score": 1.0}},
            ["ETHUSDT"],
            {"ETHUSDT": 200.0},
            {"ETHUSDT": 100.0},
            balances,
            200.0,
            [],
            "20260911",
            should_skip_duplicate_trend_action_fn=lambda *_args: False,
            append_log_fn=lambda *_args: None,
            translate_fn=lambda key, **_kwargs: key,
            format_qty_fn=lambda *_args: 1.0,
            ensure_asset_available_fn=lambda *_args: True,
            runtime_call_client_fn=lambda runtime, report, **kwargs: runtime_call_client(
                runtime, report, max_retries=0, retry_base_sec=0, **kwargs
            ),
            next_order_id_fn=lambda *_args: "actual-fill",
            set_symbol_trade_state_fn=lambda current, symbol, value: current.update({symbol: value}),
            record_trend_action_fn=lambda current, symbol, action, date: current.setdefault(
                "trend_action_history", {}
            ).update({symbol: {"action": action, "date": date}}),
            runtime_set_trade_state_fn=runtime_set_trade_state,
            runtime_notify_fn=lambda *_args: None,
        )

        self.assertEqual(cash, 95.0)
        self.assertEqual([item["order_submission"]["state"] for item in persisted], [
            "SUBMISSION_UNKNOWN", "FILLED_ACCOUNTING_PENDING", "TERMINAL"
        ])
        self.assertEqual(runtime.pending_funds, [])
        self.assertTrue(release_runtime_state_owner(runtime))

    def test_fuel_fill_pending_closes_only_after_observed_bnb_and_usdt_balances(self):
        persisted = []
        state = {
            "order_submission": {"state": "RESERVED"},
            "last_balance_snapshot": {"BNB": 0.9, "USDT": 100.0},
        }

        class Client:
            def order_market_buy(self, **payload):
                return _filled(
                    symbol="BNBUSDT",
                    side="BUY",
                    executed=0.1,
                    quote=30,
                    fills=[_fill(300, 0.1, "0", "BNB")],
                ) | {"clientOrderId": payload["newClientOrderId"]}

            def get_asset_balance(self, *, asset):
                return {"free": "1.0" if asset == "BNB" else "70.0", "locked": "0"}

            def get_simple_earn_flexible_product_position(self, *, asset):
                return {"rows": []}

        def write(current):
            persisted.append(copy.deepcopy(current))
            return True

        runtime = owned_runtime(
            dry_run=False,
            client=Client(),
            trade_state=state,
            state_loader=lambda *, normalize=False: copy.deepcopy(state),
            state_writer=write,
        )
        report = build_execution_report(runtime)
        result = top_up_bnb_fuel(
            runtime,
            report,
            100.0,
            0.0,
            [],
            min_bnb_value=10.0,
            buy_bnb_amount=30.0,
            ensure_asset_available_fn=lambda *_args: True,
            runtime_call_client_fn=lambda runtime, report, **kwargs: runtime_call_client(
                runtime, report, max_retries=0, retry_base_sec=0, **kwargs
            ),
            runtime_notify_fn=lambda *_args: None,
            append_log_fn=lambda *_args: None,
        )
        self.assertEqual(result[2], "filled_pending_snapshot")
        self.assertEqual(state["order_submission"]["state"], "FILLED_ACCOUNTING_PENDING")

        mismatch_state = copy.deepcopy(state)
        mismatch_runtime = owned_runtime(
            dry_run=False,
            client=Client(),
            trade_state=mismatch_state,
        )
        mismatch_runtime.client.get_asset_balance = lambda *, asset: {
            "free": "1.1" if asset == "BNB" else "70.0",
            "locked": "0",
        }
        mismatch_runtime.pending_funds = copy.deepcopy(runtime.pending_funds)
        with self.assertRaisesRegex(ExecutionIntegrityError, "cash_reconciliation_uncertain"):
            reconcile_runtime_cash_effects(mismatch_runtime, mismatch_state)
        self.assertEqual(mismatch_state["last_balance_snapshot"], {"BNB": 0.9, "USDT": 100.0})
        self.assertEqual(mismatch_state["order_submission"]["state"], "FILLED_ACCOUNTING_PENDING")

        reconcile_runtime_cash_effects(runtime, state)
        runtime_set_trade_state(runtime, report, state, reason="cash_reconciliation")

        self.assertEqual(state["order_submission"], {"state": "TERMINAL"})
        self.assertEqual(state["last_balance_snapshot"], {"BNB": 1.0, "USDT": 70.0})
        self.assertEqual(runtime.pending_funds, [])

    def test_fuel_balance_read_does_not_clear_incomplete_known_fill(self):
        state = {
            "order_submission": {
                "state": "FILLED_ACCOUNTING_PENDING",
                "identity_sha256": "a" * 64,
                "symbol": "BNBUSDT",
                "known_fill": {
                    "client_order_id": "known-fill",
                    "side": "BUY",
                    "executed_qty": "nan",
                    "cummulative_quote_qty": "30",
                    "commissions": [],
                },
            },
            "last_balance_snapshot": {},
        }

        class Client:
            def get_asset_balance(self, *, asset):
                return {"free": "1" if asset == "BNB" else "70", "locked": "0"}

            def get_simple_earn_flexible_product_position(self, *, asset):
                return {"rows": []}

        runtime = owned_runtime(dry_run=False, client=Client(), trade_state=state)
        runtime.pending_funds = [{
            "confirmed": True,
            "action": "buy",
            "symbol": "BNBUSDT",
            "asset": "BNB",
        }]

        with self.assertRaisesRegex(ExecutionIntegrityError, "filled_order_accounting_unverifiable"):
            reconcile_runtime_cash_effects(runtime, state)

        self.assertEqual(state["order_submission"]["state"], "FILLED_ACCOUNTING_PENDING")
        self.assertEqual(len(runtime.pending_funds), 1)

if __name__ == "__main__":
    unittest.main()
