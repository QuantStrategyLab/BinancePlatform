"""Resolve the Strategy Switch Console origin from protected environment configuration."""
from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit


def resolve_console_origin(env: Mapping[str, str] | None = None) -> str:
    """Return validated https://<netloc> from STRATEGY_SWITCH_CONSOLE_URL or EXECUTION_EVIDENCE_SYNC_URL."""
    source = os.environ if env is None else env
    raw = str(
        source.get("STRATEGY_SWITCH_CONSOLE_URL")
        or source.get("EXECUTION_EVIDENCE_SYNC_URL")
        or ""
    ).strip()
    if not raw:
        raise ValueError("console_origin_missing")
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("console_origin_invalid")
    return f"https://{parsed.netloc}"
