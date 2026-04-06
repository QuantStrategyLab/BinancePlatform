import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def install_test_stubs():
    if "binance" not in sys.modules:
        binance_module = types.ModuleType("binance")
        client_module = types.ModuleType("binance.client")
        exceptions_module = types.ModuleType("binance.exceptions")

        class Client:
            KLINE_INTERVAL_1DAY = "1d"

            def __init__(self, *args, **kwargs):
                pass

            def ping(self):
                return None

        class BinanceAPIException(Exception):
            pass

        client_module.Client = Client
        exceptions_module.BinanceAPIException = BinanceAPIException
        binance_module.client = client_module
        binance_module.exceptions = exceptions_module
        sys.modules["binance"] = binance_module
        sys.modules["binance.client"] = client_module
        sys.modules["binance.exceptions"] = exceptions_module

    if "requests" not in sys.modules:
        requests_module = types.ModuleType("requests")
        requests_module.post = lambda *args, **kwargs: None
        sys.modules["requests"] = requests_module

    if "google" not in sys.modules:
        sys.modules["google"] = types.ModuleType("google")
    if "google.cloud" not in sys.modules:
        cloud_module = types.ModuleType("google.cloud")
        sys.modules["google.cloud"] = cloud_module
        sys.modules["google"].cloud = cloud_module
    if "google.cloud.firestore" not in sys.modules:
        firestore_module = types.ModuleType("google.cloud.firestore")

        class FirestoreClient:
            def collection(self, *args, **kwargs):
                return self

            def document(self, *args, **kwargs):
                return self

            def get(self):
                raise RuntimeError("stub Firestore client should be patched in unit tests")

            def set(self, *args, **kwargs):
                return None

        firestore_module.Client = FirestoreClient
        sys.modules["google.cloud.firestore"] = firestore_module
        sys.modules["google.cloud"].firestore = firestore_module


install_test_stubs()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
PLATFORM_KIT_SRC = PROJECT_ROOT.parent / "QuantPlatformKit" / "src"
CRYPTO_STRATEGIES_SRC = PROJECT_ROOT.parent / "CryptoStrategies" / "src"
for path in (PLATFORM_KIT_SRC, CRYPTO_STRATEGIES_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import main
from degraded_mode_support import format_trend_pool_source_logs
from market_snapshot_support import capture_market_snapshot


class FakeClient:
    def __init__(self, prices):
        self.prices = dict(prices)

    def get_avg_price(self, *, symbol):
        return {"price": str(self.prices[symbol])}


class NotifyI18nTests(unittest.TestCase):
    def test_periodic_btc_status_report_uses_chinese_when_notify_lang_is_zh(self):
        state = {}
        messages = []

        with patch.dict(os.environ, {"NOTIFY_LANG": "zh"}, clear=False):
            main.maybe_send_periodic_btc_status_report(
                state,
                tg_token="",
                tg_chat_id="",
                now_utc=SimpleNamespace(
                    hour=0,
                    strftime=lambda fmt: "2026-03-27 00:00" if fmt == "%Y-%m-%d %H:%M" else "2026032700",
                ),
                interval_hours=24,
                total_equity=12500.0,
                trend_holdings_equity=3200.0,
                trend_daily_pnl=0.0125,
                btc_price=87000.0,
                btc_snapshot={
                    "ahr999": 0.7,
                    "zscore": 1.2,
                    "sell_trigger": 3.0,
                    "regime_on": True,
                },
                btc_target_ratio=0.285,
                notifier_fn=messages.append,
            )

        self.assertEqual(len(messages), 1)
        self.assertIn("💓 【策略心跳】", messages[0])
        self.assertIn("🕐 UTC 时间", messages[0])
        self.assertIn("💡 建议", messages[0])
        self.assertIn("AHR999 偏低", messages[0])

    def test_trend_pool_source_logs_use_chinese_when_notify_lang_is_zh(self):
        with patch.dict(os.environ, {"NOTIFY_LANG": "zh"}, clear=False):
            log_lines = format_trend_pool_source_logs(
                {
                    "source_kind": "last_known_good",
                    "mode": "core_major",
                    "version": "2026-03-10-core_major",
                    "as_of_date": "2026-03-10",
                    "source_project": "crypto-leader-rotation",
                    "messages": ["payload stale", "using cached pool"],
                    "degraded": True,
                },
                allow_new_trend_entries=False,
            )

        self.assertIn("趋势池来源", log_lines[0])
        self.assertTrue(any("暂停新的趋势买入" in line for line in log_lines))

    def test_capture_market_snapshot_uses_chinese_bnb_log_when_notify_lang_is_zh(self):
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
        log_buffer = []
        side_effect_calls = []

        with patch.dict(os.environ, {"NOTIFY_LANG": "zh"}, clear=False):
            capture_market_snapshot(
                runtime,
                report,
                {"ETHUSDT": {"base_asset": "ETH"}},
                log_buffer,
                min_bnb_value=20.0,
                buy_bnb_amount=30.0,
                get_total_balance_fn=lambda client, asset, log_buffer=None: {
                    "USDT": 200.0,
                    "BNB": 0.05,
                    "ETH": 1.5,
                    "BTC": 0.01,
                }[asset],
                ensure_asset_available_fn=lambda runtime, report, asset, amount, log_buffer: True,
                runtime_call_client_fn=lambda runtime, report, **kwargs: side_effect_calls.append(kwargs),
                runtime_notify_fn=lambda runtime, report, message: self.fail(f"unexpected notification: {message}"),
                append_log_fn=lambda buffer, message: buffer.append(message),
                resolve_btc_snapshot_fn=lambda runtime, btc_price, log_buffer: {"ahr999": 0.8, "zscore": 1.2},
                resolve_trend_indicators_fn=lambda runtime: {"ETHUSDT": {"score": 1.0}},
            )

        self.assertEqual(side_effect_calls[0]["method_name"], "order_market_buy")
        self.assertIn("BNB 补仓已完成", "".join(log_buffer))


if __name__ == "__main__":
    unittest.main()
