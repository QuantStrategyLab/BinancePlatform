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
    from google.api_core.exceptions import AlreadyExists
    access, client, store = session_fixture({'existing': True})
    load, save, claim, _release = access
    assert claim('owner-one') is True
    assert client.data[client.owner] == {'owner_id': 'owner-one'}
    with patch.object(access._owner._reference, 'create', side_effect=AlreadyExists('exists')):
        other, _, _ = session_fixture({'existing': True})
        with patch.object(other._owner._reference, 'create', side_effect=AlreadyExists('exists')):
            assert other.claim('owner-two') is False
    assert load(normalize=False) == {'existing': True}
    assert save({'updated': True}) is True
    store.set.assert_not_called()


def test_native_owner_delete_compares_owner_and_only_returns_after_commit():
    for existing in (None, 'new-owner', 'old-owner'):
        for error in (None, 'before_commit', 'after_commit'):
            access, client, _ = session_fixture({'value': 1})
            access.claim('old-owner')
            if existing is None:
                client.data.pop(client.owner)
            else:
                client.change(client.owner, {'owner_id': existing})
            client.failure = error
            if error or existing != 'old-owner':
                with pytest.raises(RuntimeError, match='state_session_'):
                    access.release('old-owner')
            else:
                assert access.release('old-owner') is True
                assert client.owner not in client.data
            assert not access.active
            assert sum(event[0] == 'commit' for event in client.events) <= 1


def test_native_claim_does_not_treat_uncertain_or_permission_failure_as_busy():
    from google.api_core.exceptions import DeadlineExceeded, PermissionDenied
    for owner in ('', ' ', None):
        access, _, _ = session_fixture({})
        with pytest.raises(ValueError, match='state_owner_required'):
            access.claim(owner)
        with pytest.raises(ValueError, match='state_owner_required'):
            access.release(owner)
    for error in (DeadlineExceeded('SENSITIVE_UNKNOWN'), PermissionDenied('SENSITIVE_DENIED')):
        access, _, _ = session_fixture({})
        with patch.object(access._owner._reference, 'create', side_effect=error):
            with pytest.raises(RuntimeError, match='state_session_claim_uncertain') as caught:
                access.claim('owner')
        assert not access.active
        assert 'SENSITIVE' not in str(caught.value)


# Native-shaped offline RPC fixture: never constructs an SDK client or provider.
import copy
from datetime import datetime, timedelta, timezone

import pytest

from application.interval_source_receipt_candidate import _version
from live_services import bind_trade_state_access


class SessionSnapshot:
    def __init__(self, data, version):
        self.exists = data is not None
        self.update_time = version
        self.data = copy.deepcopy(data)

    def to_dict(self):
        return copy.deepcopy(self.data)


class SessionReference:
    def __init__(self, client, path):
        self._client, self.path = client, path
        self._document_path = client._database_string + '/documents/' + path
        self.parent = SessionCollection(client, path.rsplit('/', 1)[0])

    def get(self, *, transaction, retry, timeout):
        assert retry is None and timeout == 10
        assert transaction._client is self._client and transaction.id
        assert not transaction.writes
        self._client.events.append(('get', self.path))
        transaction.reads[self.path] = self._client.versions.get(self.path)
        return SessionSnapshot(self._client.data.get(self.path), self._client.versions.get(self.path))

    def create(self, data, *, retry, timeout):
        assert retry is None and timeout == 10
        self._client.events.append(('claim', self.path))
        if self.path in self._client.data:
            from google.api_core.exceptions import AlreadyExists
            raise AlreadyExists('synthetic')
        self._client.change(self.path, data)


class SessionCollection:
    def __init__(self, client, path):
        self.client, self.path = client, path

    def document(self, name):
        return SessionReference(self.client, self.path + '/' + name)


