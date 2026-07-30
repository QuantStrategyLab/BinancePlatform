from pathlib import Path
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "main.yml"


class RuntimeTriggerWorkflowTest(unittest.TestCase):
    def test_runtime_workflow_is_dispatch_only(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("workflow_run:", workflow)
        self.assertNotIn("github.event.workflow_run", workflow)
