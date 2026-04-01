from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MONTHLY_REPORT_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "monthly_report.yml"
AI_REVIEW_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ai_review.yml"


class MonthlyReportWorkflowConfigTests(unittest.TestCase):
    def test_monthly_report_workflow_passes_hourly_dir_and_dispatches_ai_review(self) -> None:
        workflow = MONTHLY_REPORT_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("actions: write", workflow)
        self.assertIn("actions/checkout@v6", workflow)
        self.assertIn('--hourly-dir "hourly/${{ steps.month.outputs.month }}"', workflow)
        self.assertIn("gh label create monthly-review", workflow)
        self.assertIn("gh workflow run ai_review.yml", workflow)
        self.assertIn('issue_number="${{ steps.issue.outputs.issue_number }}"', workflow)

    def test_ai_review_workflow_supports_manual_dispatch(self) -> None:
        workflow = AI_REVIEW_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("issue_number:", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("Load review issue context", workflow)
        self.assertIn("api.github.com/repos", workflow)
        self.assertIn("steps.issue_context.outputs.issue_title", workflow)
        self.assertIn("steps.issue_context.outputs.issue_body", workflow)
        self.assertIn("id: claude_review", workflow)
        self.assertIn("github_token: ${{ secrets.GITHUB_TOKEN }}", workflow)
        self.assertIn('FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"', workflow)
        self.assertIn("${{ inputs.issue_number || github.event.issue.number }}", workflow)
        self.assertIn("Do not use Bash or ask for additional approval.", workflow)
        self.assertIn("The workflow will publish your final review as the issue comment.", workflow)
        self.assertIn("This is a downstream execution review, not a pure upstream pool review.", workflow)
        self.assertIn("do not treat equity delta as pure strategy PnL", workflow)
        self.assertIn("not a separate exchange fill reconciliation", workflow)
        self.assertIn("Do not assume zero trades are automatically anomalous", workflow)
        self.assertIn("Treat a zero-trade month as context-dependent", workflow)
        self.assertIn("post_monthly_ai_review_comment.py", workflow)
        self.assertIn("steps.claude_review.outputs.execution_file", workflow)


if __name__ == "__main__":
    unittest.main()
