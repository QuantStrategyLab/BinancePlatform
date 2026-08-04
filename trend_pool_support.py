import json
import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from live_services import get_firestore_client
from notify_i18n_support import translate as t
from strategy_artifact_support import (
    get_strategy_artifact_csv,
    get_strategy_artifact_env,
    get_strategy_artifact_int,
)


_RUNTIME_IDENTITY_ARTIFACTS = frozenset(
    {"live_pool", "live_pool_legacy", "latest_ranking", "latest_universe"}
)
_RUNTIME_IDENTITY_PROFILE = "crypto_live_pool_rotation"
_LEGACY_EXACT_BYTES_CONTRACT_VERSION = "qsl.crypto_live_pool_legacy_exact_bytes.v1"


def _canonical_sha256(value):
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reject_non_standard_json_constant(value):
    raise ValueError(f"non-standard JSON constant is not allowed: {value}")


def _validate_exact_legacy_artifact(payload, identity):
    errors = []
    handoff = payload.get("live_pool_legacy_exact_bytes") if isinstance(payload, dict) else None
    if not isinstance(handoff, dict):
        return {}, ["live_pool_legacy exact bytes handoff must be an object"]
    if handoff.get("contract_version") != _LEGACY_EXACT_BYTES_CONTRACT_VERSION:
        errors.append("live_pool_legacy exact bytes contract version mismatch")
    if handoff.get("encoding") != "utf-8":
        errors.append("live_pool_legacy exact bytes encoding must be utf-8")

    exact_text = handoff.get("utf8_text")
    exact_bytes = None
    if not isinstance(exact_text, str):
        errors.append("live_pool_legacy exact bytes must contain UTF-8 text")
    else:
        try:
            exact_bytes = exact_text.encode("utf-8")
        except UnicodeEncodeError:
            errors.append("live_pool_legacy exact bytes must contain valid UTF-8 text")

    artifacts = identity.get("artifacts") if isinstance(identity, dict) else None
    legacy_identity = artifacts.get("live_pool_legacy") if isinstance(artifacts, dict) else None
    expected_digest = legacy_identity.get("sha256") if isinstance(legacy_identity, dict) else None
    if exact_bytes is not None and hashlib.sha256(exact_bytes).hexdigest() != expected_digest:
        errors.append("live_pool_legacy exact bytes digest mismatch")

    exact_payload = None
    if exact_bytes is not None:
        try:
            parsed = json.loads(
                exact_text,
                parse_constant=_reject_non_standard_json_constant,
            )
        except (TypeError, ValueError):
            errors.append("live_pool_legacy exact bytes must contain valid JSON")
        else:
            if not isinstance(parsed, dict):
                errors.append("live_pool_legacy exact bytes must contain a JSON object")
            else:
                exact_payload = parsed

    if exact_payload is not None:
        exact_symbols = exact_payload.get("symbols")
        exact_symbol_map = exact_payload.get("symbol_map")
        if not isinstance(exact_symbols, dict) or not exact_symbols:
            errors.append("live_pool_legacy exact bytes symbols must be a non-empty object")
        if not isinstance(exact_symbol_map, dict) or exact_symbol_map != exact_symbols:
            errors.append("live_pool_legacy exact bytes symbol_map mismatch")
        if isinstance(exact_symbols, dict):
            if payload.get("symbols") != list(exact_symbols):
                errors.append("live_pool_legacy exact bytes symbols convenience mismatch")
            if payload.get("symbol_map") != exact_symbols:
                errors.append("live_pool_legacy exact bytes symbol_map convenience mismatch")
        for field in ("as_of_date", "version", "mode", "pool_size", "source_project"):
            if payload.get(field) != exact_payload.get(field):
                errors.append(f"live_pool_legacy exact bytes {field} convenience mismatch")

    return exact_payload or {}, errors


