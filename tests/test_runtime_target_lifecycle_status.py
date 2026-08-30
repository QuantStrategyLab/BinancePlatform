import json
import unittest

from scripts.runtime_target_lifecycle_status import resolve_lifecycle_metadata


def _target(**overrides):
    target = {
        "platform_id": "binance",
        "strategy_profile": "crypto_live_pool_rotation",
        "execution_mode": "paper",
        "dry_run_only": True,
    }
    target.update(overrides)
    return json.dumps(target)


class RuntimeTargetLifecycleStatusTests(unittest.TestCase):
    def test_enabled_target_is_resolved_from_explicit_control(self):
        result = resolve_lifecycle_metadata(_target(), "true")

        self.assertEqual(result.configured_state, "enabled")
        self.assertEqual(result.execution_mode, "paper")
        self.assertEqual(result.runtime_guard, "pass")
        self.assertEqual(result.target_id, "binance.crypto_live_pool_rotation")

    def test_disabled_target_is_resolved_without_changing_its_lane(self):
        result = resolve_lifecycle_metadata(_target(), "false")

        self.assertEqual(result.configured_state, "disabled")
        self.assertEqual(result.execution_mode, "paper")
        self.assertEqual(result.runtime_guard, "pass")

    def test_target_embedded_disabled_control_is_supported_when_legacy_variable_is_absent(self):
        result = resolve_lifecycle_metadata(_target(runtime_target_enabled=False), None)

        self.assertEqual(result.configured_state, "disabled")

    def test_malformed_control_parks_target_with_safe_metadata(self):
        result = resolve_lifecycle_metadata(_target(), "perhaps")

        self.assertEqual(result.configured_state, "enabled")
        self.assertEqual(result.execution_mode, "dry_run")
        self.assertEqual(result.runtime_guard, "attention")
        self.assertIn("RUNTIME_TARGET_ENABLED", result.error)

    def test_live_mode_cannot_claim_dry_run_only(self):
        result = resolve_lifecycle_metadata(_target(execution_mode="live"), "true")

        self.assertEqual(result.runtime_guard, "attention")
        self.assertIn("dry_run_only", result.error)
