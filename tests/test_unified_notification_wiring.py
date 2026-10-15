from types import SimpleNamespace

from unittest.mock import Mock
from notifications.digest_snapshot.digest_routing import routine_notification
from runtime_support import runtime_notify


def test_classified_routine_order_skips_transport_and_opt_out_keeps_sender(monkeypatch):
    monkeypatch.setenv("QSL_ROUTINE_NOTIFICATION_MODE", "daily_digest")
    sender = Mock(return_value=True)
    report = {"notifications": []}
    assert not routine_notification(sender, object(), report, "synthetic routine")
    sender.assert_not_called()
    assert report["notifications"][0]["transport_acknowledged"] is False
    monkeypatch.setenv("QSL_DIGEST_ROUTINE_ORDERS", "false")
    assert routine_notification(sender, object(), report, "synthetic routine")
    sender.assert_called_once()


def test_urgent_runtime_notice_still_calls_transport(monkeypatch):
    monkeypatch.setenv("QSL_ROUTINE_NOTIFICATION_MODE", "daily_digest")
    notifier = Mock(return_value=True)
    runtime = SimpleNamespace(run_id="synthetic-run", dry_run=False, notifier=notifier, tg_token="synthetic", tg_chat_id="synthetic", side_effect_log=[])
    report = {"notifications": [], "summary": {}, "side_effect_summary": {"executed_call_count": 0, "suppressed_call_count": 0}}
    assert runtime_notify(runtime, report, "synthetic risk halt")
    notifier.assert_called_once()