def validate_runtime_evidence_identity(identity, *, payload):
    errors = []
    if not isinstance(identity, dict):
        return {}, "", ["runtime_evidence_identity must be an object"]
    required = (
        "strategy_profile",
        "mode",
        "source_revision",
        "input_timestamp",
        "artifact_contract",
        "artifact_version",
        "artifacts",
    )
    for field in required:
        if field not in identity:
            errors.append(f"runtime_evidence_identity missing field: {field}")
    if errors:
        return {}, "", errors

    if identity.get("strategy_profile") != _RUNTIME_IDENTITY_PROFILE:
        errors.append("runtime_evidence_identity strategy_profile mismatch")
    if identity.get("mode") != payload.get("mode"):
        errors.append("runtime_evidence_identity mode mismatch")
    source_revision = identity.get("source_revision")
    if not isinstance(source_revision, str) or not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        errors.append("runtime_evidence_identity source_revision must be a lowercase git SHA")
    expected_timestamp = f"{payload.get('as_of_date')}T00:00:00Z"
    if identity.get("input_timestamp") != expected_timestamp:
        errors.append("runtime_evidence_identity input_timestamp mismatch")
    artifact_contract = identity.get("artifact_contract")
    if not isinstance(artifact_contract, str) or not artifact_contract.strip():
        errors.append("runtime_evidence_identity artifact_contract must be non-empty")
    declared_contract = payload.get("artifact_contract_version")
    if declared_contract and artifact_contract != declared_contract:
        errors.append("runtime_evidence_identity artifact_contract mismatch")
    if identity.get("artifact_version") != payload.get("version"):
        errors.append("runtime_evidence_identity artifact_version mismatch")

    artifacts = identity.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != _RUNTIME_IDENTITY_ARTIFACTS:
        errors.append("runtime_evidence_identity artifacts must contain the exact four release artifacts")
    else:
        for name, artifact in artifacts.items():
            if not isinstance(artifact, dict) or not isinstance(artifact.get("sha256"), str) or not re.fullmatch(
                r"[0-9a-f]{64}", artifact["sha256"]
            ):
                errors.append(f"runtime_evidence_identity artifacts.{name}.sha256 is invalid")
    if errors:
        return {}, "", errors
    normalized = json.loads(json.dumps(identity, sort_keys=True, allow_nan=False))
    return normalized, _canonical_sha256(normalized), []


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
                None,
                max_age_days_default,
            ),
        ),
        "acceptable_modes": get_strategy_artifact_csv(
            "STRATEGY_ARTIFACT_ACCEPTABLE_MODES",
            None,
            acceptable_modes_default,
        ),
        "expected_pool_size": max(
            1,
            get_strategy_artifact_int(
                "STRATEGY_ARTIFACT_EXPECTED_SIZE",
                None,
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

    runtime_evidence_identity, release_identity_sha256, identity_errors = validate_runtime_evidence_identity(
        (payload or {}).get("runtime_evidence_identity"),
        payload={
            "as_of_date": as_of_date.isoformat() if as_of_date is not None else "",
            "version": version,
            "mode": mode,
            "artifact_contract_version": (payload or {}).get("artifact_contract_version"),
        },
    )
    errors.extend(identity_errors)
    exact_payload, exact_payload_errors = _validate_exact_legacy_artifact(
        payload or {},
        runtime_evidence_identity,
    )
    errors.extend(exact_payload_errors)
    if exact_payload:
        symbol_map = parse_trend_universe_mapping(exact_payload)
        symbols = extract_trend_pool_symbols(exact_payload, symbol_map)

    ordered_symbol_map = {
        symbol: symbol_map[symbol]
        for symbol in symbols
        if symbol in symbol_map
    }

    normalized_payload = {
        "as_of_date": as_of_date.isoformat() if as_of_date is not None else "",
        "version": version,
        "mode": mode,
        "pool_size": len(symbols),
        "symbols": symbols,
        "symbol_map": ordered_symbol_map,
        "source_project": source_project,
        "runtime_evidence_identity": runtime_evidence_identity,
        "release_identity_sha256": release_identity_sha256,
        "live_pool_legacy_exact_bytes": {
            "contract_version": _LEGACY_EXACT_BYTES_CONTRACT_VERSION,
            "encoding": "utf-8",
            "utf8_text": (
                (payload or {}).get("live_pool_legacy_exact_bytes", {}).get("utf8_text", "")
                if isinstance((payload or {}).get("live_pool_legacy_exact_bytes"), dict)
                else ""
            ),
        },
    }

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "source_label": str(source_label),
        "payload": normalized_payload,
        "symbol_map": ordered_symbol_map,
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
        "CryptoLivePoolPipelines",
        "crypto-live-pool-pipelines",
        "CryptoLeaderRotation",
        "crypto-live-pool-pipelines",
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
        None,
        default_collection,
    )
    document = get_strategy_artifact_env(
        "STRATEGY_ARTIFACT_FIRESTORE_DOCUMENT",
        None,
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
        "runtime_evidence_identity": payload["runtime_evidence_identity"],
        "release_identity_sha256": payload["release_identity_sha256"],
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
        "runtime_evidence_identity": {},
        "release_identity_sha256": "",
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
        "runtime_evidence_identity": {},
        "release_identity_sha256": "",
    }
