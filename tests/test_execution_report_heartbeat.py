from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from scripts import execution_report_heartbeat as heartbeat


def _entry(updated: str, uri: str = "gs://reports/binance/crypto/2026-08/report.json"):
    return {"url": uri, "metadata": {"updated": updated}}


def _report(status: str = "ok"):
    return {
        "platform": "binance",
        "status": status,
        "strategy_profile": "crypto_live_pool_rotation",
        "service_name": "binance-platform",
        "errors": [],
    }


class ExecutionReportHeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._environment = patch.dict(os.environ, {}, clear=True)
        self._environment.start()
        self.addCleanup(self._environment.stop)
        os.environ.update(
            {
                "RUNTIME_HEARTBEAT_GCS_URI": "gs://reports",
                "RUNTIME_HEARTBEAT_REPORT_PLATFORM": "binance",
                "RUNTIME_HEARTBEAT_STRATEGY_PROFILE": "crypto_live_pool_rotation",
                "RUNTIME_HEARTBEAT_SERVICE_NAME": "binance-platform",
                "RUNTIME_HEARTBEAT_LOOKBACK_HOURS": "2.5",
                "RUNTIME_TARGET_ENABLED": "true",
            }
        )

    def test_healthy_when_recent_matching_report_is_accepted(self) -> None:
        now = dt.datetime(2026, 8, 30, 3, tzinfo=dt.timezone.utc)
        with (
            patch.object(
                heartbeat,
                "_list_reports",
                return_value=([_entry("2026-08-30T02:00:00Z")], "gs://reports"),
            ),
            patch.object(heartbeat, "_read_report", return_value=_report()),
        ):
            result = heartbeat.assess_execution_report_heartbeat(now)

        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["reason"], "status=ok")

    def test_disabled_target_does_not_read_storage(self) -> None:
        os.environ["RUNTIME_TARGET_ENABLED"] = "false"
        with patch.object(heartbeat, "_list_reports") as list_reports:
            result = heartbeat.assess_execution_report_heartbeat(
                dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc)
            )

        list_reports.assert_not_called()
        self.assertEqual(result["status"], "not_applicable")

    def test_storage_failure_is_unavailable(self) -> None:
        with patch.object(heartbeat, "_list_reports", side_effect=RuntimeError("storage denied")):
            result = heartbeat.assess_execution_report_heartbeat(
                dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc)
            )

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "storage denied")

    def test_malformed_target_control_is_unavailable(self) -> None:
        os.environ["RUNTIME_TARGET_ENABLED"] = "perhaps"
        with patch.object(heartbeat, "_list_reports") as list_reports:
            result = heartbeat.assess_execution_report_heartbeat(
                dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc)
            )

        list_reports.assert_not_called()
        self.assertEqual(result["status"], "unavailable")

    def test_list_reports_uses_scoped_month_glob(self) -> None:
        commands = []

        def fake_run(command):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps([]), stderr="")

        with patch.object(heartbeat, "_run_gcloud", side_effect=fake_run):
            heartbeat._list_reports(
                since=dt.datetime(2026, 8, 31, 23, tzinfo=dt.timezone.utc),
                now=dt.datetime(2026, 9, 1, 1, tzinfo=dt.timezone.utc),
            )

        globs = [command[4] for command in commands]
        self.assertEqual(
            globs,
            [
                "gs://reports/binance/crypto_live_pool_rotation/**/2026-08/*.json",
                "gs://reports/binance/crypto_live_pool_rotation/**/2026-09/*.json",
            ],
        )
