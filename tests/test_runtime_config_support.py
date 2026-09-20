import json
import os
import subprocess
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

from runtime_config_support import (
    assert_standard_execution_entry_permitted,
    build_live_runtime,
    load_cycle_execution_settings,
    resolve_runtime_target_enabled_flag,
)
from strategy_registry import (
    BINANCE_PLATFORM,
    BINANCE_ENABLED_PROFILES,
    CRYPTO_DOMAIN,
    DEFAULT_STRATEGY_PROFILE,
    get_platform_profile_status_matrix,
    get_supported_profiles_for_platform,
    resolve_research_strategy_definition,
)


class RuntimeConfigSupportTests(unittest.TestCase):
    def test_load_cycle_execution_settings_clamps_interval_and_reads_degraded_flag(self):
        with patch.dict(
            os.environ,
            {
                "BTC_STATUS_REPORT_INTERVAL_HOURS": "48",
                "STRATEGY_ARTIFACT_ALLOW_NEW_ENTRIES_ON_DEGRADED": "1",
            },
            clear=False,
        ):
            if not BINANCE_ENABLED_PROFILES:
                with self.assertRaisesRegex(ValueError, "Unsupported STRATEGY_PROFILE"):
                    load_cycle_execution_settings()
                return
            settings = load_cycle_execution_settings()

        self.assertEqual(settings.btc_status_report_interval_hours, 24)
        self.assertTrue(settings.allow_new_trend_entries_on_degraded)
        self.assertEqual(settings.strategy_profile, DEFAULT_STRATEGY_PROFILE)
        self.assertEqual(settings.strategy_display_name, "Crypto Live Pool Rotation")
        self.assertEqual(settings.strategy_display_name_localized, "Crypto Live Pool Rotation")
        self.assertEqual(settings.strategy_domain, CRYPTO_DOMAIN)

    def test_load_cycle_execution_settings_ignores_legacy_trend_pool_degraded_alias(self):
        with patch.dict(
            os.environ,
            {
                "TREND_POOL_ALLOW_NEW_ENTRIES_ON_DEGRADED": "1",
            },
            clear=True,
        ):
            if not BINANCE_ENABLED_PROFILES:
                with self.assertRaisesRegex(ValueError, "Unsupported STRATEGY_PROFILE"):
                    load_cycle_execution_settings()
                return
            settings = load_cycle_execution_settings()

        self.assertFalse(settings.allow_new_trend_entries_on_degraded)

    def test_load_cycle_execution_settings_rejects_unknown_strategy_profile(self):
        with patch.dict(os.environ, {"STRATEGY_PROFILE": "global_etf_rotation"}, clear=False):
            with self.assertRaisesRegex(ValueError, "Unsupported STRATEGY_PROFILE"):
                load_cycle_execution_settings()

    def test_load_cycle_execution_settings_accepts_legacy_profile_alias(self):
        with patch.dict(
            os.environ,
            {
                "STRATEGY_PROFILE": "crypto_leader_rotation",
                "NOTIFY_LANG": "zh",
            },
            clear=False,
        ):
            if not BINANCE_ENABLED_PROFILES:
                with self.assertRaisesRegex(ValueError, "Unsupported STRATEGY_PROFILE"):
                    load_cycle_execution_settings()
                return
            settings = load_cycle_execution_settings()

        self.assertEqual(settings.strategy_profile, DEFAULT_STRATEGY_PROFILE)
        self.assertEqual(settings.strategy_display_name, "Crypto Live Pool Rotation")
        self.assertEqual(settings.strategy_display_name_localized, "加密实时池轮动")

    def test_platform_supported_profiles_are_filtered_by_registry(self):
        self.assertEqual(
            get_supported_profiles_for_platform(BINANCE_PLATFORM),
            BINANCE_ENABLED_PROFILES,
        )

    def test_platform_profile_status_matrix_marks_default_profile_eligible_and_enabled(self):
        rows = get_platform_profile_status_matrix()
        self.assertTrue(len(rows) >= 1)
        default_row = next(
            row for row in rows
            if row.get("canonical_profile") == DEFAULT_STRATEGY_PROFILE
        )
        self.assertEqual(default_row["platform"], BINANCE_PLATFORM)
        self.assertEqual(default_row["display_name"], "Crypto Live Pool Rotation")
        self.assertTrue(default_row["eligible"])
        self.assertEqual(default_row["enabled"], bool(BINANCE_ENABLED_PROFILES))
        self.assertTrue(default_row["is_default"])
        self.assertTrue(default_row["is_rollback"])
        self.assertEqual(default_row["domain"], CRYPTO_DOMAIN)

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
            if not BINANCE_ENABLED_PROFILES:
                with self.assertRaisesRegex(ValueError, "Unsupported STRATEGY_PROFILE"):
                    build_live_runtime(
                        now_utc=sentinel_now,
                        state_loader=state_loader,
                        state_writer=state_writer,
                        notifier=notifier,
                    )
                return
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
        self.assertEqual(runtime.strategy_profile, DEFAULT_STRATEGY_PROFILE)
        self.assertEqual(runtime.strategy_display_name, "Crypto Live Pool Rotation")
        self.assertTrue(runtime.dry_run)
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
            if not BINANCE_ENABLED_PROFILES:
                with self.assertRaisesRegex(ValueError, "Unsupported STRATEGY_PROFILE"):
                    build_live_runtime()
                return
            runtime = build_live_runtime()

        self.assertEqual(runtime.tg_chat_id, "shared-chat-id")

    def test_build_live_runtime_prefers_qsl_global_telegram_chat_id(self):
        with patch.dict(
            os.environ,
            {
                "BINANCE_API_KEY": "api-key",
                "BINANCE_API_SECRET": "api-secret",
                "TG_TOKEN": "tg-token",
                "QSL_GLOBAL_TELEGRAM_CHAT_ID": "qsl-chat-id",
                "GLOBAL_TELEGRAM_CHAT_ID": "legacy-chat-id",
            },
            clear=False,
        ):
            if not BINANCE_ENABLED_PROFILES:
                with self.assertRaisesRegex(ValueError, "Unsupported STRATEGY_PROFILE"):
                    build_live_runtime()
                return
            runtime = build_live_runtime()

        self.assertEqual(runtime.tg_chat_id, "qsl-chat-id")

    def test_build_live_runtime_uses_runtime_target_json(self):
        runtime_target = {
            "platform_id": "binance",
            "strategy_profile": DEFAULT_STRATEGY_PROFILE,
            "dry_run_only": False,
            "deployment_selector": "default",
            "account_selector": ["default"],
            "account_scope": "default",
            "service_name": "binance-platform",
            "execution_mode": "live",
            "market": "CRYPTO",
            "market_calendar": "24/7",
            "market_timezone": "UTC",
        }
        with patch.dict(
            os.environ,
            {
                "RUNTIME_TARGET_JSON": json.dumps(runtime_target),
                "STRATEGY_PROFILE": DEFAULT_STRATEGY_PROFILE,
                "BINANCE_DRY_RUN": "false",
            },
            clear=True,
        ):
            if not BINANCE_ENABLED_PROFILES:
                with self.assertRaisesRegex(ValueError, "Unsupported STRATEGY_PROFILE"):
                    build_live_runtime()
                return
            runtime = build_live_runtime()

        self.assertFalse(runtime.dry_run)
        self.assertFalse(runtime.standard_execution_permitted)
        self.assertEqual(runtime.runtime_target.service_name, "binance-platform")
        self.assertEqual(runtime.runtime_target.strategy_profile, DEFAULT_STRATEGY_PROFILE)

    def test_missing_runtime_target_enabled_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(resolve_runtime_target_enabled_flag())
        with patch.dict(os.environ, {"RUNTIME_TARGET_ENABLED": "false"}, clear=True):
            self.assertFalse(resolve_runtime_target_enabled_flag())
        with patch.dict(os.environ, {"RUNTIME_TARGET_ENABLED": "true"}, clear=True):
            self.assertTrue(resolve_runtime_target_enabled_flag())
        with patch.dict(os.environ, {"RUNTIME_TARGET_ENABLED": "perhaps"}, clear=True):
            with self.assertRaisesRegex(ValueError, "RUNTIME_TARGET_ENABLED must be true or false"):
                resolve_runtime_target_enabled_flag()

    def _continuity_runtime_target(self, *, state: str, dry_run_only: bool = False):
        from quant_platform_kit.common.live_continuity import runtime_target_fingerprint
        from quant_platform_kit.common.runtime_target import build_runtime_target

        payload = {
            "platform_id": "binance",
            "strategy_profile": DEFAULT_STRATEGY_PROFILE,
            "dry_run_only": dry_run_only,
            "deployment_selector": "default",
            "account_selector": ["default"],
            "account_scope": "default",
            "service_name": "binance-platform",
        }
        return build_runtime_target(
            **payload,
            live_continuity={
                "state": state,
                "baseline_kind": "legacy_authorized",
                "baseline_id": "binance-lkg-20260830",
                "baseline_target_sha256": runtime_target_fingerprint(payload),
                "captured_at": "2026-08-30",
            },
            continuity_fingerprint_payload=payload,
        )

    def _patch_resolved_target(self, runtime_target):
        definition = resolve_research_strategy_definition(
            runtime_target.strategy_profile,
            platform_id=BINANCE_PLATFORM,
        )
        return patch(
            "runtime_config_support._resolve_runtime_target",
            return_value=(runtime_target, definition),
        )

    def test_live_continuity_paused_state_suppresses_standard_execution(self):
        runtime_target = self._continuity_runtime_target(state="PAUSED")
        with self._patch_resolved_target(runtime_target), patch.dict(
            os.environ,
            {
                "BINANCE_DRY_RUN": "false",
                "RUNTIME_TARGET_ENABLED": "true",
            },
            clear=True,
        ):
            runtime = build_live_runtime()

        self.assertFalse(runtime.dry_run)
        self.assertFalse(runtime.standard_execution_permitted)

    def test_reconcile_only_blocks_live_entry_even_when_dry_run_false(self):
        runtime_target = self._continuity_runtime_target(state="RECONCILE_ONLY")
        with self._patch_resolved_target(runtime_target), patch.dict(
            os.environ,
            {
                "BINANCE_DRY_RUN": "false",
                "RUNTIME_TARGET_ENABLED": "true",
            },
            clear=True,
        ):
            runtime = build_live_runtime()
            with self.assertRaisesRegex(RuntimeError, "refusing live strategy entry"):
                assert_standard_execution_entry_permitted()

        self.assertFalse(runtime.dry_run)
        self.assertFalse(runtime.standard_execution_permitted)

    def test_disabled_switch_blocks_live_entry_independent_of_dry_run(self):
        runtime_target = self._continuity_runtime_target(state="ACTIVE_LKG")
        with self._patch_resolved_target(runtime_target), patch.dict(
            os.environ,
            {
                "BINANCE_DRY_RUN": "false",
                "RUNTIME_TARGET_ENABLED": "false",
            },
            clear=True,
        ):
            runtime = build_live_runtime()
            with self.assertRaisesRegex(RuntimeError, "refusing live strategy entry"):
                assert_standard_execution_entry_permitted()

        self.assertFalse(runtime.dry_run)
        self.assertFalse(runtime.standard_execution_permitted)

    def test_paper_dry_run_path_remains_dry_when_enabled(self):
        runtime_target = self._continuity_runtime_target(state="ACTIVE_LKG", dry_run_only=True)
        with self._patch_resolved_target(runtime_target), patch.dict(
            os.environ,
            {
                "BINANCE_DRY_RUN": "true",
                "RUNTIME_TARGET_ENABLED": "true",
            },
            clear=True,
        ):
            runtime = build_live_runtime()
            settings = assert_standard_execution_entry_permitted()

        self.assertTrue(runtime.dry_run)
        self.assertTrue(runtime.standard_execution_permitted)
        self.assertTrue(settings.runtime_target_enabled)

    def test_active_live_entry_requires_enabled_and_continuity(self):
        runtime_target = self._continuity_runtime_target(state="ACTIVE_LKG")
        with self._patch_resolved_target(runtime_target), patch.dict(
            os.environ,
            {
                "BINANCE_DRY_RUN": "false",
                "RUNTIME_TARGET_ENABLED": "true",
            },
            clear=True,
        ):
            runtime = build_live_runtime()
            settings = assert_standard_execution_entry_permitted()

        self.assertFalse(runtime.dry_run)
        self.assertTrue(runtime.standard_execution_permitted)
        self.assertTrue(settings.runtime_target_enabled)

    def test_runtime_target_and_legacy_dry_run_variable_must_match(self):
        runtime_target = {
            "platform_id": "binance",
            "strategy_profile": DEFAULT_STRATEGY_PROFILE,
            "dry_run_only": False,
            "execution_mode": "live",
        }
        with patch.dict(
            os.environ,
            {
                "RUNTIME_TARGET_JSON": json.dumps(runtime_target),
                "STRATEGY_PROFILE": DEFAULT_STRATEGY_PROFILE,
                "BINANCE_DRY_RUN": "true",
            },
            clear=True,
        ):
            if not BINANCE_ENABLED_PROFILES:
                with self.assertRaisesRegex(ValueError, "Unsupported STRATEGY_PROFILE"):
                    build_live_runtime()
            else:
                with self.assertRaisesRegex(ValueError, "BINANCE_DRY_RUN"):
                    build_live_runtime()

    def test_status_script_json_matches_registry(self):
        script = ROOT / "scripts" / "print_strategy_profile_status.py"
        result = subprocess.run(
            [sys.executable, str(script), "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), get_platform_profile_status_matrix())

    def test_status_script_table_contains_expected_headers_and_profile(self):
        script = ROOT / "scripts" / "print_strategy_profile_status.py"
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("canonical_profile", result.stdout)
        self.assertIn(DEFAULT_STRATEGY_PROFILE, result.stdout)

    def test_switch_env_plan_script_json_matches_binance_runtime_shape(self):
        script = ROOT / "scripts" / "print_strategy_switch_env_plan.py"
        result = subprocess.run(
            [sys.executable, str(script), "--profile", DEFAULT_STRATEGY_PROFILE, "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )

        plan = json.loads(result.stdout)
        self.assertEqual(plan["platform"], BINANCE_PLATFORM)
        self.assertEqual(plan["canonical_profile"], DEFAULT_STRATEGY_PROFILE)
        self.assertTrue(plan["eligible"])
        self.assertEqual(plan["enabled"], bool(BINANCE_ENABLED_PROFILES))
        self.assertEqual(plan["execution_plan_available"], bool(BINANCE_ENABLED_PROFILES))
        self.assertEqual(
            plan["set_env"],
            {"STRATEGY_PROFILE": DEFAULT_STRATEGY_PROFILE} if BINANCE_ENABLED_PROFILES else {},
        )
        self.assertEqual(
            plan["blocking_reason"],
            None if BINANCE_ENABLED_PROFILES else "profile_not_execution_enabled",
        )
        self.assertIn("BINANCE_API_KEY", plan["keep_env"])
        self.assertIn("BINANCE_API_SECRET", plan["keep_env"])
        self.assertIn("TG_TOKEN", plan["keep_env"])
        self.assertIn("STRATEGY_ARTIFACT_FILE", plan["optional_env"])
        self.assertIn("STRATEGY_ARTIFACT_MANIFEST_FILE", plan["optional_env"])
        self.assertIn("DYNAMIC_REGIME_MODE", plan["optional_env"])
        self.assertIn("DYNAMIC_HARD_BTC_WEIGHT", plan["optional_env"])
        self.assertNotIn("TREND_POOL_FILE", plan["optional_env"])
        self.assertEqual(
            plan["hints"]["strategy_artifact_default_firestore_document"],
            "CRYPTO_LIVE_POOL_ROTATION_LIVE_POOL",
        )
        self.assertEqual(plan["remove_if_present"], [])

    def test_switch_env_plan_script_table_contains_expected_sections(self):
        script = ROOT / "scripts" / "print_strategy_switch_env_plan.py"
        result = subprocess.run(
            [sys.executable, str(script), "--profile", DEFAULT_STRATEGY_PROFILE],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertIn("platform: binance", result.stdout)
        self.assertIn("profile: crypto_live_pool_rotation", result.stdout)
        self.assertIn("set_env:", result.stdout)
        self.assertIn("keep_env:", result.stdout)
        self.assertIn("optional_env:", result.stdout)


if __name__ == "__main__":
    unittest.main()
