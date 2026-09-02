from __future__ import annotations

import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from application.cycle_service import write_execution_report
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
    def test_partial_then_filled_receipt_uses_monotonic_terminal_state(self) -> None:
        report = _report()
        reducer_state = {}
        record_order_submission_attempt(report)
        for response in (
            {
                "status": "PARTIALLY_FILLED",
                "clientOrderId": "client-001",
                "orderId": 42,
                "origQty": "1.00000000",
                "executedQty": "0.40000000",
            },
            {
                "status": "FILLED",
                "clientOrderId": "client-001",
                "orderId": 42,
                "origQty": "1.00000000",
                "executedQty": "1.00000000",
            },
            {
                "status": "PARTIALLY_FILLED",
                "clientOrderId": "client-001",
                "orderId": 42,
                "origQty": "1.00000000",
                "executedQty": "0.80000000",
            },
        ):
            record_order_response(
                report,
                response,
                client_order_id="client-001",
                ordered_quantity="1.0",
                event_source="reconciliation_response",
                reducer_state=reducer_state,
            )

        self.assertTrue(attach_execution_receipt_from_report(report))
        self.assertEqual(report["execution_receipt"]["outcome"], "filled")
        self.assertEqual(report["execution_receipt_observation"]["partially_filled_count"], 0)
        self.assertEqual(report["execution_receipt_observation"]["filled_count"], 1)
        self.assertEqual(
            [event["status"] for event in report["execution_order_events"]],
            ["PARTIALLY_FILLED", "FILLED"],
        )

    def test_rejected_responses_are_not_persisted_as_accepted_events(self) -> None:
        report = _report()
        reducer_state = {}
        partial = {
            "status": "PARTIALLY_FILLED",
            "clientOrderId": "client-001",
            "orderId": 42,
            "origQty": "1.00000000",
            "executedQty": "0.60000000",
        }
        self.assertTrue(
            record_order_response(
                report,
                partial,
                client_order_id="client-001",
                ordered_quantity="1.0",
                event_source="reconciliation_response",
                reducer_state=reducer_state,
            )
        )
        event_count = len(report["execution_order_events"])

        for response in (
            {**partial, "status": "FILLED", "executedQty": "0.90000000"},
            {**partial, "executedQty": "0.50000000"},
        ):
            self.assertFalse(
                record_order_response(
                    report,
                    response,
                    client_order_id="client-001",
                    ordered_quantity="1.0",
                    event_source="reconciliation_response",
                    reducer_state=reducer_state,
                )
            )
            self.assertEqual(len(report["execution_order_events"]), event_count)

    def test_expired_in_match_is_terminal_and_cannot_roll_back(self) -> None:
        report = _report()
        reducer_state = {}
        expired = {
            "status": "EXPIRED_IN_MATCH",
            "clientOrderId": "client-001",
            "orderId": 42,
        }
        self.assertTrue(
            record_order_response(
                report,
                expired,
                client_order_id="client-001",
                ordered_quantity="1.0",
                event_source="reconciliation_response",
                reducer_state=reducer_state,
            )
        )
        self.assertFalse(
            record_order_response(
                report,
                {
                    **expired,
                    "status": "FILLED",
                    "origQty": "1.00000000",
                    "executedQty": "1.00000000",
                },
                client_order_id="client-001",
                ordered_quantity="1.0",
                event_source="reconciliation_response",
                reducer_state=reducer_state,
            )
        )
        self.assertEqual([event["status"] for event in report["execution_order_events"]], ["EXPIRED_IN_MATCH"])

    def test_incomplete_filled_response_is_not_recognized(self) -> None:
        report = _report()

        accepted = record_order_response(
            report,
            {
                "status": "FILLED",
                "clientOrderId": "client-001",
                "orderId": 42,
                "origQty": "1.00000000",
                "executedQty": "0.40000000",
            },
            client_order_id="client-001",
            ordered_quantity="1.0",
            event_source="reconciliation_response",
            reducer_state={},
        )

        self.assertFalse(accepted)
        self.assertEqual(report["execution_receipt_observation"]["filled_count"], 0)

    def test_order_event_json_redacts_raw_identity_and_quantities(self) -> None:
        report = _report()
        raw_client_order_id = "QSL_20260903_sensitive-order-id"
        raw_strategy_date = "20260903"
        raw_venue_order_id = "987654321"
        raw_ordered_quantity = "0.12345678"
        raw_cumulative_quantity = "0.10000000"

        record_order_response(
            report,
            {
                "status": "PARTIALLY_FILLED",
                "clientOrderId": raw_client_order_id,
                "orderId": raw_venue_order_id,
                "origQty": raw_ordered_quantity,
                "executedQty": raw_cumulative_quantity,
            },
            client_order_id=raw_client_order_id,
            ordered_quantity=raw_ordered_quantity,
            event_source="submission_response",
            reducer_state={},
        )

        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(write_execution_report(report, reports_dir=directory))
            persisted = output_path.read_text(encoding="utf-8")

        for value in (
            raw_client_order_id,
            raw_strategy_date,
            raw_venue_order_id,
            raw_ordered_quantity,
            raw_cumulative_quantity,
        ):
            self.assertNotIn(value, persisted)
        event = json.loads(persisted)["execution_order_events"][0]
        self.assertEqual(set(event), {
            "schema_version",
            "event_id",
            "client_order_id_sha256",
            "venue_order_id_sha256",
            "status",
            "event_source",
        })

    def test_order_response_events_are_append_only_and_idempotent(self) -> None:
        report = _report()
        reducer_state = {}
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
                reducer_state=reducer_state,
            )

        events = report["execution_order_events"]
        self.assertEqual(len(events), 2)
        self.assertEqual([event["status"] for event in events], ["PARTIALLY_FILLED", "FILLED"])
        self.assertEqual(events[0]["client_order_id_sha256"], sha256(b"client-001").hexdigest())
        self.assertEqual(events[0]["venue_order_id_sha256"], sha256(b"42").hexdigest())
        self.assertEqual(events[0]["event_source"], "reconciliation_response")
        self.assertNotEqual(events[0]["event_id"], events[1]["event_id"])
        self.assertNotIn("client-001", json.dumps(events))
        self.assertNotIn("0.4", json.dumps(events))
        self.assertEqual(report["execution_receipt_observation"]["partially_filled_count"], 0)
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
