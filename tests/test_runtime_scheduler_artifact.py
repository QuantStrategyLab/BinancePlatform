from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from scripts import runtime_scheduler_artifact as artifact


NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _api_fixtures(*, run_overrides=None, job_overrides=None, artifact_overrides=None):
    run = {
        "id": 123456,
        "run_attempt": 1,
        "repository": {"full_name": artifact._REPOSITORY},
        "path": artifact._WORKFLOW_PATH,
        "head_branch": "main",
        "head_sha": "a" * 40,
        "event": "schedule",
        "created_at": _iso(NOW - timedelta(minutes=3)),
    }
    run.update(run_overrides or {})
    job = {"name": artifact._JOB_NAME, "status": "completed", "conclusion": "success"}
    job.update(job_overrides or {})
    artifact_row = {
        "name": "binance-scheduler-observation-123456-1",
        "expired": False,
        "size_in_bytes": 125,
        "created_at": _iso(NOW - timedelta(minutes=2)),
        "expires_at": _iso(NOW + timedelta(days=1)),
    }
    artifact_row.update(artifact_overrides or {})

    def get_json(url, token):
        if "/actions/workflows/" in url:
            return {"workflow_runs": [run]}
        if "/jobs?" in url:
            return {"jobs": [job]}
        if "/artifacts?" in url:
            return {"artifacts": [artifact_row]}
        raise AssertionError("unexpected endpoint")

    return get_json


