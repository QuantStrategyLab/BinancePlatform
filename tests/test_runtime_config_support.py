import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
QPK_SRC = ROOT.parent / "QuantPlatformKit" / "src"
CRYPTO_STRATEGIES_SRC = ROOT.parent / "CryptoStrategies" / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for path in (QPK_SRC, CRYPTO_STRATEGIES_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from runtime_config_support import build_live_runtime, load_cycle_execution_settings
from strategy_registry import BINANCE_PLATFORM, CRYPTO_DOMAIN, DEFAULT_STRATEGY_PROFILE, get_supported_profiles_for_platform


class RuntimeConfigSupportTests(unittest.TestCase):
    def test_load_cycle_execution_settings_clamps_interval_and_reads_degraded_flag(self):
        with patch.dict(
            os.environ,
            {
                "BTC_STATUS_REPORT_INTERVAL_HOURS": "48",
                "TREND_POOL_ALLOW_NEW_ENTRIES_ON_DEGRADED": "1",
            },
            clear=False,
        ):
            settings = load_cycle_execution_settings()

        self.assertEqual(settings.btc_status_report_interval_hours, 24)
        self.assertTrue(settings.allow_new_trend_entries_on_degraded)
        self.assertEqual(settings.strategy_profile, DEFAULT_STRATEGY_PROFILE)
        self.assertEqual(settings.strategy_domain, CRYPTO_DOMAIN)

    def test_load_cycle_execution_settings_rejects_unknown_strategy_profile(self):
        with patch.dict(os.environ, {"STRATEGY_PROFILE": "global_etf_rotation"}, clear=False):
            with self.assertRaisesRegex(ValueError, "Unsupported STRATEGY_PROFILE"):
                load_cycle_execution_settings()

    def test_platform_supported_profiles_are_filtered_by_registry(self):
        self.assertEqual(
            get_supported_profiles_for_platform(BINANCE_PLATFORM),
            frozenset({DEFAULT_STRATEGY_PROFILE}),
        )

    def test_build_live_runtime_reads_env_and_preserves_injected_hooks(self):
        sentinel_now = datetime(2026, 3, 15, tzinfo=timezone.utc)
        state_loader = object()
        state_writer = object()
        notifier = object()
        with patch.dict(
            os.environ,
            {
                "BINANCE_API_KEY": "api-key",
                "BINANCE_API_SECRET": "api-secret",
                "TG_TOKEN": "tg-token",
                "GLOBAL_TELEGRAM_CHAT_ID": "chat-id",
            },
            clear=False,
        ):
            runtime = build_live_runtime(
                now_utc=sentinel_now,
                state_loader=state_loader,
                state_writer=state_writer,
                notifier=notifier,
            )

        self.assertEqual(runtime.now_utc, sentinel_now)
        self.assertEqual(runtime.api_key, "api-key")
        self.assertEqual(runtime.api_secret, "api-secret")
        self.assertEqual(runtime.tg_token, "tg-token")
        self.assertEqual(runtime.tg_chat_id, "chat-id")
        self.assertIs(runtime.state_loader, state_loader)
        self.assertIs(runtime.state_writer, state_writer)
        self.assertIs(runtime.notifier, notifier)

    def test_build_live_runtime_uses_global_telegram_chat_id(self):
        with patch.dict(
            os.environ,
            {
                "BINANCE_API_KEY": "api-key",
                "BINANCE_API_SECRET": "api-secret",
                "TG_TOKEN": "tg-token",
                "GLOBAL_TELEGRAM_CHAT_ID": "shared-chat-id",
            },
            clear=False,
        ):
            runtime = build_live_runtime()

        self.assertEqual(runtime.tg_chat_id, "shared-chat-id")


if __name__ == "__main__":
    unittest.main()
