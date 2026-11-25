"""Resolve the Strategy Switch Console origin from protected environment configuration."""
from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit


def resolve_console_origin(env: Mapping[str, str] | None = None) -> str:
    """Return validated https://<netloc> from STRATEGY_SWITCH_CONSOLE_URL or EXECUTION_EVIDENCE_SYNC_URL."""
    source = os.environ if env is None else env
    raw = (
        str(source.get("STRATEGY_SWITCH_CONSOLE_URL") or "").strip()
        or str(source.get("EXECUTION_EVIDENCE_SYNC_URL") or "").strip()
    )
    if not raw:
        raise ValueError("console_origin_missing")
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("console_origin_invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("console_origin_invalid")
    return f"https://{parsed.netloc}"