class SessionTransaction:
    def __init__(self, client):
        self._client, self._max_attempts = client, 1
        self._id = None
        self.reads, self.writes = {}, []
        self._read_only = False

    @property
    def _write_pbs(self):
        return self.writes

    def _options_protobuf(self, retry_id):
        assert retry_id is None
        return {'read_only': self._read_only}

    def _clean_up(self):
        self._id = None
        self.writes = []

    @property
    def id(self):
        return self._id

    def _begin(self, *, retry, timeout):
        assert retry is None and timeout == 10
        self._id = b'synthetic-transaction'
        self._client.events.append(('begin',))

    def set(self, ref, data):
        self.writes.append(('set', ref.path, copy.deepcopy(data)))

    def create(self, ref, data):
        self.writes.append(('create', ref.path, copy.deepcopy(data)))

    def update(self, ref, data):
        self.writes.append(('update', ref.path, copy.deepcopy(data)))

    def delete(self, ref):
        self.writes.append(('delete', ref.path, None))

    def _commit(self, *, retry, timeout):
        assert retry is None and timeout == 10
        self._client.events.append(('commit', tuple(w[0] for w in self.writes)))
        if self._client.before_commit:
            callback, self._client.before_commit = self._client.before_commit, None
            callback(self._client)
        if any((_version(self._client.versions[path]) if path in self._client.versions else None)
               != (_version(version) if version is not None else None)
               for path, version in self.reads.items()):
            raise RuntimeError('synthetic CAS conflict')
        if self._client.failure == 'before_commit' and self.writes:
            self._client.failure = None
            raise RuntimeError('SENSITIVE_BEFORE_COMMIT')
        candidate = copy.deepcopy(self._client.data)
        for mode, path, data in self.writes:
            if mode == 'create' and path in candidate:
                raise RuntimeError('synthetic create conflict')
            if mode == 'delete':
                candidate.pop(path, None)
            elif mode == 'update':
                candidate[path].update(data)
            else:
                candidate[path] = data
        self._client.data = candidate
        for _, path, _ in self.writes:
            self._client.tick()
            self._client.versions[path] = self._client.clock
        if self._client.after_commit and self.writes:
            callback, self._client.after_commit = self._client.after_commit, None
            callback(self._client)
        if self._client.failure == 'after_commit' and self.writes:
            self._client.failure = None
            raise RuntimeError('SENSITIVE_LOST_RESPONSE')
        self._id = None
        return []

    def _rollback(self, *, retry, timeout):
        assert retry is None and timeout == 10
        self._client.events.append(('rollback',))
        self._id = None


class SessionAPI:
    def __init__(self, client):
        self.client = client

    def begin_transaction(self, *, request, retry, timeout, metadata):
        from types import SimpleNamespace
        assert request['database'] == self.client._database_string
        assert metadata == self.client._rpc_metadata
        tx = self.client.transactions[-1]
        tx._begin(retry=retry, timeout=timeout)
        return SimpleNamespace(transaction=tx.id)

    def commit(self, *, request, retry, timeout, metadata):
        tx = self.client.transactions[-1]
        assert request['database'] == self.client._database_string
        assert request['transaction'] == tx.id and tx.id
        assert request['writes'] is tx._write_pbs
        assert metadata == self.client._rpc_metadata
        assert not tx._read_only
        return tx._commit(retry=retry, timeout=timeout)

    def rollback(self, *, request, retry, timeout, metadata):
        tx = self.client.transactions[-1]
        assert request['database'] == self.client._database_string
        assert request['transaction'] == tx.id
        assert metadata == self.client._rpc_metadata
        return tx._rollback(retry=retry, timeout=timeout)


class SessionClient:
    _database_string = 'projects/synthetic-project/databases/synthetic-database'
    ledger = 'strategy/MULTI_ASSET_STATE'
    owner = ledger + '__owner'

    def __init__(self, data):
        self.clock = datetime(2026, 10, 4, tzinfo=timezone.utc)
        self.data = {} if data is None else {self.ledger: copy.deepcopy(data)}
        self.versions = {} if data is None else {self.ledger: self.clock}
        self.events, self.transactions = [], []
        self.before_commit = self.after_commit = self.failure = None
        self._firestore_api = SessionAPI(self)
        self._rpc_metadata = (('synthetic', 'safe'),)

    def tick(self):
        self.clock += timedelta(microseconds=1)

    def change(self, path, data):
        self.tick()
        self.data[path] = copy.deepcopy(data)
        self.versions[path] = self.clock

    def collection(self, name):
        return SessionCollection(self, name)

    def transaction(self, *, max_attempts, read_only=False):
        assert max_attempts == 1
        transaction = SessionTransaction(self)
        transaction._read_only = read_only
        self.transactions.append(transaction)
        return transaction


