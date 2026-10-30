import json
import ast
import unittest
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from notifications import unified_notifications as unified
from notifications import routing, operational
from notifications.digest_snapshot.unified_contract import order_identity_digest
from reporting.status_reports import maybe_send_periodic_btc_status_report


TARGET = {"platform_id": "binance", "strategy_profile": "synthetic", "service_name": "synthetic-service",
          "account_scope": "synthetic", "account_selector": ["synthetic"], "deployment_selector": "synthetic",
          "dry_run_only": False}


class Store:
    def __init__(self):
        self.data = {}
    def load(self, key):
        return self.data.get(key, (None, 0))
    def compare_and_swap(self, key, generation, payload):
        if self.load(key)[1] != generation:
            return False
        self.data[key] = (payload, generation + 1)
        return True


def env(flag):
    return {"BINANCE_UNIFIED_NOTIFICATION_ENABLED": flag, "RUNTIME_TARGET_JSON": json.dumps(TARGET),
            "DIGEST_TARGET_ID": "synthetic-target", "DIGEST_OPAQUE_ACCOUNT_UID": "synthetic-account",
            "DIGEST_STORE_TARGET": "synthetic-slot", "QSL_DIGEST_ROUTINE_ORDERS": "false"}


def report():
    return {"run_id": "synthetic-run", "started_at": datetime.now(timezone.utc).isoformat(),
            "runtime_target": TARGET, "strategy_profile": "synthetic", "service_name": "synthetic-service",
            "platform": "binance", "schema_version": "runtime_report.v1", "status": "ok", "dry_run": False,
            "errors": [], "summary": {}, "notifications": []}


