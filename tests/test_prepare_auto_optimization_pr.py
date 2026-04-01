from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.prepare_auto_optimization_pr import build_payload, evaluate_changed_files, parse_actions, render_pr_body


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PrepareAutoOptimizationPrTests(unittest.TestCase):
    def setUp(self) -> None:
        self.issue_context = {
            "number": 15,
            "title": "Monthly Optimization Tasks · BinancePlatform: 2026-04-01 / 2026-03",
            "body": """# Monthly Optimization Tasks · BinancePlatform

## Actions
- [ ] `high` Reconcile March cash flows and open-position state
  - Summary: Pull Binance transaction history for March.
  - Source: [QuantStrategyLab/BinancePlatform #9](https://github.com/QuantStrategyLab/BinancePlatform/issues/9)
- [ ] `low` Add diagnostic reporting for no-trade months [auto-pr-safe]
  - Summary: Emit explicit reason codes for skipped DCA and rotation attempts.
  - Source: [QuantStrategyLab/BinancePlatform #9](https://github.com/QuantStrategyLab/BinancePlatform/issues/9)
- [ ] `low` Add a boundary tracker [auto-pr-safe, experiment-only]
  - Summary: Track near-cutoff symbols monthly.
  - Source: [QuantStrategyLab/CryptoLeaderRotation #11](https://github.com/QuantStrategyLab/CryptoLeaderRotation/issues/11)
""",
        }

    def test_parse_actions_preserves_risk_flags_and_source(self) -> None:
        actions = parse_actions(self.issue_context["body"])

        self.assertEqual(len(actions), 3)
        self.assertEqual(actions[0]["risk_level"], "high")
        self.assertEqual(actions[1]["flags"], ["auto-pr-safe"])
        self.assertEqual(actions[2]["flags"], ["auto-pr-safe", "experiment-only"])
        self.assertEqual(actions[2]["source_label"], "QuantStrategyLab/CryptoLeaderRotation #11")

    def test_build_payload_skips_completed_bp_task_and_excludes_experiments(self) -> None:
        payload = build_payload(self.issue_context, repo_root=PROJECT_ROOT)

        self.assertFalse(payload["should_run"])
        self.assertEqual(payload["safe_task_count"], 0)
        self.assertEqual(payload["skipped_task_count"], 1)
        self.assertEqual(payload["skipped_actions"][0]["title"], "Add diagnostic reporting for no-trade months")

    def test_build_payload_keeps_runtime_sensitive_low_risk_task_as_draft_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir) / "BinancePlatform"
            repo_root.mkdir()
            payload = build_payload(self.issue_context, repo_root=repo_root)

        self.assertTrue(payload["should_run"])
        self.assertEqual(payload["safe_task_count"], 1)
        self.assertEqual(payload["auto_merge_candidate_count"], 0)
        self.assertEqual(payload["draft_only_task_count"], 1)
        self.assertFalse(payload["task_level_auto_merge_allowed"])
        self.assertIn("guarded_keyword:dca", payload["draft_only_actions"][0]["auto_merge_blocker"])

    def test_build_payload_marks_readme_note_as_auto_merge_candidate(self) -> None:
        issue_context = {
            "number": 30,
            "title": "Monthly Optimization Tasks · Sandbox",
            "body": """# Monthly Optimization Tasks · Sandbox

## Actions
- [ ] `low` Add a short README note [auto-pr-safe]
  - Summary: Document a small operator-facing behavior.
  - Source: [Sandbox #1](https://example.com/issues/1)
""",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = build_payload(issue_context, repo_root=Path(temp_dir))
        self.assertTrue(payload["task_level_auto_merge_allowed"])
        self.assertEqual(payload["auto_merge_candidate_count"], 1)

    def test_evaluate_changed_files_blocks_live_runtime_paths(self) -> None:
        allowed = evaluate_changed_files(["reporting/status_reports.py", "tests/test_status_reports.py"], repo_root=PROJECT_ROOT)
        blocked = evaluate_changed_files(["application/execution_service.py", "README.md"], repo_root=PROJECT_ROOT)

        self.assertTrue(allowed["allowed"])
        self.assertFalse(blocked["allowed"])
        self.assertEqual(blocked["blocked_files"], ["application/execution_service.py"])

    def test_render_pr_body_contains_merge_policy_and_issue_reference(self) -> None:
        issue_context = {
            "number": 30,
            "title": "Monthly Optimization Tasks · Sandbox",
            "body": """# Monthly Optimization Tasks · Sandbox

## Actions
- [ ] `low` Add a short README note [auto-pr-safe]
  - Summary: Document a small operator-facing behavior.
  - Source: [Sandbox #1](https://example.com/issues/1)
""",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = build_payload(issue_context, repo_root=Path(temp_dir))
        body = render_pr_body(payload)

        self.assertIn("<!-- auto-optimization-pr:issue-30 -->", body)
        self.assertIn("Task-level auto-merge eligible: `yes`", body)
        self.assertIn("Add a short README note", body)
        self.assertIn("Refs #30", body)


if __name__ == "__main__":
    unittest.main()
