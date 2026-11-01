from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "hide-codex-limit-comments.yml"


class HideCodexLimitCommentsWorkflowTests(unittest.TestCase):
    def test_hide_comment_job_is_time_bounded(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-latest", text)
        self.assertIn("timeout-minutes: 5", text)


if __name__ == "__main__":
    unittest.main()