class RuntimeSchedulerArtifactTests(unittest.TestCase):
    def test_selects_only_recent_successful_main_schedule_and_exact_attempt_artifact(self):
        selected = artifact.select_latest_observation(
            token="synthetic-token",
            api_base="https://api.github.com",
            now=NOW,
            get_json=_api_fixtures(),
        )
        self.assertEqual(selected, {
            "run_id": "123456",
            "artifact_name": "binance-scheduler-observation-123456-1",
            "artifact_created_at": _iso(NOW - timedelta(minutes=2)),
            "artifact_size": "125",
        })

    def test_rejects_wrong_run_context_or_unsuccessful_observer(self):
        cases = (
            ({"repository": {"full_name": "someone/else"}}, {}, {}),
            ({"path": ".github/workflows/other.yml"}, {}, {}),
            ({"head_branch": "runtime-production"}, {}, {}),
            ({"event": "push"}, {}, {}),
            ({}, {"conclusion": "failure"}, {}),
            ({}, {"name": "Other job"}, {}),
        )
        for run, job, row in cases:
            with self.subTest(run=run, job=job):
                selected = artifact.select_latest_observation(
                    token="synthetic-token",
                    api_base="https://api.github.com",
                    now=NOW,
                    get_json=_api_fixtures(run_overrides=run, job_overrides=job, artifact_overrides=row),
                )
                self.assertIsNone(selected)

    def test_selects_manual_observation_only_from_main(self):
        selected = artifact.select_latest_observation(
            token="synthetic-token",
            api_base="https://api.github.com",
            now=NOW,
            get_json=_api_fixtures(run_overrides={"event": "workflow_dispatch"}),
        )
        self.assertIsNotNone(selected)

    def test_rejects_expired_old_future_and_oversized_artifacts(self):
        cases = (
            {"expired": True},
            {"size_in_bytes": 1025},
            {"created_at": _iso(NOW - timedelta(minutes=76))},
            {"created_at": _iso(NOW + timedelta(seconds=1))},
            {"expires_at": _iso(NOW)},
            {"name": "binance-scheduler-observation-123456-2"},
        )
        for row in cases:
            with self.subTest(row=row):
                selected = artifact.select_latest_observation(
                    token="synthetic-token",
                    api_base="https://api.github.com",
                    now=NOW,
                    get_json=_api_fixtures(artifact_overrides=row),
                )
                self.assertIsNone(selected)

    def test_rejects_untrusted_api_base_and_bounded_api_payloads(self):
        self.assertIsNone(artifact.select_latest_observation(
            token="synthetic-token", api_base="https://attacker.invalid", now=NOW, get_json=_api_fixtures(),
        ))
        self.assertIsNone(artifact._api_base("https://api.github.com/other"))

    def test_api_does_not_follow_redirects_or_accept_oversized_metadata(self):
        redirect = HTTPError("https://api.github.com/safe", 302, "redirect", {}, None)
        with patch.object(artifact._OPENER, "open", side_effect=redirect) as open_request:
            self.assertIsNone(artifact._api_json("https://api.github.com/safe", "synthetic-token"))
            open_request.assert_called_once()

        class OversizedResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                self.requested_size = size
                return b"x" * size

        response = OversizedResponse()
        with patch.object(artifact._OPENER, "open", return_value=response):
            self.assertIsNone(artifact._api_json("https://api.github.com/safe", "synthetic-token"))
        self.assertEqual(response.requested_size, artifact._MAX_API_BODY + 1)

    def test_validates_only_fresh_two_field_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-scheduler-observation.json"
            path.write_text(json.dumps({
                "scheduler_state": "disabled",
                "observed_at": _iso(NOW - timedelta(minutes=3)),
            }), encoding="utf-8")
            result = artifact.validate_observation(
                artifact_path=str(path),
                artifact_created_at=_iso(NOW - timedelta(minutes=2)),
                artifact_size=str(path.stat().st_size),
                available="true",
                now=NOW,
            )
        self.assertEqual(result, {
            "scheduler_state": "disabled",
            "observed_at": _iso(NOW - timedelta(minutes=3)),
        })

    def test_invalid_body_metadata_staleness_future_and_symlink_become_unknown(self):
        payloads = (
            b'{"scheduler_state":"enabled","observed_at":"2026-10-08T11:58:00Z","scheduler_state":"disabled"}',
            b'{"scheduler_state":"enabled","observed_at":"2026-10-08T11:58:00Z","extra":true}',
            b'{"scheduler_state":"active","observed_at":"2026-10-08T11:58:00Z"}',
            b'{"scheduler_state":"enabled","observed_at":"2026-10-08T13:00:00Z"}',
            b'{"scheduler_state":"enabled","observed_at":"2026-10-08T10:44:00Z"}',
            b"x" * 1025,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observation.json"
            for body in payloads:
                with self.subTest(body=body[:80]):
                    path.write_bytes(body)
                    result = artifact.validate_observation(
                        artifact_path=str(path),
                        artifact_created_at=_iso(NOW - timedelta(minutes=2)),
                        artifact_size="125",
                        available="true",
                        now=NOW,
                    )
                    self.assertEqual(result["scheduler_state"], "unknown")
            self.assertEqual(artifact.validate_observation(
                artifact_path=str(path),
                artifact_created_at=_iso(NOW + timedelta(seconds=1)),
                artifact_size="125",
                available="true",
                now=NOW,
            )["scheduler_state"], "unknown")
            path.unlink()
            target = Path(directory) / "target.json"
            target.write_text('{"scheduler_state":"enabled","observed_at":"2026-10-08T11:58:00Z"}', encoding="utf-8")
            path.symlink_to(target)
            self.assertEqual(artifact.validate_observation(
                artifact_path=str(path),
                artifact_created_at=_iso(NOW - timedelta(minutes=2)),
                artifact_size="125",
                available="true",
                now=NOW,
            )["scheduler_state"], "unknown")

    def test_cli_selector_fails_closed_when_repository_or_api_unavailable(self):
        output = tempfile.NamedTemporaryFile(delete=False)
        output.close()
        try:
            with patch.dict(os.environ, {
                "GITHUB_OUTPUT": output.name,
                "GITHUB_REPOSITORY": "someone/else",
                "GITHUB_API_URL": "https://api.github.com",
                "GITHUB_TOKEN": "synthetic-token",
            }, clear=True):
                self.assertEqual(artifact.main(["select"]), 0)
            self.assertIn("scheduler_artifact_available=false", Path(output.name).read_text(encoding="utf-8"))
        finally:
            Path(output.name).unlink(missing_ok=True)

    def test_workflow_observer_is_independent_and_has_separate_concurrency(self):
        workflow = Path(__file__).resolve().parents[1] / ".github/workflows/runtime-target-lifecycle.yml"
        text = workflow.read_text(encoding="utf-8")
        observer = text.split("  observe-scheduler:", 1)[1].split("  publish:", 1)[0]
        publish = text.split("  publish:", 1)[1]
        self.assertNotIn("needs:", observer)
        self.assertIn("if: ${{ github.ref == 'refs/heads/main' }}", observer)
        self.assertIn("contents: read", observer)
        self.assertNotIn("actions: write", observer)
        self.assertIn("retention-days: 1", observer)
        self.assertIn("group: ${{ github.workflow }}-${{ github.ref_name }}-scheduler-observation", observer)
        self.assertIn("group: ${{ github.workflow }}-${{ github.ref_name }}", publish)
        self.assertNotIn("\nconcurrency:\n", text)
        self.assertIn("actions: read", publish)
        self.assertIn("actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a", observer)
        self.assertIn("actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c", publish)


if __name__ == "__main__":
    unittest.main()
