from __future__ import annotations

import copy
import datetime as dt
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import emit_digest_candidates as producer

NOW = dt.datetime(2026, 10, 14, 6, tzinfo=dt.timezone.utc)
TARGET = {
    "platform_id": "binance", "strategy_profile": "synthetic_core",
    "service_name": "synthetic-service", "account_scope": "synthetic-account",
    "account_selector": ["synthetic-selector"], "deployment_selector": "synthetic-host",
    "dry_run_only": False,
}


def report():
    return {
        "schema_version": "runtime_report.v1", "platform": "binance",
        "runtime_target": copy.deepcopy(TARGET), "strategy_profile": "synthetic_core",
        "service_name": "synthetic-service", "dry_run": False, "status": "ok",
        "started_at": "2026-10-14T02:00:00Z", "fill_count": 0,
        "total_equity_usdt": 100, "log_lines": ["synthetic-private-message"],
        "execution_receipt_observation": {"filled_count": 8},
    }


def env():
    return {
        "BINANCE_DIGEST_CANDIDATES_ENABLED": "true", "RUNTIME_TARGET_JSON": json.dumps(TARGET),
        "BINANCE_DIGEST_OPAQUE_ACCOUNT_UID": "synthetic-uid",
        "BINANCE_DIGEST_TARGET_ID": "synthetic-central-target",
        "BINANCE_DIGEST_STORE_TARGET": "synthetic-slot",
        "DIGEST_CANDIDATES_GCS_URI": "gs://synthetic-store/candidates",
    }


def project(value):
    return producer.project_digest_candidates(value, expected_target=TARGET,
        opaque_account_uid="synthetic-uid", target_id="synthetic-central-target", now=NOW)


