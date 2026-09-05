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


class DisabledHostObservationTests(unittest.TestCase):
    def _run_host(self, *, enabled="false", observed_at="2026-09-06T01:00:00Z"):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from scripts import runtime_target_lifecycle_status as status

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with patch.dict("os.environ", {
                "RUNTIME_TARGET_JSON": _target(),
                "RUNTIME_TARGET_ENABLED": enabled,
                **({"RUNTIME_TARGET_CONTROL_OBSERVED_AT": observed_at} if observed_at is not None else {}),
                "GITHUB_OUTPUT": str(output),
            }, clear=True):
                self.assertEqual(status.main(), 0)
            return dict(line.split("=", 1) for line in output.read_text().splitlines())

    def test_disabled_host_preserves_control_read_time_and_unknown_scheduler(self):
        values = self._run_host()
        self.assertEqual(json.loads(values["deployment_json"]), {
            "runtime_enabled": False, "scheduler_state": "unknown",
            "strategy_profile": None, "execution_mode": None,
            "observed_at": "2026-09-06T01:00:00Z",
        })
        self.assertEqual(values["target_id"], "binance.crypto_live_pool_rotation")
        self.assertEqual(values["configured_state"], "disabled")

    def test_missing_invalid_or_naive_host_time_never_becomes_an_observation(self):
        for observed_at in ("", "not-a-time", "2026-09-06T01:00:00"):
            with self.subTest(observed_at=observed_at), self.assertRaises(ValueError):
                self._run_host(observed_at=observed_at)

    def test_enabled_control_cannot_publish_a_disabled_observation(self):
        with self.assertRaises(ValueError):
            self._run_host(enabled="true")

    def test_hosted_metadata_without_control_read_does_not_claim_observation(self):
        self.assertNotIn("deployment_json", self._run_host(observed_at=None))
