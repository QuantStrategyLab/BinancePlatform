"""Real Binance export through a byte-pinned, test-only QRS receiver."""

from copy import deepcopy
from datetime import timedelta
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from notifications import unified_notifications as unified
from notifications import unified_producer as producer
from notifications.digest_snapshot.digest_candidates import cycle_evidence
from notifications.digest_snapshot.unified_contract import order_identity_digest
from tests.test_notification_consolidation import MemoryStore, STAMP, TARGET, environment, source


QRS_SHA = "bd3dc79f10b2c0b8048721695885c3f9333a53c6"
FIXTURE = Path(__file__).parent / "fixtures/qrs_notification_contract"
DAY = "2026-10-16"


@pytest.fixture
def qrs(monkeypatch):
    # These upstream scripts use bare imports. Keep their modules isolated from
    # this repository's scripts and restore the prior import state afterwards.
    names = {path.stem for path in FIXTURE.glob("*.py")} | {
        "notification_snapshot", "notification_snapshot.unified_contract", "notification_snapshot.digest_routing"}
    previous = {name: sys.modules.pop(name) for name in names if name in sys.modules}
    monkeypatch.syspath_prepend(str(FIXTURE))
    monkeypatch.setenv("QRS_UNIFIED_NOTIFICATION_ENABLED", "true")
    monkeypatch.setenv("DIGEST_EXPECTED_TARGETS_JSON", json.dumps([unified.identity(environment())]))
    monkeypatch.setenv("DIGEST_CANDIDATES_STORE_STATUS", "loaded")
    for name in ("DIGEST_CANDIDATES_JSON", "DIGEST_CANDIDATES_STORE_FAILURES_JSON"):
        monkeypatch.delenv(name, raising=False)
    try:
        yield SimpleNamespace(receiver=importlib.import_module("notification_candidate_adapter"),
            merger=importlib.import_module("merge_digest_candidates"),
            aggregator=importlib.import_module("daily_digest_aggregator"))
    finally:
        for name in names:
            sys.modules.pop(name, None)
        sys.modules.update(previous)


def test_qrs_fixture_matches_recorded_source_bytes():
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    assert provenance["commit"] == QRS_SHA and provenance["test_only"] is True
    for name, info in provenance["files"].items():
        assert hashlib.sha256((FIXTURE / name).read_bytes()).hexdigest() == info["sha256"]


def export_window(monkeypatch, order_counts):
    """Fake archived inputs and senders; execute the real routing/export code."""
    from quant_platform_kit.common.execution_receipts import build_execution_receipt
    env, store, sends, reports = environment(), MemoryStore(), [], []
    env.update(RUNTIME_TARGET_ENABLED="true", EXECUTION_REPORT_GCS_URI="gs://example-reports/private")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    for index, count in enumerate(order_counts):
        stamp = STAMP - timedelta(hours=20 - index)
        report = source(f"example-business-run-{index}", stamp, archived=True)
        orders = [{"status": "filled", "fills": [{"tradeId": f"example-{index}-{n}-{fill}"}
            for fill in range(2)]} for n in range(count)]
        report["summary"]["notification_digest"] = cycle_evidence(
            {"execution_status": "completed", "broker_submission_done": bool(orders)}, orders)
        report["execution_receipt"] = build_execution_receipt(platform="binance",
            strategy_profile=TARGET["strategy_profile"], strategy_revision="a" * 40, execution_mode="live",
            outcome="filled" if orders else "no_action", observed_at=stamp)
        assert unified.route(lambda: pytest.fail("routine notice must be deferred"), kind="routine_cycle",
            env=env, report=report, candidate_store=store) is False
        for kind in (("order_submitted", "fill") if orders else ()):
            def send():
                sends.append(kind)
                return {"transport_acknowledged": True, "transport_receipt_id": f"example-provider-{len(sends)}"}
            assert unified.route(send, kind=kind, report=report, env=env, candidate_store=store,
                order_digest=order_identity_digest(f"example-order-{index}"))["transport_acknowledged"]
        reports.append(report)

    def read(args):
        if args[0] == "ls":
            return [{"url": f"gs://example-reports/private/binance/2026-10/{index}.json"}
                for index in range(len(reports))]
        return deepcopy(reports[int(args[1].rsplit("/", 1)[1].split(".")[0])])

    now = STAMP + timedelta(hours=1)
    result = producer.emit_archive(env, now=now, read=read, store=store)
    payload = deepcopy(store.load(f"binance/{DAY}/example-slot.json")[0])
    writes = store.writes
    producer.emit_archive(env, now=now, read=read, store=store)
    assert store.writes == writes  # duplicate archive observation is idempotent
    assert result["runs"] == len(order_counts)
    return payload, sends


