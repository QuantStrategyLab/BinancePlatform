import copy
import os
import sys
import threading
import traceback
import unittest
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
    runtime_call_client,
    runtime_notify,
    runtime_set_trade_state,
    validate_runtime_evidence_aggregate,
    _ensure_order_logical_identity,
    acquire_runtime_state_owner, release_runtime_state_owner, reconcile_runtime_cash_effects,
)
from quant_platform_kit.common.runtime_target import build_runtime_target


class DurableSubmissionStateStore:
    def __init__(self, record=None):
        self.data = {"order_submission": record or {"state": "RESERVED"}}
        self.events = []

    def load(self, *, normalize=False):
        return copy.deepcopy(self.data)

    def write(self, state):
        record = copy.deepcopy(state["order_submission"])
        self.events.append(("write", record["state"]))
        self.data = copy.deepcopy(state)
        return True



def owned_runtime(**kwargs):
    """Existing submission fixtures explicitly acquire a synthetic owner."""
    runtime = ExecutionRuntime(**kwargs)
    runtime.state_owner_claim = lambda _owner: True
    runtime.state_owner_release = lambda _owner: True
    acquire_runtime_state_owner(runtime)
    if kwargs.get("trade_state") is not None:
        runtime.trade_state = kwargs["trade_state"]
    return runtime

def build_order_runtime(**kwargs):
    store = DurableSubmissionStateStore()
    kwargs.setdefault("state_loader", store.load)
    kwargs.setdefault("state_writer", store.write)
    return owned_runtime(**kwargs)


def order_query_response(*, client_order_id, status, symbol="BTCUSDT", side="BUY", quantity="0.01"):
    return {
        "status": status,
        "clientOrderId": client_order_id,
        "symbol": symbol,
        "side": side,
        "origQty": quantity,
        "origQuoteOrderQty": "0",
        "executedQty": "0" if status in {"CANCELED", "REJECTED", "EXPIRED"} else quantity,
    }


