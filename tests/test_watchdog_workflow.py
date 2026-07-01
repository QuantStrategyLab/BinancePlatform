from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "watchdog.yml"


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

    def test_watchdog_installs_locked_internal_dependency(self) -> None:
        text = self.workflow_text

        self.assertIn("qpk_req=\"$(grep -E '^quant-platform-kit @ ' \"$REQ_FILE\")\"", text)
        self.assertIn('python -m pip install "$qpk_req" google-cloud-firestore', text)
        self.assertNotIn("pip install quant-platform-kit google-cloud-firestore", text)


if __name__ == "__main__":
    unittest.main()
