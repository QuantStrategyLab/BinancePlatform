import hashlib

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


def save_trade_state(data, *, normalize_fn, collection="strategy", document="MULTI_ASSET_STATE", store=None):
    try:
        persisted_state = normalize_fn(data)
        (store if store is not None else _get_document_store()).set(collection=collection, document_id=document, data=persisted_state)
        return True
    except Exception:
        print(t("firestore_write_failed", error="state_persistence_failed"))
        return False


def bind_trade_state_access(*, normalize_fn, default_state_factory,
                            collection="strategy", document="MULTI_ASSET_STATE"):
    """Bind this runtime's state and persistent owner to the same Firestore backend."""
    store = _get_document_store()

    def load(normalize=True):
        return load_trade_state(normalize_fn=normalize_fn, default_state_factory=default_state_factory,
                                normalize=normalize, collection=collection, document=document, store=store)

    def save(data):
        return save_trade_state(data, normalize_fn=normalize_fn, collection=collection, document=document, store=store)

    def owner_document():
        return store.client.collection(collection).document(document + "__owner")

    def claim(owner_id):
        from google.api_core.exceptions import AlreadyExists
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("state_owner_required")
        try:
            owner_document().create({"owner_id": owner_id}, retry=None)
        except AlreadyExists:
            return False
        return True

    def release(owner_id):
        from google.cloud import firestore
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("state_owner_required")
        ref = owner_document()

        @firestore.transactional
        def delete_owned(transaction):
            snapshot = ref.get(transaction=transaction, retry=None)
            if not snapshot.exists or snapshot.to_dict().get("owner_id") != owner_id:
                return False
            transaction.delete(ref)
            return True

        return delete_owned(store.client.transaction(max_attempts=1))

    return load, save, claim, release


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
