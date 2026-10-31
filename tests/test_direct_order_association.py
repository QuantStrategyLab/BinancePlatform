import copy
import unittest
from unittest.mock import patch

from runtime_support import (
    ExecutionRuntime, OrderReconciliationError, ClientCallError, StatePersistenceError,
    acquire_runtime_state_owner, release_runtime_state_owner, build_execution_report,
    runtime_call_client,
)
from application.execution_service import execute_btc_dca_cycle, execute_trend_buys, execute_trend_sells


class Store:
    def __init__(self):
        self.data = {"order_submission": {"state": "RESERVED"}}

    def load(self, *, normalize=False):
        return copy.deepcopy(self.data)

    def write(self, state):
        self.data = copy.deepcopy(state)
        return True


class Client:
    def __init__(self, status="FILLED", mutation=None):
        self.status, self.mutation, self.submits = status, mutation, []

    def _submit(self, side, payload):
        self.submits.append(payload)
        response = {"status": self.status, "symbol": payload["symbol"], "side": side,
                    "type": "MARKET", "clientOrderId": payload["newClientOrderId"],
                    "origQty": str(payload.get("quantity", 0)),
                    "origQuoteOrderQty": str(payload.get("quoteOrderQty", 0)),
                    "executedQty": "0" if self.status == "REJECTED" else "1",
                    "cummulativeQuoteQty": "100",
                    "fills": [{"price": "100", "qty": "1", "commission": "0", "commissionAsset": "USDT"}]}
        if self.mutation:
            self.mutation(response)
        return response

    def order_market_buy(self, **payload):
        return self._submit("BUY", payload)

    def order_market_sell(self, **payload):
        return self._submit("SELL", payload)


