"""No broker or notification transport is used by these tests."""

import json
from copy import deepcopy

import pytest

from notifications import unified_notifications as unified
from notifications.notification_store import NotificationStore
from notifications.digest_snapshot.unified_contract import NotificationEvent
from notifications import unified_producer as producer
from datetime import datetime, timedelta, timezone


TARGET = dict(platform_id="binance", strategy_profile="example-strategy", service_name="example-service",
    account_scope="example-scope", account_selector=["example-selector"],
    deployment_selector="example-deployment", dry_run_only=False, execution_mode="live")


def environment():
    return dict(BINANCE_UNIFIED_NOTIFICATION_ENABLED="true", RUNTIME_TARGET_JSON=json.dumps(TARGET),
        DIGEST_TARGET_ID="example-target", DIGEST_OPAQUE_ACCOUNT_UID="example-account",
        DIGEST_STORE_TARGET="example-slot", DIGEST_CANDIDATES_GCS_URI="gs://example-candidates/private")


@pytest.mark.parametrize("value", [None, "", "false", "invalid", "1", "yes"])
def test_flag_is_strictly_default_off_and_keeps_existing_routing(value):
    env = {} if value is None else {unified.FLAG: value}
    assert not unified.enabled(env)
    assert unified.routing_mode(env=env) == "immediate"
    assert unified.routing_mode("daily_digest", env=env) == "daily_digest"
    env[unified.FLAG] = "true"
    assert unified.routing_mode("immediate", env=env) == "daily_digest"


def test_identity_requires_target_binding_and_does_not_mix_accounts():
    env = environment()
    report = dict(runtime_target=deepcopy(TARGET), strategy_profile=TARGET["strategy_profile"], dry_run=False)
    original = deepcopy((env, report))
    assert unified.identity(env, report)["opaque_account_uid"] == "example-account"
    assert (env, report) == original
    report["runtime_target"]["account_selector"] = ["different-example"]
    with pytest.raises(ValueError, match="notification_target_mismatch"):
        unified.identity(env, report)
    env["BINANCE_DIGEST_OPAQUE_ACCOUNT_UID"] = "another-example"
    with pytest.raises(ValueError, match="notification_identity_conflict"):
        unified.identity(env)
    env.pop("DIGEST_TARGET_ID")
    with pytest.raises(ValueError, match="notification_identity_missing"):
        unified.identity(env)


def test_store_uses_generation_conditions_and_never_retries_uncertain_write(monkeypatch):
    from google.api_core.exceptions import NotFound, PreconditionFailed
    store = NotificationStore("gs://example-candidates/private", client=object())
    calls, saved = [], {}

    class Blob:
        generation = 1

        def reload(self, **kw):
            calls.append(["reload", kw])
            if not saved:
                raise NotFound("example")

        def download_as_text(self, **kw):
            calls.append(["download", kw])
            return json.dumps(saved)

        def upload_from_string(self, body, **kw):
            calls.append(["upload", kw])
            if saved and kw["if_generation_match"] == 0:
                raise PreconditionFailed("example")
            saved.update(json.loads(body))

    monkeypatch.setattr(store, "_blob", lambda key: Blob())
    assert store.load("example") == (None, 0)
    assert store.put_once("example", {"status": "example"})
    assert store.put_once("example", {"status": "example"})
    with pytest.raises(ValueError, match="notification_candidate_conflict"):
        store.put_once("example", {"status": "different"})
    assert all(kw["retry"] is None for _, kw in calls)
    assert all("if_generation_match" in kw for op, kw in calls if op in {"download", "upload"})
    calls.clear()
    def uncertain(self, body, **kw):
        calls.append(["upload", kw])
        raise TimeoutError("example")
    monkeypatch.setattr(Blob, "upload_from_string", uncertain)
    with pytest.raises(TimeoutError):
        store.put_once("example", {})
    assert len(calls) == 1


class MemoryStore:
    def __init__(self):
        self.values, self.writes = {}, 0

    def load(self, key):
        generation, value = self.values.get(key, (0, None))
        return deepcopy(value), generation

    def compare_and_swap(self, key, generation, value):
        current = self.values.get(key, (0, None))[0]
        if current != generation:
            return False
        self.values[key] = current + 1, deepcopy(value)
        self.writes += 1
        return True


