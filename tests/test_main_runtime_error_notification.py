import os
import sys
import types
import unittest
from pathlib import Path
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
        firestore_module.Client = type("FirestoreClient", (), {})
        sys.modules["google.cloud.firestore"] = firestore_module
        sys.modules["google.cloud"].firestore = firestore_module

    if "quant_platform_kit.binance" not in sys.modules:
        qpk_binance_module = types.ModuleType("quant_platform_kit.binance")
        qpk_binance_module.connect_client = lambda *args, **kwargs: None
        qpk_binance_module.ensure_asset_available = lambda *args, **kwargs: False
        qpk_binance_module.fetch_btc_market_snapshot = lambda *args, **kwargs: {}
        qpk_binance_module.fetch_daily_indicators = lambda *args, **kwargs: {}
        qpk_binance_module.format_qty = lambda value, *args, **kwargs: str(value)
        qpk_binance_module.get_total_balance = lambda *args, **kwargs: 0.0
        qpk_binance_module.manage_usdt_earn_buffer = lambda *args, **kwargs: None
        sys.modules["quant_platform_kit.binance"] = qpk_binance_module

    if "strategy_registry" not in sys.modules:
        strategy_registry_module = types.ModuleType("strategy_registry")
        strategy_registry_module.BINANCE_PLATFORM = "binance"
        strategy_registry_module.DEFAULT_STRATEGY_PROFILE = "crypto_live_pool_rotation"
        strategy_registry_module.resolve_strategy_definition = lambda *_args, **_kwargs: types.SimpleNamespace(
            profile="crypto_live_pool_rotation",
            domain="crypto",
        )
        strategy_registry_module.resolve_strategy_metadata = lambda *_args, **_kwargs: types.SimpleNamespace(
            display_name="Crypto Live Pool Rotation",
        )
        sys.modules["strategy_registry"] = strategy_registry_module

    if "strategy_runtime" not in sys.modules:
        strategy_runtime_module = types.ModuleType("strategy_runtime")
        strategy_runtime_module.load_strategy_runtime = lambda *_args, **_kwargs: types.SimpleNamespace(
            trend_pool_size=10,
            default_local_artifact_path=Path("/tmp/live_pool.json"),
            local_artifact_candidates=(),
            artifact_contract={
                "max_age_days": 45,
                "acceptable_modes": (),
            },
        )
        sys.modules["strategy_runtime"] = strategy_runtime_module


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


