import copy
import hashlib
from contextlib import contextmanager
from threading import Lock
from types import SimpleNamespace

from application.interval_source_receipt_candidate import (
    _PATCH_FIELDS, _digest, _json, _same_backend, _version,
    build_interval_receipt_plan, persist_interval_source_receipt,
)

import requests

from notify_i18n_support import build_telegram_message, translate as t


def _get_document_store():
    """Lazy-init the cloud-agnostic document store."""
    from quant_platform_kit.cloud import get_document_store

    return get_document_store()


def get_firestore_client():
    """Return the underlying Firestore client for direct collection/document access.

    NOTE: this relies on the GCP provider's ``.client`` property and will
    raise AttributeError when the active provider is not GCP.
    """
    return _get_document_store().client


def get_state_doc_ref(*, collection="strategy", document="MULTI_ASSET_STATE"):
    """Return a Firestore document reference for the given collection/document."""
    return get_firestore_client().collection(collection).document(document)


def load_trade_state(*, normalize_fn, default_state_factory, normalize=True, collection="strategy", document="MULTI_ASSET_STATE", store=None):
    try:
        payload = (store if store is not None else _get_document_store()).get(collection=collection, document_id=document)
        if payload is not None:
            return normalize_fn(payload) if normalize else payload
        return default_state_factory() if normalize else {}
    except Exception:
        print(t("firestore_get_state_failed", error="state_load_failed"))
        return None


class StateSessionBlocked(RuntimeError):
    """Fixed local admission/CAS failure; the session may not continue."""


class StateSessionUncertain(RuntimeError):
    """A native outcome was not established; never retry or authorize a broker."""


class _BoundReference:
    """2.28-native reference view, adding bounds to the existing helper's gets.

    Native create/update use _document_path by duck typing. Reads delegate to the
    original native DocumentReference with the original native transaction.
    No client is constructed here and no transaction.get(view) is used.
    """

    def __init__(self, reference):
        self._reference = reference
        self._client = reference._client
        self.path = reference.path
        self._document_path = reference._document_path

    @property
    def parent(self):
        return SimpleNamespace(document=lambda name: _BoundReference(self._reference.parent.document(name)))

    def get(self, *, transaction, retry=None):
        if retry is not None:
            raise StateSessionBlocked("state_session_retry_forbidden")
        _same_backend(bound_client=self._client, database=self._client._database_string,
                      refs=(self._reference,), transaction=transaction)
        if self._reference.path != self.path or self._reference._document_path != self._document_path:
            raise StateSessionBlocked("state_session_reference_changed")
        return self._reference.get(transaction=transaction, retry=None, timeout=10)