def receive(qrs, tmp_path, payload, *, duplicate=False, locale="zh"):
    inputs = tmp_path / "inputs"
    inputs.mkdir(exist_ok=True)
    (inputs / "producer.json").write_text(json.dumps(payload))
    if duplicate:
        (inputs / "duplicate-observation.json").write_text(json.dumps(payload))
    merged, stats = qrs.merger.merge(None, inputs)
    rows = qrs.merger._rows(merged)
    expected = [unified.identity(environment())]
    qrs.receiver.validate_expected(expected)
    current, stale = qrs.receiver.partition(rows, DAY)
    assert not stale
    received, summary = qrs.receiver.aggregate(current, expected)
    merged_path = tmp_path / "merged.json"
    merged_path.write_text(json.dumps(merged))
    config_path = tmp_path / "platform-config.json"
    config_path.write_text(json.dumps({"notifications": {"quant_sentinel": {"daily_digest": {}}}}))
    digest, text, collected = qrs.aggregator.build_digest_payload(business_day=DAY, locale=locale,
        candidates_path=merged_path, config_path=config_path, include_github=False)
    receipt = qrs.aggregator.build_receipt(digest, collected, message_chars=len(text))
    assert receipt["notification_summary"] == summary
    assert not collected.coverage.missing_targets
    evidence_dir = os.environ.get("BACKPORT_EVIDENCE_DIR")
    if evidence_dir:
        directory = Path(evidence_dir).resolve()
        assert directory.is_relative_to(Path(__file__).resolve().parents[2])
        with (directory / "receiver-replays.jsonl").open("a") as output:
            output.write(json.dumps({"synthetic_inputs": True, "qrs_commit": QRS_SHA,
                "test": os.environ.get("PYTEST_CURRENT_TEST", ""), "candidate": payload,
                "summary": summary, "receipt": receipt, "text": text}, ensure_ascii=False) + "\n")
    return received, summary, text, receipt, collected, stats


@pytest.mark.parametrize("order_counts,event_count,immediate_count", [((0, 0), 2, 0), ((0, 1), 4, 2)])
@pytest.mark.parametrize("duplicate", [False, True])
def test_real_producer_merge_receiver_roundtrip(monkeypatch, tmp_path, qrs,
        order_counts, event_count, immediate_count, duplicate):
    payload, sends = export_window(monkeypatch, order_counts)
    assert len(payload["runs"]) == 2
    assert "notification_events" not in payload and "source_facts" not in payload
    for index, row in enumerate(payload["runs"]):
        assert row["cycle_count"] == 1 and row["order_count"] == order_counts[index]
        assert row["fill_count"] == 2 * order_counts[index]
        assert all(event["source_run_id"] == row["source_run_id"] for event in row["notification_events"])
    received, summary, text, receipt, collected, stats = receive(qrs, tmp_path, payload, duplicate=duplicate)
    assert len(received) == receipt["run_count"] == 2
    assert summary["source_inventory"]["binance"] == {"expected_targets": 1, "covered_targets": 1, "runs": 2}
    assert summary["counts_unknown"] is False and summary["run_count_unknown"] is False
    assert summary["event_count"] == event_count and summary["immediate_events"] == immediate_count
    assert sum(row["order_count"] for row in received) == sum(order_counts)
    assert receipt["total_fills_status"] == "known" and receipt["total_fills"] == 2 * sum(order_counts)
    assert len(sends) == immediate_count and stats["file_rows"] == (4 if duplicate else 2)
    zero = next(run for run in collected.runs if run.order_count == 0)
    assert zero.order_count_status == zero.fill_count_status == "known"
    # Healthy zero-order runs use QRS's no-trade wording; blocked zero-order
    # runs use its separate "本轮 0 单" wording. Neither means unknown.
    assert qrs.aggregator.digest._trade_counts(zero, "zh") == "无交易"
    assert "全窗口统计未知" not in text and "运行次数未知" not in text
    assert f"订单：{sum(order_counts)}" in text


def test_monitor_fact_attaches_only_to_observed_business_run(monkeypatch, tmp_path, qrs):
    from notifications.digest_snapshot.unified_contract import NotificationEvent
    payload, _ = export_window(monkeypatch, (0, 0))
    store, env = MemoryStore(), environment()
    key = f"binance/{DAY}/example-slot.json"
    assert store.compare_and_swap(key, 0, payload)
    observed = payload["runs"][1]
    event = NotificationEvent("healthy_heartbeat", STAMP - timedelta(hours=19),
        **unified.identity(env), source_run_id=observed["source_run_id"]).to_candidate()
    producer.persist_event(event, env=env, store=store)
    exported = store.load(key)[0]
    received, summary, *_ = receive(qrs, tmp_path, exported)
    assert len(received) == 2 and summary["event_count"] == 3
    assert [len(row["notification_events"]) for row in exported["runs"]] == [1, 2]
    with pytest.raises(ValueError, match="candidate_business_run_unverified"):
        producer.persist_event({**event, "source_run_id": "example-monitor-job"}, env=env, store=store)


def test_single_zero_order_run_roundtrip_renders_known_zero(monkeypatch, tmp_path, qrs):
    payload, sends = export_window(monkeypatch, (0,))
    received, summary, text, receipt, *_ = receive(qrs, tmp_path, payload)
    assert not sends and len(received) == receipt["run_count"] == 1
    assert summary["counts_unknown"] is False and summary["event_count"] == 1
    assert "本轮 0 单" in text and "全窗口统计未知" not in text