STAMP = datetime(2026, 10, 16, 22, tzinfo=timezone.utc)


def source(run="example-run", stamp=STAMP, *, archived=False):
    from quant_platform_kit.common.execution_receipts import build_execution_receipt
    report = dict(schema_version="runtime_report.v1", platform="binance", runtime_target=deepcopy(TARGET),
        strategy_profile=TARGET["strategy_profile"], service_name=TARGET["service_name"], dry_run=False,
        status="ok", run_id=run, started_at=stamp.isoformat(), notifications=[],
        summary={"notification_digest": dict(schema_version="qsl.notification_cycle.v1", dry_run=False,
            order_count=0, fill_count=0, routine=True)} if archived else {})
    if archived:
        report["runtime_release_receipt"] = {"attestation_state": "self_attested",
            "strategy_release": {"strategy_revision": "a" * 40}}
        report["execution_receipt"] = build_execution_receipt(platform="binance",
            strategy_profile=TARGET["strategy_profile"], strategy_revision="a" * 40,
            execution_mode="live", outcome="no_action", observed_at=stamp)
    return report


def configure(monkeypatch):
    env = environment()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


def test_routine_writes_real_candidate_before_deferral_and_replays_do_not_add_cycles(monkeypatch):
    env, store, sends = configure(monkeypatch), MemoryStore(), []
    for _ in range(2):
        report = source()
        assert unified.route(lambda: sends.append(1), kind="routine_cycle", env=env, report=report,
            candidate_store=store) is False
        assert report["notifications"][-1]["delivery_status"] == "deferred_digest"
    assert not sends and store.writes == 1
    payload = next(iter(store.values.values()))[1]
    row = payload["runs"][0]
    assert row["cycle_count"] == 1 and row["order_count"] is None and row["fill_count"] is None
    assert row["persistence_status"] == "source_archive_pending"
    assert len(payload["notification_events"]) == 1
    assert payload["window_id"] == "sgt-0600:2026-10-17"
    assert payload["report_date"] == "2026-10-18"


def test_failed_candidate_write_retains_immediate_notice_and_unknown_coverage(monkeypatch):
    env, sends = configure(monkeypatch), []
    class Failed(MemoryStore):
        def compare_and_swap(self, *a):
            raise TimeoutError("example unknown write result")
    report = source()
    assert unified.route(lambda: sends.append(1) or True, kind="routine_cycle", env=env, report=report,
        candidate_store=Failed()) is True
    assert sends == [1]
    assert report["notifications"][-1]["delivery_status"] == "candidate_write_failed"


def test_cold_start_incident_dedupe_ack_recovery_escalation_accounts_and_uncertain(monkeypatch):
    configure(monkeypatch)
    store, sends = MemoryStore(), []
    scope = unified.identity(environment())
    def event(stamp=STAMP, **kw):
        return NotificationEvent("real_error", stamp, **{**scope, **kw}, source_run_id="another-run",
            incident_reason="runtime_failure")
    def send():
        sends.append(1)
        return {"transport_acknowledged": True, "transport_receipt_id": "example-confirmed-receipt"}
    assert unified.immediate_event(event(), send, store=store)
    assert unified.immediate_event(event(), send, store=store)
    assert len(sends) == 1
    assert next(value for _, value in store.values.values() if "delivery_status" in value)["delivery_status"] == "delivered"
    unified.immediate_event(event(STAMP + timedelta(minutes=1)), send, severity=2, store=store)
    assert len(sends) == 2
    unified.recover(event(STAMP + timedelta(minutes=2)), store=store)
    unified.immediate_event(event(STAMP + timedelta(minutes=3)), send, store=store)
    assert len(sends) == 3
    # Older healthy evidence must not reset a newer active incident.
    unified.recover(event(STAMP + timedelta(minutes=1)), store=store)
    unified.immediate_event(event(STAMP + timedelta(minutes=4)), send, store=store)
    assert len(sends) == 3
    unified.immediate_event(event(opaque_account_uid="another-example-account"), send, store=store)
    assert len(sends) == 4
    uncertain = MemoryStore()
    unified.immediate_event(event(), lambda: sends.append(1) or False, store=uncertain)
    unified.immediate_event(event(), send, store=uncertain)
    assert len(sends) == 5
    assert next(value for _, value in uncertain.values.values() if "delivery_status" in value)["delivery_status"] == "uncertain"
    class Unreadable(MemoryStore):
        def load(self, key):
            raise TimeoutError("example history unavailable")
    unified.immediate_event(event(), send, store=Unreadable())
    assert len(sends) == 5


