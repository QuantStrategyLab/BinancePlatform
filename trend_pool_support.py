import json
import os
from datetime import datetime, timezone
from pathlib import Path

from live_services import get_firestore_client
from notify_i18n_support import translate as t
from strategy_artifact_support import (
    get_strategy_artifact_csv,
    get_strategy_artifact_env,
    get_strategy_artifact_int,
)


def infer_base_asset(symbol):
    return symbol[:-4] if isinstance(symbol, str) and symbol.endswith("USDT") else symbol


def get_env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)


def get_env_csv(name, default_values):
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return list(default_values)
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def parse_trend_pool_date(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def parse_trend_universe_mapping(payload):
    if not isinstance(payload, dict):
        return {}

    symbols = payload.get("symbol_map")
    if not isinstance(symbols, (dict, list)):
        symbols = payload.get("symbols")
    if not isinstance(symbols, (dict, list)):
        return {}

    parsed = {}
    if isinstance(symbols, list):
        symbol_items = ((symbol, {"base_asset": infer_base_asset(symbol)}) for symbol in symbols)
    else:
        symbol_items = symbols.items()

    for symbol, meta in symbol_items:
        if not isinstance(symbol, str) or not symbol.endswith("USDT"):
            continue
        if not isinstance(meta, dict):
            meta = {"base_asset": infer_base_asset(symbol)}
        base_asset = str(meta.get("base_asset") or infer_base_asset(symbol)).strip()
        if not base_asset:
            continue
        parsed[symbol] = {"base_asset": base_asset}
    return parsed


def extract_trend_pool_symbols(payload, symbol_map):
    if not isinstance(payload, dict):
        return list(symbol_map.keys())

    raw_symbols = payload.get("symbols")
    if isinstance(raw_symbols, list):
        ordered = raw_symbols
    elif isinstance(raw_symbols, dict):
        ordered = list(raw_symbols.keys())
    else:
        ordered = list(symbol_map.keys())

    deduped = []
    seen = set()
    for symbol in ordered:
        if symbol in symbol_map and symbol not in seen:
            deduped.append(symbol)
            seen.add(symbol)
    return deduped


def get_trend_pool_contract_settings(*, max_age_days_default, acceptable_modes_default, expected_pool_size_default):
    return {
        "max_age_days": max(
            0,
            get_strategy_artifact_int(
                "STRATEGY_ARTIFACT_MAX_AGE_DAYS",
                "TREND_POOL_MAX_AGE_DAYS",
                max_age_days_default,
            ),
        ),
        "acceptable_modes": get_strategy_artifact_csv(
            "STRATEGY_ARTIFACT_ACCEPTABLE_MODES",
            "TREND_POOL_ACCEPTABLE_MODES",
            acceptable_modes_default,
        ),
        "expected_pool_size": max(
            1,
            get_strategy_artifact_int(
                "STRATEGY_ARTIFACT_EXPECTED_SIZE",
                "TREND_POOL_EXPECTED_SIZE",
                expected_pool_size_default,
            ),
        ),
    }


def validate_trend_pool_payload(
    payload,
    source_label,
    *,
    now_utc=None,
    max_age_days,
    acceptable_modes,
    expected_pool_size,
    enforce_freshness,
):
    now_utc = now_utc or datetime.now(timezone.utc)
    acceptable_modes = list(acceptable_modes or [])
    symbol_map = parse_trend_universe_mapping(payload)
    symbols = extract_trend_pool_symbols(payload, symbol_map)
    errors = []
    warnings = []

    as_of_date = parse_trend_pool_date((payload or {}).get("as_of_date"))
    if as_of_date is None:
        errors.append(t("missing_invalid_as_of_date"))

    mode = (payload or {}).get("mode")
    if isinstance(mode, str):
        mode = mode.strip()
    else:
        mode = ""
    if not mode:
        if acceptable_modes:
            mode = acceptable_modes[0]
            warnings.append(t("mode_missing_assumed", mode=mode))
    elif acceptable_modes and mode not in acceptable_modes:
        errors.append(t("mode_not_acceptable", mode=mode, acceptable_modes=acceptable_modes))

    if not symbol_map:
        errors.append(t("symbols_map_missing_or_invalid"))
    if not symbols:
        errors.append(t("symbols_list_empty"))

    pool_size_value = (payload or {}).get("pool_size", len(symbols))
    try:
        pool_size = int(pool_size_value)
    except Exception:
        pool_size = len(symbols)
        errors.append(t("pool_size_missing_or_invalid"))

    if pool_size != len(symbols):
        errors.append(t("pool_size_mismatch", declared=pool_size, parsed=len(symbols)))
    if expected_pool_size and symbols and pool_size != int(expected_pool_size):
        errors.append(
            t(
                "pool_size_expected_mismatch",
                pool_size=pool_size,
                expected_pool_size=int(expected_pool_size),
            )
        )

    age_days = None
    is_fresh = False
    if as_of_date is not None:
        age_days = (now_utc.date() - as_of_date).days
        is_fresh = age_days <= int(max_age_days)
        if age_days < 0:
            errors.append(t("as_of_date_in_future", as_of_date=as_of_date.isoformat()))
        elif enforce_freshness and age_days > int(max_age_days):
            errors.append(
                t(
                    "payload_stale_by_days",
                    age_days=age_days,
                    max_age_days=int(max_age_days),
                )
            )

    version = (payload or {}).get("version")
    if isinstance(version, str):
        version = version.strip()
    else:
        version = ""
    if not version and as_of_date is not None and mode:
        version = f"{as_of_date.isoformat()}-{mode}"
        warnings.append(t("version_missing_synthesized"))

    source_project = (payload or {}).get("source_project")
    if isinstance(source_project, str):
        source_project = source_project.strip()
    else:
        source_project = ""
    if not source_project:
        source_project = "unknown"
        warnings.append(t("source_project_missing_unknown"))

    normalized_payload = {
        "as_of_date": as_of_date.isoformat() if as_of_date is not None else "",
        "version": version,
        "mode": mode,
        "pool_size": len(symbols),
        "symbols": symbols,
        "symbol_map": symbol_map,
        "source_project": source_project,
    }

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "source_label": str(source_label),
        "payload": normalized_payload,
        "symbol_map": symbol_map,
        "symbols": symbols,
        "pool_size": len(symbols),
        "as_of_date": normalized_payload["as_of_date"],
        "version": version,
        "mode": mode,
        "source_project": source_project,
        "age_days": age_days,
        "is_fresh": is_fresh,
    }


