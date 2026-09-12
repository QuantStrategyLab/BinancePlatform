import unittest
from types import SimpleNamespace

from infra.binance_runtime import (
    ensure_runtime_client,
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



if __name__ == "__main__":
    unittest.main()
