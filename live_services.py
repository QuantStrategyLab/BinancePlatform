import requests


_FIRESTORE_CLIENT = None


def get_firestore_client():
    global _FIRESTORE_CLIENT
    if _FIRESTORE_CLIENT is None:
        from google.cloud import firestore

        _FIRESTORE_CLIENT = firestore.Client()
    return _FIRESTORE_CLIENT


def get_state_doc_ref(*, collection="strategy", document="MULTI_ASSET_STATE"):
    return get_firestore_client().collection(collection).document(document)


def load_trade_state(*, normalize_fn, default_state_factory, normalize=True, collection="strategy", document="MULTI_ASSET_STATE"):
    try:
        doc = get_state_doc_ref(collection=collection, document=document).get()
        if doc.exists:
            payload = doc.to_dict()
            return normalize_fn(payload) if normalize else payload
        return default_state_factory() if normalize else {}
    except Exception as exc:
        print(f"Firestore get state failed: {exc}")
        return None


def save_trade_state(data, *, normalize_fn, collection="strategy", document="MULTI_ASSET_STATE"):
    try:
        persisted_state = normalize_fn(data)
        get_state_doc_ref(collection=collection, document=document).set(persisted_state)
    except Exception as exc:
        print(f"Firestore write failed: {exc}")


def send_tg_msg(token, chat_id, text):
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, data={"chat_id": chat_id, "text": f"🤖 Crypto quant assistant:\n{text}"}, timeout=10)
    except Exception:
        print("Telegram send failed")
