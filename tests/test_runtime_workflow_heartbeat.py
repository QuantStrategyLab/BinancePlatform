from __future__ import annotations

import datetime as dt
import os
import unittest
from unittest.mock import patch

from scripts import runtime_workflow_heartbeat as heartbeat


def _timestamp(minutes_ago: int) -> str:
    value = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    return value.isoformat().replace("+00:00", "Z")


class RuntimeWorkflowHeartbeatTests(unittest.TestCase):
    def test_repository_runs_fallback_finds_recent_runtime_success(self) -> None:
        runtime_run = {
            "id": 1,
            "run_number": 3083,
            "status": "completed",
            "conclusion": "success",
            "created_at": _timestamp(30),
            "path": ".github/workflows/main.yml",
            "html_url": "https://github.com/QuantStrategyLab/BinancePlatform/actions/runs/1",
        }
        heartbeat_run = {
            "id": 2,
            "run_number": 10,
            "status": "completed",
            "conclusion": "success",
            "created_at": _timestamp(10),
            "path": ".github/workflows/runtime-heartbeat.yml",
            "html_url": "https://github.com/QuantStrategyLab/BinancePlatform/actions/runs/2",
        }
        requested_urls: list[str] = []

        def fake_github_request(url: str, token: str) -> dict[str, object]:
            requested_urls.append(url)
            self.assertEqual(token, "token-1")
            if "/actions/workflows/main.yml/runs?" in url:
                return {"workflow_runs": []}
            if "/actions/runs?" in url:
                return {"workflow_runs": [heartbeat_run, runtime_run]}
            self.fail(f"unexpected GitHub API URL: {url}")

        with patch.dict(
            os.environ,
            {
                "GITHUB_REPOSITORY": "QuantStrategyLab/BinancePlatform",
                "GITHUB_TOKEN": "token-1",
                "RUNTIME_HEARTBEAT_WORKFLOW": "main.yml",
                "RUNTIME_HEARTBEAT_LOOKBACK_HOURS": "2.5",
                "RUNTIME_HEARTBEAT_FAIL_WORKFLOW_ON_ALERT": "true",
            },
            clear=True,
        ):
            with patch.object(heartbeat, "_github_request", fake_github_request):
                with patch.object(heartbeat, "_send_telegram") as send_telegram:
                    self.assertEqual(heartbeat.main(), 0)

        self.assertTrue(any("/actions/workflows/main.yml/runs?" in url for url in requested_urls))
        self.assertTrue(any("/actions/runs?" in url for url in requested_urls))
        send_telegram.assert_not_called()


if __name__ == "__main__":
    unittest.main()
