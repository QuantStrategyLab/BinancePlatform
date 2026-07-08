from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "watchdog.yml"
RUNTIME_WORKFLOW = ROOT / ".github" / "workflows" / "main.yml"
REQUIREMENTS = ROOT / "requirements.txt"
LOCK = ROOT / "requirements-lock.txt"


def _qpk_requirement(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("quant-platform-kit @ "):
            return line
    raise AssertionError(f"{path} does not pin quant-platform-kit")


def _crypto_strategies_requirement(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("crypto-strategies @ "):
            return line
    raise AssertionError(f"{path} does not pin crypto-strategies")


class WatchdogWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow_text = WORKFLOW.read_text(encoding="utf-8")

    def test_watchdog_uses_binance_runtime_identity(self) -> None:
        text = self.workflow_text

        self.assertIn("id-token: write", text)
        self.assertIn(
            "GCP_WORKLOAD_IDENTITY_PROVIDER: "
            "projects/677468735457/locations/global/workloadIdentityPools/github-actions/providers/github-main",
            text,
        )
        self.assertIn(
            "GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT: "
            "binance-platform-runtime@binancequant.iam.gserviceaccount.com",
            text,
        )
        self.assertIn("uses: google-github-actions/auth@v3", text)
        self.assertIn("workload_identity_provider: ${{ env.GCP_WORKLOAD_IDENTITY_PROVIDER }}", text)
        self.assertIn("service_account: ${{ env.GCP_WORKLOAD_IDENTITY_SERVICE_ACCOUNT }}", text)
        self.assertIn("WATCHDOG_MAX_AGE_SECONDS: ${{ vars.WATCHDOG_MAX_AGE_SECONDS || '4500' }}", text)

    def test_watchdog_installs_locked_internal_dependency(self) -> None:
        text = self.workflow_text

        self.assertIn("qpk_req=\"$(grep -E '^quant-platform-kit @ ' \"$REQ_FILE\")\"", text)
        self.assertIn('python -m pip install "$qpk_req" google-cloud-firestore', text)
        self.assertNotIn("pip install quant-platform-kit google-cloud-firestore", text)

    def test_runtime_workflow_force_reinstalls_internal_git_dependencies(self) -> None:
        text = RUNTIME_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("force_reinstall_internal_git_deps()", text)
        self.assertIn("pip\" install --force-reinstall --no-deps \"$requirement\"", text)
        self.assertIn("grep -E '^(quant-platform-kit|crypto-strategies) @ git\\+'", text)
        self.assertIn('"$REQ_FILE unchanged; reusing cached venv."', text)
        self.assertGreaterEqual(text.count("force_reinstall_internal_git_deps"), 3)

    def test_runtime_workflow_exposes_strategy_artifact_variables(self) -> None:
        text = RUNTIME_WORKFLOW.read_text(encoding="utf-8")

        for name in (
            "STRATEGY_ARTIFACT_FILE",
            "STRATEGY_ARTIFACT_MANIFEST_FILE",
            "STRATEGY_ARTIFACT_FIRESTORE_COLLECTION",
            "STRATEGY_ARTIFACT_FIRESTORE_DOCUMENT",
            "STRATEGY_ARTIFACT_MAX_AGE_DAYS",
            "STRATEGY_ARTIFACT_ACCEPTABLE_MODES",
            "STRATEGY_ARTIFACT_EXPECTED_SIZE",
            "STRATEGY_ARTIFACT_ALLOW_NEW_ENTRIES_ON_DEGRADED",
        ):
            self.assertIn(f"{name}: ${{{{ vars.{name} }}}}", text)

    def test_watchdog_reads_firestore_heartbeat_with_supported_qpk_api(self) -> None:
        text = self.workflow_text

        self.assertIn("from quant_platform_kit.common.health import HealthMonitor, is_heartbeat_fresh", text)
        self.assertIn("heartbeat = HealthMonitor().read()", text)
        self.assertIn("alive = is_heartbeat_fresh(heartbeat, max_age_seconds)", text)
        self.assertNotIn(".check_alive()", text)

    def test_watchdog_qpk_pin_includes_health_module_release(self) -> None:
        requirement = _qpk_requirement(REQUIREMENTS)
        lock = _qpk_requirement(LOCK)

        self.assertEqual(requirement, lock)
        self.assertIn("@0af622ac9d47f7ef93f9379f9ded314c27a344ff", lock)
        self.assertNotIn("@37c81901160c5b31127a27dba1c63944933fb6bf", lock)

    def test_crypto_strategies_pin_matches_qpk_health_dependency(self) -> None:
        requirement = _crypto_strategies_requirement(REQUIREMENTS)
        lock = _crypto_strategies_requirement(LOCK)

        self.assertEqual(requirement, lock)
        self.assertIn("@eb7bf665c5199f7f075af61ef5c86171eea1f057", lock)
        self.assertNotIn("@af4df02f88177f0f80e4eab7d1ee04a27283159f", lock)


if __name__ == "__main__":
    unittest.main()