def session_fixture(data=None, *, receipt_enabled=False, normalize_fn=lambda x: x):
    client = SessionClient(data)
    store = Mock(client=client)
    with patch('live_services._get_document_store', return_value=store) as factory:
        access = bind_trade_state_access(normalize_fn=normalize_fn, default_state_factory=dict,
                                         receipt_enabled=receipt_enabled)
    assert factory.call_count == 1
    return access, client, store


def test_unbound_native_save_fails_without_opening_provider():
    with patch('live_services._get_document_store', side_effect=AssertionError('provider forbidden')) as factory:
        assert save_trade_state({'ok': True}, normalize_fn=lambda x: x) is False
        factory.assert_not_called()


def test_bound_session_saves_detached_versioned_source_and_preserves_unknown_fields():
    access, client, store = session_fixture({'known': {'value': 1}, 'future': 'keep'},
                                            normalize_fn=lambda x: {'known': x['known']})
    assert access.claim('owner-one') is True
    working = access.load()
    working['known']['value'] = 2
    assert access.save(working) is True
    assert client.data[client.ledger] == {'known': {'value': 2}, 'future': 'keep'}
    working['known']['value'] = 99
    assert client.data[client.ledger]['known']['value'] == 2
    store.set.assert_not_called()
    store.get.assert_not_called()
    assert access.release('owner-one') is True
    assert not access.active


@pytest.mark.parametrize('race', ['replace_owner', 'remove_owner', 'ledger_change', 'before_commit', 'after_commit'])
def test_bound_session_failure_permanently_stops_mutation(race):
    access, client, _ = session_fixture({'value': 1})
    access.claim('owner-one')
    access.load()
    if race == 'replace_owner':
        client.change(client.owner, {'owner_id': 'other'})
    elif race == 'remove_owner':
        client.data.pop(client.owner)
    elif race == 'ledger_change':
        client.change(client.ledger, {'value': 3})
    else:
        client.failure = race
    with pytest.raises(RuntimeError, match='state_session_') as error:
        access.save({'value': 2})
    assert 'SENSITIVE' not in str(error.value)
    assert not access.active
    count = len(client.events)
    with pytest.raises(RuntimeError, match='state_session_'):
        access.save({'value': 4})
    assert len(client.events) == count
    with pytest.raises(RuntimeError):
        access.load()  # No silent rebase/reload after uncertainty.


def test_bound_session_rejects_non_atomic_owner_race():
    access, client, _ = session_fixture({'value': 1})
    access.claim('owner-one')
    access.load()
    client.before_commit = lambda c: c.change(c.owner, {'owner_id': 'replacement'})
    with pytest.raises(RuntimeError):
        access.save({'value': 2})
    assert client.data[client.ledger] == {'value': 1}
    assert not access.active


