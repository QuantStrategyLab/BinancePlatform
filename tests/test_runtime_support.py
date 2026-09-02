import os
import sys
import traceback
import unittest
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

import requests

if not hasattr(requests, "exceptions"):
    class RequestException(OSError):
        pass

    class RequestsConnectionError(RequestException):
        pass

    class HTTPError(RequestException):
        pass

    class RequestsTimeout(RequestException):
        pass

    requests.exceptions = type(
        "RequestsExceptions",
        (),
        {
            "ConnectionError": RequestsConnectionError,
            "HTTPError": HTTPError,
            "Timeout": RequestsTimeout,
        },
    )
else:
    RequestsConnectionError = requests.exceptions.ConnectionError
    HTTPError = requests.exceptions.HTTPError
    RequestsTimeout = requests.exceptions.Timeout


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
QPK_SRC = ROOT.parent / "QuantPlatformKit" / "src"
if str(QPK_SRC) not in sys.path:
    sys.path.insert(0, str(QPK_SRC))

from runtime_support import (
    ClientCallError,
    ExecutionRuntime,
    OrderReconciliationError,
    build_runtime_evidence_aggregate,
    build_execution_report,
    finalize_notification_delivery,
    record_gating_event,
    next_order_id,
    runtime_call_client,
    runtime_notify,
    runtime_set_trade_state,
    validate_runtime_evidence_aggregate,
)
from quant_platform_kit.common.runtime_target import build_runtime_target
from application.execution_receipt_adapter import attach_execution_receipt_from_report


