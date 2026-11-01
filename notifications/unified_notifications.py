"""Binance's opt-in boundary around notification delivery only."""

from __future__ import annotations

import json
import hashlib
import os
from copy import deepcopy
from contextvars import ContextVar
from datetime import datetime, timezone

from .digest_snapshot.unified_contract import (
    IncidentTracker, NotificationEvent, select_notification_route,
    effective_notification_mode, order_identity_digest, route_notification_event, unified_notification_enabled,
)
from .notification_store import NotificationStore


FLAG = "BINANCE_UNIFIED_NOTIFICATION_ENABLED"
_CYCLE = ContextVar("binance_notification_cycle", default=None)


def enabled(env=None):
    return unified_notification_enabled(os.environ if env is None else env, FLAG)


def routing_mode(mode=None, *, env=None):
    values = os.environ if env is None else env
    return effective_notification_mode({**values, **({"QSL_ROUTINE_NOTIFICATION_MODE": mode} if mode is not None else {})}, FLAG)


def _alias(env, generic, legacy):
    first, second = env.get(generic, ""), env.get(legacy, "")
    if first and second and first != second:
        raise ValueError("notification_identity_conflict")
    value = first or second
    if not isinstance(value, str) or not value.strip():
        raise ValueError("notification_identity_missing")
    return value


def identity(env, report=None):
    configured = json.loads(env.get("RUNTIME_TARGET_JSON", ""))
    fields = ("platform_id", "strategy_profile", "service_name", "account_scope",
        "account_selector", "deployment_selector", "dry_run_only")
    if (not isinstance(configured, dict) or configured.get("platform_id") != "binance"
            or any(not configured.get(k) for k in fields if k != "dry_run_only")
            or type(configured.get("dry_run_only")) is not bool):
        raise ValueError("notification_target_unknown")
    if report is not None:
        target = report.get("runtime_target") or {}
        if (any(target.get(k) != configured.get(k) for k in fields)
                or report.get("strategy_profile") != configured["strategy_profile"]
                or report.get("dry_run") != configured["dry_run_only"]):
            raise ValueError("notification_target_mismatch")
    return {
        "platform_id": "binance",
        "target_id": _alias(env, "DIGEST_TARGET_ID", "BINANCE_DIGEST_TARGET_ID"),
        "opaque_account_uid": _alias(env, "DIGEST_OPAQUE_ACCOUNT_UID", "BINANCE_DIGEST_OPAQUE_ACCOUNT_UID"),
        "strategy_profile": configured["strategy_profile"],
    }


def bind_cycle(runtime, report):
    _ORDER_IDENTITY.set("")
    return _CYCLE.set((runtime, report))


def reset_cycle(token):
    if token is not None:
        _CYCLE.reset(token)
    _ORDER_IDENTITY.set("")


def _note(report, status):
    if report is not None:
        report.setdefault("notifications", []).append({"sink": "telegram",
            "delivery_status": "suppressed" if status in {"incident_duplicate", "incident_recovered"} else status,
            "notification_disposition": status, "transport_acknowledged": False})


def _acknowledged(receipt):
    return receipt.get("transport_acknowledged") is True if isinstance(receipt, dict) else receipt is True


def _index_key(event):
    scope = [event.platform_id, event.target_id, event.opaque_account_uid, event.strategy_profile]
    return "_notification_markers/index_" + hashlib.sha256(json.dumps(scope).encode()).hexdigest() + ".json"


def _register_incident(store, event, *, recovered):
    key = _index_key(event)
    for _ in range(8):
        index, generation = store.load(key)
        index = index or {}
        previous = index.get(event.incident_reason)
        if previous and datetime.fromisoformat(previous["observed_at"]) > event.occurred_at:
            raise ValueError("notification_observation_out_of_order")
        value = {**index, event.incident_reason: {"event_kind": event.event_kind,
            "observed_at": event.occurred_at.isoformat(), "recovered": recovered}}
        if len(value) > 1000:
            raise ValueError("notification_incident_budget_exceeded")
        if value == index or store.compare_and_swap(key, generation, value):
            return
    raise RuntimeError("notification_marker_concurrent_update")


