from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import runtime_scheduler_probe as probe


class RuntimeSchedulerProbeTests(unittest.TestCase):
    def test_probe_emits_only_scheduler_state_and_time_without_runtime_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "github-output.txt"
            artifact = Path(directory) / "runtime-scheduler-observation.json"
            stdout = io.StringIO()
            with (
                patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}, clear=True),
                patch.object(probe, "observe_runtime_scheduler_state", return_value="enabled"),
                redirect_stdout(stdout),
            ):
                self.assertEqual(probe.main(["--output-path", str(artifact)]), 0)

            result = json.loads(stdout.getvalue())
            self.assertEqual(set(result), {"scheduler_state", "observed_at"})
            self.assertEqual(result["scheduler_state"], "enabled")
            self.assertTrue(result["observed_at"].endswith("Z"))
            datetime.fromisoformat(result["observed_at"].replace("Z", "+00:00"))
            self.assertEqual(json.loads(artifact.read_text(encoding="utf-8")), result)
            self.assertEqual(
                output.read_text(encoding="utf-8").splitlines(),
                [f"scheduler_state=enabled", f"observed_at={result['observed_at']}"],
            )

    def test_probe_suppresses_host_errors_and_invalid_states(self):
        cases = (
            RuntimeError("private cron contents must not be printed"),
            "unexpected-host-state",
        )
        for value in cases:
            with self.subTest(value=type(value).__name__), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "github-output.txt"
                stdout = io.StringIO()
                context = (
                    patch.object(probe, "observe_runtime_scheduler_state", side_effect=value)
                    if isinstance(value, BaseException)
                    else patch.object(probe, "observe_runtime_scheduler_state", return_value=value)
                )
                with (
                    patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}, clear=True),
                    context,
                    redirect_stdout(stdout),
                ):
                    self.assertEqual(probe.main([]), 0)

                result = json.loads(stdout.getvalue())
                self.assertEqual(result["scheduler_state"], "unknown")
                self.assertEqual(set(result), {"scheduler_state", "observed_at"})
                self.assertNotIn("private cron contents", stdout.getvalue())
                self.assertEqual(output.read_text(encoding="utf-8").splitlines()[0], "scheduler_state=unknown")

    def test_failed_github_output_cannot_leave_an_enabled_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "runtime-scheduler-observation.json"
            missing_parent_output = Path(directory) / "missing" / "github-output.txt"
            with (
                patch.dict(os.environ, {"GITHUB_OUTPUT": str(missing_parent_output)}, clear=True),
                patch.object(probe, "observe_runtime_scheduler_state", return_value="enabled"),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(probe.main(["--output-path", str(artifact)]), 0)
            self.assertEqual(json.loads(artifact.read_text(encoding="utf-8"))["scheduler_state"], "unknown")