class TestBuildExecutionReport(unittest.TestCase):
    def test_default_runtime_order_ids_are_unique_within_one_second_and_bounded(self):
        same_second = datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc)
        first = ExecutionRuntime(now_utc=same_second)
        second = ExecutionRuntime(now_utc=same_second)

        first_order_id = next_order_id(first, "T_BUY", "BTCUSDT")
        second_order_id = next_order_id(second, "T_BUY", "BTCUSDT")

        self.assertNotEqual(first_order_id, second_order_id)
        for order_id in (first_order_id, second_order_id):
            self.assertLessEqual(len(order_id), 32)
            self.assertRegex(order_id, r"^QSL_[0-9a-f]{28}$")

    def test_mismatched_response_identity_fails_closed_without_accepted_event(self):
        for source in ("submission_response", "reconciliation_response"):
            with self.subTest(source=source):
                class Client:
                    def order_market_buy(self, **kwargs):
                        if source == "submission_response":
                            return {
                                "status": "FILLED",
                                "clientOrderId": "unexpected-client-order-id",
                                "orderId": 101,
                                "origQty": "0.01000000",
                                "executedQty": "0.01000000",
                            }
                        raise TimeoutError("provider-submit-secret")

                    def get_order(self, *, symbol, origClientOrderId):
                        return {
                            "status": "FILLED",
                            "clientOrderId": "unexpected-client-order-id",
                            "orderId": 202,
                            "origQty": "0.01000000",
                            "executedQty": "0.01000000",
                        }

                runtime = ExecutionRuntime(dry_run=False, run_id=f"identity-{source}", client=Client())
                report = build_execution_report(runtime)
                report["runtime_target"] = {"execution_mode": "live"}
                report["runtime_release_receipt"] = {
                    "attestation_state": "self_attested",
                    "strategy_release": {"strategy_revision": "a" * 40},
                }

                with self.assertRaisesRegex(OrderReconciliationError, "order_reconciliation_uncertain"):
                    runtime_call_client(
                        runtime,
                        report,
                        method_name="order_market_buy",
                        payload={"symbol": "BTCUSDT", "quantity": 0.01},
                        effect_type="order_buy",
                        max_retries=0,
                        retry_base_sec=0,
                    )

                self.assertNotIn("execution_order_events", report)
                self.assertEqual(report["execution_receipt_observation"]["broker_acknowledged_count"], 0)
                self.assertEqual(report["execution_receipt_observation"]["partially_filled_count"], 0)
                self.assertEqual(report["execution_receipt_observation"]["filled_count"], 0)
                self.assertEqual(report["execution_receipt_observation"]["failed_count"], 1)
                self.assertTrue(attach_execution_receipt_from_report(report))
                self.assertNotIn(
                    report["execution_receipt"]["outcome"],
                    {"broker_acknowledged", "partially_filled", "filled"},
                )

    @staticmethod
    def runtime_evidence_inputs():
        return {
            "release_identity": {
                "strategy_profile": "crypto_live_pool_rotation",
                "mode": "core_major",
                "source_revision": "a" * 40,
                "input_timestamp": "2026-03-13T00:00:00Z",
                "artifact_contract": "crypto_live_pool_rotation.live_pool.v1",
                "artifact_version": "2026-03-13-core_major",
                "artifacts": {"live_pool": {"sha256": "b" * 64}},
            },
            "risk_engine": {"outcome": "APPROVE", "policy_version": "bootstrap_small_account_v2"},
            "effective_exposure_cap": {
                "value": 0.5,
                "mandate_version": "bootstrap_small_account_v2",
                "source": "approved_risk_mandate",
            },
            "stop_breaker_evaluation": {
                "stop_evaluated": True,
                "breaker_evaluated": True,
                "outcome": "CLEAR",
                "policy_version": "bootstrap_small_account_v2",
            },
            "reconciliation": {"status": "MISSING"},
        }

    def test_runtime_evidence_aggregate_is_redacted_and_static_only(self):
        aggregate = build_runtime_evidence_aggregate(**self.runtime_evidence_inputs())

        self.assertTrue(validate_runtime_evidence_aggregate(aggregate)["ok"])
        self.assertFalse(aggregate["verified_active"])
        self.assertFalse(aggregate["fills_verified"])
        self.assertFalse(aggregate["capital_use_verified"])
        self.assertNotIn("orders", str(aggregate))

    def test_runtime_evidence_aggregate_fails_closed_for_risk_reconciliation_and_sensitive_fields(self):
        aggregate = build_runtime_evidence_aggregate(**self.runtime_evidence_inputs())
        aggregate["risk_engine"]["outcome"] = "REJECT"
        aggregate["reconciliation"] = {"status": "MATCHED"}
        aggregate["positions"] = [{"symbol": "BTCUSDT"}]
        aggregate["release_identity"]["headers"] = {"authorization": "redacted"}

        validation = validate_runtime_evidence_aggregate(aggregate)

        self.assertFalse(validation["ok"])
        self.assertIn("runtime_evidence_aggregate risk_engine.outcome must be APPROVE", validation["errors"])
        self.assertIn(
            "runtime_evidence_aggregate reconciliation.MATCHED requires durable_receipt_sha256",
            validation["errors"],
        )
        self.assertIn("runtime_evidence_aggregate contains forbidden field: positions", validation["errors"])
        self.assertIn("runtime_evidence_aggregate contains forbidden field: headers", validation["errors"])

    def test_runtime_evidence_aggregate_rejects_static_matched_reconciliation(self):
        matched_inputs = self.runtime_evidence_inputs()
        matched_inputs["reconciliation"] = {
            "status": "MATCHED",
            "durable_receipt_sha256": "c" * 64,
            "identity_sha256": "d" * 64,
        }
        mismatched_inputs = self.runtime_evidence_inputs()
        mismatched_inputs["reconciliation"] = {
            "status": "MISMATCHED",
            "durable_receipt_sha256": "c" * 64,
            "identity_sha256": "d" * 64,
            "observed_identity_sha256": "e" * 64,
        }

        with self.assertRaisesRegex(ValueError, "MATCHED is not valid for static acceptance"):
            build_runtime_evidence_aggregate(**matched_inputs)
        self.assertTrue(validate_runtime_evidence_aggregate(build_runtime_evidence_aggregate(**mismatched_inputs))["ok"])

    def test_report_contains_enrichment_fields(self):
        runtime = ExecutionRuntime(dry_run=True, run_id="test-001")
        report = build_execution_report(runtime)
        self.assertIsNone(report["total_equity_usdt"])
        self.assertIsNone(report["trend_equity_usdt"])
        self.assertFalse(report["circuit_breaker_triggered"])
        self.assertIsNone(report["degraded_mode_level"])
        self.assertEqual(report["upstream_pool_symbols"], [])
        self.assertEqual(report["gating_summary"], {})
        self.assertEqual(report["gating_events"], [])

    def test_report_preserves_existing_fields(self):
        runtime = ExecutionRuntime(dry_run=False, run_id="test-002")
        with patch.dict(
            os.environ,
            {
                "STRATEGY_PROFILE": "crypto_live_pool_rotation",
                "SERVICE_NAME": "binance-runtime",
                "LOG_DEPLOY_TARGET": "vps",
            },
            clear=False,
        ):
            report = build_execution_report(runtime)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["run_id"], "test-002")
        self.assertFalse(report["dry_run"])
        self.assertEqual(report["schema_version"], "runtime_report.v1")
        self.assertEqual(report["platform"], "binance")
        self.assertEqual(report["strategy_profile"], "crypto_live_pool_rotation")
        self.assertIn("buy_sell_intents", report)
        self.assertIn("log_lines", report)

    def test_runtime_target_disable_suppresses_client_side_effects_without_dry_run(self):
        observed = {"calls": 0}

        class Client:
            def create_order(self, **_kwargs):
                observed["calls"] += 1
                return {"status": "unexpected"}

        runtime = ExecutionRuntime(
            dry_run=False,
            run_id="target-disabled",
            client=Client(),
            standard_execution_permitted=False,
        )
        report = build_execution_report(runtime)

        result = runtime_call_client(
            runtime,
            report,
            method_name="create_order",
            payload={"symbol": "BTCUSDT"},
            effect_type="order",
        )

        self.assertEqual(result["status"], "suppressed")
        self.assertEqual(observed["calls"], 0)
        self.assertEqual(report["side_effect_summary"]["suppressed_call_count"], 1)
        self.assertFalse(report["standard_execution_permitted"])

    def test_state_writer_failure_stops_before_success_is_recorded(self):
        runtime = ExecutionRuntime(
            dry_run=False,
            run_id="state-write-failure",
            state_writer=lambda _state: False,
        )
        report = build_execution_report(runtime)

        with self.assertRaisesRegex(RuntimeError, "state_persistence_failed") as raised:
            runtime_set_trade_state(runtime, report, {"ok": True}, reason="cycle_complete")

        self.assertEqual(type(raised.exception).__name__, "StatePersistenceError")
        self.assertEqual(report["side_effect_summary"]["executed_call_count"], 0)

    def test_state_writer_exception_is_sanitized(self):
        def fail_write(_state):
            raise RuntimeError("provider-secret-state-write-error")

        runtime = ExecutionRuntime(
            dry_run=False,
            run_id="state-write-exception",
            state_writer=fail_write,
        )
        report = build_execution_report(runtime)

        with self.assertRaisesRegex(RuntimeError, "state_persistence_failed") as raised:
            runtime_set_trade_state(runtime, report, {"ok": True}, reason="cycle_complete")

        rendered = "".join(traceback.format_exception(raised.exception))
        self.assertEqual(type(raised.exception).__name__, "StatePersistenceError")
        self.assertNotIn("provider-secret-state-write-error", rendered)
        self.assertEqual(report["side_effect_summary"]["executed_call_count"], 0)

    def test_order_transport_errors_with_pending_reconciliation_stop_without_resubmit(self):
        for error_type in (TimeoutError, ConnectionError, RequestsTimeout, RequestsConnectionError):
            with self.subTest(error_type=error_type.__name__):
                observed = []

                class Client:
                    def order_market_buy(self, **kwargs):
                        observed.append(("submit", kwargs["newClientOrderId"]))
                        raise error_type("provider-submit-secret")

                    def get_order(self, *, symbol, origClientOrderId):
                        observed.append(("reconcile", symbol, origClientOrderId))
                        return {"status": "NEW", "clientOrderId": origClientOrderId}

                runtime = ExecutionRuntime(dry_run=False, run_id="stable-order", client=Client())
                report = build_execution_report(runtime)

                with self.assertRaisesRegex(OrderReconciliationError, "order_reconciliation_uncertain"):
                    runtime_call_client(
                        runtime,
                        report,
                        method_name="order_market_buy",
                        payload={"symbol": "BTCUSDT", "quantity": 0.01},
                        effect_type="order_buy",
                        max_retries=1,
                        retry_base_sec=0,
                    )

                self.assertEqual([call[0] for call in observed], ["submit", "reconcile"])
                self.assertEqual(observed[0][1], observed[1][2])

    def test_order_reconciliation_none_response_does_not_resubmit(self):
        observed = []

        class Client:
            def order_market_buy(self, **kwargs):
                observed.append(("submit", kwargs["newClientOrderId"]))
                raise TimeoutError("provider-submit-secret")

            def get_order(self, *, symbol, origClientOrderId):
                observed.append(("reconcile", symbol, origClientOrderId))
                return None

        runtime = ExecutionRuntime(dry_run=False, run_id="none-reconciliation", client=Client())
        report = build_execution_report(runtime)

        with self.assertRaisesRegex(OrderReconciliationError, "order_reconciliation_uncertain") as raised:
            runtime_call_client(
                runtime,
                report,
                method_name="order_market_buy",
                payload={"symbol": "BTCUSDT", "quantity": 0.01},
                effect_type="order_buy",
                max_retries=1,
                retry_base_sec=0,
            )

        self.assertEqual([call[0] for call in observed], ["submit", "reconcile"])
        self.assertNotIn("provider-submit-secret", "".join(traceback.format_exception(raised.exception)))

    def test_order_reconciliation_query_error_does_not_resubmit(self):
        observed = []

        class Client:
            def order_market_buy(self, **kwargs):
                observed.append(("submit", kwargs["newClientOrderId"]))
                raise TimeoutError("provider-submit-secret")

            def get_order(self, *, symbol, origClientOrderId):
                observed.append(("reconcile", symbol, origClientOrderId))
                raise RuntimeError("provider-query-secret")

        runtime = ExecutionRuntime(dry_run=False, run_id="query-error", client=Client())
        report = build_execution_report(runtime)

        with self.assertRaisesRegex(OrderReconciliationError, "order_reconciliation_uncertain") as raised:
            runtime_call_client(
                runtime,
                report,
                method_name="order_market_buy",
                payload={"symbol": "BTCUSDT", "quantity": 0.01},
                effect_type="order_buy",
                max_retries=1,
                retry_base_sec=0,
            )

        rendered = "".join(traceback.format_exception(raised.exception))
        self.assertEqual([call[0] for call in observed], ["submit", "reconcile"])
        self.assertNotIn("provider-submit-secret", rendered)
        self.assertNotIn("provider-query-secret", rendered)

    def test_order_reconciliation_non_mapping_response_does_not_resubmit(self):
        observed = []

        class Client:
            def order_market_buy(self, **kwargs):
                observed.append(("submit", kwargs["newClientOrderId"]))
                raise TimeoutError("provider-submit-secret")

            def get_order(self, *, symbol, origClientOrderId):
                observed.append(("reconcile", symbol, origClientOrderId))
                return ["unexpected"]

        runtime = ExecutionRuntime(dry_run=False, run_id="invalid-reconciliation", client=Client())
        report = build_execution_report(runtime)

        with self.assertRaisesRegex(OrderReconciliationError, "order_reconciliation_uncertain"):
            runtime_call_client(
                runtime,
                report,
                method_name="order_market_buy",
                payload={"symbol": "BTCUSDT", "quantity": 0.01},
                effect_type="order_buy",
                max_retries=1,
                retry_base_sec=0,
            )

        self.assertEqual([call[0] for call in observed], ["submit", "reconcile"])

    def test_order_not_found_after_transport_uncertainty_does_not_resubmit(self):
        observed = []

        class OrderNotFound(Exception):
            code = -2013

        class Client:
            def order_market_buy(self, **kwargs):
                observed.append(("submit", kwargs["newClientOrderId"]))
                if len([call for call in observed if call[0] == "submit"]) == 1:
                    raise TimeoutError("provider-submit-secret")
                return {"status": "FILLED", "clientOrderId": kwargs["newClientOrderId"]}

            def get_order(self, *, symbol, origClientOrderId):
                observed.append(("reconcile", symbol, origClientOrderId))
                raise OrderNotFound("provider-query-secret")

        runtime = ExecutionRuntime(dry_run=False, run_id="order-not-found-retry", client=Client())
        report = build_execution_report(runtime)

        with self.assertRaisesRegex(OrderReconciliationError, "order_reconciliation_uncertain") as raised:
            runtime_call_client(
                runtime,
                report,
                method_name="order_market_buy",
                payload={"symbol": "BTCUSDT", "quantity": 0.01},
                effect_type="order_buy",
                max_retries=1,
                retry_base_sec=0,
            )

        rendered = "".join(traceback.format_exception(raised.exception)) + str(report)
        self.assertEqual([call[0] for call in observed], ["submit", "reconcile"])
        self.assertEqual(observed[0][1], observed[1][2])
        self.assertEqual(
            report["execution_receipt_observation"],
            {
                "submission_attempted_count": 1,
                "broker_acknowledged_count": 0,
                "partially_filled_count": 0,
                "filled_count": 0,
                "transport_uncertain_count": 1,
                "failed_count": 1,
            },
        )
        self.assertEqual(report["side_effect_summary"], {"executed_call_count": 0, "suppressed_call_count": 1})
        self.assertNotIn("provider-submit-secret", rendered)
        self.assertNotIn("provider-query-secret", rendered)

    def test_order_not_found_after_all_uncertain_retries_raises_integrity_error(self):
        observed = []

        class OrderNotFound(Exception):
            code = -2013

        class Client:
            def order_market_buy(self, **kwargs):
                observed.append(("submit", kwargs["newClientOrderId"]))
                raise TimeoutError("provider-submit-secret")

            def get_order(self, *, symbol, origClientOrderId):
                observed.append(("reconcile", symbol, origClientOrderId))
                raise OrderNotFound("provider-query-secret")

        runtime = ExecutionRuntime(dry_run=False, run_id="order-not-found-exhausted", client=Client())
        report = build_execution_report(runtime)

        with self.assertRaisesRegex(OrderReconciliationError, "order_reconciliation_uncertain") as raised:
            runtime_call_client(
                runtime,
                report,
                method_name="order_market_buy",
                payload={"symbol": "BTCUSDT", "quantity": 0.01},
                effect_type="order_buy",
                max_retries=1,
                retry_base_sec=0,
            )

        rendered = "".join(traceback.format_exception(raised.exception)) + str(report)
        self.assertEqual([call[0] for call in observed], ["submit", "reconcile"])
        self.assertEqual(observed[0][1], observed[1][2])
        self.assertNotIn("provider-submit-secret", rendered)
        self.assertNotIn("provider-query-secret", rendered)

    def test_deterministic_order_rejection_does_not_reconcile(self):
        observed = []

        class RejectedOrder(Exception):
            code = -1013

        class Client:
            def order_market_buy(self, **kwargs):
                observed.append(("submit", kwargs["newClientOrderId"]))
                raise RejectedOrder("SENSITIVE_PROVIDER_SENTINEL")

            def get_order(self, **_kwargs):
                observed.append(("reconcile",))

        runtime = ExecutionRuntime(dry_run=False, run_id="rejected-order", client=Client())
        report = build_execution_report(runtime)

        with self.assertRaises(ClientCallError) as raised:
            runtime_call_client(
                runtime,
                report,
                method_name="order_market_buy",
                payload={"symbol": "BTCUSDT", "quantity": 0.01},
                effect_type="order_buy",
                max_retries=1,
                retry_base_sec=0,
            )

        self.assertEqual([call[0] for call in observed], ["submit"])
        self.assertNotIsInstance(raised.exception, OrderReconciliationError)
        self.assertNotIn("SENSITIVE_PROVIDER_SENTINEL", str(raised.exception) + str(report))

    def test_requests_http_error_does_not_reconcile(self):
        observed = []

        class Client:
            def order_market_buy(self, **_kwargs):
                observed.append("submit")
                raise HTTPError("SENSITIVE_PROVIDER_SENTINEL")

            def get_order(self, **_kwargs):
                observed.append("reconcile")

        runtime = ExecutionRuntime(dry_run=False, run_id="http-error", client=Client())
        report = build_execution_report(runtime)

        with self.assertRaises(ClientCallError):
            runtime_call_client(
                runtime,
                report,
                method_name="order_market_buy",
                payload={"symbol": "BTCUSDT", "quantity": 0.01},
                effect_type="order_buy",
                max_retries=1,
                retry_base_sec=0,
            )

        self.assertEqual(observed, ["submit"])

    def test_binance_unknown_execution_status_codes_reconcile(self):
        class UncertainOrder(Exception):
            def __init__(self, code):
                super().__init__("SENSITIVE_PROVIDER_SENTINEL")
                self.code = code

        for code in (-1001, -1006, -1007):
            with self.subTest(code=code):
                observed = []

                class Client:
                    def order_market_buy(self, **kwargs):
                        observed.append("submit")
                        raise UncertainOrder(code)

                    def get_order(self, *, symbol, origClientOrderId):
                        observed.append("reconcile")
                        return {"status": "NEW", "clientOrderId": origClientOrderId}

                runtime = ExecutionRuntime(dry_run=False, run_id=f"uncertain-{code}", client=Client())
                report = build_execution_report(runtime)

                with self.assertRaisesRegex(OrderReconciliationError, "order_reconciliation_uncertain"):
                    runtime_call_client(
                        runtime,
                        report,
                        method_name="order_market_buy",
                        payload={"symbol": "BTCUSDT", "quantity": 0.01},
                        effect_type="order_buy",
                        max_retries=0,
                        retry_base_sec=0,
                    )

                self.assertEqual(observed, ["submit", "reconcile"])

    def test_direct_non_filled_order_statuses_stop_before_callers_update_state(self):
        for status, expected_observation in (
            (
                "NEW",
                {
                    "submission_attempted_count": 1,
                    "broker_acknowledged_count": 1,
                    "partially_filled_count": 0,
                    "filled_count": 0,
                    "transport_uncertain_count": 0,
                    "failed_count": 0,
                },
            ),
            (
                "PARTIALLY_FILLED",
                {
                    "submission_attempted_count": 1,
                    "broker_acknowledged_count": 0,
                    "partially_filled_count": 1,
                    "filled_count": 0,
                    "transport_uncertain_count": 0,
                    "failed_count": 0,
                },
            ),
        ):
            with self.subTest(status=status):
                class Client:
                    def order_market_buy(self, **kwargs):
                        response = {"status": status, "clientOrderId": kwargs["newClientOrderId"]}
                        if status == "PARTIALLY_FILLED":
                            response.update({"origQty": "0.01000000", "executedQty": "0.00500000"})
                        return response

                runtime = ExecutionRuntime(dry_run=False, run_id=f"direct-{status}", client=Client())
                report = build_execution_report(runtime)

                with self.assertRaisesRegex(OrderReconciliationError, "order_reconciliation_uncertain"):
                    runtime_call_client(
                        runtime,
                        report,
                        method_name="order_market_buy",
                        payload={"symbol": "BTCUSDT", "quantity": 0.01},
                        effect_type="order_buy",
                        max_retries=0,
                        retry_base_sec=0,
                    )

                self.assertEqual(report["execution_receipt_observation"], expected_observation)
                self.assertEqual(report["side_effect_summary"], {"executed_call_count": 1, "suppressed_call_count": 0})

    def test_direct_terminal_failure_status_is_not_returned_as_success(self):
        for status in ("CANCELED", "EXPIRED", "REJECTED"):
            with self.subTest(status=status):
                class Client:
                    def order_market_buy(self, **kwargs):
                        return {"status": status, "clientOrderId": kwargs["newClientOrderId"]}

                runtime = ExecutionRuntime(dry_run=False, run_id=f"direct-{status}", client=Client())
                report = build_execution_report(runtime)

                with self.assertRaisesRegex(ClientCallError, "order_submission_failed"):
                    runtime_call_client(
                        runtime,
                        report,
                        method_name="order_market_buy",
                        payload={"symbol": "BTCUSDT", "quantity": 0.01},
                        effect_type="order_buy",
                        max_retries=0,
                        retry_base_sec=0,
                    )

                self.assertEqual(report["execution_receipt_observation"]["failed_count"], 1)
                self.assertEqual(report["side_effect_summary"], {"executed_call_count": 1, "suppressed_call_count": 0})

    def test_direct_filled_order_records_one_fill_and_returns_success(self):
        class Client:
            def order_market_buy(self, **kwargs):
                return {
                    "status": "FILLED",
                    "clientOrderId": kwargs["newClientOrderId"],
                    "orderId": 101,
                    "origQty": "0.01000000",
                    "executedQty": "0.01000000",
                }

        runtime = ExecutionRuntime(dry_run=False, run_id="direct-filled", client=Client())
        report = build_execution_report(runtime)

        result = runtime_call_client(
            runtime,
            report,
            method_name="order_market_buy",
            payload={"symbol": "BTCUSDT", "quantity": 0.01},
            effect_type="order_buy",
            max_retries=0,
            retry_base_sec=0,
        )

        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(
            report["execution_receipt_observation"],
            {
                "submission_attempted_count": 1,
                "broker_acknowledged_count": 0,
                "partially_filled_count": 0,
                "filled_count": 1,
                "transport_uncertain_count": 0,
                "failed_count": 0,
            },
        )
        self.assertEqual(report["side_effect_summary"], {"executed_call_count": 1, "suppressed_call_count": 0})
        event = report["execution_order_events"][0]
        self.assertEqual(event["event_source"], "submission_response")
        self.assertEqual(event["client_order_id_sha256"], sha256(result["clientOrderId"].encode()).hexdigest())
        self.assertEqual(event["venue_order_id_sha256"], sha256(b"101").hexdigest())
        self.assertEqual(event["status"], "FILLED")

    def test_reconciled_filled_order_preserves_stable_order_event(self):
        observed = {}

        class Client:
            def order_market_buy(self, **kwargs):
                observed["client_order_id"] = kwargs["newClientOrderId"]
                raise TimeoutError("provider-submit-secret")

            def get_order(self, *, symbol, origClientOrderId):
                observed["reconciliation_query"] = (symbol, origClientOrderId)
                return {
                    "status": "FILLED",
                    "clientOrderId": origClientOrderId,
                    "orderId": 202,
                    "origQty": "0.02000000",
                    "executedQty": "0.02000000",
                }

        runtime = ExecutionRuntime(dry_run=False, run_id="reconciled-filled", client=Client())
        report = build_execution_report(runtime)

        result = runtime_call_client(
            runtime,
            report,
            method_name="order_market_buy",
            payload={"symbol": "BTCUSDT", "quantity": 0.02},
            effect_type="order_buy",
            max_retries=0,
            retry_base_sec=0,
        )

        self.assertEqual(result["status"], "FILLED")
        event = report["execution_order_events"][0]
        self.assertEqual(event["event_source"], "reconciliation_response")
        self.assertEqual(event["client_order_id_sha256"], sha256(observed["client_order_id"].encode()).hexdigest())
        self.assertEqual(observed["reconciliation_query"], ("BTCUSDT", observed["client_order_id"]))
        self.assertEqual(event["venue_order_id_sha256"], sha256(b"202").hexdigest())
        self.assertEqual(event["status"], "FILLED")

    def test_report_uses_runtime_target_service_identity(self):
        runtime_target = build_runtime_target(
            platform_id="binance",
            strategy_profile="crypto_live_pool_rotation",
            dry_run_only=False,
            service_name="binance-platform",
        )
        runtime = ExecutionRuntime(
            dry_run=False,
            run_id="test-runtime-target",
            runtime_target=runtime_target,
        )

        report = build_execution_report(runtime)

        self.assertEqual(report["service_name"], "binance-platform")
        self.assertEqual(report["runtime_target"]["service_name"], "binance-platform")

    def test_record_gating_event_updates_summary_and_events(self):
        report = {}

        record_gating_event(
            report,
            gate="trend_buy_below_min_budget",
            category="trend",
            symbol="ETHUSDT",
            detail={"budget_usdt": 12.0},
        )
        record_gating_event(
            report,
            gate="trend_buy_below_min_budget",
            category="trend",
        )

        self.assertEqual(report["gating_summary"]["trend_buy_below_min_budget"], 2)
        self.assertEqual(report["gating_events"][0]["symbol"], "ETHUSDT")
        self.assertEqual(report["gating_events"][0]["detail"]["budget_usdt"], 12.0)

    def test_runtime_notify_persists_only_safe_failed_delivery_receipt(self):
        runtime = ExecutionRuntime(
            dry_run=False,
            run_id="test-notification",
            tg_token="secret-token",
            tg_chat_id="private-chat",
            notifier=lambda **_kwargs: {
                "sink": "telegram",
                "delivery_status": "failed",
                "transport_acknowledged": False,
                "error_type": "telegram_rejected",
            },
        )
        report = build_execution_report(runtime)

        acknowledged = runtime_notify(runtime, report, "sensitive notification body")
        finalize_notification_delivery(report)

        serialized = str(report)
        self.assertFalse(acknowledged)
        self.assertEqual(report["status"], "error")
        self.assertFalse(
            report["summary"]["notification_delivery_summary"]["all_acknowledged"]
        )
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("private-chat", serialized)
        self.assertNotIn("sensitive notification body", serialized)