class DirectAssociationTests(unittest.TestCase):
    def make_runtime(self, client):
        store = Store()
        runtime = ExecutionRuntime(dry_run=False, client=client, state_loader=store.load, state_writer=store.write)
        runtime.state_owner_claim = lambda _owner: True
        runtime.state_owner_release = lambda _owner: True
        acquire_runtime_state_owner(runtime)
        return runtime, store

    @patch("runtime_support._rate_limit_pause")
    @patch("runtime_support._notification_order_identity")
    def test_direct_response_binding_and_ack(self, _notification, _pause):
        cases = [
            ("FILLED", lambda r: r.update(clientOrderId="different"), False),
            ("REJECTED", lambda r: r.update(clientOrderId="different"), False),
            ("FILLED", lambda r: r.update(symbol="BTCUSDT"), False),
            ("FILLED", lambda r: r.update(side="SELL"), False),
            ("FILLED", lambda r: r.update(type="LIMIT"), False),
            ("FILLED", lambda r: r.update(origQty="2"), False),
            ("FILLED", lambda r: r.pop("origQty"), False),
            ("FILLED", lambda r: [r.pop(k, None) for k in ("status", "side", "type", "origQty", "origQuoteOrderQty", "executedQty", "cummulativeQuoteQty", "fills")], False),
            ("FILLED", None, True),
            ("REJECTED", None, True),
        ]
        for status, mutation, matches in cases:
            with self.subTest(status=status, mutation=mutation):
                client = Client(status, mutation)
                runtime, store = self.make_runtime(client)
                call = lambda: runtime_call_client(runtime, build_execution_report(runtime),
                    method_name="order_market_buy", payload={"symbol": "ETHUSDT", "quantity": 1}, effect_type="order_buy", max_retries=0)
                if not matches:
                    with self.assertRaises(OrderReconciliationError): call()
                    self.assertEqual(store.data["order_submission"]["state"], "SUBMISSION_UNKNOWN")
                    self.assertFalse(release_runtime_state_owner(runtime))
                elif status == "FILLED":
                    call()
                    self.assertEqual(store.data["order_submission"]["state"], "FILLED_ACCOUNTING_PENDING")
                else:
                    with self.assertRaises(ClientCallError): call()
                    self.assertEqual(store.data["order_submission"]["state"], "TERMINAL")
                self.assertEqual(len(client.submits), 1)

    @patch("runtime_support._rate_limit_pause")
    @patch("runtime_support._notification_order_identity")
    def test_conflict_stops_actual_btc_buy_consumer(self, _notification, _pause):
        client = Client(mutation=lambda r: r.update(clientOrderId="different"))
        runtime, store = self.make_runtime(client)
        report = build_execution_report(runtime)
        state, balances, writes = {}, {"BTCUSDT": 0.0}, []
        with self.assertRaises(OrderReconciliationError):
            execute_btc_dca_cycle(runtime, report, state, balances, {"BTCUSDT": 100}, 500,
                500, 100, 0, {"ahr999": 0.5, "zscore": 0, "sell_trigger": 3},
                0.5, 100, "today", [], append_log_fn=lambda *a: None,
                translate_fn=lambda key, **kw: key, format_qty_fn=lambda *a: 1.0,
                ensure_asset_available_fn=lambda *a: True,
                runtime_call_client_fn=lambda rt, rep, **kw: runtime_call_client(rt, rep, max_retries=0, **kw),
                next_order_id_fn=lambda *a: "test", runtime_notify_fn=lambda *a: None,
                runtime_set_trade_state_fn=lambda *a, **kw: writes.append(True))
        self.assertEqual(state, {})
        self.assertEqual(balances, {"BTCUSDT": 0.0})
        self.assertEqual(writes, [])
        self.assertEqual(store.data["order_submission"]["state"], "SUBMISSION_UNKNOWN")
        self.assertFalse(release_runtime_state_owner(runtime))
        self.assertEqual(len(client.submits), 1)

    @patch("runtime_support._rate_limit_pause")
    @patch("runtime_support._notification_order_identity")
    def test_quote_order_binding(self, _notification, _pause):
        for mismatch in (False, True):
            client = Client(mutation=(lambda r: r.update(origQuoteOrderQty="101")) if mismatch else None)
            runtime, store = self.make_runtime(client)
            call = lambda: runtime_call_client(runtime, build_execution_report(runtime),
                method_name="order_market_buy", payload={"symbol": "BNBUSDT", "quoteOrderQty": 100},
                effect_type="order_buy", max_retries=0)
            if mismatch:
                with self.assertRaises(OrderReconciliationError): call()
                self.assertEqual(store.data["order_submission"]["state"], "SUBMISSION_UNKNOWN")
                self.assertFalse(release_runtime_state_owner(runtime))
            else:
                call()
                self.assertEqual(store.data["order_submission"]["state"], "FILLED_ACCOUNTING_PENDING")
            self.assertEqual(len(client.submits), 1)

    def test_non_market_method_is_rejected_before_submit(self):
        client = Client()
        runtime, store = self.make_runtime(client)
        with self.assertRaises(StatePersistenceError):
            runtime_call_client(runtime, build_execution_report(runtime), method_name="order_limit_buy",
                payload={"symbol": "ETHUSDT", "quantity": 1}, effect_type="order_buy", max_retries=0)
        self.assertEqual(client.submits, [])
        self.assertNotEqual(store.data["order_submission"]["state"], "SUBMISSION_UNKNOWN")

    @patch("runtime_support._rate_limit_pause")
    @patch("runtime_support._notification_order_identity")
    def test_conflict_stops_actual_trend_buy_and_sell_consumers(self, _notification, _pause):
        for side in ("BUY", "SELL"):
            with self.subTest(side=side):
                client = Client(mutation=lambda r: r.update(clientOrderId="different"))
                runtime, store = self.make_runtime(client)
                report = build_execution_report(runtime)
                state, balances = {}, {"ETHUSDT": 1.0}
                calls = []
                kwargs = dict(should_skip_duplicate_trend_action_fn=lambda *a: False,
                    append_log_fn=lambda *a: None, translate_fn=lambda key, **kw: key,
                    format_qty_fn=lambda *a: 1.0, ensure_asset_available_fn=lambda *a: True,
                    runtime_call_client_fn=lambda rt, rep, **kw: runtime_call_client(rt, rep, max_retries=0, **kw),
                    next_order_id_fn=lambda *a: "test", set_symbol_trade_state_fn=lambda *a: calls.append("state"),
                    record_trend_action_fn=lambda *a: calls.append("action"),
                    runtime_set_trade_state_fn=lambda *a, **kw: calls.append("write"), runtime_notify_fn=lambda *a: None)
                with self.assertRaises(OrderReconciliationError):
                    if side == "BUY":
                        execute_trend_buys(runtime, report, state, {"ETHUSDT": {"weight": 1, "relative_score": 1}},
                            ["ETHUSDT"], {"ETHUSDT": 100}, {"ETHUSDT": 100}, balances, 500, [], "today", **kwargs)
                    else:
                        execute_trend_sells(runtime, report, state, {"ETHUSDT": {"base_asset": "ETH"}},
                            {"ETHUSDT": "signal"}, {"ETHUSDT": 100}, balances, 500, [], "today", **kwargs)
                self.assertEqual(balances, {"ETHUSDT": 1.0})
                self.assertEqual(state, {})
                self.assertEqual(calls, [])
                self.assertEqual(store.data["order_submission"]["state"], "SUBMISSION_UNKNOWN")
                self.assertFalse(release_runtime_state_owner(runtime))
                self.assertEqual(len(client.submits), 1)
