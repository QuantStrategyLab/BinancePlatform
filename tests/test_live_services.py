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


def test_bound_trade_state_access_shares_one_store_and_uses_native_create():
    from live_services import bind_trade_state_access
    from google.api_core.exceptions import AlreadyExists
    store = Mock()
    document = store.client.collection.return_value.document.return_value
    with patch('live_services._get_document_store', return_value=store) as factory:
        load, save, claim, _release = bind_trade_state_access(normalize_fn=lambda x: x, default_state_factory=dict)
        assert claim('owner-one') is True
        document.create.assert_called_once_with({'owner_id': 'owner-one'}, retry=None)
        document.create.side_effect = AlreadyExists('exists')
        assert claim('owner-one') is False
        store.get.return_value = {'existing': True}
        assert load(normalize=False) == {'existing': True}
        assert save({'updated': True}) is True
        assert factory.call_count == 1


def test_native_owner_delete_compares_owner_and_only_returns_after_commit():
    import pytest
    from google.api_core.exceptions import Aborted, DeadlineExceeded, PermissionDenied
    from google.cloud import firestore
    from google.cloud.firestore_v1.transaction import transactional
    from live_services import bind_trade_state_access

    for existing in (None, 'new-owner', 'old-owner'):
        for error in (None, Aborted('conflict'), DeadlineExceeded('unknown'), PermissionDenied('denied')):
            store = Mock()
            ref = store.client.collection.return_value.document.return_value
            ref.get.return_value = Mock(exists=existing is not None)
            ref.get.return_value.to_dict.return_value = {'owner_id': existing}
            tx = store.client.transaction.return_value
            tx._max_attempts, tx._read_only = 1, False
            tx._commit.side_effect = error
            with patch('live_services._get_document_store', return_value=store), patch.object(firestore, 'transactional', transactional, create=True):
                _, _, _, release = bind_trade_state_access(normalize_fn=lambda x: x, default_state_factory=dict)
                if error:
                    with pytest.raises((ValueError, DeadlineExceeded, PermissionDenied)):
                        release('old-owner')
                    tx._rollback.assert_called_once()
                else:
                    assert release('old-owner') is (existing == 'old-owner')
                ref.get.assert_called_once_with(transaction=tx, retry=None)
                store.client.transaction.assert_called_once_with(max_attempts=1)
                assert tx.delete.call_count == int(existing == 'old-owner')
                tx._commit.assert_called_once()


def test_native_claim_does_not_treat_uncertain_or_permission_failure_as_busy():
    import pytest
    from google.api_core.exceptions import DeadlineExceeded, PermissionDenied
    from live_services import bind_trade_state_access
    store = Mock()
    with patch('live_services._get_document_store', return_value=store):
        _, _, claim, release = bind_trade_state_access(normalize_fn=lambda x: x, default_state_factory=dict)
        for owner in ('', ' ', None):
            with pytest.raises(ValueError, match='state_owner_required'):
                claim(owner)
            with pytest.raises(ValueError, match='state_owner_required'):
                release(owner)
        for error in (DeadlineExceeded('unknown'), PermissionDenied('denied')):
            store.client.collection.return_value.document.return_value.create.side_effect = error
            with pytest.raises(type(error)):
                claim('owner')
