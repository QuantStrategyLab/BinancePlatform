import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from market_snapshot_support import capture_market_snapshot, top_up_bnb_fuel
from runtime_support import ExecutionRuntime, OrderReconciliationError, build_execution_report, runtime_call_client


class FakeClient:
    def __init__(self, prices):
        self.prices = dict(prices)

    def get_avg_price(self, *, symbol):
        return {"price": str(self.prices[symbol])}


class MarketSnapshotSupportTests(unittest.TestCase):
    @staticmethod
    def _top_up_with_bnb_failure(failure, notifications):
        runtime = SimpleNamespace()
        return top_up_bnb_fuel(
            runtime,
            {"buy_sell_intents": []},
            200.0,
            3.0,
            [],
            min_bnb_value=20.0,
            buy_bnb_amount=30.0,
            ensure_asset_available_fn=lambda *_args: True,
            runtime_call_client_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
            runtime_notify_fn=lambda _runtime, _report, message: notifications.append(message),
            append_log_fn=lambda *_args: None,
        )

    def test_capture_market_snapshot_is_read_only_and_collects_balances(self):
        runtime = SimpleNamespace(
            client=FakeClient(
                {
                    "BNBUSDT": 300.0,
                    "ETHUSDT": 2500.0,
                    "SOLUSDT": 150.0,
                    "BTCUSDT": 60000.0,
                }
            )
        )
        report = {"buy_sell_intents": []}
        log_buffer = []
        side_effect_calls = []
        balance_map = {
            "USDT": 200.0,
            "BNB": 0.05,
            "ETH": 1.5,
            "SOL": 2.0,
            "BTC": 0.01,
        }

        snapshot = capture_market_snapshot(
            runtime,
            report,
            {
                "ETHUSDT": {"base_asset": "ETH"},
                "SOLUSDT": {"base_asset": "SOL"},
            },
            log_buffer,
            get_total_balance_fn=lambda client, asset, log_buffer=None: balance_map[asset],
            resolve_btc_snapshot_fn=lambda runtime, btc_price, log_buffer: {"ahr999": 0.8, "zscore": 1.2},
            resolve_trend_indicators_fn=lambda runtime: {"ETHUSDT": {"score": 1.0}, "SOLUSDT": {"score": 0.5}},
        )

        self.assertEqual(report["buy_sell_intents"], [])
        self.assertEqual(side_effect_calls, [])
        self.assertAlmostEqual(snapshot["u_total"], 200.0)
        self.assertAlmostEqual(snapshot["fuel_val"], 15.0, places=2)
        self.assertEqual(snapshot["prices"]["ETHUSDT"], 2500.0)
        self.assertEqual(snapshot["balances"]["SOLUSDT"], 2.0)
        self.assertEqual(snapshot["balances"]["BTCUSDT"], 0.01)
        self.assertEqual(snapshot["trend_indicators"]["ETHUSDT"]["score"], 1.0)
        self.assertEqual(log_buffer, [])

    def test_top_up_bnb_fuel_stops_for_a_new_snapshot_after_a_filled_response(self):
        class FilledOrderClient:
            def __init__(self):
                self.calls = []

            def order_market_buy(self, **payload):
                self.calls.append(payload)
                return {"status": "FILLED", "cummulativeQuoteQty": "30"}

        client = FilledOrderClient()
        runtime = ExecutionRuntime(
            dry_run=False,
            run_id="bnb-fuel-filled",
            client=client,
            state_loader=lambda *, normalize=False: {"order_submission": {"state": "RESERVED"}},
            state_writer=lambda _state: True,
        )
        report = build_execution_report(runtime)

        u_total, fuel_val, continue_execution = top_up_bnb_fuel(
            runtime,
            report,
            200.0,
            15.0,
            [],
            min_bnb_value=20.0,
            buy_bnb_amount=30.0,
            ensure_asset_available_fn=lambda *_args: True,
            runtime_call_client_fn=lambda *_args, **_kwargs: {"status": "suppressed"},
            runtime_notify_fn=lambda *_args: None,
            append_log_fn=lambda *_args: None,
        )

        self.assertEqual((u_total, fuel_val, continue_execution), (200.0, 15.0, "unconfirmed"))

        log_buffer = []
        with patch.dict(os.environ, {"NOTIFY_LANG": "zh"}, clear=False):
            u_total, fuel_val, continue_execution = top_up_bnb_fuel(
                runtime,
                build_execution_report(runtime),
                200.0,
                15.0,
                log_buffer,
                min_bnb_value=20.0,
                buy_bnb_amount=30.0,
                ensure_asset_available_fn=lambda *_args: True,
                runtime_call_client_fn=runtime_call_client,
                runtime_notify_fn=lambda *_args: None,
                append_log_fn=lambda buffer, message: buffer.append(message),
            )

        self.assertEqual((u_total, fuel_val, continue_execution), (200.0, 15.0, "filled_pending_snapshot"))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["symbol"], "BNBUSDT")
        self.assertIn("BNB 补仓已完成", "".join(log_buffer))

    def test_capture_market_snapshot_raises_when_btc_snapshot_is_missing(self):
        runtime = SimpleNamespace(
            client=FakeClient(
                {
                    "BNBUSDT": 300.0,
                    "ETHUSDT": 2500.0,
                    "BTCUSDT": 60000.0,
                }
            )
        )
        report = {"buy_sell_intents": []}

        with self.assertRaisesRegex(RuntimeError, "BTC indicators insufficient"):
            capture_market_snapshot(
                runtime,
                report,
                {"ETHUSDT": {"base_asset": "ETH"}},
                [],
                get_total_balance_fn=lambda client, asset, log_buffer=None: {
                    "USDT": 100.0,
                    "BNB": 1.0,
                    "ETH": 0.5,
                    "BTC": 0.01,
                }[asset],
                resolve_btc_snapshot_fn=lambda runtime, btc_price, log_buffer: None,
                resolve_trend_indicators_fn=lambda runtime: {},
            )

    def test_bnb_top_up_integrity_failure_aborts_cycle(self):
        notifications = []

        with self.assertRaises(OrderReconciliationError):
            self._top_up_with_bnb_failure(
                OrderReconciliationError("order_reconciliation_uncertain"),
                notifications,
            )

        self.assertEqual(notifications, [])

    def test_bnb_top_up_business_failure_notifies_without_exception_text(self):
        notifications = []

        u_total, fuel_val, fuel_status = self._top_up_with_bnb_failure(
            RuntimeError("SENSITIVE_PROVIDER_SENTINEL"),
            notifications,
        )

        self.assertEqual((u_total, fuel_val, fuel_status), (200.0, 3.0, "unconfirmed"))
        self.assertEqual(len(notifications), 1)
        self.assertIn("order_execution_failed", notifications[0])
        self.assertNotIn("SENSITIVE_PROVIDER_SENTINEL", notifications[0])


if __name__ == "__main__":
    unittest.main()
