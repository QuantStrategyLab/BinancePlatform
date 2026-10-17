"""Bridge event facts and existing archived reports into one window candidate."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from .digest_snapshot.digest_archive import _read
from .digest_snapshot.digest_candidates import project_reports
from .digest_snapshot.unified_contract import sgt_business_window
from .notification_store import NotificationStore


def _source(report, *, archived):
    from .unified_notifications import report_issue
    issue = report_issue(report) if archived else None
    # Free-form text, balances, raw order IDs and selectors never enter this ledger.
    return {
        "run_id": report["run_id"], "started_at": report["started_at"],
        "status": report["status"], "dry_run": report["dry_run"], "archived": archived,
        "summary": {"notification_digest": deepcopy((report.get("summary") or {}).get("notification_digest"))
            if archived and not issue else None},
        "notification_evidence_issue": issue[0] if issue else None,
        "error_summary": {"errors": [{"message": "source_cycle_alert"}] if (
            report.get("errors") or (report.get("error_summary") or {}).get("errors")
            or report.get("execution_blocked_reason") or report.get("circuit_breaker_triggered") or issue) else []},
    }


def _report(source, target):
    return {**source, "schema_version": "runtime_report.v1", "platform": target["platform_id"],
        "strategy_profile": target["strategy_profile"], "service_name": target["service_name"],
        "runtime_target": target}


def update_window(env, *, events=(), reports=(), archived=False, complete_archive=False, day=None,
                  store=None):
    """CAS-merge by source run. Both old/new producers call this same writer."""
    from .unified_notifications import identity, _alias
    scope = identity(env)
    target = json.loads(env["RUNTIME_TARGET_JSON"])
    slot = _alias(env, "DIGEST_STORE_TARGET", "BINANCE_DIGEST_STORE_TARGET")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", slot):
        raise ValueError("candidate_store_identity_missing")
    days = {event["business_day"] for event in events}
    days.update(sgt_business_window(datetime.fromisoformat(report["started_at"].replace("Z", "+00:00"))).business_day.isoformat()
        for report in reports)
    if day is not None:
        days.add(day)
    if len(days) != 1:
        raise ValueError("candidate_window_ambiguous")
    day = days.pop()
    window = sgt_business_window(datetime.fromisoformat(day + "T06:00:00+08:00"))
    key = f"binance/{day}/{slot}.json"
    current = datetime.now(timezone.utc)
    incoming = {}
    for report in reports:
        identity(env, report)
        # Reuse all protected-target/status/count checks. For an old completed
        # window validate against its end, after the containing-window check.
        stamp = datetime.fromisoformat(report["started_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp > current:
            raise ValueError("candidate_source_time_invalid")
        if not window.start <= stamp < window.end:
            raise ValueError("candidate_source_outside_window")
        project_reports([report], expected_target=target, opaque_account_uid=scope["opaque_account_uid"],
            target_id=scope["target_id"], now=max(stamp, window.end))
        source = _source(report, archived=archived)
        run = source["run_id"]
        if run in incoming and incoming[run] != source:
            raise ValueError("candidate_duplicate_run_conflict")
        incoming[run] = source
    if any(datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00")) > current for event in events):
        raise ValueError("candidate_source_time_invalid")
    store = store or NotificationStore(env.get("DIGEST_CANDIDATES_GCS_URI", ""))
    for _ in range(8):
        previous, generation = store.load(key)
        previous = previous or {}
        # Read the reviewed draft's ledger during migration; new candidates
        # carry each sanitized source fact and its events inside that run row.
        sources = deepcopy(previous.get("source_facts") or {
            row["source_run_id"]: row["source_fact"] for row in previous.get("runs", [])
            if row.get("source_run_id") and row.get("source_fact")})
        for run, source in incoming.items():
            prior = sources.get(run)
            if prior and prior.get("archived") and not source["archived"]:
                continue
            if prior and prior.get("archived") and source["archived"] and prior != source:
                # Preserve the incident and unknown counts rather than replacing
                # contradictory completed revisions with a healthy snapshot.
                source = {**source, "status": "error", "summary": {},
                    "error_summary": {"errors": [{"message": "candidate_source_conflict"}]}}
            if prior and prior.get("error_summary", {}).get("errors"):
                source = {**source, "error_summary": prior["error_summary"]}
            sources[run] = source
        facts = deepcopy(previous.get("notification_events") or {})
        for row in previous.get("runs", []):
            for event in row.get("notification_events", []):
                event_key = event["dedupe_key"]
                if event.get("incident_reason"):
                    event_key += ":" + hashlib.sha256(event["incident_reason"].encode()).hexdigest()
                facts[event_key] = deepcopy(event)
        for event in events:
            if not event.get("dedupe_key") or any(event.get(k) != scope[k] for k in scope):
                raise ValueError("candidate_event_identity_unknown")
            if event.get("source_run_id") not in sources:
                # A workflow ID is not evidence that a business run happened.
                # The caller retains the immediate incident path on failure.
                raise ValueError("candidate_business_run_unverified")
            event_key = event["dedupe_key"]
            if event.get("incident_reason"):
                event_key += ":" + hashlib.sha256(event["incident_reason"].encode()).hexdigest()
            if event_key in facts and facts[event_key] != event:
                raise ValueError("candidate_event_conflict")
            facts[event_key] = event
        # The legacy projector aggregates a window. Project one source at a
        # time here so QRS receives the business run identity and its own counts.
        rows = []
        for run, source in sorted(sources.items()):
            row = project_reports([_report(source, target)], expected_target=target,
                opaque_account_uid=scope["opaque_account_uid"], target_id=scope["target_id"],
                now=window.end)["runs"][0]
            row.update(source_run_id=run, source_fact=deepcopy(source),
                notification_events=[deepcopy(event) for event in facts.values() if event.get("source_run_id") == run])
            row["field_status"] = {name: "known" if row.get(name) is not None else "unknown"
                for name in ("order_count", "fill_count", "cycle_count")}
            if not source.get("archived"):
                row["persistence_status"] = "source_archive_pending"
            rows.append(row)
        payload = {"schema_version": "qsl.digest_candidates.v1", "producer_status": "projected", "runs": rows}
        payload.update(window_id=window.window_id, report_date=window.report_date.isoformat())
        legacy = previous.get("legacy_evidence")
        if previous.get("runs") and not previous.get("source_facts") and not all(
                row.get("source_fact") for row in previous["runs"]):
            legacy = previous["runs"]
        if legacy and not complete_archive:
            payload["legacy_evidence"] = legacy
            for row in payload["runs"]:
                row.update(order_count=None, fill_count=None, cycle_count=None, status="alert",
                    reason_code="candidate_legacy_coverage_unknown")
        if len(sources) > 10000 or len(facts) > 20000:
            raise ValueError("candidate_window_budget_exceeded")
        if payload == previous or store.compare_and_swap(key, generation, payload):
            return True
    raise RuntimeError("candidate_concurrent_update")


def persist_event(event, *, env, report=None, store=None):
    event = {**event, "workflow_run_id": env.get("GITHUB_RUN_ID") or None}
    return update_window(env, events=[event], reports=[report] if report is not None else [], store=store)


def emit_archive(env, *, now=None, read=_read, store=None):
    from .unified_notifications import identity
    bindings = env.get("DIGEST_TARGET_BINDINGS_JSON", "").strip()
    if bindings:
        values = json.loads(bindings)
        if not isinstance(values, list) or not values or len(values) > 100 or not all(isinstance(v, dict) for v in values):
            raise ValueError("candidate_bindings_invalid")
        results = [emit_archive({**env, **value, "DIGEST_TARGET_BINDINGS_JSON": ""}, now=now, read=read, store=store)
            for value in values]
        return {"status": "projected", "uploaded": any(v["uploaded"] for v in results), "targets": len(results)}
    if env.get("RUNTIME_TARGET_ENABLED") == "false":
        return {"status": "target_disabled", "uploaded": False}
    if env.get("RUNTIME_TARGET_ENABLED") != "true":
        raise ValueError("candidate_target_enablement_unknown")
    identity(env)
    target = json.loads(env["RUNTIME_TARGET_JSON"])
    current = now or datetime.now(timezone.utc)
    window = sgt_business_window(current - timedelta(days=1))
    prefix = env.get("EXECUTION_REPORT_GCS_URI", "").rstrip("/")
    if not re.fullmatch(r"gs://[a-z0-9][a-z0-9._-]+/[^\s?#*]+", prefix):
        raise ValueError("candidate_archive_configuration_missing")
    months = {stamp.astimezone(timezone.utc).strftime("%Y-%m") for stamp in (window.start, window.end)}
    entries = []
    for month in sorted(months):
        entries.extend(read(["ls", "--json", f"{prefix}/binance/**/{month}/*.json"]))
    if len(entries) > 10000:
        raise ValueError("candidate_archive_budget_exceeded")
    reports, seen_uris = [], set()
    for entry in entries:
        uri = str(entry.get("url", "")).split("#", 1)[0]
        if uri in seen_uris:
            continue
        seen_uris.add(uri)
        if not uri.startswith(f"{prefix}/binance/"):
            raise ValueError("candidate_source_locator_mismatch")
        report = read(["cat", uri])
        source_target = report.get("runtime_target") or {}
        fields = ("platform_id", "strategy_profile", "service_name", "account_scope", "account_selector",
            "deployment_selector", "dry_run_only")
        if not isinstance(source_target, dict) or any(source_target.get(k) != target.get(k) for k in fields):
            continue
        stamp = datetime.fromisoformat(str(report.get("started_at", "")).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("candidate_source_time_invalid")
        if window.start <= stamp < window.end:
            reports.append(report)
    if not reports:
        # Crypto is continuous. Absence cannot establish an idle healthy day.
        raise ValueError("candidate_due_source_absent")
    # Runtime observations of this same run can have unknown counts. Preserve
    # them as event facts; only completed source facts determine window totals.
    update_window(env, reports=reports, archived=True, complete_archive=True,
        day=window.business_day.isoformat(), store=store)
    from .unified_notifications import observe_archived_report
    observe_archived_report(max(reports, key=lambda report: report["started_at"]), env=env, store=store)
    return {"status": "projected", "uploaded": True, "runs": len({r["run_id"] for r in reports}),
        "window_id": window.window_id, "source_set_sha256": hashlib.sha256(
            json.dumps(sorted({r["run_id"] for r in reports})).encode()).hexdigest()}
