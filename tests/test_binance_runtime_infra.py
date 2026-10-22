import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call

from infra.binance_runtime import (
    ensure_asset_available_runtime,
    ensure_runtime_client,
    manage_usdt_earn_buffer_runtime,
    resolve_runtime_btc_snapshot,
    resolve_runtime_trend_indicators,
)
from runtime_support import ExecutionIntegrityError


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

    def test_ensure_asset_available_runtime_checks_only_spot_free(self):
        for dry_run in (False, True):
            for spot_free, expected in (("0", False), ("2", False), ("3", True), ("4", True)):
                with self.subTest(dry_run=dry_run, spot_free=spot_free):
                    client = Mock()
                    client.get_asset_balance.return_value = {"free": spot_free, "locked": "100"}
                    client.get_simple_earn_flexible_product_position.return_value = {
                        "rows": [{"productId": "earn-1", "totalAmount": "90",
                                  "canRedeem": True, "collateralAmount": "0"}],
                    }
                    report = {"redemption_subscription_intents": []}
                    effects = Mock()

                    available = ensure_asset_available_runtime(
                        SimpleNamespace(client=client, dry_run=dry_run), report, "ETH", 3.0, [],
                        runtime_call_client_fn=effects.runtime_call_client,
                        append_log_fn=effects.append_log,
                        runtime_notify_fn=effects.runtime_notify,
                        translate_fn=effects.translate,
                        sleep_fn=effects.sleep,
                    )

                    self.assertIs(available, expected)
                    self.assertEqual(client.mock_calls, [call.get_asset_balance(asset="ETH")])
                    client.get_simple_earn_flexible_product_position.assert_not_called()
                    client.redeem_simple_earn_flexible_product.assert_not_called()
                    client.subscribe_simple_earn_flexible_product.assert_not_called()
                    self.assertEqual(report, {"redemption_subscription_intents": []})
                    self.assertEqual(effects.mock_calls, [])

    def test_manage_usdt_earn_buffer_runtime_is_noop(self):
        for dry_run in (False, True):
            for spot_free in ("0", "50", "100", "150"):
                for override in (None, 0.0, 150.0):
                    with self.subTest(dry_run=dry_run, spot_free=spot_free, override=override):
                        client = Mock()
                        client.get_asset_balance.return_value = {"free": spot_free}
                        client.get_simple_earn_flexible_product_list.return_value = {
                            "rows": [{"productId": "earn-1"}],
                        }
                        client.get_simple_earn_flexible_product_position.return_value = {
                            "rows": [{"productId": "earn-1", "totalAmount": "90",
                                      "canRedeem": True, "collateralAmount": "0"}],
                        }
                        report = {"redemption_subscription_intents": []}
                        log_buffer = []
                        effects = Mock()

                        result = manage_usdt_earn_buffer_runtime(
                            SimpleNamespace(client=client, dry_run=dry_run), report, 100.0, log_buffer,
                            runtime_call_client_fn=effects.runtime_call_client,
                            append_log_fn=effects.append_log,
                            translate_fn=effects.translate,
                            spot_free_override=override,
                        )

                        self.assertIsNone(result)
                        self.assertEqual(client.mock_calls, [])
                        client.redeem_simple_earn_flexible_product.assert_not_called()
                        client.subscribe_simple_earn_flexible_product.assert_not_called()
                        self.assertEqual(effects.mock_calls, [])
                        self.assertEqual(report, {"redemption_subscription_intents": []})
                        self.assertEqual(log_buffer, [])

    def test_asset_balance_integrity_errors_are_not_swallowed(self):
        failure = ExecutionIntegrityError("order_reconciliation_uncertain")
        observed = {"notifications": [], "logs": [], "sleeps": []}

        class Client:
            def get_asset_balance(self, *, asset):
                raise failure

        with self.assertRaises(ExecutionIntegrityError) as raised:
            ensure_asset_available_runtime(
                SimpleNamespace(client=Client(), dry_run=False),
                {"redemption_subscription_intents": []},
                "ETH",
                3.0,
                [],
                runtime_call_client_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
                append_log_fn=lambda _buffer, message: observed["logs"].append(message),
                runtime_notify_fn=lambda _runtime, _report, text: observed["notifications"].append(text),
                translate_fn=lambda key, **_kwargs: key,
                sleep_fn=lambda seconds: observed["sleeps"].append(seconds),
            )

        self.assertIs(raised.exception, failure)
        self.assertEqual(observed, {"notifications": [], "logs": [], "sleeps": []})

    def test_ensure_runtime_client_marks_report_aborted_after_retries(self):
        runtime = SimpleNamespace(client=None, api_key="key", api_secret="secret")
        report = {"status": "ok"}
        observed = {"sleeps": [], "errors": [], "notifications": []}

        connected = ensure_runtime_client(
            runtime,
            report,
            connect_client_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("SENSITIVE_PROVIDER_SENTINEL")
            ),
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
        self.assertNotIn("SENSITIVE_PROVIDER_SENTINEL", str(report) + str(observed))

    def test_asset_errors_use_safe_messages(self):
        sentinel = "SENSITIVE_PROVIDER_SENTINEL"
        class Client:
            def get_asset_balance(self, **kwargs):
                raise RuntimeError(sentinel)
        runtime = SimpleNamespace(client=Client(), dry_run=False)
        report = {"redemption_subscription_intents": []}
        with self.assertRaisesRegex(ExecutionIntegrityError, "asset_availability_failed") as first:
            ensure_asset_available_runtime(runtime, report, "ETH", 3.0, [], runtime_call_client_fn=lambda **kwargs: None,
                append_log_fn=lambda *args: None, runtime_notify_fn=lambda *args: None,
                translate_fn=lambda key, **kwargs: key, sleep_fn=lambda *args: None)
        self.assertNotIn(sentinel, str(first.exception) + str(report))


if __name__ == "__main__":
    unittest.main()


def test_asset_availability_rejects_invalid_spot_or_required_amount():
    import pytest
    for spot_free, required in (('NaN', 3), ('Infinity', 3), ('-1', 3),
                                ('2', float('nan')), ('2', float('inf')), ('2', 0), ('2', -1)):
        client = Mock()
        client.get_asset_balance.return_value = {'free': spot_free}
        runtime = SimpleNamespace(client=client, dry_run=False)
        effects = Mock()
        report = {'redemption_subscription_intents': []}
        with pytest.raises(ExecutionIntegrityError, match='asset_availability_failed'):
            ensure_asset_available_runtime(runtime, report, 'ETH', required, [],
                runtime_call_client_fn=effects.runtime_call_client, append_log_fn=effects.append_log,
                runtime_notify_fn=effects.runtime_notify, translate_fn=effects.translate, sleep_fn=effects.sleep)
        assert client.mock_calls == [call.get_asset_balance(asset='ETH')]
        assert effects.mock_calls == []
        assert report == {'redemption_subscription_intents': []}
