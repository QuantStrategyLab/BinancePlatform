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
        strategy_registry_module.resolve_strategy_definition = lambda *_args, **_kwargs: types.SimpleNamespace(
            profile="crypto_leader_rotation",
            domain="crypto",
        )
        strategy_registry_module.resolve_strategy_metadata = lambda *_args, **_kwargs: types.SimpleNamespace(
            display_name="Crypto Leader Rotation",
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
    def test_main_notifies_telegram_when_cli_entrypoint_fails_before_cycle(self):
        observed = {"messages": []}

        def fake_run_cli_entrypoint(**_kwargs):
            raise RuntimeError("runtime setup failed")

        def fake_send_tg_msg(token, chat_id, text):
            observed["messages"].append((token, chat_id, text))

        with patch.dict(
            os.environ,
            {
                "TG_TOKEN": "token-1",
                "GLOBAL_TELEGRAM_CHAT_ID": "chat-1",
                "STRATEGY_PROFILE": "crypto_leader_rotation",
            },
            clear=False,
        ):
            with patch.object(main, "run_cli_entrypoint", fake_run_cli_entrypoint):
                with patch.object(main, "send_tg_msg", fake_send_tg_msg):
                    with self.assertRaises(RuntimeError):
                        main.main()

        self.assertEqual(len(observed["messages"]), 1)
        self.assertEqual(observed["messages"][0][0], "token-1")
        self.assertEqual(observed["messages"][0][1], "chat-1")
        self.assertIn("Binance strategy run failed", observed["messages"][0][2])
        self.assertIn("RuntimeError: runtime setup failed", observed["messages"][0][2])


if __name__ == "__main__":
    unittest.main()