class _MarkerStore:
    def __init__(self, store, observed_at):
        self.store, self.observed_at = store, observed_at

    def read(self, key):
        previous, version = self.store.load(f"_notification_markers/{key}.json")
        if previous is not None:
            if not previous.get("observed_at"):
                raise ValueError("notification_marker_history_incomplete")
            stamp = datetime.fromisoformat(previous["observed_at"])
            if stamp.tzinfo is None:
                raise ValueError("notification_marker_history_incomplete")
            if stamp > self.observed_at:
                raise ValueError("notification_observation_out_of_order")
        return (version if previous is not None else None), previous

    def compare_and_set(self, key, version, value):
        return self.store.compare_and_swap(f"_notification_markers/{key}.json", version or 0,
            {**value, "observed_at": self.observed_at.isoformat()})


def immediate_event(event, send, *, report=None, store=None, state="active", severity=1, recovered=False):
    """Only a durable reservation authorizes a send; no memory fallback."""
    try:
        if not all((event.platform_id, event.target_id, event.opaque_account_uid, event.strategy_profile, event.incident_reason)):
            _note(report, "incident_coverage_unknown")
            return False
        store = store or NotificationStore(os.environ.get("DIGEST_CANDIDATES_GCS_URI", ""))
        _register_incident(store, event, recovered=recovered)
        tracker = IncidentTracker(_MarkerStore(store, event.occurred_at))
        decision = tracker.observe(event, state=state, severity=severity, recovered=recovered)
        if decision.action != "send":
            _note(report, "incident_" + decision.action)
            if decision.action == "duplicate":
                _, previous = tracker.store.read(decision.incident_key)
                return previous.get("delivery_status") == "delivered"
            return False
        notification_count = len(report.get("notifications", [])) if report is not None else 0
        try:
            receipt = send()
        except Exception:
            tracker.record_delivery(decision)
            _note(report, "incident_send_uncertain")
            return False
        acknowledged = _acknowledged(receipt)
        receipt_id = receipt.get("transport_receipt_id") if isinstance(receipt, dict) else None
        if acknowledged and receipt_id is None and report is not None:
            # The runtime sender returns a boolean and appends its receipt to
            # this report. An earlier send's receipt cannot confirm this send.
            receipt_id = next((value.get("transport_receipt_id") for value in reversed(
                report.get("notifications", [])[notification_count:])
                if value.get("transport_acknowledged") is True and value.get("transport_receipt_id")), None)
        if (not isinstance(receipt_id, str) or not receipt_id.strip()
                or receipt_id.startswith("transport-ack:")
                or receipt_id in {decision.incident_key, decision.transition_key, event.to_candidate()["dedupe_key"]}):
            receipt_id = None
        tracker.record_delivery(decision, receipt_id=receipt_id if acknowledged else None)
        return acknowledged
    except Exception:
        # Incomplete history/unknown writes cannot authorize untracked retries.
        _note(report, "incident_history_unavailable")
        return False


