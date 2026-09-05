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


def _observed_report():
    return {
        **_report(),
        "started_at": "2026-08-30T10:00:00.123456+08:00",
        "standard_execution_permitted": False,
        "dry_run": True,
        "runtime_target": {
            "platform_id": "binance",
            "strategy_profile": "crypto_live_pool_rotation",
            "execution_mode": "dry_run",
        },
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
        self.assertNotIn("deployment", result)

    def test_disabled_target_does_not_read_storage(self) -> None:
        os.environ["RUNTIME_TARGET_ENABLED"] = "false"
        with patch.object(heartbeat, "_list_reports") as list_reports:
            result = heartbeat.assess_execution_report_heartbeat(
                dt.datetime(2026, 8, 30, tzinfo=dt.timezone.utc)
            )

        list_reports.assert_not_called()
        self.assertEqual(result["status"], "not_applicable")
        self.assertNotIn("deployment", result)

    def test_observation_uses_report_permission_and_original_run_time(self) -> None:
        now = dt.datetime(2026, 8, 30, 3, tzinfo=dt.timezone.utc)
        with (
            patch.object(heartbeat, "_list_reports", return_value=([_entry("2026-08-30T02:59:00Z")], "gs://reports")),
            patch.object(heartbeat, "_read_report", return_value=_observed_report()),
        ):
            result = heartbeat.assess_execution_report_heartbeat(now)

        self.assertEqual(result["deployment"], {
            "runtime_enabled": False,
            "scheduler_state": "unknown",
            "strategy_profile": "crypto_live_pool_rotation",
            "execution_mode": "dry_run",
            "observed_at": "2026-08-30T02:00:00Z",
        })
        self.assertEqual(result["observed_at"], "2026-08-30T03:00:00Z")

    def test_missing_invalid_or_future_run_time_omits_observation(self) -> None:
        now = dt.datetime(2026, 8, 30, 3, tzinfo=dt.timezone.utc)
        for timestamp in (None, "invalid", "2026-08-30T02:00:00", "2026-08-30T04:00:00Z"):
            with self.subTest(timestamp=timestamp):
                report = {**_observed_report(), "started_at": timestamp}
                self.assertIsNone(heartbeat._deployment_observation(report, now=now))
        self.assertIsNone(heartbeat._deployment_observation(_report(), now=now))

    def test_permission_is_strict_boolean_and_mode_does_not_use_environment(self) -> None:
        now = dt.datetime(2026, 8, 30, 3, tzinfo=dt.timezone.utc)
        os.environ["BINANCE_DRY_RUN"] = "false"
        for permission in (None, "true", 1):
            with self.subTest(permission=permission):
                report = {**_observed_report(), "standard_execution_permitted": permission}
                observed = heartbeat._deployment_observation(report, now=now)
                self.assertIsNone(observed["runtime_enabled"])
                self.assertEqual(observed["execution_mode"], "dry_run")

    def test_report_mode_fallback_and_conflict_remain_bounded(self) -> None:
        now = dt.datetime(2026, 8, 30, 3, tzinfo=dt.timezone.utc)
        report = _observed_report()
        report["runtime_target"].pop("execution_mode")
        self.assertEqual(heartbeat._deployment_observation(report, now=now)["execution_mode"], "dry_run")
        report["runtime_target"]["execution_mode"] = {}
        report["dry_run"] = None
        self.assertIsNone(heartbeat._deployment_observation(report, now=now)["execution_mode"])
        report["dry_run"] = True
        report["runtime_target"]["execution_mode"] = "live"
        self.assertIsNone(heartbeat._deployment_observation(report, now=now)["execution_mode"])
        report["runtime_target"]["strategy_profile"] = "other_profile"
        self.assertIsNone(heartbeat._deployment_observation(report, now=now))

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