class StubNativeAPI:
    """Real 2.28 references/transactions/protos, with every RPC stubbed."""
    def __init__(self, client, previous):
        self.client = client
        self.ledger = client.document('strategy/MULTI_ASSET_STATE')._document_path
        self.owner = self.ledger + '__owner'
        self.data = {self.ledger: copy.deepcopy(previous)}
        self.nanos = 101
        self.versions = {self.ledger: self.nanos}
        self.calls = []
        self.failure = None
        self.read_only_begins = []

    def _bounds(self, name, retry, timeout, metadata):
        assert retry is None and timeout == 10
        assert metadata == self.client._rpc_metadata
        self.calls.append(name)

    def begin_transaction(self, *, request, retry, timeout, metadata):
        from google.cloud.firestore_v1.types import BeginTransactionResponse
        self._bounds('begin', retry, timeout, metadata)
        assert request['database'] == self.client._database_string
        options = request['options']
        self.read_only_begins.append(bool(options is not None and options._pb.HasField('read_only')))
        return BeginTransactionResponse(transaction=b'synthetic-native-transaction')

    def rollback(self, *, request, retry, timeout, metadata):
        self._bounds('rollback', retry, timeout, metadata)
        assert request['transaction'] == b'synthetic-native-transaction'
        if self.failure == 'rollback':
            raise RuntimeError('SENSITIVE_CLEANUP')

    def batch_get_documents(self, *, request, retry, timeout, metadata):
        from google.cloud.firestore_v1 import _helpers
        from google.cloud.firestore_v1.types import BatchGetDocumentsResponse, Document
        from google.protobuf.timestamp_pb2 import Timestamp
        self._bounds('get', retry, timeout, metadata)
        assert request['transaction'] == b'synthetic-native-transaction'
        path = request['documents'][0]
        timestamp = Timestamp(seconds=1791072000, nanos=self.versions.get(path, self.nanos))
        if path not in self.data:
            return iter([BatchGetDocumentsResponse(missing=path, read_time=timestamp)])
        return iter([BatchGetDocumentsResponse(found=Document(name=path,
            fields=_helpers.encode_dict(self.data[path]), create_time=timestamp, update_time=timestamp),
            read_time=timestamp)])

    def commit(self, *, request, retry, timeout, metadata):
        from google.api_core.exceptions import AlreadyExists
        from google.cloud.firestore_v1 import _helpers
        from google.cloud.firestore_v1.types import CommitResponse, WriteResult
        from google.protobuf.timestamp_pb2 import Timestamp
        self._bounds('commit', retry, timeout, metadata)
        candidate = copy.deepcopy(self.data)
        writes = request['writes']
        assert writes
        # Owner create is the sole nontransactional mutation; its exists=False
        # precondition still applies. All ledger/receipt writes have native ID.
        if request['transaction'] is None:
            assert len(writes) == 1 and writes[0].update.name == self.owner
            assert writes[0].current_document.exists is False
        else:
            assert request['transaction'] == b'synthetic-native-transaction'
        for write in writes:
            if write.delete:
                candidate.pop(write.delete, None)
                continue
            path = write.update.name
            value = _helpers.decode_dict(write.update.fields, self.client)
            if write._pb.HasField('current_document') and not write.current_document.exists and path in candidate:
                raise AlreadyExists('synthetic create conflict')
            if write.update_mask.field_paths:
                assert path in candidate
                candidate[path].update(value)
            else:
                candidate[path] = value
        before = self.data
        self.data = candidate
        for write in writes:
            path = write.delete or write.update.name
            if before.get(path) != candidate.get(path):
                self.nanos += 1
                self.versions[path] = self.nanos
        timestamp = Timestamp(seconds=1791072000, nanos=self.nanos)
        if self.failure == 'commit':
            raise RuntimeError('SENSITIVE_COMMIT_LOST')
        return CommitResponse(write_results=[WriteResult(update_time=timestamp) for _ in writes],
                              commit_time=timestamp)


def native_session_fixture(previous, *, receipt_enabled=False, normalize_fn=lambda x: x):
    import google.auth
    import socket
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.firestore_v1.client import Client
    from google.cloud.firestore_v1.services.firestore.transports.grpc import FirestoreGrpcTransport
    def forbidden(*args, **kwargs):
        raise AssertionError('native fixture must not use credentials or network')
    with patch.object(google.auth, 'default', forbidden), patch.object(socket.socket, 'connect', forbidden), \
         patch.object(FirestoreGrpcTransport, 'create_channel', forbidden):
        client = Client(project='synthetic-project', database='synthetic-database', credentials=AnonymousCredentials())
        api = StubNativeAPI(client, previous)
        client._firestore_api_internal = api  # No GAPIC client/channel construction.
        store = Mock(client=client)
        with patch('live_services._get_document_store', return_value=store):
            access = bind_trade_state_access(normalize_fn=normalize_fn, default_state_factory=dict,
                                             receipt_enabled=receipt_enabled)
    return access, api