class MainRuntimeErrorNotificationTests(unittest.TestCase):
    def test_main_wires_cli_entrypoint_with_runtime_builder_and_cycle_runner(self):
        observed = {}

        def fake_run_cli_entrypoint(**kwargs):
            observed.update(kwargs)
            return {"ok": True}

        with patch.object(main, "run_cli_entrypoint", fake_run_cli_entrypoint):
            result = main.main()

        self.assertEqual(result, {"ok": True})
        self.assertIs(observed["runtime_builder"], main.build_live_runtime)
        self.assertIs(observed["execute_cycle"], main.execute_cycle)
        self.assertIs(observed["output_printer"], print)
        self.assertIs(observed["exit_fn"], sys.exit)
        self.assertFalse(hasattr(main, "app"))

    def test_main_notifies_telegram_when_cli_entrypoint_fails_before_cycle(self):
        sentinel = "SENSITIVE_PROVIDER_SENTINEL"
        observed = {"messages": [], "printed": []}

        def fake_run_cli_entrypoint(**_kwargs):
            raise RuntimeError(sentinel)

        def fake_send_tg_msg(token, chat_id, text):
            observed["messages"].append((token, chat_id, text))

        with patch.dict(
            os.environ,
            {
                "TG_TOKEN": "token-1",
                "GLOBAL_TELEGRAM_CHAT_ID": "chat-1",
                "STRATEGY_PROFILE": "crypto_live_pool_rotation",
            },
            clear=False,
        ):
            with patch.object(main, "run_cli_entrypoint", fake_run_cli_entrypoint):
                with patch.object(main, "send_tg_msg", fake_send_tg_msg):
                    with patch("builtins.print", lambda *args, **_kwargs: observed["printed"].append(" ".join(map(str, args)))):
                        with patch.object(main.traceback, "print_exc") as print_exc:
                            with self.assertRaises(RuntimeError):
                                main.main()

        self.assertEqual(len(observed["messages"]), 1)
        self.assertEqual(observed["messages"][0][0], "token-1")
        self.assertEqual(observed["messages"][0][1], "chat-1")
        self.assertIn("Binance strategy run failed", observed["messages"][0][2])
        self.assertIn("runtime_setup_failed", observed["messages"][0][2])
        self.assertNotIn(sentinel, str(observed))
        print_exc.assert_not_called()

    def test_runtime_notification_delivery_failure_log_is_sanitized(self):
        sentinel = "SENSITIVE_PROVIDER_SENTINEL"
        observed = []

        with patch.dict(
            os.environ,
            {"TG_TOKEN": "token-1", "GLOBAL_TELEGRAM_CHAT_ID": "chat-1"},
            clear=False,
        ):
            with patch.object(
                main,
                "send_tg_msg",
                side_effect=RuntimeError(sentinel),
            ):
                with patch("builtins.print", lambda *args, **_kwargs: observed.append(" ".join(map(str, args)))):
                    self.assertFalse(main._notify_runtime_error(RuntimeError("outer")))

        self.assertNotIn(sentinel, str(observed))

    def test_main_helper_error_logs_are_sanitized(self):
        sentinel = "SENSITIVE_PROVIDER_SENTINEL"
        log_buffer = []

        def fail_balance_lookup(_client, _asset, **kwargs):
            kwargs["on_spot_error"](RuntimeError(sentinel))
            kwargs["on_earn_error"](RuntimeError(sentinel))
            raise kwargs["balance_error_cls"]("balance_lookup_failed")

        with patch.object(main, "qpk_get_total_balance", fail_balance_lookup):
            with self.assertRaises(Exception):
                main.get_total_balance(object(), "BTC", log_buffer=log_buffer)

        with patch.dict(os.environ, {"BTC_CYCLE_INDICATORS_PATH": "/tmp/sentinel.json"}):
            with patch.object(main.Path, "read_text", side_effect=RuntimeError(sentinel)):
                main.enrich_btc_snapshot_with_cycle_indicators({}, log_buffer)

        self.assertNotIn(sentinel, str(log_buffer))

    def test_earn_redeem_maintenance_errors_use_fixed_safe_reasons(self):
        sentinel = "SENSITIVE_PROVIDER_SENTINEL"
        messages = []
        log_buffer = []

        def fail_redeem(_client, _asset, _required_amount, **kwargs):
            kwargs["on_error"](RuntimeError(sentinel))
            return False

        def fail_maintenance(_client, _target_buffer, **kwargs):
            kwargs["on_error"](RuntimeError(sentinel))

        with patch.object(main, "qpk_ensure_asset_available", fail_redeem):
            with patch.object(main, "send_tg_msg", lambda _token, _chat_id, text: messages.append(text)):
                self.assertFalse(main.ensure_asset_available(object(), "USDT", 10.0, "token", "chat"))

        with patch.object(main, "qpk_manage_usdt_earn_buffer", fail_maintenance):
            main.manage_usdt_earn_buffer(object(), 100.0, "token", "chat", log_buffer)

        rendered = str(messages) + str(log_buffer)
        self.assertIn("earn_redeem_failed", str(messages))
        self.assertIn("earn_maintenance_failed", str(log_buffer))
        self.assertNotIn(sentinel, rendered)

    def test_btc_daily_fetch_failure_log_is_sanitized(self):
        sentinel = "SENSITIVE_PROVIDER_SENTINEL"
        log_buffer = []

        def fail_fetch(_client, _btc_price, **kwargs):
            kwargs["on_fetch_error"](RuntimeError(sentinel))
            return None

        with patch.object(main, "qpk_fetch_btc_market_snapshot", fail_fetch):
            self.assertIsNone(main.fetch_btc_market_snapshot(object(), 100_000.0, log_buffer=log_buffer))

        self.assertIn("btc_daily_fetch_failed", str(log_buffer))
        self.assertNotIn(sentinel, str(log_buffer))

    def test_runtime_error_notification_requires_transport_acknowledgement(self):
        with patch.dict(
            os.environ,
            {
                "TG_TOKEN": "token-1",
                "GLOBAL_TELEGRAM_CHAT_ID": "chat-1",
            },
            clear=False,
        ):
            with patch.object(
                main,
                "send_tg_msg",
                return_value={
                    "delivery_status": "failed",
                    "transport_acknowledged": False,
                },
            ):
                self.assertFalse(main._notify_runtime_error(RuntimeError("boom")))

    def test_runtime_error_notification_prefers_qsl_global_telegram_chat_id(self):
        observed = {}

        def fake_send_tg_msg(token, chat_id, _text):
            observed["token"] = token
            observed["chat_id"] = chat_id
            return True

        with patch.dict(
            os.environ,
            {
                "TG_TOKEN": "token-1",
                "QSL_GLOBAL_TELEGRAM_CHAT_ID": "qsl-chat-id",
                "GLOBAL_TELEGRAM_CHAT_ID": "legacy-chat-id",
            },
            clear=False,
        ):
            with patch.object(main, "send_tg_msg", fake_send_tg_msg):
                self.assertTrue(main._notify_runtime_error(RuntimeError("boom")))

        self.assertEqual(observed, {"token": "token-1", "chat_id": "qsl-chat-id"})


if __name__ == "__main__":
    unittest.main()
