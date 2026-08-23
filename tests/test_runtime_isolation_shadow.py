from __future__ import annotations

import importlib.util
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "runtime-isolation-shadow.yml"
VALIDATOR = ROOT / "scripts" / "assert_no_order_shadow_report.py"
FULL_SHA_ACTION = re.compile(r"(?:-\s+)?uses:\s+[^\s@]+@[0-9a-f]{40}(?:\s+#\s+v\d+)?$")


def load_validator():
    spec = importlib.util.spec_from_file_location("assert_no_order_shadow_report", VALIDATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load shadow report validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeIsolationShadowWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.validator = load_validator()

    def test_shadow_workflow_is_manual_ephemeral_and_has_no_secret_capability(self) -> None:
        workflow = self.workflow

        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("pull_request_target:", workflow)
        self.assertIn("runs-on: ubuntu-latest", workflow)
        self.assertNotIn("runs-on: self-hosted", workflow)
        self.assertIn("contents: read", workflow)
        self.assertNotIn("id-token: write", workflow)
        self.assertNotIn("environment:", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("BINANCE_API_KEY", workflow)
        self.assertNotIn("BINANCE_API_SECRET", workflow)

    def test_shadow_workflow_uses_fixture_replay_and_pins_actions(self) -> None:
        workflow = self.workflow
        action_lines = [line.strip() for line in workflow.splitlines() if "uses:" in line]

        self.assertTrue(action_lines)
        self.assertTrue(all(FULL_SHA_ACTION.fullmatch(line) for line in action_lines))
        self.assertIn("run_cycle_replay.py", workflow)
        self.assertIn("assert_no_order_shadow_report.py", workflow)
        self.assertIn('BINANCE_DRY_RUN: "true"', workflow)
        self.assertNotIn("python main.py", workflow)

    def test_validator_accepts_only_dry_run_with_zero_executed_calls(self) -> None:
        accepted = {
            "status": "ok",
            "dry_run": True,
            "side_effect_summary": {"executed_call_count": 0, "suppressed_call_count": 3},
        }
        self.assertEqual(self.validator.validate_no_order_report(accepted), [])

        for field, value in (
            ("dry_run", False),
            ("status", "aborted"),
        ):
            rejected = json.loads(json.dumps(accepted))
            rejected[field] = value
            self.assertTrue(self.validator.validate_no_order_report(rejected))

        executed = json.loads(json.dumps(accepted))
        executed["side_effect_summary"]["executed_call_count"] = 1
        self.assertTrue(self.validator.validate_no_order_report(executed))


if __name__ == "__main__":
    unittest.main()