def test_installed_native_sdk_reference_views_and_transaction_rpc_bounds():
    import importlib.metadata
    assert importlib.metadata.version('google-cloud-firestore') == '2.28.0'
    access, api = native_session_fixture({'value': 1})
    assert access.claim('owner-one') is True
    assert access.load() == {'value': 1}
    assert access.save({'value': 2}) is True
    assert access.release('owner-one') is True
    assert api.data[api.ledger] == {'value': 2}
    assert api.owner not in api.data
    assert api.calls.count('commit') == 3  # claim, fenced state save, fenced release


def test_installed_sdk_nanoseconds_are_not_collapsed_for_source_cas():
    access, api = native_session_fixture({'value': 1})
    access.claim('owner-one')
    access.load()
    # Real SDK snapshot update_time: nanos differ but datetime equality does not.
    old = access._source_version
    api.versions[api.ledger] += 1
    from google.api_core.datetime_helpers import DatetimeWithNanoseconds
    advanced = DatetimeWithNanoseconds.from_rfc3339('2026-10-04T00:00:00.000000102Z')
    assert old == advanced and _version(old) != _version(advanced)
    commits = api.calls.count('commit')
    with pytest.raises(RuntimeError, match='state_session_source_changed'):
        access.save({'value': 2})
    assert api.calls.count('commit') == commits
    assert not access.active


@pytest.mark.parametrize('failure', ['commit', 'rollback'])
def test_installed_sdk_uncertainty_is_sanitized_with_no_rpc_retry(failure):
    access, api = native_session_fixture({'value': 1})
    access.claim('owner-one')
    access.load()
    api.failure = failure
    start = len(api.calls)
    with pytest.raises(RuntimeError, match='state_session_') as caught:
        access.save({'value': 2})
    assert 'SENSITIVE' not in str(caught.value)
    assert api.calls[start:].count('commit') == 1
    assert api.calls[start:].count('rollback') == 1
    assert not access.active


def test_installed_sdk_receipt_create_patch_and_bounded_read_only_readback():
    from test_interval_source_receipt_candidate import material
    previous, following, interval = material()
    access, api = native_session_fixture(previous, receipt_enabled=True)
    access.claim('owner-one')
    access.load(normalize=False)
    assert access.save(following, interval=interval) is True
    assert api.data[api.ledger] == following
    receipts = [value for path, value in api.data.items() if '__interval_receipt_' in path]
    assert len(receipts) == 1 and receipts[0]['interval'] == interval
    assert api.read_only_begins == [True, False, True, True]
    following['last_reset_date'] = '2026-10-05'
    assert access.save(following) is True
    assert api.data[api.ledger]['earn_accrual_checkpoint']['observed_at'] == interval['end_at']


@pytest.mark.parametrize('key', ['earn_accrual_checkpoint', 'external_cash_flow_cursor'])
@pytest.mark.parametrize('change', ['remove', 'replace'])
def test_receipt_mode_ordinary_saves_cannot_regress_forward_fields(key, change):
    from test_interval_source_receipt_candidate import material
    previous, _, _ = material()
    access, client, _ = session_fixture(previous, receipt_enabled=True)
    access.claim('owner-one')
    state = access.load(normalize=False)
    if change == 'remove':
        state.pop(key)
    else:
        state[key]['observed_at'] = '2026-10-03T23:58:00+00:00'
    calls = len(client.events)
    with pytest.raises(RuntimeError, match='state_session_checkpoint_requires_receipt'):
        access.save(state)
    assert len(client.events) == calls
    assert client.data[client.ledger] == previous
    assert not access.active


def test_receipt_mode_blocks_checkpointless_source_before_any_legacy_write():
    access, client, _ = session_fixture({'legacy': True}, receipt_enabled=True)
    access.claim('owner-one')
    with pytest.raises(RuntimeError, match='state_session_load_uncertain'):
        access.load()
    assert client.data[client.ledger] == {'legacy': True}
    assert not access.active
    assert not any(e[0] == 'commit' for e in client.events)


def test_default_off_can_initialize_missing_ledger_with_fenced_create_or_set():
    access, client, _ = session_fixture()
    access.claim('owner-one')
    assert access.load() == {}
    assert access.save({'new': True}) is True
    assert client.data[client.ledger] == {'new': True}
    assert not any('__interval_receipt_' in key for key in client.data)


