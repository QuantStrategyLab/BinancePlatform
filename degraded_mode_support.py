from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trend_pool_support import (
    build_static_trend_pool_resolution,
    build_trend_pool_resolution,
    get_default_live_pool_candidates,
    get_last_known_good_trend_pool,
    get_trend_pool_contract_settings,
    load_trend_pool_from_file,
    load_trend_pool_from_firestore,
)


def resolve_trend_pool_source(
    state: dict[str, Any] | None = None,
    *,
    now_utc: datetime | None = None,
    default_live_pool_legacy_path: Path,
    default_firestore_collection: str,
    default_firestore_document: str,
    last_good_payload_key: str,
    static_trend_universe: dict[str, dict[str, str]],
    max_age_days_default: int,
    acceptable_modes_default: tuple[str, ...],
    expected_pool_size_default: int,
) -> dict[str, Any]:
    """Resolve the upstream trend pool with explicit degraded-state fallbacks."""
    now_utc = now_utc or datetime.now(timezone.utc)
    settings = get_trend_pool_contract_settings(
        max_age_days_default=max_age_days_default,
        acceptable_modes_default=acceptable_modes_default,
        expected_pool_size_default=expected_pool_size_default,
    )
    messages: list[str] = []

    firestore_result = load_trend_pool_from_firestore(
        now_utc=now_utc,
        settings=settings,
        default_collection=default_firestore_collection,
        default_document=default_firestore_document,
    )
    if firestore_result.get("ok"):
        return build_trend_pool_resolution(
            firestore_result,
            source_kind="fresh_upstream",
            degraded=False,
            now_utc=now_utc,
            messages=[f"Loaded fresh upstream trend pool from {firestore_result['source_label']}"],
        )
    messages.extend(firestore_result.get("errors", []))
    messages.extend(firestore_result.get("warnings", []))

    last_good_result = get_last_known_good_trend_pool(
        state or {},
        now_utc=now_utc,
        settings=settings,
        last_good_payload_key=last_good_payload_key,
    )
    if last_good_result.get("ok"):
        return build_trend_pool_resolution(
            last_good_result,
            source_kind="last_known_good",
            degraded=True,
            now_utc=now_utc,
            messages=messages + ["Using last known good upstream trend pool after fresh upstream validation failed."],
        )
    messages.extend(last_good_result.get("errors", []))

    configured_path = str(os.getenv("TREND_POOL_FILE", "")).strip()
    file_candidates: list[Path] = []
    if configured_path:
        file_candidates.append(Path(configured_path).expanduser())
    file_candidates.extend(get_default_live_pool_candidates(default_live_pool_legacy_path))

    seen_candidates: set[str] = set()
    for pool_path in file_candidates:
        pool_path = Path(pool_path)
        resolved_path = str(pool_path.expanduser())
        if resolved_path in seen_candidates:
            continue
        seen_candidates.add(resolved_path)

        file_result = load_trend_pool_from_file(pool_path, now_utc=now_utc, settings=settings)
        if file_result.get("ok"):
            return build_trend_pool_resolution(
                file_result,
                source_kind="local_file",
                degraded=True,
                now_utc=now_utc,
                messages=messages + [f"Using validated local trend pool fallback from {pool_path}"],
            )
        messages.extend(file_result.get("errors", []))
        messages.extend(file_result.get("warnings", []))

    return build_static_trend_pool_resolution(
        now_utc=now_utc,
        messages=messages + ["Falling back to built-in static trend universe as last resort."],
        static_trend_universe=static_trend_universe,
    )


def load_trend_universe_from_live_pool(
    state: dict[str, Any] | None = None,
    *,
    now_utc: datetime | None = None,
    default_live_pool_legacy_path: Path,
    default_firestore_collection: str,
    default_firestore_document: str,
    last_good_payload_key: str,
    static_trend_universe: dict[str, dict[str, str]],
    max_age_days_default: int,
    acceptable_modes_default: tuple[str, ...],
    expected_pool_size_default: int,
) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    resolution = resolve_trend_pool_source(
        state=state,
        now_utc=now_utc,
        default_live_pool_legacy_path=default_live_pool_legacy_path,
        default_firestore_collection=default_firestore_collection,
        default_firestore_document=default_firestore_document,
        last_good_payload_key=last_good_payload_key,
        static_trend_universe=static_trend_universe,
        max_age_days_default=max_age_days_default,
        acceptable_modes_default=acceptable_modes_default,
        expected_pool_size_default=expected_pool_size_default,
    )
    return resolution["symbol_map"], resolution


def update_trend_pool_state(
    state: dict[str, Any],
    resolution: dict[str, Any],
    *,
    last_good_payload_key: str,
) -> None:
    state["trend_pool_source"] = str(resolution.get("source_kind", ""))
    state["trend_pool_source_detail"] = str(resolution.get("source_label", ""))
    state["trend_pool_is_fresh"] = bool(resolution.get("is_fresh", False))
    state["trend_pool_degraded"] = bool(resolution.get("degraded", False))
    state["trend_pool_as_of_date"] = str(resolution.get("as_of_date", ""))
    state["trend_pool_version"] = str(resolution.get("version", ""))
    state["trend_pool_mode"] = str(resolution.get("mode", ""))
    state["trend_pool_source_project"] = str(resolution.get("source_project", ""))
    state["trend_pool_loaded_at"] = str(resolution.get("loaded_at", ""))
    state["trend_pool_messages"] = [str(message) for message in resolution.get("messages", [])]

    if resolution.get("source_kind") == "fresh_upstream":
        state[last_good_payload_key] = dict(resolution.get("payload", {}))


def format_trend_pool_source_logs(
    trend_pool_resolution: dict[str, Any],
    *,
    allow_new_trend_entries: bool,
) -> list[str]:
    log_lines = [
        (
            f"📡 Trend pool source: {trend_pool_resolution['source_kind']} | "
            f"mode={trend_pool_resolution['mode'] or 'unknown'} | "
            f"version={trend_pool_resolution['version'] or 'unknown'} | "
            f"as_of={trend_pool_resolution['as_of_date'] or 'n/a'} | "
            f"project={trend_pool_resolution['source_project']}"
        )
    ]
    log_lines.extend(f"   · {message}" for message in trend_pool_resolution.get("messages", [])[-3:])
    if trend_pool_resolution["degraded"] and not allow_new_trend_entries:
        log_lines.append("⚠️ Upstream trend pool is degraded; pausing new trend buys and retaining the current monthly execution pool.")
    return log_lines