class TestBuildExecutionReport(unittest.TestCase):
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
        runtime = owned_runtime(dry_run=True, run_id="test-001")
        report = build_execution_report(runtime)
        self.assertIsNone(report["total_equity_usdt"])
        self.assertIsNone(report["trend_equity_usdt"])
        self.assertFalse(report["circuit_breaker_triggered"])
        self.assertIsNone(report["degraded_mode_level"])
        self.assertEqual(report["upstream_pool_symbols"], [])
        self.assertEqual(report["gating_summary"], {})
        self.assertEqual(report["gating_events"], [])

    def test_report_preserves_existing_fields(self):
        runtime = owned_runtime(dry_run=False, run_id="test-002")
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

        runtime = owned_runtime(
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
        runtime = owned_runtime(
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

        runtime = owned_runtime(
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

    def test_http_and_transport_errors_keep_unknown_without_terminal_response(self):
        for error_type in (HTTPError, TimeoutError):
            with self.subTest(error_type=error_type.__name__):
                store = DurableSubmissionStateStore()

                class Client:
                    def order_market_buy(self, **_kwargs):
                        store.events.append(("submit",))
                        raise error_type("provider-submit-secret")

                    def get_order(self, *, origClientOrderId, **_kwargs):
                        store.events.append(("query",))
                        return order_query_response(client_order_id=origClientOrderId, status="NEW")

                runtime = owned_runtime(
                    dry_run=False,
                    run_id=f"unknown-{error_type.__name__}",
                    client=Client(),
                    state_loader=store.load,
                    state_writer=store.write,
                )

                with self.assertRaisesRegex(OrderReconciliationError, "order_reconciliation_uncertain"):
                    runtime_call_client(
                        runtime,
                        build_execution_report(runtime),
                        method_name="order_market_buy",
                        payload={"symbol": "BTCUSDT", "quantity": 0.01},
                        effect_type="order_buy",
                        max_retries=0,
                    )

                self.assertEqual(store.data["order_submission"]["state"], "SUBMISSION_UNKNOWN")
                self.assertEqual([event[0] for event in store.events], ["write", "submit", "query"])

    def test_concurrent_runners_with_same_scheduled_order_submit_once(self):
        # Same original two-runner counterexample, now entering through the claim.
        # The barrier moves before claim: a loser must never call the state loader.
        shared_state = {"order_submission": {"state": "RESERVED"}}
        loader_barrier = threading.Barrier(2)
        lock = threading.Lock()
        submissions = []
        errors = []
        owners, reads, blocked = [], [], []

        def claim(owner):
            loader_barrier.wait(timeout=2)
            with lock:
                if owners:
                    return False
                owners.append(owner)
                return True

        def load(*, normalize=False):
            with lock:
                reads.append(True)
                return copy.deepcopy(shared_state)

        def write(state):
            with lock:
                shared_state.clear()
                shared_state.update(copy.deepcopy(state))
            return True

        class Client:
            def order_market_buy(self, **kwargs):
                with lock:
                    submissions.append(kwargs["newClientOrderId"])
                return {"status": "FILLED"}

        def execute(run_id):
            runtime = ExecutionRuntime(
                dry_run=False,
                run_id=run_id,
                client=Client(),
                state_loader=load,
                state_writer=write,
                state_owner_claim=claim,
                state_owner_release=lambda _owner: True,
            )
            try:
                if not acquire_runtime_state_owner(runtime):
                    blocked.append(run_id)
                    return
                runtime_call_client(
                    runtime,
                    build_execution_report(runtime),
                    method_name="order_market_buy",
                    payload={"symbol": "BTCUSDT", "quantity": 0.01},
                    effect_type="order_buy",
                    max_retries=0,
                )
            except Exception as exc:
                errors.append(exc)

        runners = [threading.Thread(target=execute, args=(f"duplicate-{index}",)) for index in range(2)]
        for runner in runners:
            runner.start()
        for runner in runners:
            runner.join(timeout=3)

        self.assertFalse(any(runner.is_alive() for runner in runners))
        self.assertEqual(errors, [])
        self.assertEqual(len(submissions), 1)
        self.assertEqual(len(reads), 1)
        self.assertEqual(len(blocked), 1)

    def test_earn_timeout_submits_once_and_keeps_unknown(self):
        cases = (
            ("redeem_simple_earn_flexible_product", "earn_redeem", "redeemId"),
            ("subscribe_simple_earn_flexible_product", "earn_subscribe", "purchaseId"),
        )
        for method_name, effect_type, _response_id in cases:
            with self.subTest(method_name=method_name):
                store = DurableSubmissionStateStore()
                calls = []

                class Client:
                    def redeem_simple_earn_flexible_product(self, **kwargs):
                        calls.append(("redeem", kwargs))
                        raise TimeoutError("provider-submit-secret")

                    def subscribe_simple_earn_flexible_product(self, **kwargs):
                        calls.append(("subscribe", kwargs))
                        raise TimeoutError("provider-submit-secret")

                runtime = owned_runtime(
                    dry_run=False,
                    run_id=f"earn-timeout-{effect_type}",
                    client=Client(),
                    state_loader=store.load,
                    state_writer=store.write,
                )

                with patch("runtime_support._rate_limit_pause"), patch("runtime_support.time.sleep"):
                    with self.assertRaises(RuntimeError) as raised:
                        runtime_call_client(
                            runtime,
                            build_execution_report(runtime),
                            method_name=method_name,
                            payload={"productId": "earn-1", "amount": 1.0},
                            effect_type=effect_type,
                        )

                self.assertEqual(len(calls), 1)
                self.assertIsInstance(raised.exception, OrderReconciliationError)
                self.assertEqual(store.data["order_submission"]["state"], "SUBMISSION_UNKNOWN")
                self.assertEqual(store.data["order_submission"]["method_name"], method_name)

    def test_confirmed_earn_success_terminalizes_existing_submission_state(self):
        cases = (
            ("redeem_simple_earn_flexible_product", "earn_redeem", "redeemId"),
            ("subscribe_simple_earn_flexible_product", "earn_subscribe", "purchaseId"),
        )
        for method_name, effect_type, response_id in cases:
            with self.subTest(method_name=method_name):
                store = DurableSubmissionStateStore()
                calls = []

                class Client:
                    def redeem_simple_earn_flexible_product(self, **kwargs):
                        calls.append(("redeem", kwargs))
                        return {"success": True, "redeemId": 1}

                    def subscribe_simple_earn_flexible_product(self, **kwargs):
                        calls.append(("subscribe", kwargs))
                        return {"success": True, "purchaseId": 1}

                runtime = owned_runtime(
                    dry_run=False,
                    run_id=f"earn-success-{effect_type}",
                    client=Client(),
                    state_loader=store.load,
                    state_writer=store.write,
                )

                response = runtime_call_client(
                    runtime,
                    build_execution_report(runtime),
                    method_name=method_name,
                    payload={"productId": "earn-1", "amount": 1.0},
                    effect_type=effect_type,
                    max_retries=0,
                )

                self.assertTrue(response["success"])
                self.assertIn(response_id, response)
                self.assertEqual(len(calls), 1)
                self.assertEqual(store.data["order_submission"], {"state": "TERMINAL"})

    def test_unconfirmed_earn_response_keeps_unknown(self):
        store = DurableSubmissionStateStore()

        class Client:
            def subscribe_simple_earn_flexible_product(self, **_kwargs):
                return {"success": False}

        runtime = owned_runtime(
            dry_run=False,
            run_id="earn-unconfirmed",
            client=Client(),
            state_loader=store.load,
            state_writer=store.write,
        )

        with self.assertRaisesRegex(OrderReconciliationError, "order_reconciliation_uncertain"):
            runtime_call_client(
                runtime,
                build_execution_report(runtime),
                method_name="subscribe_simple_earn_flexible_product",
                payload={"productId": "earn-1", "amount": 1.0},
                effect_type="earn_subscribe",
                max_retries=0,
            )

        self.assertEqual(store.data["order_submission"]["state"], "SUBMISSION_UNKNOWN")

    def test_unknown_earn_blocks_later_funding_without_order_query(self):
        store = DurableSubmissionStateStore(
            {
                "state": "SUBMISSION_UNKNOWN",
                "identity_sha256": "a" * 64,
                "method_name": "subscribe_simple_earn_flexible_product",
            }
        )
        observed = []

        class Client:
            def order_market_buy(self, **_kwargs):
                observed.append("order_submit")
                raise AssertionError("UNKNOWN must block funding submission")

            def subscribe_simple_earn_flexible_product(self, **_kwargs):
                observed.append("earn_submit")
                raise AssertionError("UNKNOWN must block funding submission")

            def get_order(self, **_kwargs):
                observed.append("order_query")
                raise AssertionError("Earn UNKNOWN must never use market-order reconciliation")

        for method_name, payload, effect_type in (
            ("order_market_buy", {"symbol": "BTCUSDT", "quantity": 0.01}, "order_buy"),
            (
                "subscribe_simple_earn_flexible_product",
                {"productId": "earn-1", "amount": 1.0},
                "earn_subscribe",
            ),
        ):
            with self.subTest(method_name=method_name):
                runtime = owned_runtime(
                    dry_run=False,
                    run_id=f"blocked-{method_name}",
                    client=Client(),
                    state_loader=store.load,
                    state_writer=store.write,
                )
                with self.assertRaisesRegex(OrderReconciliationError, "order_reconciliation_uncertain"):
                    runtime_call_client(
                        runtime,
                        build_execution_report(runtime),
                        method_name=method_name,
                        payload=payload,
                        effect_type=effect_type,
                        max_retries=0,
                    )

        self.assertEqual(observed, [])
        self.assertEqual(store.data["order_submission"]["state"], "SUBMISSION_UNKNOWN")

        market_store = DurableSubmissionStateStore(
            {"state": "SUBMISSION_UNKNOWN", "identity_sha256": "b" * 64, "symbol": "BTCUSDT"}
        )
        runtime = owned_runtime(
            dry_run=False,
            run_id="blocked-earn-after-market",
            client=Client(),
            state_loader=market_store.load,
            state_writer=market_store.write,
        )
        with self.assertRaisesRegex(OrderReconciliationError, "order_reconciliation_uncertain"):
            runtime_call_client(
                runtime,
                build_execution_report(runtime),
                method_name="subscribe_simple_earn_flexible_product",
                payload={"productId": "earn-1", "amount": 1.0},
                effect_type="earn_subscribe",
                max_retries=0,
            )

        self.assertEqual(observed, [])
        self.assertEqual(market_store.data["order_submission"]["state"], "SUBMISSION_UNKNOWN")

    def test_restart_with_exact_unknown_intent_recovers_without_resubmit(self):
        payload = {"symbol": "BTCUSDT", "quantity": 0.01, "newClientOrderId": "restart-unknown"}
        _, identity_sha256 = _ensure_order_logical_identity(
            owned_runtime(), "order_market_buy", payload
        )
        store = DurableSubmissionStateStore({
            "state": "SUBMISSION_UNKNOWN",
            "identity_sha256": identity_sha256,
            "symbol": "BTCUSDT",
        })

        class Client:
            def order_market_buy(self, **_kwargs):
                store.events.append(("submit",))
                raise AssertionError("UNKNOWN must not submit")

            def get_order(self, *, origClientOrderId, **_kwargs):
                store.events.append(("query",))
                return order_query_response(client_order_id=origClientOrderId, status="FILLED")

        runtime = owned_runtime(
            dry_run=False,
            run_id="restart-unknown",
            client=Client(),
            state_loader=store.load,
            state_writer=store.write,
        )

        result = runtime_call_client(
            runtime,
            build_execution_report(runtime),
            method_name="order_market_buy",
            payload=payload,
            effect_type="order_buy",
            max_retries=0,
        )

        self.assertEqual(result["status"], "FILLED")
        self.assertEqual([event[0] for event in store.events], ["query", "write"])
        self.assertEqual(store.data["order_submission"], {"state": "TERMINAL"})

    def test_unknown_order_cannot_complete_a_different_requested_order(self):
        variants = (
            ("symbol", "order_market_buy", {"symbol": "ETHUSDT", "quantity": 0.01}),
            ("side", "order_market_sell", {"symbol": "BTCUSDT", "quantity": 0.01}),
            ("quantity", "order_market_buy", {"symbol": "BTCUSDT", "quantity": 0.02}),
        )
        for label, new_method, new_payload in variants:
            with self.subTest(changed=label):
                store = DurableSubmissionStateStore()

                class InitialClient:
                    def order_market_buy(self, **_kwargs):
                        store.events.append(("submit_old",))
                        raise TimeoutError("provider-submit-secret")

                    def get_order(self, *, origClientOrderId, **_kwargs):
                        store.events.append(("query_old",))
                        return order_query_response(client_order_id=origClientOrderId, status="NEW")

                initial_runtime = owned_runtime(
                    dry_run=False,
                    run_id="old-unknown",
                    client=InitialClient(),
                    state_loader=store.load,
                    state_writer=store.write,
                )
                with self.assertRaisesRegex(OrderReconciliationError, "order_reconciliation_uncertain"):
                    runtime_call_client(
                        initial_runtime,
                        build_execution_report(initial_runtime),
                        method_name="order_market_buy",
                        payload={"symbol": "BTCUSDT", "quantity": 0.01, "newClientOrderId": "unknown-order"},
                        effect_type="order_buy",
                        max_retries=0,
                    )

                class RestartClient:
                    def order_market_buy(self, **_kwargs):
                        store.events.append(("submit_new",))
                        raise AssertionError("UNKNOWN must not submit a new order")

                    def order_market_sell(self, **_kwargs):
                        store.events.append(("submit_new",))
                        raise AssertionError("UNKNOWN must not submit a new order")

                    def get_order(self, *, origClientOrderId, **_kwargs):
                        store.events.append(("query_old",))
                        return order_query_response(client_order_id=origClientOrderId, status="FILLED")

                restart_runtime = owned_runtime(
                    dry_run=False,
                    run_id="new-request",
                    client=RestartClient(),
                    state_loader=store.load,
                    state_writer=store.write,
                )
                with self.assertRaisesRegex(OrderReconciliationError, "order_reconciliation_intent_mismatch"):
                    runtime_call_client(
                        restart_runtime,
                        build_execution_report(restart_runtime),
                        method_name=new_method,
                        payload={**new_payload, "newClientOrderId": "unknown-order"},
                        effect_type="order_buy" if new_method.endswith("buy") else "order_sell",
                        max_retries=0,
                    )

                self.assertNotIn(("submit_new",), store.events)
                self.assertEqual(store.data["order_submission"]["state"], "SUBMISSION_UNKNOWN")

    def test_unknown_order_with_different_client_identity_does_not_query_or_submit(self):
        payload = {"symbol": "BTCUSDT", "quantity": 0.01, "newClientOrderId": "old-identity"}
        _, identity_sha256 = _ensure_order_logical_identity(
            owned_runtime(), "order_market_buy", payload
        )
        store = DurableSubmissionStateStore({
            "state": "SUBMISSION_UNKNOWN",
            "identity_sha256": identity_sha256,
            "symbol": "BTCUSDT",
        })
        observed = []

        class Client:
            def order_market_buy(self, **_kwargs):
                observed.append("submit")
                raise AssertionError("UNKNOWN must not submit")

            def get_order(self, **_kwargs):
                observed.append("query")
                raise AssertionError("different identities must not reconcile")

        runtime = owned_runtime(
            dry_run=False,
            run_id="different-client-identity",
            client=Client(),
            state_loader=store.load,
            state_writer=store.write,
        )
        with self.assertRaisesRegex(OrderReconciliationError, "order_reconciliation_intent_mismatch"):
            runtime_call_client(
                runtime,
                build_execution_report(runtime),
                method_name="order_market_buy",
                payload={"symbol": "BTCUSDT", "quantity": 0.01, "newClientOrderId": "new-identity"},
                effect_type="order_buy",
                max_retries=0,
            )

        self.assertEqual(observed, [])
        self.assertEqual(store.data["order_submission"]["state"], "SUBMISSION_UNKNOWN")

    def test_unknown_quote_order_uses_original_quote_amount_not_fill_amount(self):
        store = DurableSubmissionStateStore()
        payload = {"symbol": "BNBUSDT", "quoteOrderQty": 20, "newClientOrderId": "quote-unknown"}

        class InitialClient:
            def order_market_buy(self, **_kwargs):
                raise TimeoutError("provider-submit-secret")

            def get_order(self, *, origClientOrderId, **_kwargs):
                response = order_query_response(
                    client_order_id=origClientOrderId,
                    status="NEW",
                    symbol="BNBUSDT",
                    quantity="0",
                )
                response["origQuoteOrderQty"] = "20"
                return response

        initial_runtime = owned_runtime(
            dry_run=False,
            run_id="quote-initial",
            client=InitialClient(),
            state_loader=store.load,
            state_writer=store.write,
        )
        with self.assertRaisesRegex(OrderReconciliationError, "order_reconciliation_uncertain"):
            runtime_call_client(
                initial_runtime,
                build_execution_report(initial_runtime),
                method_name="order_market_buy",
                payload=payload,
                effect_type="order_buy",
                max_retries=0,
            )

        class RestartClient:
            def order_market_buy(self, **_kwargs):
                raise AssertionError("UNKNOWN must not submit")

            def get_order(self, *, origClientOrderId, **_kwargs):
                response = order_query_response(
                    client_order_id=origClientOrderId,
                    status="FILLED",
                    symbol="BNBUSDT",
                    quantity="0",
                )
                response.update({"origQuoteOrderQty": "20", "cummulativeQuoteQty": "19.5"})
                return response

        restart_runtime = owned_runtime(
            dry_run=False,
            run_id="quote-restart",
            client=RestartClient(),
            state_loader=store.load,
            state_writer=store.write,
        )
        result = runtime_call_client(
            restart_runtime,
            build_execution_report(restart_runtime),
            method_name="order_market_buy",
            payload=payload,
            effect_type="order_buy",
            max_retries=0,
        )

        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(store.data["order_submission"], {"state": "TERMINAL"})

    def test_verified_terminal_query_response_is_required_to_write_terminal(self):
        store = DurableSubmissionStateStore()

        class Client:
            def order_market_buy(self, **_kwargs):
                store.events.append(("submit",))
                raise HTTPError("provider-submit-secret")

            def get_order(self, *, origClientOrderId, **_kwargs):
                store.events.append(("query",))
                return order_query_response(client_order_id=origClientOrderId, status="REJECTED")

        runtime = owned_runtime(
            dry_run=False,
            run_id="verified-terminal",
            client=Client(),
            state_loader=store.load,
            state_writer=store.write,
        )

        with self.assertRaisesRegex(ClientCallError, "order_submission_failed"):
            runtime_call_client(
                runtime,
                build_execution_report(runtime),
                method_name="order_market_buy",
                payload={"symbol": "BTCUSDT", "quantity": 0.01, "newClientOrderId": "verified-terminal"},
                effect_type="order_buy",
                max_retries=0,
            )

        self.assertEqual(store.data["order_submission"], {"state": "TERMINAL"})
        self.assertEqual([event[0] for event in store.events], ["write", "submit", "query", "write"])

    def test_terminal_write_failure_keeps_unknown_and_restart_does_not_resubmit(self):
        class TerminalWriteFailureStore(DurableSubmissionStateStore):
            def write(self, state):
                record = copy.deepcopy(state["order_submission"])
                self.events.append(("write", record["state"]))
                if record["state"] == "TERMINAL":
                    return False
                self.data = copy.deepcopy(state)
                return True

        store = TerminalWriteFailureStore()

        class InitialClient:
            def order_market_buy(self, **_kwargs):
                store.events.append(("submit",))
                return {"status": "FILLED"}

        initial_runtime = owned_runtime(
            dry_run=False,
            run_id="terminal-write-failure",
            client=InitialClient(),
            state_loader=store.load,
            state_writer=store.write,
        )

        with self.assertRaisesRegex(RuntimeError, "state_persistence_failed"):
            runtime_call_client(
                initial_runtime,
                build_execution_report(initial_runtime),
                method_name="order_market_buy",
                payload={"symbol": "BTCUSDT", "quantity": 0.01, "newClientOrderId": "terminal-write-failure"},
                effect_type="order_buy",
                max_retries=0,
            )

        class RestartClient:
            def order_market_buy(self, **_kwargs):
                store.events.append(("submit",))
                raise AssertionError("UNKNOWN must not submit")

            def get_order(self, *, origClientOrderId, **_kwargs):
                store.events.append(("query",))
                return order_query_response(client_order_id=origClientOrderId, status="FILLED")

        restart_runtime = owned_runtime(
            dry_run=False,
            run_id="terminal-write-failure-restart",
            client=RestartClient(),
            state_loader=store.load,
            state_writer=store.write,
        )

        with self.assertRaisesRegex(RuntimeError, "state_persistence_failed"):
            runtime_call_client(
                restart_runtime,
                build_execution_report(restart_runtime),
                method_name="order_market_buy",
                payload={"symbol": "BTCUSDT", "quantity": 0.01, "newClientOrderId": "terminal-write-failure"},
                effect_type="order_buy",
                max_retries=0,
            )

        self.assertEqual(store.data["order_submission"]["state"], "SUBMISSION_UNKNOWN")
        self.assertEqual([event[0] for event in store.events], ["write", "submit", "write", "query", "write"])

    def test_local_pre_submit_failure_does_not_call_client_or_write_unknown(self):
        store = DurableSubmissionStateStore()

        class Client:
            def order_market_buy(self, **_kwargs):
                store.events.append(("submit",))
                raise AssertionError("pre-submit validation must stop first")

        runtime = owned_runtime(
            dry_run=False,
            run_id="invalid-symbol",
            client=Client(),
            state_loader=store.load,
            state_writer=store.write,
        )

        with self.assertRaisesRegex(RuntimeError, "submission_state_invalid"):
            runtime_call_client(
                runtime,
                build_execution_report(runtime),
                method_name="order_market_buy",
                payload={"symbol": "invalid symbol", "quantity": 0.01},
                effect_type="order_buy",
                max_retries=0,
            )

        self.assertEqual(store.events, [])
        self.assertEqual(store.data["order_submission"], {"state": "RESERVED"})

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
                        return order_query_response(client_order_id=origClientOrderId, status="NEW")

                runtime = build_order_runtime(dry_run=False, run_id="stable-order", client=Client())
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

        runtime = build_order_runtime(dry_run=False, run_id="none-reconciliation", client=Client())
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

        runtime = build_order_runtime(dry_run=False, run_id="query-error", client=Client())
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

        runtime = build_order_runtime(dry_run=False, run_id="invalid-reconciliation", client=Client())
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

        runtime = build_order_runtime(dry_run=False, run_id="order-not-found-retry", client=Client())
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

        runtime = build_order_runtime(dry_run=False, run_id="order-not-found-exhausted", client=Client())
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

    def test_deterministic_order_rejection_queries_before_fail_closed(self):
        observed = []

        class RejectedOrder(Exception):
            code = -1013

        class Client:
            def order_market_buy(self, **kwargs):
                observed.append(("submit", kwargs["newClientOrderId"]))
                raise RejectedOrder("SENSITIVE_PROVIDER_SENTINEL")

            def get_order(self, **_kwargs):
                observed.append(("reconcile",))
                return {"status": "NEW"}

        runtime = build_order_runtime(dry_run=False, run_id="rejected-order", client=Client())
        report = build_execution_report(runtime)

        with self.assertRaises(OrderReconciliationError) as raised:
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
        self.assertNotIn("SENSITIVE_PROVIDER_SENTINEL", str(raised.exception) + str(report))

    def test_requests_http_error_queries_before_fail_closed(self):
        observed = []

        class Client:
            def order_market_buy(self, **_kwargs):
                observed.append("submit")
                raise HTTPError("SENSITIVE_PROVIDER_SENTINEL")

            def get_order(self, **_kwargs):
                observed.append("reconcile")
                return {"status": "NEW"}

        runtime = build_order_runtime(dry_run=False, run_id="http-error", client=Client())
        report = build_execution_report(runtime)

        with self.assertRaises(OrderReconciliationError):
            runtime_call_client(
                runtime,
                report,
                method_name="order_market_buy",
                payload={"symbol": "BTCUSDT", "quantity": 0.01},
                effect_type="order_buy",
                max_retries=1,
                retry_base_sec=0,
            )

        self.assertEqual(observed, ["submit", "reconcile"])

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
                        return order_query_response(client_order_id=origClientOrderId, status="NEW")

                runtime = build_order_runtime(dry_run=False, run_id=f"uncertain-{code}", client=Client())
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
                        return {"status": status, "clientOrderId": kwargs["newClientOrderId"], "executedQty": "0"}

                runtime = build_order_runtime(dry_run=False, run_id=f"direct-{status}", client=Client())
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
                        return {"status": status, "clientOrderId": kwargs["newClientOrderId"], "executedQty": "0"}

                runtime = build_order_runtime(dry_run=False, run_id=f"direct-{status}", client=Client())
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
                return {"status": "FILLED", "clientOrderId": kwargs["newClientOrderId"]}

        runtime = build_order_runtime(dry_run=False, run_id="direct-filled", client=Client())
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

    def test_report_uses_runtime_target_service_identity(self):
        runtime_target = build_runtime_target(
            platform_id="binance",
            strategy_profile="crypto_live_pool_rotation",
            dry_run_only=False,
            service_name="binance-platform",
        )
        runtime = owned_runtime(
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
        runtime = owned_runtime(
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


def test_unowned_live_call_refuses_before_state_or_broker():
    from unittest.mock import Mock
    client, loader, writer = Mock(), Mock(), Mock()
    runtime = ExecutionRuntime(dry_run=False, client=client, state_loader=loader, state_writer=writer)
    with unittest.TestCase().assertRaisesRegex(RuntimeError, 'state_owner_required'):
        runtime_call_client(runtime, build_execution_report(runtime), method_name='order_market_buy', payload={'symbol': 'BTCUSDT', 'quantity': 0.01}, effect_type='order_buy', max_retries=0)
    loader.assert_not_called()
    writer.assert_not_called()
    client.order_market_buy.assert_not_called()


def test_owner_release_requires_confirmed_fill_and_existing_persisted_action():
    import pytest
    from unittest.mock import Mock
    from runtime_support import StatePersistenceError
    client = Mock()
    client.order_market_buy.return_value = {'status': 'FILLED'}
    runtime = build_order_runtime(client=client)
    release = runtime.state_owner_release = Mock(return_value=True)
    report = build_execution_report(runtime)
    runtime_call_client(runtime, report, method_name='order_market_buy',
                        payload={'symbol': 'ETHUSDT', 'quantity': 1}, effect_type='order_buy')
    assert release_runtime_state_owner(runtime) is False
    release.assert_not_called()
    state = runtime.trade_state
    state['trend_action_history'] = {'ETHUSDT': {'action': 'buy', 'date': runtime.now_utc.strftime('%Y%m%d')}}
    writer = runtime.state_writer
    runtime.state_writer = lambda _state: False
    with pytest.raises(StatePersistenceError):
        runtime_set_trade_state(runtime, report, state, reason='trend_buy:ETHUSDT')
    assert release_runtime_state_owner(runtime) is False
    runtime.state_writer = writer
    runtime_set_trade_state(runtime, report, state, reason='trend_buy:ETHUSDT')
    assert release_runtime_state_owner(runtime) is True
    release.assert_called_once()
    assert acquire_runtime_state_owner(runtime) is True
    assert runtime.trade_state is None


def test_partial_or_unknown_fill_retains_owner_but_zero_fill_rejection_releases():
    import pytest
    from unittest.mock import Mock
    for status, filled in [('PARTIALLY_FILLED', '0.5'), ('CANCELED', '0.5'), ('CANCELED', '0')]:
        client = Mock()
        client.order_market_buy.return_value = {'status': status, 'executedQty': filled}
        runtime = build_order_runtime(client=client)
        release = runtime.state_owner_release = Mock(return_value=True)
        with pytest.raises((ClientCallError, OrderReconciliationError)):
            runtime_call_client(runtime, build_execution_report(runtime), method_name='order_market_buy',
                                payload={'symbol': 'ETHUSDT', 'quantity': 1}, effect_type='order_buy')
        assert release_runtime_state_owner(runtime) is (filled == '0')
        assert release.call_count == int(filled == '0')


def test_confirmed_earn_and_fuel_require_fresh_balances_and_successful_state_write():
    import pytest
    from unittest.mock import Mock
    from runtime_support import ExecutionIntegrityError, StatePersistenceError
    for method, payload, effect, asset, response in [
        ('order_market_buy', {'symbol': 'BNBUSDT', 'quantity': 1}, 'order_buy', None, {'status': 'FILLED'}),
        ('subscribe_simple_earn_flexible_product', {'productId': 'synthetic', 'amount': 1}, 'earn_subscribe', 'USDT', {'success': True, 'purchaseId': 1}),
    ]:
        client = Mock()
        getattr(client, method).return_value = response
        client.get_asset_balance.return_value = {'free': '10', 'locked': '0'}
        client.get_simple_earn_flexible_product_position.return_value = {'rows': []}
        runtime = build_order_runtime(client=client)
        runtime.state_owner_release = Mock(return_value=True)
        report = build_execution_report(runtime)
        runtime_call_client(runtime, report, method_name=method, payload=payload, effect_type=effect, accounting_asset=asset)
        assert release_runtime_state_owner(runtime) is False
        state = runtime.trade_state
        client.get_asset_balance.side_effect = TimeoutError('synthetic')
        with pytest.raises(ExecutionIntegrityError, match='cash_reconciliation_uncertain'):
            reconcile_runtime_cash_effects(runtime, state)
        assert release_runtime_state_owner(runtime) is False
        client.get_asset_balance.side_effect = None
        reconcile_runtime_cash_effects(runtime, state)
        runtime.state_writer = lambda _state: False
        with pytest.raises(StatePersistenceError):
            runtime_set_trade_state(runtime, report, state, reason='cycle_complete')
        assert release_runtime_state_owner(runtime) is False
        runtime.state_writer = lambda _state: True
        runtime_set_trade_state(runtime, report, state, reason='cycle_complete')
        assert release_runtime_state_owner(runtime) is True


def test_release_uncertainty_does_not_authorize_old_owner_and_readonly_never_claims():
    import pytest
    from unittest.mock import Mock
    from runtime_support import StatePersistenceError
    runtime = owned_runtime()
    runtime.state_owner_release = Mock(side_effect=TimeoutError('synthetic'))
    with pytest.raises(StatePersistenceError, match='state_owner_release_uncertain'):
        release_runtime_state_owner(runtime)
    assert runtime.state_owner_held is False
    for kwargs in ({'dry_run': True}, {'standard_execution_permitted': False}):
        claim = Mock()
        runtime = ExecutionRuntime(state_owner_claim=claim, **kwargs)
        assert acquire_runtime_state_owner(runtime) is True
        claim.assert_not_called()
        assert runtime.state_owner_held is False
