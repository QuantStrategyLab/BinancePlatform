"""Workflow observations share the runtime incident ledger; no broker imports."""

from __future__ import annotations

import json
import os
from datetime import datetime

from . import unified_notifications as unified
from .digest_snapshot.unified_contract import NotificationEvent, sgt_business_window
from .notification_store import NotificationStore


_TEXT = {
    "zh": {
        "runtime_failure": "⚠️ 运行失败\n请查看本次运行日志。",
        "dispatch_missing": "⚠️ 未观察到应有的周期运行\n请检查调度状态。",
        "query_unavailable": "⚠️ 无法核实运行状态\n运行记录暂时不可用，请检查监测日志。",
        "receipt_missing": "⚠️ 缺少执行回执\n本窗口的运行情况尚未核实。",
        "receipt_unacceptable": "⚠️ 执行回执不可接受\n请检查执行结果和运行日志。",
    },
    "en": {
        "runtime_failure": "⚠️ Runtime failed\nReview this run's log.",
        "dispatch_missing": "⚠️ Expected runtime cycles were not observed\nCheck the dispatcher state.",
        "query_unavailable": "⚠️ Runtime state could not be verified\nRun records are unavailable. Review the monitor log.",
        "receipt_missing": "⚠️ Execution receipt missing\nExecution coverage for this window is unverified.",
        "receipt_unacceptable": "⚠️ Execution receipt is unacceptable\nReview the execution result and run log.",
    },
}


def message(category):
    locale = "zh" if os.environ.get("NOTIFY_LANG", "zh").lower().startswith("zh") else "en"
    return _TEXT[locale][category]


def send_configured(text):
    from live_services import send_tg_msg
    return send_tg_msg(os.environ.get("TG_TOKEN") or os.environ.get("TELEGRAM_TOKEN"),
        os.environ.get("QSL_GLOBAL_TELEGRAM_CHAT_ID") or os.environ.get("GLOBAL_TELEGRAM_CHAT_ID"), text)


def _correlated_event(assessment, *, env, store=None):
    latest = assessment.get("latest_run") or {}
    workflow_run_id = str(latest.get("id") or "")
    stamp = latest.get("created_at")
    if not stamp or not workflow_run_id:
        return None
    scope = unified.identity(env)
    day = sgt_business_window(datetime.fromisoformat(stamp.replace("Z", "+00:00"))).business_day.isoformat()
    slot = unified._alias(env, "DIGEST_STORE_TARGET", "BINANCE_DIGEST_STORE_TARGET")
    store = store or NotificationStore(env.get("DIGEST_CANDIDATES_GCS_URI", ""))
    payload, _ = store.load(f"binance/{day}/{slot}.json")
    facts = (payload or {}).get("notification_events") or {}
    matching = [event for event in facts.values() if str(event.get("workflow_run_id")) == workflow_run_id
        and event.get("incident_reason") and all(event.get(k) == value for k, value in scope.items())]
    return max(matching, key=lambda event: event["occurred_at"]) if matching else None


def notify_workflow(assessment, original_message, *, send=None, store=None):
    if unified.enabled() and send is None and (
            not (os.environ.get("TG_TOKEN") or os.environ.get("TELEGRAM_TOKEN"))
            or not (os.environ.get("QSL_GLOBAL_TELEGRAM_CHAT_ID") or os.environ.get("GLOBAL_TELEGRAM_CHAT_ID"))):
        print(json.dumps({"notification_status": "blocked", "reason": "notification_target_unconfigured"}))
        return False
    send = send or send_configured
    if not unified.enabled():
        return send(original_message)
    reason = assessment.get("reason")
    values = dict(os.environ)
    occurred_at, source_run_id = None, None
    kind, incident, category = "real_error", "heartbeat_query_unavailable", "query_unavailable"
    if reason == "runtime_startup_failed":
        kind, incident, category = "real_error", "runtime_failure", "runtime_failure"
    elif reason == "latest_runtime_completed_unsuccessfully":
        kind, incident, category = "real_error", "runtime_failure", "runtime_failure"
        try:
            correlated = _correlated_event(assessment, env=os.environ, store=store)
        except Exception:
            print(json.dumps({"notification_status": "blocked", "reason": "notification_history_unavailable"}))
            return False
        if correlated:
            kind, incident = correlated["event_kind"], correlated["incident_reason"]
            occurred_at = datetime.fromisoformat(correlated["occurred_at"])
            source_run_id = correlated["source_run_id"]
        else:
            raw_stamp = (assessment.get("latest_run") or {}).get("created_at")
            if raw_stamp:
                occurred_at = datetime.fromisoformat(raw_stamp.replace("Z", "+00:00"))
        values["GITHUB_RUN_ID"] = str((assessment.get("latest_run") or {}).get("id") or "")
    elif reason not in {"github_actions_query_unavailable", "runtime_startup_failed"}:
        kind, incident, category = "receipt_missing", "runtime_dispatch_missing", "dispatch_missing"
    return unified.route(lambda: send(message(category)), kind=kind, reason=incident, env=values,
        marker_store=store, candidate_store=store, occurred_at=occurred_at, source_run_id=source_run_id)


def recover_workflow(assessment, *, store=None):
    if not unified.enabled() or assessment.get("status") != "healthy":
        return
    try:
        scope = unified.identity(os.environ)
        latest = assessment.get("latest_success") or {}
        stamp = datetime.fromisoformat(str(latest["created_at"]).replace("Z", "+00:00"))
        for reason in ("runtime_dispatch_missing", "heartbeat_query_unavailable"):
            unified.recover(NotificationEvent("healthy_heartbeat", stamp, **scope,
                source_run_id=str(latest.get("id") or ""), incident_reason=reason), store=store)
    except Exception:
        print(json.dumps({"notification_status": "blocked", "reason": "notification_recovery_unverified"}))
