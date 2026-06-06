import unittest
from types import SimpleNamespace

from infra.binance_runtime import (
    ensure_asset_available_runtime,
    ensure_runtime_client,
    manage_usdt_earn_buffer_runtime,
    resolve_runtime_btc_snapshot,
    resolve_runtime_trend_indicators,
)


class BinanceRuntimeInfraTests(unittest.TestCase):
    def test_resolve_runtime_btc_snapshot_prefers_injected_snapshot(self):
        runtime = SimpleNamespace(client=object(), btc_market_snapshot={"ahr999": 0.8})

        snapshot = resolve_runtime_btc_snapshot(
            runtime,
            50_000.0,
            [],
            fetch_btc_market_snapshot_fn=lambda *_args, **_kwargs: self.fail("should not fetch"),
        )

        self.assertEqual(snapshot, {"ahr999": 0.8})
        self.assertIsNot(snapshot, runtime.btc_market_snapshot)

    def test_resolve_runtime_btc_snapshot_retries_before_success(self):
        runtime = SimpleNamespace(client=object(), btc_market_snapshot=None)
        log_buffer = []
        observed = {"calls": 0, "sleeps": []}

        def fetch_snapshot(_client, _btc_price, log_buffer=None):
            observed["calls"] += 1
            if observed["calls"] < 3:
                return None
            return {"ahr999": 0.8}

        snapshot = resolve_runtime_btc_snapshot(
            runtime,
            50_000.0,
            log_buffer,
            fetch_btc_market_snapshot_fn=fetch_snapshot,
            max_attempts=3,
            retry_delays=(1, 2),
            sleep_fn=lambda seconds: observed["sleeps"].append(seconds),
            append_log_fn=lambda buffer, message: buffer.append(message),
            retry_log_message_fn=lambda attempt, max_attempts, delay_seconds: (
                f"retry {attempt}/{max_attempts} after {delay_seconds}s"
            ),
        )

        self.assertEqual(snapshot, {"ahr999": 0.8})
        self.assertEqual(observed["calls"], 3)
        self.assertEqual(observed["sleeps"], [1, 2])
        self.assertEqual(log_buffer, ["retry 2/3 after 1s", "retry 3/3 after 2s"])

    def test_resolve_runtime_btc_snapshot_returns_none_after_retries(self):
        runtime = SimpleNamespace(client=object(), btc_market_snapshot=None)
        observed = {"calls": 0, "sleeps": []}

        def fetch_missing_snapshot(*_args, **_kwargs):
            observed["calls"] += 1
            return None

        snapshot = resolve_runtime_btc_snapshot(
            runtime,
            50_000.0,
            [],
            fetch_btc_market_snapshot_fn=fetch_missing_snapshot,
            max_attempts=2,
            retry_delays=(1,),
            sleep_fn=lambda seconds: observed["sleeps"].append(seconds),
        )

        self.assertIsNone(snapshot)
        self.assertEqual(observed["calls"], 2)
        self.assertEqual(observed["sleeps"], [1])

    def test_resolve_runtime_trend_indicators_fetches_when_not_injected(self):
        runtime = SimpleNamespace(client=object(), trend_indicator_snapshots=None)
        observed_symbols = []

        indicators = resolve_runtime_trend_indicators(
            runtime,
            ["ETHUSDT", "SOLUSDT"],
            fetch_daily_indicators_fn=lambda _client, symbol: observed_symbols.append(symbol) or {"symbol": symbol},
        )

        self.assertEqual(observed_symbols, ["ETHUSDT", "SOLUSDT"])
        self.assertEqual(indicators["ETHUSDT"]["symbol"], "ETHUSDT")
        self.assertEqual(indicators["SOLUSDT"]["symbol"], "SOLUSDT")

    def test_ensure_asset_available_runtime_redeems_from_earn_when_spot_short(self):
        class Client:
            def get_asset_balance(self, *, asset):
                return {"free": "2.0"}

            def get_simple_earn_flexible_product_position(self, *, asset):
                return {"rows": [{"productId": "earn-1", "totalAmount": "5.0"}]}

        runtime = SimpleNamespace(client=Client(), dry_run=False)
        report = {"redemption_subscription_intents": []}
        observed = {"calls": [], "logs": [], "notifications": [], "sleep": []}

        available = ensure_asset_available_runtime(
            runtime,
            report,
            "ETH",
            3.0,
            [],
            runtime_call_client_fn=lambda _runtime, _report, method_name, payload, effect_type: observed["calls"].append(
                (method_name, payload, effect_type)
            ),
            append_log_fn=lambda _buffer, message: observed["logs"].append(message),
            runtime_notify_fn=lambda _runtime, _report, text: observed["notifications"].append(text),
            translate_fn=lambda key, **kwargs: f"{key}:{kwargs}" if kwargs else key,
            sleep_fn=lambda seconds: observed["sleep"].append(seconds),
        )

        self.assertTrue(available)
        self.assertEqual(report["redemption_subscription_intents"][0]["action"], "redeem")
        self.assertEqual(observed["calls"][0][0], "redeem_simple_earn_flexible_product")
        self.assertEqual(observed["sleep"], [3])
        self.assertEqual(observed["notifications"], [])
        self.assertEqual(len(observed["logs"]), 1)

    def test_manage_usdt_earn_buffer_runtime_subscribes_excess_spot(self):
        class Client:
            def get_asset_balance(self, *, asset):
                return {"free": "150.0"}

            def get_simple_earn_flexible_product_list(self, *, asset):
                return {"rows": [{"productId": "earn-1"}]}

        runtime = SimpleNamespace(client=Client())
        report = {"redemption_subscription_intents": []}
        observed = {"calls": [], "logs": []}

        manage_usdt_earn_buffer_runtime(
            runtime,
            report,
            100.0,
            [],
            runtime_call_client_fn=lambda _runtime, _report, method_name, payload, effect_type: observed["calls"].append(
                (method_name, payload, effect_type)
            ),
            append_log_fn=lambda _buffer, message: observed["logs"].append(message),
            translate_fn=lambda key, **kwargs: f"{key}:{kwargs}" if kwargs else key,
        )

        self.assertEqual(report["redemption_subscription_intents"][0]["action"], "subscribe")
        self.assertEqual(report["redemption_subscription_intents"][0]["amount"], 50.0)
        self.assertEqual(observed["calls"][0][0], "subscribe_simple_earn_flexible_product")
        self.assertEqual(len(observed["logs"]), 1)

    def test_ensure_runtime_client_marks_report_aborted_after_retries(self):
        runtime = SimpleNamespace(client=None, api_key="key", api_secret="secret")
        report = {"status": "ok"}
        observed = {"sleeps": [], "errors": [], "notifications": []}

        connected = ensure_runtime_client(
            runtime,
            report,
            connect_client_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
            append_report_error_fn=lambda report, message, stage: observed["errors"].append((stage, message)),
            runtime_notify_fn=lambda _runtime, _report, text: observed["notifications"].append(text),
            translate_fn=lambda key, **kwargs: key,
            sleep_fn=lambda seconds: observed["sleeps"].append(seconds),
        )

        self.assertFalse(connected)
        self.assertIsNone(runtime.client)
        self.assertEqual(report["status"], "aborted")
        self.assertEqual(observed["sleeps"], [3, 3])
        self.assertEqual(observed["errors"][0][0], "client")
        self.assertEqual(len(observed["notifications"]), 1)


if __name__ == "__main__":
    unittest.main()