class BoundTradeStateAccess:
    """One owner and detached full-version source for all candidate native writes.

    Iterable for the existing four-callable binding contract. Native saves never
    use DocumentStore.set, never refresh a stale source, and never retry a commit.
    Any failed operation poisons this session, including owner/load/readback.
    """

    def __init__(self, store, *, normalize_fn, default_state_factory, collection,
                 document, receipt_enabled=False):
        if type(receipt_enabled) is not bool:
            raise ValueError("state_session_receipt_flag_invalid")
        self._store = store
        self._client = store.client
        self._database = self._client._database_string
        self._ledger = _BoundReference(self._client.collection(collection).document(document))
        self._owner = _BoundReference(self._client.collection(collection).document(document + "__owner"))
        _same_backend(bound_client=self._client, database=self._database, refs=(self._ledger, self._owner))
        self._normalize = normalize_fn
        self._default = default_state_factory
        self._receipt_enabled = receipt_enabled
        self._owner_id = ""
        self._source = None
        self._source_version = None
        self._loaded = False
        self._invalid = False  # Terminal latch: never reset after construction.
        self._session_lock = Lock()
        self._busy = False

    def __iter__(self):
        return iter((self.load, self.save, self.claim, self.release))

    @property
    def receipt_enabled(self):
        return self._receipt_enabled

    @property
    def active(self):
        with self._session_lock:
            return bool(self._owner_id) and not self._invalid and not self._busy

    def ensure_active(self):
        if not self.active:
            raise StateSessionBlocked("state_session_inactive")

    def invalidate(self):
        # This lock is never held across an RPC, so external revocation can
        # latch immediately while an operation is in flight. No operation rearms.
        with self._session_lock:
            self._invalid = True

    def _check_operation_locked(self):
        if self._invalid or not self._busy:
            raise StateSessionUncertain("state_session_invalidated")

    def _check_operation(self):
        with self._session_lock:
            self._check_operation_locked()

    @contextmanager
    def _operation(self, *, claiming=False, closing=False):
        # Atomic admission; overlap/reentrancy fails immediately and poisons the
        # session rather than letting either caller continue using changing CAS.
        with self._session_lock:
            if self._busy:
                self._invalid = True
                raise StateSessionBlocked("state_session_busy")
            if self._invalid or (claiming and self._owner_id):
                raise StateSessionBlocked("state_session_claim_unavailable" if claiming else "state_session_inactive")
            if not claiming and not self._owner_id:
                raise StateSessionBlocked("state_session_inactive")
            self._busy = True
        try:
            yield
            # Completion and clearing busy are atomic with external invalidate.
            with self._session_lock:
                self._check_operation_locked()
                if closing:
                    self._invalid = True
                self._busy = False
        except BaseException:
            self.invalidate()
            with self._session_lock:
                self._busy = False
            raise

    def _publish_source(self, raw, version, *, loaded=False):
        with self._session_lock:
            self._check_operation_locked()
            self._source, self._source_version = raw, version
            if loaded:
                self._loaded = True

    @staticmethod
    def _valid_owner(owner_id):
        if not isinstance(owner_id, str) or not owner_id or owner_id != owner_id.strip():
            raise ValueError("state_owner_required")

    def _run(self, callback, *, read_only=False):
        # Firestore 2.28 Transaction._begin/_commit/_rollback omit RPC controls;
        # inherited WriteBatch.commit sends transaction=None. Use exact GAPIC
        # calls with the original native transaction ID and staged write protos.
        self._check_operation()
        transaction = self._client.transaction(max_attempts=1, read_only=read_only)
        _same_backend(bound_client=self._client, database=self._database,
                      refs=(self._ledger, self._owner), transaction=transaction)
        api = self._client._firestore_api
        metadata = self._client._rpc_metadata
        rollback_attempted = False
        try:
            response = api.begin_transaction(
                request={"database": self._database, "options": transaction._options_protobuf(None)},
                retry=None, timeout=10, metadata=metadata,
            )
            if not response.transaction:
                raise StateSessionUncertain("state_session_begin_uncertain")
            transaction._id = response.transaction
            self._check_operation()
            result = callback(transaction)
            self._check_operation()
            if transaction._write_pbs:
                if read_only:
                    raise StateSessionBlocked("state_session_read_only_write")
                api.commit(request={"database": self._database, "writes": transaction._write_pbs,
                                    "transaction": transaction.id},
                           retry=None, timeout=10, metadata=metadata)
                transaction._clean_up()
            else:
                rollback_attempted = True
                api.rollback(request={"database": self._database, "transaction": transaction.id},
                             retry=None, timeout=10, metadata=metadata)
                transaction._clean_up()
            self._check_operation()
            return result
        except BaseException:
            if transaction.id and not rollback_attempted:
                try:
                    api.rollback(request={"database": self._database, "transaction": transaction.id},
                                 retry=None, timeout=10, metadata=metadata)
                except Exception:
                    pass  # Rollback cannot establish the outcome of a failed commit.
            raise
        finally:
            transaction._clean_up()

    def _read_owned(self, transaction):
        owner = self._owner.get(transaction=transaction, retry=None)
        ledger = self._ledger.get(transaction=transaction, retry=None)
        if not owner.exists or owner.to_dict() != {"owner_id": self._owner_id}:
            raise StateSessionBlocked("state_session_owner_changed")
        raw = ledger.to_dict() if ledger.exists else None
        if raw is not None and not isinstance(raw, dict):
            raise StateSessionBlocked("state_session_source_invalid")
        version = ledger.update_time if ledger.exists else None
        if ledger.exists:
            _version(version)  # Validate full SDK seconds+nanos without deepcopy.
            _digest(raw)
        return copy.deepcopy(raw), version

    def _matches_source(self, raw, version):
        return ((None if version is None else _version(version))
                == (None if self._source_version is None else _version(self._source_version))
                and _json(raw) == _json(self._source))

    def claim(self, owner_id):
        from google.api_core.exceptions import AlreadyExists
        self._valid_owner(owner_id)
        with self._operation(claiming=True):
            try:
                _same_backend(bound_client=self._client, database=self._database,
                              refs=(self._ledger._reference, self._owner._reference))
                self._owner._reference.create({"owner_id": owner_id}, retry=None, timeout=10)
            except AlreadyExists:
                return False
            except Exception:
                raise StateSessionUncertain("state_session_claim_uncertain") from None
            with self._session_lock:
                self._check_operation_locked()
                self._owner_id = owner_id
            return True

    def load(self, normalize=True):
        with self._operation():
            if self._loaded:
                raise StateSessionBlocked("state_session_reload_forbidden")
            try:
                raw, version = self._run(self._read_owned, read_only=True)
                if self.receipt_enabled and (raw is None or not all(k in raw for k in ("earn_accrual_checkpoint", "external_cash_flow_cursor"))):
                    raise StateSessionBlocked("state_session_checkpoint_required")
                output = self._normalize(copy.deepcopy(raw)) if raw is not None and normalize else (
                    self._default() if normalize else copy.deepcopy(raw or {}))
                self._publish_source(raw, version, loaded=True)
                return copy.deepcopy(output)
            except Exception:
                raise StateSessionUncertain("state_session_load_uncertain") from None

    def _readback(self, intended):
        raw, version = self._run(self._read_owned, read_only=True)
        if raw is None or _json(raw) != _json(intended):
            raise StateSessionUncertain("state_session_readback_mismatch")
        if self._source_version is not None:
            actual, previous = _version(version), _version(self._source_version)
            # 2.28 WriteResult: unchanged writes may retain previous update_time.
            unchanged = _json(intended) == _json(self._source)
            if actual < previous or (actual == previous and not unchanged):
                raise StateSessionUncertain("state_session_readback_version_invalid")
        return raw, version

    def save(self, data, *, interval=None):
        with self._operation():
            if not self._loaded:
                raise StateSessionBlocked("state_session_source_required")
            try:
                normalized = copy.deepcopy(self._normalize(copy.deepcopy(data)))
                if not isinstance(normalized, dict):
                    raise StateSessionBlocked("state_session_state_invalid")
                source = self._source or {}
                if interval is not None:
                    if not self.receipt_enabled or self._source is None:
                        raise StateSessionBlocked("state_session_receipt_unavailable")
                    # Normalizer defaults/filtered unknown fields are not an interval
                    # mutation. Compare working semantics, then patch detached RAW.
                    before = self._normalize(copy.deepcopy(source))
                    if _json({k: v for k, v in before.items() if k not in _PATCH_FIELDS}) != _json(
                            {k: v for k, v in normalized.items() if k not in _PATCH_FIELDS}):
                        raise StateSessionBlocked("state_session_forward_fields_changed")
                    intended = {**copy.deepcopy(source), **{k: normalized[k] for k in _PATCH_FIELDS}}
                    plan = build_interval_receipt_plan(
                        ledger_path=self._ledger.path, source_ledger_update_time=self._source_version,
                        previous_state=source, next_state=intended, interval=interval,
                    )
                    phase = 0

                    def receipt_runner(callback):
                        nonlocal phase
                        phase += 1
                        if phase > 2:
                            raise StateSessionBlocked("state_session_receipt_runner_reused")
                        return self._run(callback, read_only=phase == 2)

                    persist_interval_source_receipt(
                        bound_client=self._client, transaction_runner=receipt_runner,
                        ledger_ref=self._ledger, owner_ref=self._owner, owner_id=self._owner_id,
                        expected_ledger_update_time=self._source_version, plan=plan,
                    )
                else:
                    if self.receipt_enabled and any(_json(source.get(k)) != _json(normalized.get(k))
                                                   for k in ("earn_accrual_checkpoint", "external_cash_flow_cursor")):
                        raise StateSessionBlocked("state_session_checkpoint_requires_receipt")
                    # Preserve source-only future fields under the same exact CAS.
                    intended = {**copy.deepcopy(source), **normalized}

                    def stage(transaction):
                        raw, version = self._read_owned(transaction)
                        if not self._matches_source(raw, version):
                            raise StateSessionBlocked("state_session_source_changed")
                        transaction.set(self._ledger, intended)

                    self._run(stage)
                self._publish_source(*self._readback(intended))
                return True
            except StateSessionBlocked:
                raise
            except Exception:
                raise StateSessionUncertain("state_session_write_uncertain") from None

    def release(self, owner_id):
        self._valid_owner(owner_id)
        with self._operation(closing=True):
            if owner_id != self._owner_id:
                raise StateSessionBlocked("state_session_owner_changed")
            try:
                def stage(transaction):
                    if self._loaded:
                        raw, version = self._read_owned(transaction)
                        if not self._matches_source(raw, version):
                            raise StateSessionBlocked("state_session_source_changed")
                    else:
                        owner = self._owner.get(transaction=transaction, retry=None)
                        if not owner.exists or owner.to_dict() != {"owner_id": owner_id}:
                            raise StateSessionBlocked("state_session_owner_changed")
                    transaction.delete(self._owner)
                self._run(stage)
                return True
            except Exception:
                raise StateSessionUncertain("state_session_release_uncertain") from None


