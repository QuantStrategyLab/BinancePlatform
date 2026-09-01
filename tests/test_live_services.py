from unittest.mock import Mock, patch

from live_services import load_trade_state, save_trade_state, send_tg_msg


def test_load_trade_state_logs_only_safe_failure_reason():
    store = Mock()
    store.get.side_effect = RuntimeError("SENSITIVE_PROVIDER_SENTINEL")

    with patch("live_services._get_document_store", return_value=store), patch("builtins.print") as print_mock:
        result = load_trade_state(normalize_fn=lambda value: value, default_state_factory=dict)

    rendered = " ".join(str(call) for call in print_mock.call_args_list)
    assert result is None
    assert "state_load_failed" in rendered
    assert "SENSITIVE_PROVIDER_SENTINEL" not in rendered


def test_save_trade_state_logs_only_safe_failure_reason():
    store = Mock()
    store.set.side_effect = RuntimeError("provider-secret-state-write-error")

    with patch("live_services._get_document_store", return_value=store), patch("builtins.print") as print_mock:
        result = save_trade_state({"ok": True}, normalize_fn=lambda value: value)

    rendered = " ".join(str(call) for call in print_mock.call_args_list)
    assert result is False
    assert "state_persistence_failed" in rendered
    assert "provider-secret-state-write-error" not in rendered


def test_send_tg_msg_rejects_telegram_ok_false():
    response = Mock(status_code=200)
    response.json.return_value = {"ok": False, "description": "rejected"}

    with patch("live_services.requests.post", return_value=response):
        receipt = send_tg_msg("token-value", "chat-value", "hello")

    assert receipt["delivery_status"] == "failed"
    assert receipt["transport_acknowledged"] is False
    assert receipt["error_type"] == "telegram_rejected"
    assert "token-value" not in str(receipt)
    assert "chat-value" not in str(receipt)
    assert "hello" not in str(receipt)


def test_send_tg_msg_records_safe_acknowledged_receipt():
    response = Mock(status_code=200)
    response.json.return_value = {"ok": True, "result": {"message_id": 123}}

    with patch("live_services.requests.post", return_value=response):
        receipt = send_tg_msg("token-value", "chat-value", "hello")

    assert receipt["delivery_status"] == "sent"
    assert receipt["transport_acknowledged"] is True
    assert len(receipt["compact_text_sha256"]) == 64
    assert receipt["compact_text_length"] > 0
