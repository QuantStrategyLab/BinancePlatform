import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import runtime_target_lifecycle_monitoring as monitoring
from scripts.runtime_target_lifecycle_monitoring import resolve_monitoring


class RuntimeTargetLifecycleMonitoringTests(unittest.TestCase):
    def test_report_observation_is_forwarded_without_changing_its_time(self):
        deployment = {
            "runtime_enabled": False,
            "scheduler_state": "unknown",
            "strategy_profile": "crypto_live_pool_rotation",
            "execution_mode": "dry_run",
            "observed_at": "2026-08-30T02:00:00Z",
        }
        with tempfile.TemporaryDirectory() as directory:
            assessment = Path(directory) / "assessment.json"
            output = Path(directory) / "output.txt"
            assessment.write_text(json.dumps({"status": "healthy", "deployment": deployment}), encoding="utf-8")
            with patch.dict(os.environ, {
                "CONFIGURED_STATE": "enabled",
                "CONFIGURATION_GUARD": "pass",
                "EXECUTION_HEARTBEAT_PATH": str(assessment),
                "GITHUB_OUTPUT": str(output),
            }, clear=True):
                self.assertEqual(monitoring.main(), 0)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(json.loads(values["deployment_json"]), deployment)

    def test_missing_report_does_not_synthesize_environment_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.txt"
            with patch.dict(os.environ, {
                "CONFIGURED_STATE": "enabled",
                "CONFIGURATION_GUARD": "pass",
                "RUNTIME_TARGET_ENABLED": "true",
                "BINANCE_DRY_RUN": "false",
                "GITHUB_OUTPUT": str(output),
            }, clear=True):
                self.assertEqual(monitoring.main(), 0)
            self.assertNotIn("deployment_json", output.read_text())

    def test_lifecycle_passes_only_report_observation_to_shared_publisher(self):
        workflow = Path(".github/workflows/runtime-target-lifecycle.yml").read_text(encoding="utf-8")
        self.assertIn("deployment-json: ${{ steps.monitoring.outputs.deployment_json }}", workflow)
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
