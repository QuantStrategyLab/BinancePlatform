from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTO_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "auto_optimization_pr.yml"
CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"


class AutoOptimizationPrWorkflowConfigTests(unittest.TestCase):
    def test_auto_optimization_workflow_handles_monthly_task_issues(self) -> None:
        workflow = AUTO_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("issues:", workflow)
        self.assertIn("monthly-optimization-task", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("issue_number:", workflow)
        self.assertIn("actions: write", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("pull-requests: write", workflow)
        self.assertIn("ANTHROPIC_API_KEY", workflow)
        self.assertIn("prepare_auto_optimization_pr.py", workflow)
        self.assertIn("anthropics/claude-code-action@v1", workflow)
        self.assertIn("Export selected task summary", workflow)
        self.assertIn("claude_args: --max-turns 8", workflow)
        self.assertIn("timeout-minutes: 15", workflow)
        self.assertIn("steps.selected_tasks.outputs.task_summary", workflow)
        self.assertIn("Do not use Bash in this workflow.", workflow)
        self.assertIn("The workflow will run CI after the draft PR is created.", workflow)
        self.assertIn("gh pr create --draft", workflow)
        self.assertIn("gh workflow run ci.yml", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn('FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"', workflow)
        self.assertIn("You are working inside BinancePlatform, the downstream execution repository.", workflow)

    def test_ci_workflow_supports_manual_dispatch(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)


if __name__ == "__main__":
    unittest.main()