def test_forward_uses_raw_source_when_normalizer_adds_defaults_or_filters_future_fields():
    from test_interval_source_receipt_candidate import material
    previous, following, interval = material()
    def normalize(raw):
        result = copy.deepcopy(raw)
        result.pop('unknown_future_field', None)
        result.setdefault('normalized_default', 0)
        return result
    access, client, _ = session_fixture(previous, receipt_enabled=True, normalize_fn=normalize)
    access.claim('owner-one')
    access.load(normalize=False)
    assert access.save(normalize(following), interval=interval) is True
    assert client.data[client.ledger] == following
    assert 'normalized_default' not in client.data[client.ledger]
    working = normalize(following)
    working['last_reset_date'] = '2026-10-05'
    assert access.save(working) is True
    assert client.data[client.ledger]['unknown_future_field'] == {'keep': True}


@pytest.mark.parametrize('race', ['conflicting_receipt', 'owner_after_commit', 'poststate_after_commit'])
def test_forward_conflict_or_uncertain_readback_stops_session_and_retains_atomic_result(race):
    from test_interval_source_receipt_candidate import material
    from application.interval_source_receipt_candidate import build_interval_receipt_plan
    previous, following, interval = material()
    access, client, _ = session_fixture(previous, receipt_enabled=True)
    access.claim('owner-one')
    access.load(normalize=False)
    if race == 'conflicting_receipt':
        plan = build_interval_receipt_plan(ledger_path=client.ledger, source_ledger_update_time=access._source_version,
                                           previous_state=previous, next_state=following, interval=interval)
        client.change(plan.receipt_path, {'conflicting': 'SENSITIVE_FINANCIAL_ROW'})
    elif race == 'owner_after_commit':
        client.after_commit = lambda c: c.change(c.owner, {'owner_id': 'replacement'})
    else:
        client.after_commit = lambda c: c.change(c.ledger, {**following, 'future_race': True})
    with pytest.raises(RuntimeError, match='state_session_') as caught:
        access.save(following, interval=interval)
    assert 'SENSITIVE' not in str(caught.value)
    assert not access.active
    receipts = [v for k, v in client.data.items() if '__interval_receipt_' in k]
    assert len(receipts) == 1
    if race == 'conflicting_receipt':
        assert client.data[client.ledger] == previous
    else:
        assert client.data[client.ledger]['earn_accrual_checkpoint'] == following['earn_accrual_checkpoint']
        assert receipts[0]['interval'] == interval
    calls = len(client.events)
    with pytest.raises(RuntimeError):
        access.save(following, interval=interval)
    assert len(client.events) == calls  # No automatic retry after ambiguous outcome.


def test_wrong_backend_reference_is_rejected_before_reads():
    first = SessionClient({'value': 1})
    other = SessionClient({'value': 1})
    with patch.object(first, 'collection', return_value=SessionCollection(other, 'strategy')):
        with patch('live_services._get_document_store', return_value=Mock(client=first)):
            with pytest.raises(ValueError, match='interval_receipt_backend_mismatch'):
                bind_trade_state_access(normalize_fn=lambda x: x, default_state_factory=dict)
    assert not first.events and not other.events


def test_wrong_transaction_client_is_rejected_before_native_begin_or_document_reads():
    access, client, _ = session_fixture({'value': 1})
    access.claim('owner-one')
    other = SessionClient({'value': 1})
    with patch.object(client, 'transaction', return_value=SessionTransaction(other)):
        with pytest.raises(RuntimeError, match='state_session_load_uncertain'):
            access.load()
    assert not any(event[0] in {'begin', 'get'} for event in client.events)
    assert not access.active


def test_port_default_repr_omits_detached_financial_source_and_owner():
    access, _, _ = session_fixture({'SENSITIVE_BALANCE': 'private'})
    access.claim('SENSITIVE_OWNER')
    access.load()
    assert 'SENSITIVE' not in repr(access)


