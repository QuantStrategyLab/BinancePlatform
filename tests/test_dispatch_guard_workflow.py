from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "install-dispatch-guard.yml"


class DispatchGuardWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow_text = WORKFLOW.read_text(encoding="utf-8")

    def test_dispatch_guard_retries_transient_failures(self) -> None:
        text = self.workflow_text

        self.assertIn('DISPATCH_MAX_ATTEMPTS="${DISPATCH_MAX_ATTEMPTS:-4}"', text)
        self.assertIn('DISPATCH_RETRY_BASE_SECONDS="${DISPATCH_RETRY_BASE_SECONDS:-15}"', text)
        self.assertIn("is_retryable_http_status()", text)
        self.assertIn("000|500|502|503|504) return 0 ;;", text)
        self.assertIn('retryable=true', text)
        self.assertIn('sleep "$delay"', text)
        self.assertIn('dispatch retry ${attempt}/${DISPATCH_MAX_ATTEMPTS}', text)

    def test_dispatch_guard_keeps_non_retryable_failures_immediate(self) -> None:
        text = self.workflow_text

        self.assertIn('if [ "$attempt" -ge "$DISPATCH_MAX_ATTEMPTS" ] || [ "$retryable" != "true" ]; then', text)
        self.assertIn('fail_dispatch "$reason" "$details"', text)
        self.assertIn('GitHub dispatch returned HTTP ${http_status}', text)

    def test_dispatch_guard_bounds_curl_runtime(self) -> None:
        text = self.workflow_text

        self.assertIn("--connect-timeout 20", text)
        self.assertIn("--max-time 60", text)


if __name__ == "__main__":
    unittest.main()
