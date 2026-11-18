from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def get_strategy_artifact_env(name: str, legacy_name: str | None = None, default: str = "") -> str:
    primary = str(os.getenv(name, "")).strip()
    if primary:
        return primary
    if legacy_name:
        legacy = str(os.getenv(legacy_name, "")).strip()
        if legacy:
            return legacy
    return str(default)


def get_strategy_artifact_int(name: str, legacy_name: str | None, default: int) -> int:
    raw = get_strategy_artifact_env(name, legacy_name)
    if not raw:
        return int(default)
    try:
        return int(raw)
    except Exception:
        return int(default)


def get_strategy_artifact_csv(
    name: str,
    legacy_name: str | None,
    default_values: Iterable[str],
) -> list[str]:
    raw = get_strategy_artifact_env(name, legacy_name)
    if not raw:
        return list(default_values)
    return [item.strip() for item in raw.split(",") if item.strip()]


def build_strategy_artifact_file_candidates(
    *,
    configured_path: str,
    default_candidates: Iterable[Path],
) -> list[Path]:
    candidates: list[Path] = []
    if configured_path:
        candidates.append(Path(configured_path).expanduser())
    for candidate in default_candidates:
        path = Path(candidate).expanduser()
        if path not in candidates:
            candidates.append(path)
    return candidates