class DigestOnlyDispatch(unittest.TestCase):
    def test_coverage_locale_respects_alias_variants_and_blank_fallback(self):
        for values, expected in (({"QSL_NOTIFY_LANG": " zh-CN ", "NOTIFY_LANG": "en"}, "zh"),
                                 ({"QSL_NOTIFY_LANG": "EN-us", "NOTIFY_LANG": "zh"}, "en"),
                                 ({"QSL_NOTIFY_LANG": " ", "NOTIFY_LANG": " "}, "zh")):
            with self.subTest(values=values), patch.dict("os.environ", values, clear=True):
                self.assertEqual(operational.message("coverage_failure"), operational._TEXT[expected]["coverage_failure"])

    def test_verified_reconciled_identity_never_reuses_prior_order(self):
        tree = ast.parse((Path(__file__).parents[1] / "runtime_support.py").read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {"_notification_order_identity", "runtime_call_client"}]
        module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
        association = {"symbol": "SYNUSDT", "side": "BUY", "client_order_id": "current-id"}
        response = {"status": "FILLED"}
        side_effects, broker_calls = [], []
        class Client:
            def order_market_buy(self, **kwargs):
                broker_calls.append(kwargs)
                raise TimeoutError("synthetic")
        runtime = SimpleNamespace(dry_run=False, standard_execution_permitted=True, client=Client(),
                                  pending_funds=[], fuel_symbol="BNBUSDT", fuel_asset="BNB", side_effect_log=[
                                      {"effect_type": "order_buy", "payload": {"newClientOrderId": "old-id"}}])
        class ReconcileError(Exception):
            pass
        namespace = {"_EARN_METHOD_BY_EFFECT_TYPE": {}, "_EARN_SUCCESS_ID_BY_METHOD": {},
            "_ORDER_SUBMISSION_UNKNOWN": "UNKNOWN", "_ORDER_SUBMISSION_RESERVED": "RESERVED",
            "_ORDER_SUBMISSION_TERMINAL": "TERMINAL", "_ORDER_SUBMISSION_FILLED_ACCOUNTING_PENDING": "PENDING",
            "require_runtime_state_owner": lambda runtime: None,
            "_ensure_order_logical_identity": lambda runtime, method, payload: ({**payload, "newClientOrderId": "current-id"}, "current-hash"),
            "_build_order_request_association": lambda *args: association,
            "_reconcile_uncertain_order": lambda *args: response,
            "_reconciled_order_matches_request": lambda result, assoc: result is response,
            "_complete_order_response": lambda runtime, report, state, result, **kwargs: result,
            "_persist_order_submission_state": lambda *args: None,
            "record_order_submission_attempt": lambda report: None,
            "record_order_transport_uncertainty": lambda report: None,
            "_is_order_transport_uncertainty": lambda exc: True,
            "_rate_limit_pause": lambda: None,
            "record_side_effect": lambda *args, **kwargs: side_effects.append(kwargs),
            "OrderReconciliationError": ReconcileError, "ExecutionIntegrityError": ReconcileError,
            "StatePersistenceError": ReconcileError}
        exec(compile(module, "runtime_support.py", "exec"), namespace)
        call = namespace["runtime_call_client"]
        payload = {"symbol": "SYNUSDT", "quantity": 1}
        for state in ("UNKNOWN", "RESERVED"):
            with self.subTest(state=state):
                runtime.pending_funds.clear()
                broker_calls.clear()
                namespace["_load_order_submission_state"] = lambda runtime: ({},
                    {"state": state, "identity_sha256": "current-hash", "symbol": "SYNUSDT"})
                self.assertEqual(call(runtime, {}, method_name="order_market_buy", payload=payload, effect_type="order_buy"), response)
                self.assertEqual(unified.current_order_digest(runtime), order_identity_digest("current-id"))
                self.assertEqual(len(broker_calls), 0 if state == "UNKNOWN" else 1)
        namespace["_reconciled_order_matches_request"] = lambda *args: False
        namespace["_load_order_submission_state"] = lambda runtime: ({}, {"state": "UNKNOWN", "identity_sha256": "current-hash", "symbol": "SYNUSDT"})
        with self.assertRaises(ReconcileError):
            call(runtime, {}, method_name="order_market_buy", payload=payload, effect_type="order_buy")
        self.assertIsNone(unified.current_order_digest(runtime))
        self.assertEqual(side_effects, [])
        runtime.client.order_market_buy = lambda **kwargs: response
        namespace["_load_order_submission_state"] = lambda runtime: ({}, {"state": "RESERVED"})
        self.assertEqual(call(runtime, {}, method_name="order_market_buy", payload=payload, effect_type="order_buy"), response)
        self.assertEqual(unified.current_order_digest(runtime), order_identity_digest("current-id"))
        self.assertEqual(len(side_effects), 1)
        for hook in ("begin_order_attempt", "record_confirmed_order_identity"):
            with self.subTest(hook=hook), patch.object(unified, hook, side_effect=RuntimeError("synthetic")):
                self.assertEqual(call(runtime, {}, method_name="order_market_buy", payload=payload, effect_type="order_buy"), response)
                if hook == "begin_order_attempt":
                    self.assertEqual(unified.current_order_digest(runtime), order_identity_digest("current-id"))
                else:
                    self.assertIsNone(unified.current_order_digest(runtime))

    def test_runtime_notify_entry_routes_flag_off_and_uses_error_only_fallback(self):
        tree = ast.parse((Path(__file__).parents[1] / "runtime_support.py").read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "runtime_notify")
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        sent = []
        namespace = {"_legacy_runtime_notify": lambda runtime, report, text, urgent: sent.append(text) or True}
        exec(compile(module, "runtime_support.py", "exec"), namespace)
        runtime, source = SimpleNamespace(dry_run=False), report()
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(namespace["runtime_notify"](runtime, source, "normal success", urgent=False))
        self.assertEqual(len(sent), 1)
        self.assertNotIn("normal success", sent[0])
        self.assertIn("日报候选", sent[0])

    def test_actual_routine_route_writes_candidate_for_all_flag_values(self):
        for flag in ("", "false", "true"):
            for kind in ("routine_cycle", "fill"):
                with self.subTest(flag=flag, kind=kind):
                    store, sent, coverage = Store(), [], []
                    source = report()
                    result = unified.route(lambda: sent.append("normal"), kind=kind, env=env(flag), report=source,
                        candidate_store=store, order_digest=order_identity_digest("synthetic-order") if kind == "fill" else None,
                        send_coverage=lambda: coverage.append("coverage"))
                    self.assertFalse(result)
                    self.assertEqual((sent, coverage), ([], []))
                    rows = next(iter(store.data.values()))[0]["runs"]
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["source_run_id"], "synthetic-run")
                    self.assertEqual(source["notifications"][-1]["delivery_status"], "deferred_digest")

    def test_failed_identity_or_store_never_sends_success(self):
        source, sent, coverage = report(), [], []
        invalid = env("")
        invalid.pop("DIGEST_TARGET_ID")
        result = unified.route(lambda: sent.append("normal"), kind="routine_cycle", env=invalid, report=source,
                               send_coverage=lambda: coverage.append("coverage"))
        self.assertFalse(result)
        self.assertEqual((sent, coverage), ([], ["coverage"]))
        result = unified.route(lambda: sent.append("normal"), kind="routine_cycle", env=invalid, report=source,
                               send_coverage=lambda: (_ for _ in ()).throw(OSError("synthetic")))
        self.assertFalse(result)
        self.assertEqual(sent, [])
        class Broken(Store):
            def compare_and_swap(self, *args):
                raise OSError("synthetic")
        coverage.clear()
        result = unified.route(lambda: sent.append("normal"), kind="routine_cycle", env=env("true"), report=source,
                               candidate_store=Broken(), send_coverage=lambda: coverage.append("coverage"))
        self.assertFalse(result)
        self.assertEqual((sent, coverage), ([], ["coverage"]))

    def test_flag_off_routine_facade_still_uses_unified_boundary(self):
        runtime = SimpleNamespace(dry_run=False, side_effect_log=[])
        with patch.object(unified, "route", return_value=False) as route:
            routing.routine_notification(lambda *args: True, runtime, report(), "normal", routine_order=False)
        self.assertEqual(route.call_args.kwargs["kind"], "routine_cycle")

    def test_healthy_workflow_does_not_send_under_legacy_flag(self):
        sent = []
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(operational.notify_workflow({"status": "healthy"}, "healthy", send=sent.append))
        self.assertEqual(sent, [])

    def test_direct_periodic_heartbeat_without_coverage_sends_error_only(self):
        sent = []
        now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        snapshot = {"ahr999": 1.0, "zscore": 0.0, "sell_trigger": 2.0, "regime_on": True}
        with patch.dict("os.environ", {}, clear=True):
            result = maybe_send_periodic_btc_status_report({}, "token", "chat", now, 1, 100, 50, 0, 1,
                snapshot, .5, "synthetic", translate_fn=lambda key, **kwargs: key, separator="-",
                send_tg_msg_fn=lambda token, chat, text: sent.append(text) or True)
        self.assertFalse(result)
        self.assertEqual(len(sent), 1)
        self.assertIn("日报候选", sent[0])
        self.assertNotIn("heartbeat_title", sent[0])

    def test_direct_periodic_heartbeat_with_context_persists_without_send(self):
        sent, facts = [], []
        source = report()
        runtime = SimpleNamespace(dry_run=False)
        now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
        snapshot = {"ahr999": 1.0, "zscore": 0.0, "sell_trigger": 2.0, "regime_on": True}
        with patch.dict("os.environ", env("false"), clear=True), \
             patch("notifications.unified_producer.persist_event", side_effect=lambda fact, **kw: facts.append(fact) or True):
            token = unified.bind_cycle(runtime, source)
            try:
                result = maybe_send_periodic_btc_status_report({}, "token", "chat", now, 1, 100, 50, 0, 1,
                    snapshot, .5, "synthetic", translate_fn=lambda key, **kwargs: key, separator="-",
                    send_tg_msg_fn=lambda token, chat, text: sent.append(text) or True)
            finally:
                unified.reset_cycle(token)
        self.assertFalse(result)
        self.assertEqual(sent, [])
        self.assertEqual(facts[0]["event_kind"], "healthy_heartbeat")

    def test_true_error_keeps_legacy_immediate_delivery(self):
        sent = []
        result = unified.route(lambda: sent.append("error") or True,
                               kind="real_error", env=env("false"), report=report())
        self.assertTrue(result)
        self.assertEqual(sent, ["error"])

    def test_both_exporter_entrypoints_use_same_writer_with_flags_off(self):
        from scripts import emit_digest_candidates, emit_unified_digest_candidates
        expected = {"status": "projected", "uploaded": True}
        with patch("notifications.unified_producer.emit_archive", return_value=expected) as archive:
            self.assertEqual(emit_digest_candidates.emit_digest_candidates(env("false")), expected)
        archive.assert_called_once()
        with patch.object(emit_unified_digest_candidates, "emit_archive", return_value=expected) as archive:
            self.assertEqual(emit_unified_digest_candidates.emit(env("false")), expected)
        archive.assert_called_once()

    def test_lifecycle_has_one_writer_and_existing_alert_after_failure(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/runtime-target-lifecycle.yml").read_text()
        self.assertEqual(workflow.count("run: uv run --no-sync python scripts/emit_unified_digest_candidates.py"), 1)
        self.assertNotIn("run: uv run --no-sync python scripts/emit_digest_candidates.py", workflow)
        self.assertLess(workflow.index("id: digest_projection"), workflow.index("DIGEST_PROJECTION_FAILED:"))
        self.assertIn("steps.digest_projection.outcome == 'failure'", workflow)

    def test_existing_monitor_sends_only_coverage_error_after_projection_failure(self):
        from scripts import observe_notification_health
        sent = []
        with patch.dict("os.environ", {"DIGEST_PROJECTION_FAILED": "true", "NOTIFY_LANG": "en"}, clear=True), \
             patch.object(operational, "send_configured", side_effect=lambda text: sent.append(text) or True):
            self.assertEqual(observe_notification_health.main(), 0)
        self.assertEqual(sent, [operational._TEXT["en"]["coverage_failure"]])
        with patch.dict("os.environ", {"DIGEST_PROJECTION_FAILED": "true", "NOTIFY_LANG": "en"}, clear=True), \
             patch.object(operational, "send_configured", return_value={"transport_acknowledged": False}):
            self.assertEqual(observe_notification_health.main(), 1)


if __name__ == "__main__":
    unittest.main()