def save_trade_state(data, *, normalize_fn, collection="strategy", document="MULTI_ASSET_STATE",
                     store=None, bound_access=None):
    """Public native API: explicit matching bound session required, no blind set.

    store alone is intentionally insufficient. Custom memory savers use the infra
    injection seam rather than masquerading as a native ownership capability.
    """
    if (not isinstance(bound_access, BoundTradeStateAccess)
            or bound_access._ledger.path != collection + "/" + document
            or (store is not None and store is not bound_access._store)
            or normalize_fn is not bound_access._normalize):
        print(t("firestore_write_failed", error="state_persistence_failed"))
        return False
    return bound_access.save(data)


def bind_trade_state_access(*, normalize_fn, default_state_factory,
                            collection="strategy", document="MULTI_ASSET_STATE", receipt_enabled=False):
    """Bind one owned/versioned session; receipt retention is explicitly opt-in."""
    return BoundTradeStateAccess(_get_document_store(), normalize_fn=normalize_fn,
                                default_state_factory=default_state_factory, collection=collection,
                                document=document, receipt_enabled=receipt_enabled)


def send_tg_msg(token, chat_id, text):
    message = build_telegram_message(text)
    receipt = {
        "sink": "telegram",
        "delivery_status": "failed",
        "transport_acknowledged": False,
        "compact_text_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "compact_text_length": len(message),
    }
    if not token or not chat_id:
        return {**receipt, "error_type": "missing_target"}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = requests.post(
            url,
            data={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        if int(getattr(response, "status_code", 500)) >= 400:
            return {**receipt, "error_type": "http_error"}
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return {**receipt, "error_type": "telegram_rejected"}
        return {
            **receipt,
            "delivery_status": "sent",
            "transport_acknowledged": True,
        }
    except Exception as exc:
        print(t("telegram_send_failed"))
        return {**receipt, "error_type": type(exc).__name__}