class DigestCandidatesTest(unittest.TestCase):
    def test_source_date_retained_counts_and_financial_fields_stay_unknown(self):
        payload = project(report())
        row = payload["runs"][0]
        self.assertEqual(row["business_day"], "2026-10-13")
        self.assertTrue(row["actually_ran"])
        self.assertEqual(row["target_id"], "synthetic-central-target")
        self.assertEqual(row["execution_status"], "execution_unknown")
        self.assertEqual(row["status"], "alert")
        for field in ("fill_count", "order_count", "cycle_count"):
            self.assertIsNone(row[field])
            self.assertEqual(row["field_status"][field], "counts_unknown")
        for field in ("equity", "equity_usd", "holdings", "account_scope", "log_lines", "runtime_target"):
            self.assertNotIn(field, row)
        self.assertNotIn("synthetic-private-message", json.dumps(payload))

    def test_every_source_identity_dimension_rejects_mismatch(self):
        for key in TARGET:
            with self.subTest(key=key):
                source = report()
                source["runtime_target"][key] = ["other"] if key == "account_selector" else (True if key == "dry_run_only" else "other")
                with self.assertRaises(producer.CandidateBlocked):
                    project(source)
        for key in ("platform", "strategy_profile", "service_name", "dry_run"):
            source = report()
            source[key] = True if key == "dry_run" else "other"
            with self.assertRaises(producer.CandidateBlocked):
                project(source)

    def test_missing_identity_never_derived_from_account_labels(self):
        with self.assertRaisesRegex(producer.CandidateBlocked, "candidate_identity_missing_or_invalid"):
            producer.project_digest_candidates(report(), expected_target=TARGET,
                opaque_account_uid="", target_id="", now=NOW)

    def test_future_stale_naive_or_missing_dates_reject(self):
        for value in ("2026-10-14T07:00:00Z", "2026-10-12T00:00:00Z", "2026-10-14T01:00:00", None):
            source = report()
            source["started_at"] = value
            with self.assertRaisesRegex(producer.CandidateBlocked, "candidate_source_time_invalid"):
                project(source)

    def test_error_aborted_remain_alerts_unfinished_source_rejects(self):
        for status in ("error", "aborted"):
            source = report()
            source["status"] = status
            self.assertEqual(project(source)["runs"][0]["execution_status"], "execution_alert")
        for status in ("started", "", None):
            source = report()
            source["status"] = status
            with self.assertRaises(producer.CandidateBlocked):
                project(source)

    def test_existing_source_failure_stays_alert_without_copying_error_text(self):
        for key, value in (("errors", ["synthetic-private-error"]),
                           ("error_summary", {"errors": [{"message": "synthetic-private-error"}]}),
                           ("execution_blocked_reason", "synthetic-private-error")):
            source = report()
            source[key] = value
            payload = project(source)
            self.assertEqual(payload["runs"][0]["execution_status"], "execution_alert")
            self.assertEqual(payload["runs"][0]["reason_code"], "binance_source_cycle_alert")
            self.assertNotIn("synthetic-private-error", json.dumps(payload))

    def test_default_off_never_reads_or_writes(self):
        with patch.object(producer.heartbeat, "_list_reports") as read, patch.object(producer, "_upload") as upload:
            self.assertEqual(producer.emit_digest_candidates({}), {"status": "disabled", "uploaded": False})
            read.assert_not_called()
            upload.assert_not_called()

    def test_bad_enable_or_missing_mapping_stops_before_cloud(self):
        cases = [{"BINANCE_DIGEST_CANDIDATES_ENABLED": "TRUE"}]
        for key in ("RUNTIME_TARGET_JSON", "BINANCE_DIGEST_OPAQUE_ACCOUNT_UID", "BINANCE_DIGEST_TARGET_ID", "BINANCE_DIGEST_STORE_TARGET", "DIGEST_CANDIDATES_GCS_URI"):
            case = env()
            del case[key]
            cases.append(case)
        with patch.object(producer.heartbeat, "_list_reports") as read, patch.object(producer, "_upload") as upload:
            for case in cases:
                with self.assertRaises(producer.CandidateBlocked):
                    producer.emit_digest_candidates(case, now=NOW)
            read.assert_not_called()
            upload.assert_not_called()

    def _emit(self, upload):
        entries = [{"url": "gs://synthetic-reports/binance/synthetic_core/host/2026-10/report.json",
                    "metadata": {"updated": "2026-10-14T02:01:00Z"}}]
        with patch.dict(os.environ, {}, clear=True), patch.object(producer.heartbeat, "_list_reports", return_value=(entries, "gs://synthetic-reports")), patch.object(producer.heartbeat, "_read_report", return_value=report()), patch.object(producer, "_upload", upload):
            return producer.emit_digest_candidates(env(), now=NOW)

    def test_upload_once_private_single_day_convention_and_cleanup(self):
        saved = []
        def upload(path, destination):
            saved.append(path)
            self.assertEqual(destination, "gs://synthetic-store/candidates/binance/2026-10-13/synthetic-slot.json")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["runs"][0]["target_id"], "synthetic-central-target")
        self.assertTrue(self._emit(upload)["uploaded"])
        self.assertEqual(len(saved), 1)
        self.assertFalse(saved[0].exists())

    def test_unknown_upload_result_stops_without_retry_or_private_output(self):
        upload = Mock(side_effect=TimeoutError("synthetic-private-url?credential=secret"))
        with self.assertRaisesRegex(producer.CandidateBlocked, "candidate_store_write_failed_or_unknown"):
            self._emit(upload)
        self.assertEqual(upload.call_count, 1)
        output = io.StringIO()
        with patch.object(producer, "emit_digest_candidates", side_effect=RuntimeError("synthetic-private-token")), redirect_stdout(output):
            self.assertEqual(producer.main(), 1)
        self.assertNotIn("synthetic-private-token", output.getvalue())
        self.assertIsNone(json.loads(output.getvalue())["uploaded"])
        output = io.StringIO()
        with patch.object(producer, "emit_digest_candidates", side_effect=producer.CandidateBlocked("candidate_store_write_failed_or_unknown")), redirect_stdout(output):
            self.assertEqual(producer.main(), 1)
        self.assertIsNone(json.loads(output.getvalue())["uploaded"])

    def test_mismatched_latest_source_does_not_fallback_to_old_success(self):
        entries = [{"url": "gs://other-store/private.json", "metadata": {"updated": "2026-10-14T02:01:00Z"}}]
        with patch.dict(os.environ, {}, clear=True), patch.object(producer.heartbeat, "_list_reports", return_value=(entries, "gs://synthetic-reports")), patch.object(producer.heartbeat, "_read_report") as read, patch.object(producer, "_upload") as upload:
            with self.assertRaisesRegex(producer.CandidateBlocked, "candidate_source_locator_mismatch"):
                producer.emit_digest_candidates(env(), now=NOW)
            read.assert_not_called()
            upload.assert_not_called()

    def test_sdk_write_retry_is_explicitly_disabled(self):
        # Import-free fake of the existing cloud dependency; no network/ADC.
        import sys
        blob = Mock()
        storage = Mock()
        storage.Blob.from_string.return_value = blob
        with patch.dict(sys.modules, {"google.cloud.storage": storage, "google.cloud": Mock(storage=storage)}):
            producer._upload(Path("synthetic.json"), "gs://synthetic/destination")
        self.assertIsNone(blob.upload_from_filename.call_args.kwargs["retry"])

    def test_workflow_opt_in_preserves_original_monitor_outcome_and_permissions(self):
        text = (Path(__file__).parents[1] / ".github/workflows/runtime-target-lifecycle.yml").read_text()
        step = text.split("- name: Project and store private digest candidate (opt-in)", 1)[1]
        self.assertIn("vars.BINANCE_DIGEST_CANDIDATES_ENABLED == 'true'", step)
        self.assertIn("continue-on-error: true", step)
        self.assertIn("secrets.BINANCE_DIGEST_OPAQUE_ACCOUNT_UID", step)
        self.assertIn("secrets.DIGEST_CANDIDATES_GCS_URI", step)
        self.assertIn("run: uv run --no-sync python scripts/emit_digest_candidates.py", step)
        self.assertNotIn("run: python scripts/emit_digest_candidates.py", step)
        for forbidden in ("TG_TOKEN", "BINANCE_API_KEY", "workflow_dispatch", "id-token", "upload-artifact"):
            self.assertNotIn(forbidden, step)


if __name__ == "__main__":
    unittest.main()
