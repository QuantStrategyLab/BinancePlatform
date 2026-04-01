from __future__ import annotations

import unittest

from scripts.post_monthly_ai_review_comment import (
    COMMENT_MARKER,
    build_comment_body,
    extract_latest_assistant_text,
)


class PostMonthlyAiReviewCommentTests(unittest.TestCase):
    def test_extract_latest_assistant_text_returns_last_text_reply(self) -> None:
        execution_log = [
            {"type": "system", "subtype": "init"},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Working on it."},
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}},
                        {"type": "text", "text": "## English\nFinal review\n\n## 中文\n最终结论"},
                    ]
                },
            },
            {"type": "result", "subtype": "success"},
        ]

        review_text = extract_latest_assistant_text(execution_log)

        self.assertEqual(review_text, "## English\nFinal review\n\n## 中文\n最终结论")

    def test_build_comment_body_includes_marker_and_run_link(self) -> None:
        body = build_comment_body("Review content", "https://github.com/example/repo/actions/runs/1")

        self.assertIn(COMMENT_MARKER, body)
        self.assertIn("## Claude Monthly Review", body)
        self.assertIn("Review content", body)
        self.assertIn("actions/runs/1", body)


if __name__ == "__main__":
    unittest.main()
