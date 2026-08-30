import unittest

from scripts.runtime_target_lifecycle_monitoring import resolve_monitoring


class RuntimeTargetLifecycleMonitoringTests(unittest.TestCase):
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
