from __future__ import annotations

import unittest

from application.execution_receipt_adapter import (
    attach_execution_receipt_from_report,
    record_order_response,
    record_order_submission_attempt,
    record_order_transport_uncertainty,
    validate_report_execution_receipt,
)


REVISION = "a" * 40


def _report() -> dict[str, object]:
    return {
        "platform": "binance",
        "strategy_profile": "crypto_live_pool_rotation",
        "dry_run": False,
        "status": "ok",
        "runtime_target": {"execution_mode": "live"},
        "runtime_release_receipt": {
            "attestation_state": "self_attested",
            "strategy_release": {"strategy_revision": REVISION},
        },
    }


class ExecutionReceiptAdapterTest(unittest.TestCase):
    def test_explicit_filled_status_can_claim_a_fill(self) -> None:
        report = _report()
        record_order_submission_attempt(report)
        record_order_response(report, {"status": "FILLED"})

        self.assertTrue(attach_execution_receipt_from_report(report))
        self.assertEqual(report["execution_receipt"]["outcome"], "filled")

    def test_new_order_is_an_acknowledgement_not_a_fill(self) -> None:
        report = _report()
        record_order_submission_attempt(report)
        record_order_response(report, {"status": "NEW"})

        self.assertTrue(attach_execution_receipt_from_report(report))
        self.assertEqual(report["execution_receipt"]["outcome"], "broker_acknowledged")

    def test_transport_uncertainty_requires_reconciliation(self) -> None:
        report = _report()
        record_order_submission_attempt(report)
        record_order_transport_uncertainty(report)

        self.assertTrue(attach_execution_receipt_from_report(report))
        self.assertEqual(report["execution_receipt"]["outcome"], "reconciliation_required")

    def test_disabled_execution_is_risk_blocked(self) -> None:
        report = _report()
        report["execution_blocked_reason"] = "runtime_target_disabled"

        self.assertTrue(attach_execution_receipt_from_report(report))
        self.assertEqual(report["execution_receipt"]["outcome"], "risk_blocked")


    def test_receipt_binds_to_runtime_verified_live_authority(self) -> None:
        report = _report()
        report["runtime_release_receipt"] = {"attestation_state": "legacy_unattested"}
        report["binance_live_authority"] = {
            "strategy_revision": REVISION,
            "runtime_revision": "b" * 40,
            "config_sha256": "c" * 64,
        }
        report["finished_at"] = "2026-11-12T20:01:00Z"
        self.assertTrue(attach_execution_receipt_from_report(report))
        receipt = validate_report_execution_receipt(report)
        self.assertEqual(receipt["outcome"], "no_action")
        self.assertEqual(receipt["strategy_revision"], REVISION)
        self.assertEqual(receipt["observed_at"], report["finished_at"])
        report["binance_live_authority"]["strategy_revision"] = "d" * 40
        with self.assertRaises(ValueError):
            validate_report_execution_receipt(report)

    def test_aborted_report_cannot_claim_no_action(self) -> None:
        report = _report()
        report["status"] = "aborted"
        self.assertTrue(attach_execution_receipt_from_report(report))
        self.assertEqual(report["execution_receipt"]["outcome"], "failed")
