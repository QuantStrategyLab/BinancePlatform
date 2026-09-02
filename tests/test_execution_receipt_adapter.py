from __future__ import annotations

import unittest

from application.execution_receipt_adapter import (
    attach_execution_receipt_from_report,
    record_order_response,
    record_order_submission_attempt,
    record_order_transport_uncertainty,
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
    def test_order_response_events_are_append_only_and_idempotent(self) -> None:
        report = _report()
        partial = {
            "status": "PARTIALLY_FILLED",
            "clientOrderId": "client-001",
            "orderId": 42,
            "origQty": "1.00000000",
            "executedQty": "0.40000000",
        }
        filled = {**partial, "status": "FILLED", "executedQty": "1.00000000"}

        for response in (partial, partial, filled):
            record_order_response(
                report,
                response,
                client_order_id="client-001",
                ordered_quantity="1.0",
                event_source="reconciliation_response",
            )

        events = report["execution_order_events"]
        self.assertEqual(len(events), 2)
        self.assertEqual([event["status"] for event in events], ["PARTIALLY_FILLED", "FILLED"])
        self.assertEqual(events[0]["client_order_id"], "client-001")
        self.assertEqual(events[0]["venue_order_id"], "42")
        self.assertEqual(events[0]["ordered_quantity"], "1")
        self.assertEqual(events[0]["cumulative_filled_quantity"], "0.4")
        self.assertEqual(events[0]["event_source"], "reconciliation_response")
        self.assertNotEqual(events[0]["event_id"], events[1]["event_id"])
        self.assertEqual(report["execution_receipt_observation"]["partially_filled_count"], 1)
        self.assertEqual(report["execution_receipt_observation"]["filled_count"], 1)

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