@pytest.mark.parametrize("kind", ["order_submitted", "fill", "receipt_missing", "receipt_stale",
    "receipt_unacceptable", "auth_failure", "order_status_unknown", "risk_escalation", "blocked_run", "real_error"])
def test_real_orders_and_all_urgent_classes_remain_immediate_even_with_candidate_failure(monkeypatch, kind):
    env, sends = configure(monkeypatch), []
    env["QSL_DIGEST_ROUTINE_ORDERS"] = "true"
    class Failed(MemoryStore):
        def load(self, key):
            raise TimeoutError("example candidate unavailable")
    unified.route(lambda: sends.append(1) or {"transport_acknowledged": True}, kind=kind,
        reason="example-incident", env=env, report=source(), candidate_store=Failed(), marker_store=MemoryStore())
    assert sends == [1]


def test_two_producers_and_runtime_event_share_one_window_object_and_unique_run_counts(monkeypatch):
    env, store = configure(monkeypatch), MemoryStore()
    env.update(RUNTIME_TARGET_ENABLED="true", EXECUTION_REPORT_GCS_URI="gs://example-reports/private")
    report = source(stamp=STAMP - timedelta(hours=1))
    unified.route(lambda: pytest.fail("routine send"), kind="routine_cycle", env=env, report=report,
        candidate_store=store)
    reports = [source("example-run", STAMP - timedelta(hours=1), archived=True),
        source("second-run", STAMP - timedelta(hours=23), archived=True)]
    def read(args):
        if args[0] == "ls":
            return [{"url": f"gs://example-reports/private/binance/2026-10/{i}.json"} for i in range(2)]
        return deepcopy(reports[int(args[1].rsplit("/", 1)[1].split(".")[0])])
    emit = producer.emit_archive
    def same_writer(values, **kw):
        return emit(values, now=STAMP + timedelta(hours=1), read=read, store=store)
    from scripts import emit_digest_candidates as old, emit_unified_digest_candidates as new
    monkeypatch.setattr(producer, "emit_archive", same_writer)
    monkeypatch.setattr(new, "emit_archive", same_writer)
    old.emit_digest_candidates(env)
    new.emit(env)
    assert len(store.values) == 1
    row = next(iter(store.values.values()))[1]["runs"][0]
    assert (row["cycle_count"], row["order_count"], row["fill_count"], row["status"]) == (2, 0, 0, "ok")
    assert store.writes == 2  # one runtime observation, one completed-window upgrade
    assert len(next(iter(store.values.values()))[1]["source_facts"]) == 2


def test_contradictory_completed_revisions_cannot_erase_an_incident_or_invent_counts(monkeypatch):
    env, store = configure(monkeypatch), MemoryStore()
    report = source(archived=True)
    producer.update_window(env, reports=[report], archived=True, store=store)
    report["summary"]["notification_digest"]["order_count"] = 1
    producer.update_window(env, reports=[report], archived=True, store=store)
    row = next(iter(store.values.values()))[1]["runs"][0]
    assert row["status"] == "alert" and row["order_count"] is None and row["fill_count"] is None


def test_continuous_window_boundaries_no_run_and_missing_identity_stay_unknown(monkeypatch):
    env, store = configure(monkeypatch), MemoryStore()
    env.update(RUNTIME_TARGET_ENABLED="true", EXECUTION_REPORT_GCS_URI="gs://example-reports/private")
    with pytest.raises(ValueError, match="candidate_due_source_absent"):
        producer.emit_archive(env, now=STAMP, read=lambda args: [], store=store)
    assert not store.values
    # The upper bound belongs to the next business window.
    for delta in (timedelta(microseconds=-1), timedelta(0)):
        producer.update_window(env, reports=[source(str(delta), STAMP + delta, archived=True)],
            archived=True, store=store)
    assert len(store.values) == 2
    broken = {**env, "DIGEST_OPAQUE_ACCOUNT_UID": ""}
    with pytest.raises(ValueError, match="notification_identity_missing"):
        producer.update_window(broken, reports=[source()], store=store)


