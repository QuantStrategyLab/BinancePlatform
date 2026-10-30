import contextlib
import hashlib
import io
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from scripts.project_binance_authority import main, project

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def payload():
    return {
        "decision": "APPROVE", "authority_scope": "LIVE", "continuous_inputs_allowed": True,
        "runtime_target": {"platform_id": "binance", "strategy_profile": "synthetic-profile",
                           "account_scope": "private-account-sentinel", "account_selector": ["private-selector-sentinel"],
                           "deployment_selector": "private-deployment-sentinel"},
        "strategy_revision": "1" * 40, "runner_revision": "2" * 40, "config_sha256": "3" * 64,
        "mandate": {"mandate_id": "private-mandate-sentinel", "mandate_version": "v1",
                    "effective_at": "2026-10-30T11:00:00Z", "expires_at": "2026-10-30T13:00:00Z",
                    "max_snapshot_age_seconds": 300, "effective_exposure_cap": 0.5,
                    "product_caps": 1, "nominal_caps": 1, "product_leverage_factors": 1,
                    "allowed_nonzero_assets": ["BTCUSDT"], "loss_budget": 10},
    }


def observe(value, *, now=NOW):
    raw = json.dumps(value, separators=(",", ":"))
    return project(raw, hashlib.sha256(raw.encode()).hexdigest(), "4" * 40, now_utc=now)


class AuthorityTimingTests(unittest.TestCase):
    def test_fixed_interval_boundaries_and_invalid_range(self):
        for effective, expires, expected in (
            ("2026-10-30T11:00:00Z", "2026-10-30T13:00:00Z", "within_declared_fixed_interval"),
            ("2026-10-30T12:00:00Z", "2026-10-30T13:00:00Z", "within_declared_fixed_interval"),
            ("2026-10-30T12:00:01Z", "2026-10-30T13:00:00Z", "not_yet_effective"),
            ("2026-10-30T11:00:00Z", "2026-10-30T12:00:00Z", "expired"),
            ("2026-10-30T11:00:00Z", "2026-10-30T11:59:59Z", "expired"),
            ("2026-10-30T12:00:00Z", "2026-10-30T12:00:00Z", "invalid_interval"),
            ("2026-10-30T13:00:00Z", "2026-10-30T12:00:00Z", "invalid_interval"),
        ):
            value = payload()
            value["mandate"].update(effective_at=effective, expires_at=expires)
            with self.subTest(effective=effective, expires=expires):
                result = observe(value)
                self.assertEqual(result["mandate_timing"], expected)
                self.assertEqual(result["observed_at"], "2026-10-30T12:00:00Z")
                self.assertEqual(result["observation_version"], "authority-observation/v1")
                self.assertEqual(result["target_comparison"], "unavailable")

    def test_until_revoked_does_not_claim_revocation_status(self):
        value = payload()
        value["mandate"].update(validity_mode="until_revoked", expires_at=None)
        self.assertEqual(observe(value)["mandate_timing"], "after_effective_revocation_unknown")
        value["mandate"]["effective_at"] = "2026-10-30T12:00:01Z"
        self.assertEqual(observe(value)["mandate_timing"], "not_yet_effective")

    def test_existing_projection_and_private_fields_remain_safe(self):
        result = observe(payload())
        self.assertEqual(result["status"], "projected")
        self.assertEqual(result["strategy_revision"], "1" * 40)
        self.assertEqual(result["runner_revision"], "2" * 40)
        self.assertEqual(result["config_sha256"], "3" * 64)
        self.assertEqual(result["authority_source_revision"], "4" * 40)
        self.assertEqual(len(result["authority_sha256"]), 64)
        self.assertNotIn("private-", json.dumps(result))
        for invalid_now in (datetime(2026, 10, 30), "2026-10-30T12:00:00Z"):
            with self.subTest(now=invalid_now), self.assertRaises(ValueError):
                observe(payload(), now=invalid_now)

    def test_valid_cli_reports_only_fixed_metadata(self):
        raw = json.dumps(payload(), separators=(",", ":"))
        env = {"BINANCE_RISK_AUTHORITY_JSON": raw,
               "BINANCE_RISK_AUTHORITY_SHA256": hashlib.sha256(raw.encode()).hexdigest(),
               "BINANCE_RISK_AUTHORITY_SOURCE_REVISION": "4" * 40}
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(output):
            self.assertEqual(main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(set(result), {"status", "strategy_revision", "runner_revision", "config_sha256",
                                      "authority_sha256", "authority_source_revision", "observation_version",
                                      "observed_at", "mandate_timing", "target_comparison"})
        self.assertEqual(result["target_comparison"], "unavailable")
        self.assertNotIn("private-", output.getvalue())
        self.assertNotIn("synthetic-profile", output.getvalue())

    def test_digest_mismatch_and_malformed_timestamps_fail_closed(self):
        value = payload()
        raw = json.dumps(value)
        with self.assertRaises(ValueError):
            project(raw, "0" * 64, "4" * 40, now_utc=NOW)
        for timestamp in ("not-a-time", "2026-10-30T12:00:00", "2026-10-30T12:00:00+08:00"):
            value = payload()
            value["mandate"]["effective_at"] = timestamp
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                observe(value)

    def test_cli_exception_is_redacted(self):
        raw = '{"private-secret-sentinel": true}'
        env = {"BINANCE_RISK_AUTHORITY_JSON": raw,
               "BINANCE_RISK_AUTHORITY_SHA256": hashlib.sha256(raw.encode()).hexdigest(),
               "BINANCE_RISK_AUTHORITY_SOURCE_REVISION": "4" * 40}
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(output):
            self.assertEqual(main(), 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "failed", "reason_code": "authority_projection_invalid"})
        self.assertNotIn("private-secret-sentinel", output.getvalue())

        raw = json.dumps(payload(), separators=(",", ":"))
        env["BINANCE_RISK_AUTHORITY_JSON"] = raw
        env["BINANCE_RISK_AUTHORITY_SHA256"] = hashlib.sha256(raw.encode()).hexdigest()
        output = io.StringIO()
        errors = io.StringIO()
        with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(output), \
             contextlib.redirect_stderr(errors), patch("scripts.project_binance_authority._timing_observation",
                                                       side_effect=RuntimeError("private-exception-sentinel")):
            self.assertEqual(main(), 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "failed", "reason_code": "authority_projection_invalid"})
        self.assertEqual(errors.getvalue(), "")
        self.assertNotIn("private-exception-sentinel", output.getvalue())


if __name__ == "__main__":
    unittest.main()
