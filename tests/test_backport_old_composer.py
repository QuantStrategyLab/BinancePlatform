"""The old cycle composer and report builder with fake business boundaries."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from application.execution_receipt_adapter import record_order_response, record_order_submission_attempt
from notifications import unified_notifications as unified
from notifications import unified_producer as producer
from runtime_support import build_execution_report, runtime_notify
from tests import test_cycle_service as old_fixture
from tests.test_notification_consolidation import MemoryStore, STAMP, TARGET, configure
from tests.test_notification_qrs_roundtrip import qrs as qrs, receive, DAY


def compose(monkeypatch, *, run="example-old-run", permitted=True, order=False, attested=True):
    env=configure(monkeypatch)
    monkeypatch.setattr("runtime_support.observe_runtime_scheduler_state",lambda:{"state":"unknown"})
    sends=[]
    target=SimpleNamespace(**TARGET, to_dict=lambda:deepcopy(TARGET))
    runtime=SimpleNamespace(runtime_target=target, dry_run=False,
        state_owner_claim=lambda _:True, state_owner_release=lambda _:True,
        now_utc=STAMP-timedelta(hours=20), run_id=run, strategy_profile=TARGET["strategy_profile"],
        strategy_domain="crypto", strategy_display_name="Example strategy",
        strategy_display_name_localized="示例策略", standard_execution_permitted=True,
        tg_token="example-token", tg_chat_id="example-chat", side_effect_log=[], order_sequence=0,
        notifier=lambda **kw:sends.append(kw) or {"transport_acknowledged":True,
            "transport_receipt_id":f"example-provider-message-{len(sends)}"})
    real=old_fixture.execute_strategy_cycle
    store=MemoryStore()

    def execute(*args, **kwargs):
        def build(rt):
            report=build_execution_report(rt)
            if attested:
                report["runtime_release_receipt"]={"attestation_state":"self_attested",
                    "strategy_release":{"strategy_revision":"a"*40}}
            return report
        kwargs["build_execution_report"]=build
        kwargs["runtime_notify"]=runtime_notify
        kwargs["maybe_send_periodic_btc_status_report"]=lambda *a,**kw:kw["notifier_fn"]("Example healthy cycle")
        if order:
            def rotate(rt,report,*a,**kw):
                record_order_submission_attempt(report)
                record_order_response(report,{"status":"FILLED"})
                rt.side_effect_log.append({"effect_type":"order_buy","payload":{"newClientOrderId":"example-old-order"}})
                unified.routine_notice(runtime_notify,rt,report,"Example fill",routine_order=True)
                return 185.0
            kwargs["execute_trend_rotation"]=rotate
        return real(*args,**kwargs)

    with patch.object(old_fixture,"execute_strategy_cycle",execute), patch.object(unified,"NotificationStore",return_value=store), \
            patch.object(producer,"NotificationStore",return_value=store):
        report,_events=old_fixture.CycleServiceTests()._run_funds_cycle(permitted,runtime=runtime)
    return env,store,report,sends


def archived_payload(env,store,reports):
    env={**env,"RUNTIME_TARGET_ENABLED":"true","EXECUTION_REPORT_GCS_URI":"gs://example-reports/private"}
    def read(args):
        if args[0]=="ls":
            return [{"url":f"gs://example-reports/private/binance/2026-10/{i}.json"} for i in range(len(reports))]
        return deepcopy(reports[int(args[1].rsplit('/',1)[1].split('.')[0])])
    producer.emit_archive(env,now=STAMP+timedelta(hours=1),read=read,store=store)
    return store.load(f"binance/{DAY}/example-slot.json")[0]


def test_actual_old_composer_builder_archive_and_final_receiver(monkeypatch,tmp_path,qrs):
    env,store,report,sends=compose(monkeypatch)
    assert report["status"]=="ok" and report["execution_receipt"]["outcome"]=="no_action"
    assert report["summary"]["notification_digest"]=={
        "schema_version":"qsl.notification_cycle.v1","dry_run":False,"order_count":0,"fill_count":0,"routine":True}
    assert not sends
    payload=archived_payload(env,store,[report])
    rows,summary,text,receipt,*_=receive(qrs,tmp_path,payload,duplicate=True)
    assert len(rows)==receipt["run_count"]==1 and summary["event_count"]==1
    assert not summary["counts_unknown"] and "本轮 0 单" in text
    assert rows[0]["source_fact"]["archived"] is True


def test_old_composer_order_is_immediate_and_unknown_fill_count_is_not_inferred(monkeypatch,tmp_path,qrs):
    env,store,report,sends=compose(monkeypatch,order=True)
    assert len(sends)==1 and sends[0]["text"]=="Example fill"
    assert report["execution_receipt"]["outcome"]=="filled"
    digest=report["summary"]["notification_digest"]
    assert digest["order_count"]==1 and digest["fill_count"] is None
    payload=archived_payload(env,store,[report])
    rows,summary,text,receipt,*_=receive(qrs,tmp_path,payload)
    assert len(rows)==1 and rows[0]["order_count"]==1 and rows[0]["fill_count"] is None
    assert summary["counts_unknown"] and receipt["total_fills"] is None
    assert "全窗口统计未知" in text


@pytest.mark.parametrize("permitted,attested",[(False,True),(True,False)])
def test_old_composer_blocked_or_unattested_is_unknown_and_keeps_immediate_incident(
        monkeypatch,tmp_path,qrs,permitted,attested):
    env,store,report,_=compose(monkeypatch,permitted=permitted,attested=attested)
    sends=[]
    assert unified.report_issue(report) is not None
    unified.observe_archived_report(report,env=env,store=store,
        send=lambda text:sends.append(text) or True)
    assert len(sends)==1
    marker=next(value for _,value in store.values.values() if "delivery_status" in value)
    assert marker["delivery_status"]=="uncertain" and marker["receipt_id"] is None
    unified.observe_archived_report(report,env=env,store=store,
        send=lambda text:sends.append(text) or True)
    assert len(sends)==1
    payload=store.load(f"binance/{DAY}/example-slot.json")[0]
    rows,summary,text,receipt,*_=receive(qrs,tmp_path,payload)
    assert rows[0]["order_count"] is None and rows[0]["fill_count"] is None
    assert summary["counts_unknown"] and receipt["total_fills_status"]=="unknown"
    assert "本轮 0 单" not in text


def test_empty_crypto_archive_is_unproven_and_cannot_create_zero_run_candidate(monkeypatch):
    env=configure(monkeypatch)
    env.update(RUNTIME_TARGET_ENABLED="true",EXECUTION_REPORT_GCS_URI="gs://example-reports/private")
    store=MemoryStore()
    with pytest.raises(ValueError,match="candidate_due_source_absent"):
        producer.emit_archive(env,now=STAMP+timedelta(hours=1),read=lambda _:[],store=store)
    assert not store.values


def test_only_calendar_authoritative_idle_projection_can_skip_zero_runs(monkeypatch,tmp_path,qrs):
    from notifications.digest_snapshot.digest_archive import emit
    import quant_platform_kit.common.runtime_heartbeat_policy as policy
    import json
    env=configure(monkeypatch)
    env.update(QSL_ROUTINE_NOTIFICATION_MODE="daily_digest",RUNTIME_TARGET_ENABLED="true",
        EXECUTION_REPORT_GCS_URI="gs://example-reports/private",
        DIGEST_NOTIFICATION_SCHEDULE_JSON=json.dumps({"scheduler":{"main_time":"0 20 * * 1-5","timezone":"UTC"}}))
    monkeypatch.setattr(policy,"filter_due_targets",lambda *a,**kw:([],True))
    outputs=[]
    emit(env,now=STAMP+timedelta(hours=1),read=lambda _:[],upload=lambda uri,body:outputs.append(json.loads(body)))
    rows,summary,text,receipt,*_=receive(qrs,tmp_path,outputs[0])
    assert rows==[] and summary["source_inventory"]["binance"]["runs"]==0
    assert text=="" and receipt["evidence_status"]=="verified_idle"
    outputs.clear()
    monkeypatch.setattr(policy,"filter_due_targets",lambda *a,**kw:([],False))
    with pytest.raises(ValueError,match="candidate_due_source_absent"):
        emit(env,now=STAMP+timedelta(hours=1),read=lambda _:[],upload=lambda *a:outputs.append(a))
    assert not outputs
