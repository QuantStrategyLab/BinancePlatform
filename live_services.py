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


def load_trade_state(*, normalize_fn, default_state_factory, normalize=True, collection="strategy", document="MULTI_ASSET_STATE"):
    try:
        payload = _get_document_store().get(collection=collection, document_id=document)
        if payload is not None:
            return normalize_fn(payload) if normalize else payload
        return default_state_factory() if normalize else {}
    except Exception:
        print(t("firestore_get_state_failed", error="state_load_failed"))
        return None


def save_trade_state(data, *, normalize_fn, collection="strategy", document="MULTI_ASSET_STATE"):
    try:
        persisted_state = normalize_fn(data)
        _get_document_store().set(collection=collection, document_id=document, data=persisted_state)
        return True
    except Exception:
        print(t("firestore_write_failed", error="state_persistence_failed"))
        return False


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
