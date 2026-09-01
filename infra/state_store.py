"""Runtime trade state persistence helpers."""

from __future__ import annotations

from live_services import (
    load_trade_state as live_load_trade_state,
    save_trade_state as live_save_trade_state,
)

DEFAULT_STATE_COLLECTION = "strategy"
DEFAULT_STATE_DOCUMENT = "MULTI_ASSET_STATE"


def load_runtime_trade_state(
    *,
    normalize_fn,
    default_state_factory,
    normalize=True,
    loader_fn=live_load_trade_state,
    collection=DEFAULT_STATE_COLLECTION,
    document=DEFAULT_STATE_DOCUMENT,
):
    return loader_fn(
        normalize_fn=normalize_fn,
        default_state_factory=default_state_factory,
        normalize=normalize,
        collection=collection,
        document=document,
    )


def save_runtime_trade_state(
    data,
    *,
    normalize_fn,
    saver_fn=live_save_trade_state,
    collection=DEFAULT_STATE_COLLECTION,
    document=DEFAULT_STATE_DOCUMENT,
):
    return saver_fn(
        data,
        normalize_fn=normalize_fn,
        collection=collection,
        document=document,
    )
