from unittest.mock import Mock, patch

from live_services import send_tg_msg


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