def get_default_live_pool_candidates(default_live_pool_legacy_path):
    candidates = []
    search_roots = {
        Path(__file__).resolve().parents[1],
        Path.cwd().resolve(),
        Path.home(),
        Path("/home/ubuntu"),
    }
    repo_names = (
        "CryptoSnapshotPipelines",
        "crypto-snapshot-pipelines",
        "CryptoLeaderRotation",
        "crypto-leader-rotation",
    )

    for root in search_roots:
        for repo_name in repo_names:
            candidate = root / repo_name / "data" / "output" / "live_pool_legacy.json"
            if candidate not in candidates:
                candidates.append(candidate)

    if default_live_pool_legacy_path not in candidates:
        candidates.insert(0, default_live_pool_legacy_path)
    return candidates


def load_trend_pool_from_firestore(
    *,
    now_utc,
    settings,
    default_collection,
    default_document,
):
    collection = get_strategy_artifact_env(
        "STRATEGY_ARTIFACT_FIRESTORE_COLLECTION",
        "TREND_POOL_FIRESTORE_COLLECTION",
        default_collection,
    )
    document = get_strategy_artifact_env(
        "STRATEGY_ARTIFACT_FIRESTORE_DOCUMENT",
        "TREND_POOL_FIRESTORE_DOCUMENT",
        default_document,
    )
    settings = settings or {}
    source_label = f"firestore:{collection}/{document}"

    try:
        payload = get_firestore_client().collection(collection).document(document).get()
        if not payload.exists:
            return {
                "ok": False,
                "errors": [t("missing_firestore_document", collection=collection, document=document)],
                "warnings": [],
                "source_label": source_label,
            }

        return validate_trend_pool_payload(
            payload.to_dict(),
            source_label=source_label,
            now_utc=now_utc,
            max_age_days=settings["max_age_days"],
            acceptable_modes=settings["acceptable_modes"],
            expected_pool_size=settings["expected_pool_size"],
            enforce_freshness=True,
        )
    except Exception as exc:
        return {
            "ok": False,
            "errors": [t("firestore_read_failed", error=exc)],
            "warnings": [],
            "source_label": source_label,
        }