def test_release_checks_loaded_source_and_never_deletes_owner_on_source_race():
    access, client, _ = session_fixture({'value': 1})
    access.claim('owner-one')
    access.load()
    client.change(client.ledger, {'value': 2})
    with pytest.raises(RuntimeError, match='state_session_release_uncertain'):
        access.release('owner-one')
    assert client.owner in client.data
    assert not access.active


def test_receipt_mode_is_read_only_after_construction():
    access, _, _ = session_fixture({})
    assert access.receipt_enabled is False
    with pytest.raises(AttributeError):
        access.receipt_enabled = True


@pytest.mark.parametrize('phase', ['claim', 'load'])
def test_reference_view_revalidates_actual_native_delegate_identity(phase):
    access, client, _ = session_fixture({'value': 1})
    other = SessionClient({'value': 1})
    if phase == 'load':
        access.claim('owner-one')
    access._owner._reference._client = other
    with pytest.raises(RuntimeError, match='state_session_'):
        if phase == 'claim':
            access.claim('owner-one')
        else:
            access.load()
    assert not other.events
    assert not any(event[0] == 'get' for event in client.events)
    assert not access.active


def test_native_documented_noop_write_keeps_exact_version_without_blocking_next_save():
    access, api = native_session_fixture({'value': 1})
    access.claim('owner-one')
    state = access.load()
    previous = _version(access._source_version)
    assert access.save(state) is True
    assert _version(access._source_version) == previous
    assert access.active
    assert access.save({'value': 2}) is True
    assert _version(access._source_version) > previous
    assert api.data[api.ledger] == {'value': 2}


def test_changed_poststate_with_unadvanced_native_version_is_uncertain():
    access, api = native_session_fixture({'value': 1})
    access.claim('owner-one')
    access.load()
    previous = api.versions[api.ledger]
    commit = api.commit
    def unchanged_version_commit(**kwargs):
        result = commit(**kwargs)
        api.versions[api.ledger] = previous
        return result
    with patch.object(api, 'commit', side_effect=unchanged_version_commit):
        with pytest.raises(RuntimeError, match='state_session_'):
            access.save({'value': 2})
    assert api.data[api.ledger] == {'value': 2}  # No invented rollback after committed body.
    assert not access.active


def test_native_claim_lost_response_keeps_lock_but_never_grants_local_authority():
    access, api = native_session_fixture({'value': 1})
    api.failure = 'commit'
    with pytest.raises(RuntimeError, match='state_session_claim_uncertain'):
        access.claim('owner-one')
    assert api.data[api.owner] == {'owner_id': 'owner-one'}
    assert not access.active
    calls = len(api.calls)
    with pytest.raises(RuntimeError, match='state_session_claim_unavailable'):
        access.claim('owner-one')
    assert len(api.calls) == calls


def test_native_sdk_actual_normalizer_and_business_forward_preserve_raw_audit_fields():
    import main
    from test_forward_earn_accounting import materials, NOW
    from test_portfolio_service import consume_forward_bound
    from runtime_support import ExecutionRuntime, acquire_runtime_state_owner, build_execution_report, runtime_set_trade_state
    previous, observation, cash = materials()
    previous['daily_external_principal_usdt'] = 0.0
    previous['future_audit_field'] = {'keep': True}
    access, api = native_session_fixture(previous, receipt_enabled=True, normalize_fn=main.normalize_trade_state)
    runtime = ExecutionRuntime(now_utc=NOW, client=Mock(), bound_state_access=access,
        state_loader=access.load, state_writer=access.save, state_owner_claim=access.claim,
        state_owner_release=access.release)
    assert acquire_runtime_state_owner(runtime)
    runtime.trade_state = main.normalize_trade_state(access.load(normalize=False))
    runtime.earn_accrual_observation = observation
    report = build_execution_report(runtime)
    runtime_set_trade_state(runtime, report, runtime.trade_state, reason='trend_pool_metadata_refresh')
    assert consume_forward_bound(runtime, observation, cash, report)
    assert api.data[api.ledger]['future_audit_field'] == {'keep': True}
    assert api.data[api.ledger]['earn_accrual_checkpoint'] == observation
    receipts = [v for k, v in api.data.items() if '__interval_receipt_' in k]
    assert len(receipts) == 1 and receipts[0]['interval'] == report['external_cash_flow_interval']