def test_real_archived_receipt_proves_recovery_but_missing_or_stale_receipt_does_not(monkeypatch):
    env, store, sends = configure(monkeypatch), MemoryStore(), []
    scope = unified.identity(env)
    incident = NotificationEvent("real_error", STAMP, **scope, source_run_id="failed-run", incident_reason="runtime_failure")
    def send():
        return sends.append(1) or {"transport_acknowledged": True, "transport_receipt_id": "example-receipt"}
    unified.immediate_event(incident, send, store=store)
    incomplete = source("missing-receipt", STAMP + timedelta(minutes=1), archived=True)
    incomplete.pop("execution_receipt")
    unified.observe_archived_report(incomplete, env=env, store=store, send=lambda text: send())
    assert len(sends) == 2  # the missing receipt is a distinct immediate incident
    # No healthy label may recover the original error without receipt proof.
    unified.immediate_event(incident, send, store=store)
    assert len(sends) == 2
    valid = source("recovered-run", STAMP + timedelta(minutes=2), archived=True)
    original = deepcopy(valid)
    unified.observe_archived_report(valid, env=env, store=store, send=lambda text: send())
    assert valid == original
    unified.immediate_event(NotificationEvent("real_error", STAMP + timedelta(minutes=3), **scope,
        source_run_id="worsened-run", incident_reason="runtime_failure"), send, store=store)
    assert len(sends) == 3
    unified.observe_archived_report(source("stale-healthy-run", STAMP + timedelta(minutes=1), archived=True),
        env=env, store=store, send=lambda text: send())
    unified.immediate_event(NotificationEvent("real_error", STAMP + timedelta(minutes=4), **scope,
        source_run_id="still-failed-run", incident_reason="runtime_failure"), send, store=store)
    assert len(sends) == 3


def test_workflow_failure_correlates_to_runtime_incident_and_flags_off_delegates_original(monkeypatch):
    from notifications import operational
    env, store, sends = configure(monkeypatch), MemoryStore(), []
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    env["GITHUB_RUN_ID"] = "42"
    report = source()
    producer.persist_event(NotificationEvent("real_error", STAMP, **unified.identity(env),
        source_run_id=report["run_id"], incident_reason="cycle_state_persistence_io_error").to_candidate(),
        env=env, report=report, store=store)
    assessment = dict(reason="latest_runtime_completed_unsuccessfully",
        latest_run={"id": 42, "created_at": STAMP.isoformat()})
    assert operational._correlated_event(assessment, env=env, store=store)["incident_reason"] == "cycle_state_persistence_io_error"
    def send(text):
        return sends.append(text) or {"transport_acknowledged": True, "transport_receipt_id": "example-receipt"}
    # Runtime and monitor use the same reason despite having different run IDs.
    unified.immediate_event(NotificationEvent("real_error", STAMP, **unified.identity(env),
        source_run_id=report["run_id"], incident_reason="cycle_state_persistence_io_error"), lambda: send("original"), store=store)
    operational.notify_workflow(assessment, "monitor", send=send, store=store)
    assert sends == ["original"]
    monkeypatch.delenv(unified.FLAG)
    operational.notify_workflow(assessment, "unchanged legacy", send=send, store=store)
    assert sends[-1] == "unchanged legacy"


def test_snapshot_matches_declared_qpk_source_hash_and_new_strings_are_single_language(monkeypatch):
    import hashlib
    from pathlib import Path
    from notifications import operational
    root = Path(__file__).resolve().parents[1] / "notifications/digest_snapshot"
    provenance = json.loads((root / "provenance.json").read_text())
    assert hashlib.sha256((root / "unified_contract.py").read_bytes()).hexdigest() == provenance["files"]["unified_contract.py"]["sha256"]
    assert provenance["snapshot_commit"] == "60a30ff9a5abc8654787095c9d5a003485afb325"
    for locale in ("zh", "en"):
        monkeypatch.setenv("NOTIFY_LANG", locale)
        for category in operational._TEXT[locale]:
            text = operational.message(category)
            if locale == "zh":
                assert not any("a" <= ch.lower() <= "z" for ch in text)
            else:
                assert not any("\u4e00" <= ch <= "\u9fff" for ch in text)
