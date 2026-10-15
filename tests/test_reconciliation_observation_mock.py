"""Synthetic-only collector contract and isolation checks (no runtime imports)."""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from application.reconciliation_observation import collect_mock_observation


FIXTURES = Path(__file__).parent / "fixtures/reconciliation_observation"
NOW = datetime(2000, 1, 1, 0, 2, tzinfo=timezone.utc)


class MockObservationTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((FIXTURES / "config.json").read_text())
        self.fixture = json.loads((FIXTURES / "matched.json").read_text())
        self.reports = []

    def collect(self):
        return collect_mock_observation(
            self.config, self.fixture, persist_report=self.reports.append, now=NOW,
        )

    def test_complete_match_is_redacted_and_observation_only(self):
        original = copy.deepcopy(self.fixture)
        payload = self.collect()
        row = payload["observations"][0]
        self.assertEqual((payload["data_status"], row["comparison_status"], row["coverage"]),
                         ("ready", "matched", "complete"))
        self.assertEqual(row["evidence_sample_count"], 1)
        self.assertEqual(payload["policy"], {
            "purpose": "observation_only", "no_order": True, "execution_authority_granted": False,
        })
        self.assertEqual(len(self.reports), 1)
        self.assertEqual(self.fixture, original)
        for private in ("binance-paper-mock-001", "balances", "ledger_entries", "candidate_sha256"):
            self.assertNotIn(private, json.dumps(payload))

    def test_difference_is_ready_and_never_repairs_reference(self):
        self.fixture["broker_before"]["balances"]["USDT"] = "101"
        self.fixture["broker_after"]["balances"]["USDT"] = "101"
        payload = self.collect()
        self.assertEqual(payload["data_status"], "ready")
        self.assertEqual(payload["observations"][0]["comparison_status"], "mismatched")
        self.assertEqual(self.fixture["reference"]["balances"]["USDT"], "100")

    def test_missing_required_inputs_are_incomparable_without_fake_values(self):
        for field, reason in (("reference", "reference_unavailable"), ("ledger_before", "ledger_unavailable")):
            with self.subTest(field=field):
                fixture = copy.deepcopy(self.fixture)
                fixture[field] = None
                payload = collect_mock_observation(self.config, fixture, persist_report=self.reports.append, now=NOW)
                row = payload["observations"][0]
                self.assertEqual(row["comparison_status"], "incomparable")
                self.assertEqual(row["evidence_sample_count"], 0)
                self.assertIn(reason, row["reason_codes"])
                self.assertNotEqual(payload["data_status"], "ready")
                self.assertNotEqual(row["coverage"], "complete")
                self.assertIsNone(self.reports[-1]["inputs"][field])

    def test_busy_partial_identity_and_time_failures(self):
        changes = [
            ("broker_after", "balances", {"USDT": "102", "BTC": "0.01"}, "broker_changed"),
            ("ledger_after", "version", "synthetic-ledger-v2", "ledger_changed"),
            ("broker_before", "complete", False, "broker_snapshot_incomplete"),
            ("broker_before", "account_id", "synthetic-other-account", "account_identity_mismatch"),
            ("reference", "scope_version", "other-synthetic-scope", "reference_binding_invalid"),
            ("broker_before", "observed_at", "1999-12-31T23:00:00Z", "input_stale"),
            ("broker_before", "observed_at", "2000-01-01T00:10:00Z", "input_future"),
        ]
        for section, key, value, reason in changes:
            with self.subTest(reason=reason):
                fixture = copy.deepcopy(self.fixture)
                fixture[section][key] = value
                payload = collect_mock_observation(self.config, fixture, persist_report=self.reports.append, now=NOW)
                self.assertEqual(payload["observations"][0]["comparison_status"], "incomparable")
                self.assertIn(reason, payload["errors"])

    def test_missing_report_cannot_be_ready_and_errors_are_sanitized(self):
        def fail(_):
            raise OSError("synthetic sensitive provider response must not escape")
        payload = collect_mock_observation(self.config, self.fixture, persist_report=fail, now=NOW)
        self.assertEqual(payload["errors"], ["report_persistence_failed"])
        self.assertIsNone(payload["observations"][0]["report_id"])
        self.assertEqual(payload["observations"][0]["comparison_status"], "incomparable")
        self.assertNotIn("sensitive", json.dumps(payload))

    def test_non_mock_config_is_rejected(self):
        for field, value in (("mode", "live"), ("environment", "live"), ("expected_account_id", "12345678")):
            with self.subTest(field=field):
                config = {**self.config, field: value}
                with self.assertRaisesRegex(ValueError, "mock_config_invalid"):
                    collect_mock_observation(config, self.fixture, persist_report=self.reports.append, now=NOW)
        self.assertEqual(self.reports, [])

    def test_entrypoint_has_no_network_or_trading_initialization(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            code = '''
import importlib.abc, runpy, socket, sys
class NoRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'binance', 'google', 'quant_platform_kit', 'main', 'infra', 'runtime_support', 'live_services'} or fullname in {'application.broker_reconciliation', 'application.reconciliation_recovery'}:
            raise AssertionError('runtime import forbidden')
sys.meta_path.insert(0, NoRuntime())
def blocked(*args, **kwargs):
    raise AssertionError('network forbidden')
socket.socket.connect = blocked
socket.create_connection = blocked
sys.argv = ['collect_reconciliation_observation.py', '--config', sys.argv[1], '--fixture', sys.argv[2], '--report-dir', sys.argv[3], '--now', '2000-01-01T00:02:00Z']
runpy.run_path('scripts/collect_reconciliation_observation.py', run_name='__main__')
'''
            result = subprocess.run([sys.executable, "-c", code, str(FIXTURES / "config.json"),
                                     str(FIXTURES / "matched.json"), directory], cwd=root,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)["data_status"], "ready")
            report = Path(directory) / "fixture-report-001.json"
            original = report.read_bytes()
            # Create-only reports: a replay cannot overwrite even synthetic evidence.
            result = subprocess.run([sys.executable, "-c", code, str(FIXTURES / "config.json"),
                                     str(FIXTURES / "matched.json"), directory], cwd=root,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)["errors"], ["report_persistence_failed"])
            self.assertEqual(report.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