def load_trend_pool_from_file(path, *, now_utc, settings):
    source_label = f"file:{path}"
    try:
        pool_path = Path(path).expanduser()
        if not pool_path.exists():
            return {
                "ok": False,
                "errors": [t("pool_file_not_found", pool_path=pool_path)],
                "warnings": [],
                "source_label": source_label,
            }
        payload = json.loads(pool_path.read_text(encoding="utf-8"))
        return validate_trend_pool_payload(
            payload,
            source_label=source_label,
            now_utc=now_utc,
            max_age_days=settings["max_age_days"],
            acceptable_modes=settings["acceptable_modes"],
            expected_pool_size=settings["expected_pool_size"],
            enforce_freshness=True,
        )
    except Exception as exc:
        return {
            "ok": False,
            "errors": [t("pool_file_read_failed", error=exc)],
            "warnings": [],
            "source_label": source_label,
        }


def build_trend_pool_resolution(validated_payload, *, source_kind, degraded, now_utc=None, messages=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    payload = dict(validated_payload["payload"])
    return {
        "source_kind": str(source_kind),
        "source_label": validated_payload.get("source_label", source_kind),
        "degraded": bool(degraded),
        "is_fresh": bool(validated_payload.get("is_fresh", False)),
        "messages": list(messages or []) + list(validated_payload.get("warnings", [])),
        "errors": list(validated_payload.get("errors", [])),
        "loaded_at": now_utc.isoformat(),
        "payload": payload,
        "symbol_map": payload["symbol_map"],
        "symbols": payload["symbols"],
        "pool_size": payload["pool_size"],
        "as_of_date": payload["as_of_date"],
        "version": payload["version"],
        "mode": payload["mode"],
        "source_project": payload["source_project"],
    }


def get_last_known_good_trend_pool(state, *, now_utc, settings, last_good_payload_key):
    payload = {}
    if isinstance(state, dict):
        payload = state.get(last_good_payload_key, {})
    return validate_trend_pool_payload(
        payload,
        source_label="state:last_known_good",
        now_utc=now_utc,
        max_age_days=settings["max_age_days"],
        acceptable_modes=settings["acceptable_modes"],
        expected_pool_size=settings["expected_pool_size"],
        enforce_freshness=False,
    )


def build_static_trend_pool_resolution(*, now_utc=None, messages=None, static_trend_universe=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    static_trend_universe = static_trend_universe or {}
    payload = {
        "as_of_date": "",
        "version": "static-fallback",
        "mode": "static",
        "pool_size": len(static_trend_universe),
        "symbols": list(static_trend_universe.keys()),
        "symbol_map": {symbol: meta.copy() for symbol, meta in static_trend_universe.items()},
        "source_project": "BinancePlatform",
    }
    return {
        "source_kind": "static",
        "source_label": "static:built_in",
        "degraded": True,
        "is_fresh": False,
        "messages": list(messages or []),
        "errors": [],
        "loaded_at": now_utc.isoformat(),
        "payload": payload,
        "symbol_map": payload["symbol_map"],
        "symbols": payload["symbols"],
        "pool_size": payload["pool_size"],
        "as_of_date": payload["as_of_date"],
        "version": payload["version"],
        "mode": payload["mode"],
        "source_project": payload["source_project"],
    }
