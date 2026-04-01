from __future__ import annotations

import unittest

from scripts.render_monthly_ai_review import build_full_review_markdown, render_secondary_review_markdown


class RenderMonthlyAiReviewTests(unittest.TestCase):
    def test_render_secondary_review_markdown_includes_actions_and_flags(self) -> None:
        payload = {
            "provider_display_name": "GPT Secondary Review",
            "verdict": "partial_agree",
            "risk_level": "medium",
            "production_recommendation": "research_only",
            "summary": "Runtime looked mostly healthy but some gates still need confirmation.",
            "key_findings": ["No-trade reasons appear plausible but should stay visible."],
            "recommended_actions": [
                {
                    "title": "Add one more gating summary table",
                    "owner_repo": "BinancePlatform",
                    "risk_level": "low",
                    "auto_pr_safe": True,
                    "experiment_only": False,
                    "summary": "Make monthly execution review easier to verify.",
                }
            ],
            "follow_up_checks": ["Watch whether the same gate dominates again next month."],
        }

        markdown = render_secondary_review_markdown(payload)

        self.assertIn("## Secondary Review (GPT Secondary Review)", markdown)
        self.assertIn("`partial_agree`", markdown)
        self.assertIn("auto-pr-safe", markdown)
        self.assertIn("Watch whether the same gate", markdown)

    def test_build_full_review_markdown_includes_primary_and_secondary_sections(self) -> None:
        markdown = build_full_review_markdown(
            "## English\nPrimary review",
            primary_title="Claude Primary Review",
            secondary_review_payload={
                "provider_display_name": "GPT Secondary Review",
                "verdict": "agree",
                "risk_level": "low",
                "production_recommendation": "keep_production_as_is",
                "summary": "Looks consistent.",
                "key_findings": ["No blocking issue found."],
                "recommended_actions": [],
                "follow_up_checks": [],
            },
        )

        self.assertIn("## Claude Primary Review", markdown)
        self.assertIn("## Secondary Review (GPT Secondary Review)", markdown)
        self.assertIn("## English", markdown)


if __name__ == "__main__":
    unittest.main()
