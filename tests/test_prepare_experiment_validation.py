from __future__ import annotations

import unittest

from scripts.prepare_experiment_validation import build_payload


class PrepareExperimentValidationTests(unittest.TestCase):
    def test_build_payload_selects_cycle_replay_for_experiment_task(self) -> None:
        issue_context = {
            "number": 15,
            "title": "Monthly Optimization Tasks · BinancePlatform",
            "body": """# Monthly Optimization Tasks · BinancePlatform

## Actions
- [ ] `medium` Review account size suitability for current strategy parameters
  - Summary: Assess whether the strategy is now operating below its practical capital threshold.
  - Source: [QuantStrategyLab/BinancePlatform #9](https://example.com/9)
- [ ] `medium` Add downstream liquidity validation for TAOUSDT-class assets [experiment-only]
  - Summary: Ensure BinancePlatform applies its own ADV/spread checks before trading low-liquidity pool members.
  - Source: [QuantStrategyLab/CryptoSnapshotPipelines #11](https://example.com/11)
""",
        }

        payload = build_payload(issue_context)

        self.assertTrue(payload["should_run"])
        self.assertEqual(payload["experiment_task_count"], 1)
        self.assertTrue(payload["run_cycle_replay"])

    def test_build_payload_skips_when_no_experiment_tasks_exist(self) -> None:
        issue_context = {
            "number": 30,
            "title": "Monthly Optimization Tasks · BinancePlatform",
            "body": """# Monthly Optimization Tasks · BinancePlatform

## Actions
- [ ] `low` Add diagnostic reporting for no-trade months [auto-pr-safe]
  - Summary: Emit explicit reason codes for skipped DCA and rotation attempts.
  - Source: [QuantStrategyLab/BinancePlatform #9](https://example.com/9)
""",
        }

        payload = build_payload(issue_context)

        self.assertFalse(payload["should_run"])
        self.assertEqual(payload["experiment_task_count"], 0)
        self.assertIn("No experiment-only tasks", payload["skip_reason"])


if __name__ == "__main__":
    unittest.main()
