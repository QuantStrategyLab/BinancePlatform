from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import unittest
from unittest.mock import patch

from scripts import runtime_workflow_heartbeat as heartbeat


def _timestamp(minutes_ago: int) -> str:
    value = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    return value.isoformat().replace("+00:00", "Z")


class RuntimeWorkflowHeartbeatTests(unittest.TestCase):
    def test_github_request_retries_service_unavailable(self) -> None:
        class FakeResponse:
            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps({"workflow_runs": []}).encode()

        unavailable = urllib.error.HTTPError(
            "https://api.github.com/example",
            503,
            "Service Unavailable",
            {"Retry-After": "0"},
            None,
        )
        with patch.object(
            heartbeat.urllib.request,
            "urlopen",
            side_effect=[unavailable, FakeResponse()],
        ) as urlopen:
            with patch.object(heartbeat.time, "sleep") as sleep:
                result = heartbeat._github_request("https://api.github.com/example", "token-1")

        self.assertEqual(result, {"workflow_runs": []})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.0)

    def test_github_request_does_not_retry_non_transient_http_error(self) -> None:
        forbidden = urllib.error.HTTPError(
            "https://api.github.com/example",
            403,
            "Forbidden",
            {},
            None,
        )
        with patch.object(heartbeat.urllib.request, "urlopen", side_effect=forbidden) as urlopen:
            with patch.object(heartbeat.time, "sleep") as sleep:
                with self.assertRaises(urllib.error.HTTPError):
                    heartbeat._github_request("https://api.github.com/example", "token-1")

        urlopen.assert_called_once()
        sleep.assert_not_called()

    def test_github_request_stops_after_bounded_network_retries(self) -> None:
        unavailable = urllib.error.URLError("temporary DNS failure")
        with patch.object(heartbeat.urllib.request, "urlopen", side_effect=unavailable) as urlopen:
            with patch.object(heartbeat.time, "sleep") as sleep:
                with self.assertRaises(urllib.error.URLError):
                    heartbeat._github_request("https://api.github.com/example", "token-1")

        self.assertEqual(urlopen.call_count, heartbeat._GITHUB_API_MAX_ATTEMPTS)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0, 4.0])

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
