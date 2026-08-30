from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import urllib.error
import unittest
from unittest.mock import patch

from scripts import runtime_workflow_heartbeat as heartbeat


def _timestamp(minutes_ago: int) -> str:
    value = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
    return value.isoformat().replace("+00:00", "Z")


def _runtime_run(
    *,
    created_at: dt.datetime,
    status: str = "completed",
    conclusion: str | None = "success",
    run_number: int = 1,
) -> dict[str, object]:
    return {
        "id": run_number,
        "run_number": run_number,
        "status": status,
        "conclusion": conclusion,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "html_url": f"https://github.com/QuantStrategyLab/BinancePlatform/actions/runs/{run_number}",
    }


class RuntimeWorkflowHeartbeatTests(unittest.TestCase):
    def test_lifecycle_workflow_has_no_broker_authority_and_uses_pinned_actions(self) -> None:
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "runtime-target-lifecycle.yml"
        ).read_text(encoding="utf-8")
        action_lines = [line.strip() for line in workflow.splitlines() if "uses:" in line]

        self.assertNotIn("BINANCE_API_KEY", workflow)
        self.assertNotIn("BINANCE_API_SECRET", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("source-id: binance.runtime-target-lifecycle", workflow)
        self.assertIn("EXECUTION_EVIDENCE_SYNC_TOKEN: ${{ secrets.EXECUTION_EVIDENCE_SYNC_TOKEN }}", workflow)
        self.assertTrue(action_lines)
        self.assertTrue(all("@" in line and len(line.rsplit("@", 1)[1].split()[0]) == 40 for line in action_lines))

    def test_disabled_target_skips_github_api_lookup_and_writes_assessment(self) -> None:
        with patch.dict(
            os.environ,
            {"RUNTIME_TARGET_ENABLED": "false"},
            clear=True,
        ):
            with patch.object(heartbeat, "_github_request") as request:
                self.assertEqual(heartbeat.main(), 0)

        request.assert_not_called()

    def test_github_api_outage_is_reported_as_unavailable_when_non_failing(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "token-1",
                "RUNTIME_HEARTBEAT_FAIL_WORKFLOW_ON_ALERT": "false",
            },
            clear=True,
        ):
            with patch.object(heartbeat, "_list_runtime_runs", side_effect=OSError("network unavailable")):
                self.assertEqual(heartbeat.main(), 0)

    def test_single_missing_dispatch_is_deferred_with_auditable_assessment(self) -> None:
        now = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.timezone.utc)
        result = heartbeat._assess_runtime_heartbeat(
            runs=[_runtime_run(created_at=now - dt.timedelta(hours=1.5))],
            now=now,
            lookback_hours=1.0,
            expected_interval_hours=1.0,
            max_consecutive_misses=2,
        )

        self.assertEqual(result["schema"], "qsl.runtime_heartbeat_assessment.v1")
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "awaiting_dispatch_confirmation")
        self.assertEqual(result["consecutive_misses"], 1)
        self.assertEqual(result["query"]["runs_returned"], 1)

    def test_missing_dispatches_escalate_only_after_threshold(self) -> None:
        now = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.timezone.utc)
        result = heartbeat._assess_runtime_heartbeat(
            runs=[_runtime_run(created_at=now - dt.timedelta(hours=2.1))],
            now=now,
            lookback_hours=1.0,
            expected_interval_hours=1.0,
            max_consecutive_misses=2,
        )

        self.assertEqual(result["status"], "alert")
        self.assertEqual(result["reason"], "consecutive_runtime_dispatches_missing")
        self.assertEqual(result["consecutive_misses"], 2)

    def test_latest_completed_runtime_failure_remains_an_immediate_alert(self) -> None:
        now = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.timezone.utc)
        result = heartbeat._assess_runtime_heartbeat(
            runs=[
                _runtime_run(
                    created_at=now - dt.timedelta(minutes=15),
                    conclusion="failure",
                    run_number=2,
                ),
                _runtime_run(created_at=now - dt.timedelta(hours=1), run_number=1),
            ],
            now=now,
            lookback_hours=1.0,
            expected_interval_hours=1.0,
            max_consecutive_misses=2,
        )

        self.assertEqual(result["status"], "alert")
        self.assertEqual(result["reason"], "latest_runtime_completed_unsuccessfully")

    def test_recent_pending_dispatch_is_parked_not_alerted(self) -> None:
        now = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.timezone.utc)
        result = heartbeat._assess_runtime_heartbeat(
            runs=[
                _runtime_run(
                    created_at=now - dt.timedelta(minutes=10),
                    status="in_progress",
                    conclusion=None,
                    run_number=2,
                ),
                _runtime_run(created_at=now - dt.timedelta(hours=2), run_number=1),
            ],
            now=now,
            lookback_hours=1.0,
            expected_interval_hours=1.0,
            max_consecutive_misses=2,
        )

        self.assertEqual(result["status"], "parked")
        self.assertEqual(result["reason"], "runtime_dispatch_pending")

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

    def test_send_telegram_rejects_api_ok_false(self) -> None:
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b'{"ok":false,"description":"rejected"}'

        with patch.dict(
            os.environ,
            {"TG_TOKEN": "token-1", "GLOBAL_TELEGRAM_CHAT_ID": "chat-1"},
            clear=True,
        ):
            with patch.object(heartbeat.urllib.request, "urlopen", return_value=FakeResponse()):
                self.assertFalse(heartbeat._send_telegram("heartbeat failed"))

    def test_send_telegram_prefers_qsl_global_telegram_chat_id(self) -> None:
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b'{"ok":true}'

        observed = {}

        def fake_urlopen(request, timeout):
            observed["body"] = request.data.decode("utf-8")
            observed["timeout"] = timeout
            return FakeResponse()

        with patch.dict(
            os.environ,
            {
                "TG_TOKEN": "token-1",
                "QSL_GLOBAL_TELEGRAM_CHAT_ID": "qsl-chat-id",
                "GLOBAL_TELEGRAM_CHAT_ID": "legacy-chat-id",
            },
            clear=True,
        ):
            with patch.object(heartbeat.urllib.request, "urlopen", side_effect=fake_urlopen):
                self.assertTrue(heartbeat._send_telegram("heartbeat failed"))

        self.assertIn("chat_id=qsl-chat-id", observed["body"])
        self.assertNotIn("legacy-chat-id", observed["body"])


if __name__ == "__main__":
    unittest.main()