def route(send, *, kind, report=None, runtime=None, reason=None, state="active", severity=1,
          env=None, candidate_store=None, marker_store=None, order_digest=None,
          occurred_at=None, source_run_id=None, send_coverage=None):
    values = os.environ if env is None else env
    selected = select_notification_route(event_kind=kind, env=values, flag_key=FLAG)
    def coverage_failure():
        if send_coverage is not None:
            try:
                send_coverage()
            except Exception:
                _note(report, "coverage_alert_failed")
    if selected == "legacy":
        return send()
    context = _CYCLE.get()
    if report is None and context:
        runtime, report = context
    if runtime is not None and getattr(runtime, "dry_run", False):
        return send()
    try:
        scope = identity(values, report)
        raw_stamp = report.get("started_at") if report is not None else None
        stamp = occurred_at or (datetime.fromisoformat(raw_stamp.replace("Z", "+00:00")) if raw_stamp else datetime.now(timezone.utc))
        source_run = source_run_id or (str(report.get("run_id") or "") if report is not None else values.get("GITHUB_RUN_ID"))
        event = NotificationEvent(kind, stamp, **scope, source_run_id=source_run, incident_reason=reason,
            order_execution_digest=order_digest)
    except Exception:
        _note(report, "candidate_identity_unknown")
        if selected == "digest_candidate":
            coverage_failure()
        return send() if selected != "digest_candidate" else False
    from .unified_producer import persist_event
    try:
        result = route_notification_event(event, env=values, flag_key=FLAG,
            persist_candidate=lambda fact: persist_event(fact, env=values, report=report, store=candidate_store),
            legacy=send, immediate=lambda: immediate_event(
                event, send, report=report, store=marker_store, state=state, severity=severity))
        if not result.candidate_persisted:
            _note(report, "candidate_write_failed")
        if result.route == "digest_candidate":
            _note(report, "deferred_digest")
            return False
        return result.result
    except Exception:
        _note(report, "candidate_write_failed")
        if selected == "digest_candidate":
            coverage_failure()
        return False


def runtime_notice(runtime, report, text, *, urgent, send, send_coverage=None):
    kind, reason, severity = "routine_cycle", None, 1
    if urgent:
        failure = (report.get("diagnostics") or {}).get("cycle_failure") or {}
        if report.get("circuit_breaker_triggered"):
            kind, reason, severity = "risk_escalation", "risk_escalation", 2
        elif failure.get("error_type") == "permission_error":
            kind, reason = "auth_failure", "auth_failure"
        elif failure.get("error_type") in {"execution_integrity_error", "order_reconciliation_error"}:
            kind, reason = "order_status_unknown", "order_status_unknown"
        else:
            kind, reason = "real_error", "runtime_failure"
            if failure.get("stage") and failure.get("error_type"):
                reason = "cycle_" + failure["stage"] + "_" + failure["error_type"]
    return route(send, kind=kind, reason=reason, severity=severity, runtime=runtime, report=report, send_coverage=send_coverage)


def routine_notice(sender, runtime, report, text, *, routine_order):
    # The legacy sender sees its original signature, args and relative order.
    # This context suppresses the generic alert wrapper for an actual order.
    token = _ORDER_SEND.set(True) if routine_order else None
    try:
        order_digest = current_order_digest(runtime) if routine_order else None
        def coverage():
            guard = _ORDER_SEND.set(True)
            try:
                return sender(runtime, report, _coverage_message())
            finally:
                _ORDER_SEND.reset(guard)
        return route(lambda: sender(runtime, report, text), kind="fill" if routine_order else "routine_cycle",
            runtime=runtime, report=report, order_digest=order_digest,
            send_coverage=coverage)
    finally:
        if token is not None:
            _ORDER_SEND.reset(token)


_ORDER_SEND = ContextVar("binance_notification_order_send", default=False)
_ORDER_IDENTITY = ContextVar("binance_notification_order_identity", default=None)


def sending_order():
    return _ORDER_SEND.get()


def begin_order_attempt():
    _ORDER_IDENTITY.set("")


def record_confirmed_order_identity(client_order_id):
    if isinstance(client_order_id, str) and client_order_id:
        _ORDER_IDENTITY.set(order_identity_digest(client_order_id))


def current_order_digest(runtime):
    current = _ORDER_IDENTITY.get()
    if current is not None:
        return current or None
    for effect in reversed(getattr(runtime, "side_effect_log", [])):
        if effect.get("effect_type") in {"order_buy", "order_sell"}:
            raw_id = (effect.get("payload") or {}).get("newClientOrderId")
            return order_identity_digest(raw_id) if isinstance(raw_id, str) and raw_id else None
    return None


def _coverage_message():
    from .operational import message
    return message("coverage_failure")


