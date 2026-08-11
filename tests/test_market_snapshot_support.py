import unittest
from types import SimpleNamespace
from unittest.mock import patch

from market_snapshot_support import capture_market_snapshot, execute_bnb_fuel_top_up


class FakeClient:
    def __init__(self, prices):
        self.prices = dict(prices)

    def get_avg_price(self, *, symbol):
        return {"price": str(self.prices[symbol])}


class MarketSnapshotSupportTests(unittest.TestCase):
    def test_capture_market_snapshot_never_orders_bnb_before_strategy_authority(self):
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
            min_bnb_value=20.0,
            buy_bnb_amount=30.0,
            get_total_balance_fn=lambda client, asset, log_buffer=None: balance_map[asset],
            ensure_asset_available_fn=lambda runtime, report, asset, amount, log_buffer: True,
            runtime_call_client_fn=lambda runtime, report, **kwargs: side_effect_calls.append(kwargs),
            runtime_notify_fn=lambda runtime, report, message: self.fail(f"unexpected notification: {message}"),
            append_log_fn=lambda buffer, message: buffer.append(message),
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
        self.assertNotIn("BNB top-up completed", "".join(log_buffer))

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
                min_bnb_value=10.0,
                buy_bnb_amount=15.0,
                get_total_balance_fn=lambda client, asset, log_buffer=None: {
                    "USDT": 100.0,
                    "BNB": 1.0,
                    "ETH": 0.5,
                    "BTC": 0.01,
                }[asset],
                ensure_asset_available_fn=lambda runtime, report, asset, amount, log_buffer: True,
                runtime_call_client_fn=lambda runtime, report, **kwargs: None,
                runtime_notify_fn=lambda runtime, report, message: None,
                append_log_fn=lambda buffer, message: buffer.append(message),
                resolve_btc_snapshot_fn=lambda runtime, btc_price, log_buffer: None,
                resolve_trend_indicators_fn=lambda runtime: {},
            )

    def test_bnb_top_up_requires_validated_authority(self):
        runtime = SimpleNamespace(client=object())
        report = {"buy_sell_intents": []}
        calls = []
        snapshot = {
            "u_total": 200.0,
            "fuel_val": 15.0,
            "bnb_total": 0.05,
            "bnb_price": 300.0,
            "bnb_top_up_required": True,
            "bnb_top_up_amount": 30.0,
            "bnb_fuel_symbol": "BNBUSDT",
        }
        kwargs = {
            "ensure_asset_available_fn": lambda *_args, **_kwargs: True,
            "runtime_call_client_fn": lambda _runtime, _report, **payload: calls.append(payload),
            "runtime_notify_fn": lambda *_args, **_kwargs: None,
            "append_log_fn": lambda *_args, **_kwargs: None,
        }

        no_authority = execute_bnb_fuel_top_up(
            runtime,
            report,
            snapshot,
            [],
            execution_authority=None,
            **kwargs,
        )
        self.assertEqual(no_authority, (200.0, 15.0))
        self.assertEqual(calls, [])

        with patch("market_snapshot_support.is_execution_authority_valid", return_value=True):
            authorized = execute_bnb_fuel_top_up(
                runtime,
                report,
                snapshot,
                [],
                execution_authority=object(),
                **kwargs,
            )

        self.assertAlmostEqual(authorized[0], 170.0)
        self.assertAlmostEqual(authorized[1], 44.85, places=2)
        self.assertEqual(calls[0]["method_name"], "order_market_buy")


if __name__ == "__main__":
    unittest.main()
