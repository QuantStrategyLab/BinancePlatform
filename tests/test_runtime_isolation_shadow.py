from __future__ import annotations

import importlib.util
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "runtime-isolation-shadow.yml"
HOST_PROFILE_WORKFLOW = ROOT / ".github" / "workflows" / "runtime-isolation-host-profile.yml"
VALIDATOR = ROOT / "scripts" / "assert_no_order_shadow_report.py"
PORTABLE_RUNNER = ROOT / "scripts" / "run_isolation_shadow_fixture.py"
FULL_SHA_ACTION = re.compile(r"(?:-\s+)?uses:\s+[^\s@]+@[0-9a-f]{40}(?:\s+#\s+v\d+)?$")


def load_validator():
    spec = importlib.util.spec_from_file_location("assert_no_order_shadow_report", VALIDATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load shadow report validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def job_block(workflow: str, job: str, next_job: str | None = None) -> str:
    start = workflow.index(f"  {job}:\n")
    end = workflow.index(f"  {next_job}:\n", start) if next_job else len(workflow)
    return workflow[start:end]


class RuntimeIsolationShadowWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.validator = load_validator()

    def test_shadow_workflow_is_manual_ephemeral_and_has_no_secret_capability(self) -> None:
        workflow = self.workflow
        github_shadow = job_block(workflow, "fixed-input-shadow", "current-runner-shadow")
        current_runner_shadow = job_block(workflow, "current-runner-shadow", "compare-shadow-digests")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("pull_request_target:", workflow)
        self.assertIn("runs-on: ubuntu-latest", github_shadow)
        self.assertNotIn("runs-on: self-hosted", github_shadow)
        self.assertIn("if: ${{ inputs.include_current_runner }}", current_runner_shadow)
        self.assertIn("runs-on: self-hosted", current_runner_shadow)
        self.assertNotIn("environment:", current_runner_shadow)
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
        self.assertIn("run_isolation_shadow_fixture.py", workflow)
        self.assertIn("runtime_isolation_shadow.sha256", workflow)
        self.assertIn("Compare semantic report digests", workflow)
        self.assertIn('BINANCE_DRY_RUN: "true"', workflow)
        self.assertNotIn("python main.py", workflow)
        self.assertTrue(PORTABLE_RUNNER.is_file())

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

    def test_semantic_digest_ignores_only_deployment_identity(self) -> None:
        first = {
            "status": "ok",
            "dry_run": True,
            "run_id": "github-run",
            "run_source": "github_actions",
            "deploy_target": "vps",
            "notifications": [{"run_id": "github-run", "delivery_status": "suppressed"}],
            "side_effect_summary": {"executed_call_count": 0, "suppressed_call_count": 3},
            "buy_sell_intents": [{"symbol": "BTCUSDT", "action": "buy"}],
        }
        second = json.loads(json.dumps(first))
        second.update({"run_id": "cloud-run", "run_source": "runtime", "deploy_target": "cloud_run"})
        second["notifications"][0]["run_id"] = "cloud-run"

        self.assertEqual(
            self.validator.semantic_report_sha256(first),
            self.validator.semantic_report_sha256(second),
        )
        second["buy_sell_intents"][0]["action"] = "sell"
        self.assertNotEqual(
            self.validator.semantic_report_sha256(first),
            self.validator.semantic_report_sha256(second),
        )

    def test_host_profile_workflow_has_no_secret_or_cloud_authority(self) -> None:
        workflow = HOST_PROFILE_WORKFLOW.read_text(encoding="utf-8")
        action_lines = [line.strip() for line in workflow.splitlines() if "uses:" in line]

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("runs-on: self-hosted", workflow)
        self.assertIn("contents: read", workflow)
        self.assertNotIn("id-token: write", workflow)
        self.assertNotIn("environment:", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("BINANCE_API_KEY", workflow)
        self.assertNotIn("BINANCE_API_SECRET", workflow)
        self.assertTrue(action_lines)
        self.assertTrue(all(FULL_SHA_ACTION.fullmatch(line) for line in action_lines))


if __name__ == "__main__":
    unittest.main()