def test_overlapping_public_admission_cannot_refresh_source_under_another_operation():
    import threading
    access, client, _ = session_fixture({'value': 1})
    access.claim('owner-one')
    access.load()
    admitted = threading.Event()
    resume = threading.Event()
    errors, successes = [], []
    original_invalidate = access.invalidate
    original_normalize = access._normalize
    def pause_first_once():
        if threading.current_thread().name == 'source-first' and not admitted.is_set():
            admitted.set()
            assert resume.wait(5), 'deterministic overlap barrier timed out'
    def gated_invalidate():
        # On the original code this pauses exactly after ensure_active but
        # before its check-then-invalidate admission. On serialized code the
        # normalizer below pauses the first operation after atomic admission.
        pause_first_once()
        original_invalidate()
    def gated_normalize(data):
        pause_first_once()
        return original_normalize(data)
    access.invalidate = gated_invalidate
    access._normalize = gated_normalize
    def save(value):
        try:
            successes.append(access.save({'value': value}))
        except RuntimeError as error:
            errors.append(str(error))
    first = threading.Thread(target=save, args=(2,), name='source-first')
    second = threading.Thread(target=save, args=(3,), name='source-second')
    first.start()
    try:
        assert admitted.wait(5), 'first operation did not reach admission barrier'
        second.start()
        second.join(5)
        assert not second.is_alive(), 'busy admission must fail without waiting'
    finally:
        resume.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert successes == []
    assert len(errors) == 2
    assert any('state_session_busy' in error for error in errors)
    assert client.data[client.ledger] == {'value': 1}
    assert access._source == {'value': 1}
    assert not access.active


@pytest.mark.parametrize('operation', ['claim', 'load', 'save', 'receipt', 'release'])
def test_mid_rpc_invalidation_is_irreversible_and_cannot_publish_source_or_success(operation):
    from test_interval_source_receipt_candidate import material
    previous, following, interval = material()
    if operation == 'receipt':
        access, api = native_session_fixture(previous, receipt_enabled=True)
    else:
        access, api = native_session_fixture({'value': 1})
    if operation != 'claim':
        access.claim('owner-one')
    if operation in {'save', 'receipt', 'release'}:
        access.load(normalize=False)
    source = copy.deepcopy(access._source)
    source_version = access._source_version
    method = 'rollback' if operation == 'load' else 'commit'
    original = getattr(api, method)
    def invalidate_during_rpc(**kwargs):
        result = original(**kwargs)
        access.invalidate()
        return result
    with patch.object(api, method, side_effect=invalidate_during_rpc):
        with pytest.raises(RuntimeError, match='state_session_'):
            if operation == 'claim':
                access.claim('owner-one')
            elif operation == 'load':
                access.load()
            elif operation == 'receipt':
                access.save(following, interval=interval)
            elif operation == 'release':
                access.release('owner-one')
            else:
                access.save({'value': 2})
    assert not access.active
    assert access._source == source
    assert access._source_version is source_version
    calls = len(api.calls)
    with pytest.raises(RuntimeError):
        access.save({'value': 3})
    assert len(api.calls) == calls


@pytest.mark.parametrize('nested', ['claim', 'load', 'save', 'release'])
def test_caught_reentrant_public_call_poisons_session_before_any_native_write(nested):
    access, client, _ = session_fixture({'value': 1})
    access.claim('owner-one')
    access.load()
    errors = []
    def reentrant_normalize(data):
        try:
            if nested == 'claim':
                access.claim('owner-two')
            elif nested == 'load':
                access.load()
            elif nested == 'save':
                access.save({'value': 3})
            else:
                access.release('owner-one')
        except RuntimeError as error:
            errors.append(str(error))
        return data  # Catching busy does not reauthorize the outer operation.
    access._normalize = reentrant_normalize
    calls = len(client.events)
    with pytest.raises(RuntimeError, match='state_session_'):
        access.save({'value': 2})
    assert errors == ['state_session_busy']
    assert len(client.events) == calls
    assert client.data[client.ledger] == {'value': 1}
    assert not access.active