def external_notice(send, *, kind="routine_cycle", reason=None, send_coverage=None):
    return route(send, kind=kind, reason=reason, send_coverage=send_coverage)


def recover(event, *, store=None):
    return immediate_event(event, lambda: False, state="recovered", severity=0, recovered=True, store=store)


def report_issue(report):
    """Notification classification only; never changes acceptance or a report."""
    if report.get("status") != "ok" or report.get("errors") or (report.get("error_summary") or {}).get("errors"):
        failure = (report.get("diagnostics") or {}).get("cycle_failure") or {}
        reason = "runtime_failure"
        if failure.get("stage") and failure.get("error_type"):
            reason = "cycle_" + failure["stage"] + "_" + failure["error_type"]
        return "real_error", reason, 1
    if report.get("circuit_breaker_triggered") or report.get("execution_blocked_reason"):
        return "risk_escalation", "risk_escalation", 2
    if not report.get("execution_receipt"):
        return "receipt_missing", "execution_receipt", 1
    try:
        from quant_platform_kit.common.execution_receipts import attach_execution_receipt
        # Public validation includes release/target binding; leave the original
        # report and all existing acceptance functions unchanged.
        attach_execution_receipt(deepcopy(report), report["execution_receipt"])
    except (ValueError, TypeError, KeyError):
        return "receipt_unacceptable", "execution_receipt", 1
    outcome = report["execution_receipt"]["outcome"]
    if outcome in {"submitted", "broker_acknowledged", "partially_filled", "reconciliation_required"}:
        return "order_status_unknown", "execution_receipt", 2
    if outcome not in {"no_action", "no_signal", "no_rebalance", "filled"}:
        return "receipt_unacceptable", "execution_receipt", 1
    if ((report.get("summary") or {}).get("notification_digest") or {}).get("routine") is not True:
        return "receipt_unacceptable", "execution_receipt", 1
    return None


def observe_archived_report(report, *, env=None, store=None, send=None):
    values = os.environ if env is None else env
    if report.get("dry_run") is not False:
        return
    try:
        scope = identity(values, report)
        stamp = datetime.fromisoformat(report["started_at"].replace("Z", "+00:00"))
        from .unified_producer import update_window
        try:
            if update_window(values, reports=[report], archived=True, store=store) is not True:
                raise RuntimeError("candidate_write_unconfirmed")
        except Exception:
            _note(report, "candidate_write_failed")
            from .operational import send_configured
            (send or send_configured)(_coverage_message())
            return False
        issue = report_issue(report)
        if issue:
            from .operational import message, send_configured
            if send is None and (not (values.get("TG_TOKEN") or values.get("TELEGRAM_TOKEN"))
                    or not (values.get("QSL_GLOBAL_TELEGRAM_CHAT_ID") or values.get("GLOBAL_TELEGRAM_CHAT_ID"))):
                _note(report, "notification_target_unconfigured")
                return
            kind, reason, severity = issue
            category = "receipt_missing" if kind == "receipt_missing" else (
                "runtime_failure" if kind == "real_error" else "receipt_unacceptable")
            return route(lambda: (send or send_configured)(message(category)), kind=kind, reason=reason,
                severity=severity, env=values, report=report, marker_store=store, candidate_store=store)
        store = store or NotificationStore(values.get("DIGEST_CANDIDATES_GCS_URI", ""))
        event = NotificationEvent("healthy_heartbeat", stamp, **scope, source_run_id=report["run_id"])
        index, _ = store.load(_index_key(event))
        for reason, prior in (index or {}).items():
            if not prior.get("recovered"):
                recover(NotificationEvent("healthy_heartbeat", stamp, **scope,
                    source_run_id=report["run_id"], incident_reason=reason), store=store)
    except Exception:
        _note(report, "notification_recovery_unverified")
        from .operational import send_configured
        try:
            (send or send_configured)(_coverage_message())
        except Exception:
            _note(report, "coverage_alert_failed")
        return False

