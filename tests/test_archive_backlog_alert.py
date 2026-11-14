import base64
import json
from unittest.mock import Mock

import pytest

from infra.archive_alert import relay


@pytest.fixture
def mocked_sender(monkeypatch):
    monkeypatch.setenv("PRODUCTION_POLICY", "prod-policy")
    monkeypatch.setenv("PRODUCTION_SUBSCRIPTION", "prod-sub")
    monkeypatch.setenv("TEST_POLICY", "check-policy")
    monkeypatch.setenv("TEST_SUBSCRIPTION", "check-sub")
    sender = Mock(return_value=1)
    monkeypatch.setattr(relay, "send_alert", sender)
    return sender


def post(policy="prod-policy", sub="prod-sub", **changes):
    incident = {
        "state": "open",
        "incident_id": "0.synthetic",
        "policy_name": policy,
        "resource": {"labels": {"subscription_id": sub}},
    }
    incident.update(changes)
    payload = base64.b64encode(json.dumps({"incident": incident}).encode()).decode()
    return relay.app.test_client().post("/", json={"message": {"data": payload}})


def test_production_alert(mocked_sender):
    assert post().status_code == 204
    assert "30 分钟" in mocked_sender.call_args.args[0]
    assert "测试" not in mocked_sender.call_args.args[0]


def test_test_alert_has_label(mocked_sender):
    assert post("check-policy", "check-sub").status_code == 204
    assert mocked_sender.call_args.args[0].startswith("【测试】")


def test_reject_other_subscription_and_repeat(mocked_sender):
    assert post(sub="unknown").status_code == 204
    assert post(state="closed").status_code == 204
    assert post(renotify=True).status_code == 204
    mocked_sender.assert_not_called()


def test_transport_failure_is_retried(mocked_sender, capsys):
    mocked_sender.side_effect = TimeoutError("private endpoint diagnostic")
    assert post().status_code == 503
    out = capsys.readouterr().out
    assert "archive_alert_retry" in out
    assert "private endpoint diagnostic" not in out
