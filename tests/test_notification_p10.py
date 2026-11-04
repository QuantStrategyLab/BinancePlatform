"""Offline P10 checks: fake transports only, no broker or cloud calls."""
from unittest.mock import Mock

import pytest

from notifications import operational, routing, unified_notifications as unified


@pytest.mark.parametrize("flag", ["false", "true"])
@pytest.mark.parametrize("status", ["healthy", "not_applicable", "parked", "deferred"])
def test_monitor_nonerrors_never_send(monkeypatch, flag, status):
    monkeypatch.setenv(unified.FLAG, flag)
    sender = Mock()
    assert operational.notify_workflow({"status": status}, "legacy", send=sender) is False
    sender.assert_not_called()


def test_flag_off_error_uses_safe_template_not_arbitrary_legacy(monkeypatch):
    monkeypatch.setenv(unified.FLAG, "false")
    monkeypatch.setenv("NOTIFY_LANG", "zh-CN")
    sender = Mock(return_value=True)
    assert operational.notify_workflow({"status": "unavailable", "reason": "unknown"},
                                      "account=fake-private-account token=fake-secret-token", send=sender)
    text = sender.call_args.args[0]
    assert text.startswith("【资产管理｜运行错误】")
    assert "是否提交订单：未知" in text
    assert "运行链接不可用" in text
    assert "fake-private-account" not in text and "fake-secret-token" not in text


def test_immediate_facade_does_not_forward_raw_diagnostics(monkeypatch):
    monkeypatch.setenv(unified.FLAG, "false")
    sender = Mock(return_value=True)
    assert routing.immediate_notification(sender, object(), {}, "fake-private-account",
                                          reason="unrecognized-fake-private-account")
    text = sender.call_args.args[2]
    assert "fake-private-account" not in text
    assert "【资产管理｜运行错误】" in text


def test_disabled_routine_does_not_send_or_access_store(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "false")
    sender, store = Mock(), Mock()
    assert unified.route(sender, kind="healthy_heartbeat", candidate_store=store) is False
    sender.assert_not_called()
    store.load.assert_not_called()


def test_unknown_monitor_state_remains_error(monkeypatch):
    monkeypatch.setenv(unified.FLAG, "false")
    sender = Mock(return_value=True)
    assert operational.notify_workflow({"status": "unknown"}, "", send=sender)
    sender.assert_called_once()


def test_disabled_error_status_and_untrusted_evidence(monkeypatch):
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "false")
    monkeypatch.setenv("QSL_NOTIFY_LANG", "zh-CN")
    text = operational.message("unknown", observed_at="not-a-time",
                               source_url="https://fake-private-host/?token=fake-secret-token")
    assert "已停用；运行、订单和成交：不适用" in text
    assert "时间：未核实" in text
    assert "无法核实运行状态" in text
    assert "fake-private-host" not in text and "fake-secret-token" not in text


def test_real_run_link_and_sgt_timestamp(monkeypatch):
    monkeypatch.setenv("QSL_NOTIFY_LANG", "en")
    url = "https://github.com/QuantStrategyLab/BinancePlatform/actions/runs/123"
    text = operational.message("runtime_failure", observed_at="2026-11-05T00:00:00Z", source_url=url)
    assert "2026-11-05 08:00:00 +0800" in text
    assert url in text
    assert "order submission unknown" in text
