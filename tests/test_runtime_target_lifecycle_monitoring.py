import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import runtime_target_lifecycle_monitoring as monitoring
from scripts.runtime_target_lifecycle_monitoring import resolve_monitoring


class RuntimeTargetLifecycleMonitoringTests(unittest.TestCase):
    def test_scheduler_only_snapshot_does_not_claim_runtime_enablement(self):
        with tempfile.TemporaryDirectory() as directory:
            assessment = Path(directory) / "assessment.json"
            scheduler = Path(directory) / "scheduler.json"
            output = Path(directory) / "output.txt"
            observed_at = "2026-08-30T02:00:00Z"
            assessment.write_text(json.dumps({"status": "healthy", "deployment": {"runtime_enabled": True}}), encoding="utf-8")
            scheduler.write_text(json.dumps({"scheduler_state": "enabled", "observed_at": observed_at}), encoding="utf-8")
            with patch.dict(os.environ, {
                "CONFIGURED_STATE": "enabled",
                "CONFIGURATION_GUARD": "pass",
                "EXECUTION_HEARTBEAT_PATH": str(assessment),
                "SCHEDULER_OBSERVATION_PATH": str(scheduler),
                "TARGET_ID": "binance.crypto_live_pool_rotation",
                "EXECUTION_MODE": "dry_run",
                "GITHUB_OUTPUT": str(output),
            }, clear=True):
                self.assertEqual(monitoring.main(), 0)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        deployment = json.loads(values["deployment_json"])
        self.assertEqual(deployment, {
            "runtime_enabled": None,
            "scheduler_state": "enabled",
            "strategy_profile": "crypto_live_pool_rotation",
            "execution_mode": "dry_run",
            "observed_at": observed_at,
        })

    def test_qualified_execution_report_deployment_keeps_priority_and_original_time(self):
        report_deployment = {
            "runtime_enabled": True,
            "scheduler_state": "enabled",
            "strategy_profile": "crypto_live_pool_rotation",
            "execution_mode": "paper",
            "observed_at": "2026-08-30T01:55:00Z",
        }
        with tempfile.TemporaryDirectory() as directory:
            assessment = Path(directory) / "assessment.json"
            scheduler = Path(directory) / "scheduler.json"
            output = Path(directory) / "output.txt"
            assessment.write_text(json.dumps({"status": "healthy", "deployment": report_deployment}), encoding="utf-8")
            scheduler.write_text(json.dumps({"scheduler_state": "disabled", "observed_at": "2026-08-30T02:00:00Z"}), encoding="utf-8")
            with patch.dict(os.environ, {
                "CONFIGURED_STATE": "enabled",
                "CONFIGURATION_GUARD": "pass",
                "EXECUTION_HEARTBEAT_PATH": str(assessment),
                "SCHEDULER_OBSERVATION_PATH": str(scheduler),
                "TARGET_ID": "binance.crypto_live_pool_rotation",
                "EXECUTION_MODE": "dry_run",
                "GITHUB_OUTPUT": str(output),
            }, clear=True):
                self.assertEqual(monitoring.main(), 0)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(json.loads(values["deployment_json"]), report_deployment)

    def test_alert_does_not_hide_scheduler_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            assessment = Path(directory) / "assessment.json"
            scheduler = Path(directory) / "scheduler.json"
            output = Path(directory) / "output.txt"
            assessment.write_text(json.dumps({"status": "alert", "deployment": {"runtime_enabled": True}}), encoding="utf-8")
            scheduler.write_text(json.dumps({"scheduler_state": "disabled", "observed_at": "2026-08-30T02:00:00Z"}), encoding="utf-8")
            with patch.dict(os.environ, {
                "CONFIGURED_STATE": "enabled",
                "CONFIGURATION_GUARD": "pass",
                "EXECUTION_HEARTBEAT_PATH": str(assessment),
                "SCHEDULER_OBSERVATION_PATH": str(scheduler),
                "TARGET_ID": "binance.crypto_live_pool_rotation",
                "EXECUTION_MODE": "dry_run",
                "GITHUB_OUTPUT": str(output),
            }, clear=True):
                self.assertEqual(monitoring.main(), 0)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(json.loads(values["deployment_json"])["scheduler_state"], "paused")
        self.assertEqual(values["execution_heartbeat"], "attention")

    def test_missing_or_invalid_scheduler_observation_does_not_forge_a_fresh_time(self):
        self.assertIsNone(monitoring._scheduler_deployment_json(
            {"scheduler_state": "unknown", "observed_at": ""},
            target_id="binance.profile",
            execution_mode="paper",
        ))
        self.assertIsNone(monitoring._scheduler_deployment_json(
            None, target_id="binance.profile", execution_mode="paper",
        ))

    def test_scheduler_unknown_with_real_observation_time_is_forwarded(self):
        result = monitoring._scheduler_deployment_json(
            {"scheduler_state": "unknown", "observed_at": "2026-08-30T02:00:00Z"},
            target_id="binance.profile",
            execution_mode="paper",
        )
        self.assertEqual(json.loads(result), {
            "runtime_enabled": None,
            "scheduler_state": "unknown",
            "strategy_profile": "profile",
            "execution_mode": "paper",
            "observed_at": "2026-08-30T02:00:00Z",
        })

    def test_invalid_target_or_execution_mode_is_not_forwarded(self):
        observation = {"scheduler_state": "enabled", "observed_at": "2026-08-30T02:00:00Z"}
        self.assertIsNone(monitoring._scheduler_deployment_json(
            observation, target_id="binance.unresolved/secret", execution_mode="paper",
        ))
        self.assertIsNone(monitoring._scheduler_deployment_json(
            observation, target_id="binance.profile", execution_mode="unknown",
        ))

    def test_missing_report_does_not_synthesize_environment_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.txt"
            with patch.dict(os.environ, {
                "CONFIGURED_STATE": "enabled",
                "CONFIGURATION_GUARD": "pass",
                "RUNTIME_TARGET_ENABLED": "true",
                "BINANCE_DRY_RUN": "false",
                "SCHEDULER_OBSERVATION_PATH": str(Path(directory) / "missing-scheduler.json"),
                "TARGET_ID": "binance.profile",
                "EXECUTION_MODE": "paper",
                "GITHUB_OUTPUT": str(output),
            }, clear=True):
                self.assertEqual(monitoring.main(), 0)
            self.assertNotIn("deployment_json", output.read_text())

    def test_lifecycle_passes_validated_scheduler_observation_to_shared_publisher(self):
        workflow = Path(".github/workflows/runtime-target-lifecycle.yml").read_text(encoding="utf-8")
        self.assertIn("deployment-json: ${{ steps.monitoring.outputs.deployment_json }}", workflow)
        self.assertIn("SCHEDULER_OBSERVATION_PATH: ${{ runner.temp }}/validated-runtime-scheduler-observation.json", workflow)
        self.assertNotIn("observe-gcp:", workflow)

    def test_enabled_target_requires_both_workflow_and_execution_evidence(self):
        result = resolve_monitoring(
            configured_state="enabled",
            configuration_guard="pass",
            workflow_status="healthy",
            execution_status="healthy",
        )

        self.assertEqual(result, {"runtime_guard": "pass", "execution_heartbeat": "pass"})

    def test_one_late_workflow_dispatch_is_not_a_false_alert(self):
        result = resolve_monitoring(
            configured_state="enabled",
            configuration_guard="pass",
            workflow_status="deferred",
            execution_status="healthy",
        )

        self.assertEqual(result, {"runtime_guard": "not_due", "execution_heartbeat": "pass"})

    def test_execution_evidence_failure_parks_enabled_target(self):
        result = resolve_monitoring(
            configured_state="enabled",
            configuration_guard="pass",
            workflow_status="healthy",
            execution_status="alert",
        )

        self.assertEqual(result, {"runtime_guard": "pass", "execution_heartbeat": "attention"})

    def test_disabled_target_never_claims_execution_evidence(self):
        result = resolve_monitoring(
            configured_state="disabled",
            configuration_guard="pass",
            workflow_status="healthy",
            execution_status="healthy",
        )

        self.assertEqual(result, {"runtime_guard": "pass", "execution_heartbeat": "not_applicable"})

    def test_invalid_configuration_takes_precedence_over_monitoring_results(self):
        result = resolve_monitoring(
            configured_state="enabled",
            configuration_guard="attention",
            workflow_status="healthy",
            execution_status="healthy",
        )

        self.assertEqual(result, {"runtime_guard": "attention", "execution_heartbeat": "not_due"})
