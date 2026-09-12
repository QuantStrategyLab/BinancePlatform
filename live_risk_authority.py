"""Read and bind the externally approved Binance LIVE risk material.

The file is a static approval input.  Current market/account inputs are bound
only at evaluation time, after the runtime has collected the complete cycle
snapshot.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from quant_platform_kit.risk.contracts import CandidateRiskIdentity

AUTHORITY_FILE_ENV = "BINANCE_RISK_AUTHORITY_FILE"
AUTHORITY_SHA256_ENV = "BINANCE_RISK_AUTHORITY_SHA256"
AUTHORITY_SOURCE_REVISION_ENV = "BINANCE_RISK_AUTHORITY_SOURCE_REVISION"
MAX_AUTHORITY_BYTES = 64 * 1024
ACCOUNT_MODE = "single_strategy_account_v1"
_AUTHORITY_FIELDS = frozenset(
    {
        "decision",
        "authority_scope",
        "runtime_target",
        "strategy_revision",
        "runner_revision",
        "config_sha256",
        "continuous_inputs_allowed",
        "mandate",
    }
)
_RUNTIME_TARGET_FIELDS = frozenset(
    {"platform_id", "strategy_profile", "account_scope", "account_selector", "deployment_selector"}
)
_MANDATE_FIELDS = frozenset(
    {
        "mandate_id",
        "mandate_version",
        "effective_at",
        "expires_at",
        "max_snapshot_age_seconds",
        "effective_exposure_cap",
        "loss_budget",
        "product_caps",
        "nominal_caps",
        "product_leverage_factors",
        "allowed_nonzero_assets",
        "product_effective_caps",
        "max_nonzero_assets",
    }
)
_REQUIRED_MANDATE_FIELDS = _MANDATE_FIELDS - {"product_effective_caps", "max_nonzero_assets"}
_FORBIDDEN_AUTHORITY_FIELDS = frozenset(
    {"no_live", "no_order", "no_paper", "no_shadow", "research_only", "paper_only"}
)

class LiveRiskAuthorityError(ValueError):
    """Sanitized configuration or binding failure; always fail closed."""

    def __init__(self, reason: str):
        super().__init__(f"live risk authority configuration invalid: {reason}")

@dataclass(frozen=True)
class LiveRiskAuthority:
    authority_receipt_sha256: str
    source_revision: str
    runtime_target_binding: dict[str, Any]
    strategy_revision: str
    runner_revision: str
    config_sha256: str
    continuous_inputs_allowed: bool
    mandate: dict[str, Any]

def _error(reason: str) -> LiveRiskAuthorityError:
    return LiveRiskAuthorityError(reason)

def _digest(value: str, *, field: str, length: int = 64) -> str:
    if type(value) is not str or len(value) != length or any(c not in "0123456789abcdef" for c in value):
        raise _error(f"invalid {field}")
    return value

def _revision(value: str, *, field: str) -> str:
    return _digest(value, field=field, length=40)

def _finite(value: Any, *, field: str, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool):
        raise _error(f"invalid {field}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise _error(f"invalid {field}") from None
    if not math.isfinite(number) or (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
        raise _error(f"invalid {field}")
    return number

def _parse_utc(value: Any, *, field: str) -> datetime:
    if type(value) is not str:
        raise _error(f"invalid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _error(f"invalid {field}") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise _error(f"invalid {field}")
    return parsed.astimezone(timezone.utc)

def _strict_json(raw: bytes) -> Mapping[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise _error("duplicate authority field")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(_error("non-finite JSON value")),
        )
    except LiveRiskAuthorityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _error("invalid authority JSON") from None
    if not isinstance(value, Mapping):
        raise _error("authority JSON must be an object")
    return value

def _regular_file_bytes(path: Path) -> bytes:
    try:
        details = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(details.st_mode):
            raise _error("authority file must be a regular non-symlink file")
        if details.st_size <= 0 or details.st_size > MAX_AUTHORITY_BYTES:
            raise _error("authority file size is invalid")
        raw = path.read_bytes()
    except LiveRiskAuthorityError:
        raise
    except OSError:
        raise _error("authority file cannot be read") from None
    if len(raw) != details.st_size:
        raise _error("authority file changed while reading")
    return raw

def _target_binding(runtime_target: Any) -> dict[str, Any]:
    if runtime_target is None:
        raise _error("runtime target is missing")
    binding = {
        "platform_id": str(getattr(runtime_target, "platform_id", "")),
        "strategy_profile": str(getattr(runtime_target, "strategy_profile", "")),
        "account_scope": str(getattr(runtime_target, "account_scope", "")),
        "account_selector": list(getattr(runtime_target, "account_selector", ()) or ()),
        "deployment_selector": str(getattr(runtime_target, "deployment_selector", "")),
    }
    if any(not value or value == "None" for value in (binding["platform_id"], binding["strategy_profile"], binding["account_scope"], binding["deployment_selector"])):
        raise _error("runtime target identity is incomplete")
    if binding["account_scope"] == "default" or binding["account_selector"] in ([], ["default"]):
        raise _error("runtime target account identity is ambiguous")
    if any(type(value) is not str or not value.strip() or value == "default" for value in binding["account_selector"]):
        raise _error("runtime target account identity is ambiguous")
    return binding

def _validate_target_binding(value: Any, runtime_target: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _RUNTIME_TARGET_FIELDS:
        raise _error("runtime target binding is invalid")
    expected = _target_binding(runtime_target)
    actual = {
        "platform_id": value.get("platform_id"),
        "strategy_profile": value.get("strategy_profile"),
        "account_scope": value.get("account_scope"),
        "account_selector": value.get("account_selector"),
        "deployment_selector": value.get("deployment_selector"),
    }
    if not isinstance(actual["account_selector"], list) or actual != expected:
        raise _error("runtime target mismatch")
    return dict(actual)

def _validate_mandate(value: Any, *, now_utc: datetime) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not _REQUIRED_MANDATE_FIELDS.issubset(value):
        raise _error("mandate fields are incomplete")
    if set(value) - _MANDATE_FIELDS:
        raise _error("mandate fields are unsupported")
    if any(field in value for field in _FORBIDDEN_AUTHORITY_FIELDS):
        raise _error("mandate contains contradictory execution flags")
    for field in ("mandate_id", "mandate_version"):
        if type(value.get(field)) is not str or not value[field].strip():
            raise _error(f"invalid {field}")
    effective_at = _parse_utc(value["effective_at"], field="effective_at")
    expires_at = _parse_utc(value["expires_at"], field="expires_at")
    if expires_at <= effective_at or now_utc < effective_at or now_utc >= expires_at:
        raise _error("authority expired or not yet effective")
    _finite(value["max_snapshot_age_seconds"], field="max_snapshot_age_seconds", minimum=0.001)
    _finite(value["effective_exposure_cap"], field="effective_exposure_cap", minimum=0.0, maximum=1.0)
    _finite(value["loss_budget"], field="loss_budget", minimum=0.0)
    if not isinstance(value["allowed_nonzero_assets"], list) or not value["allowed_nonzero_assets"]:
        raise _error("allowed_nonzero_assets is invalid")
    if any(type(asset) is not str or not asset.strip() for asset in value["allowed_nonzero_assets"]):
        raise _error("allowed_nonzero_assets is invalid")
    for field in ("product_caps", "nominal_caps", "product_leverage_factors"):
        if not isinstance(value[field], (Mapping, int, float)) or isinstance(value[field], bool):
            raise _error(f"invalid {field}")
    return dict(value)

def resolve_strategy_revision() -> str:
    try:
        distribution = importlib.metadata.distribution("crypto-strategies")
        direct_url = distribution.read_text("direct_url.json")
        payload = json.loads(direct_url or "")
        vcs_info = payload["vcs_info"]
        revision = vcs_info["commit_id"]
        if vcs_info.get("vcs") != "git" or vcs_info.get("requested_revision") != revision:
            raise _error("installed strategy source is not pinned")
        module_spec = importlib.util.find_spec("crypto_strategies")
        module_origin = Path(module_spec.origin).resolve() if module_spec and module_spec.origin else None
        package_root = Path(distribution.locate_file("crypto_strategies")).resolve()
        if module_origin is None or package_root not in module_origin.parents:
            raise _error("imported strategy source is outside the installed distribution")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, ModuleNotFoundError, importlib.metadata.PackageNotFoundError):
        raise _error("installed strategy revision is unavailable") from None
    return _revision(revision, field="strategy_revision")

def resolve_runner_revision() -> str:
    repo_root = Path(__file__).resolve().parent
    try:
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if dirty:
            raise _error("runner checkout has tracked modifications")
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except LiveRiskAuthorityError:
        raise
    except (OSError, subprocess.CalledProcessError):
        raise _error("runner revision is unavailable") from None
    return _revision(revision, field="runner_revision")

def canonical_input_digest(value: Any) -> str:
    def normalize(item: Any) -> Any:
        if item is None or isinstance(item, (str, bool)):
            return item
        if isinstance(item, datetime):
            if item.tzinfo is None:
                raise _error("input datetime has no timezone")
            return item.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, Mapping):
            if any(type(key) is not str for key in item):
                raise _error("input mapping key is not a string")
            return {key: normalize(item[key]) for key in sorted(item)}
        if isinstance(item, (tuple, list)):
            return [normalize(value) for value in item]
        if isinstance(item, set):
            normalized = [normalize(value) for value in item]
            return sorted(normalized, key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")))
        if isinstance(item, int):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise _error("input contains a non-finite number")
            return item
        if hasattr(item, "item"):
            return normalize(item.item())
        raise _error(f"unsupported input value type: {type(item).__name__}")

    try:
        encoded = json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise _error("input cannot be canonically encoded") from None
    return hashlib.sha256(encoded).hexdigest()

def load_live_risk_authority(
    *,
    env: Mapping[str, str] | None = None,
    runtime_target: Any,
    strategy_revision: str | None = None,
    runner_revision: str | None = None,
    config_sha256: str | None = None,
    now_utc: datetime | None = None,
) -> LiveRiskAuthority | None:
    source = os.environ if env is None else env
    values = {name: str(source.get(name) or "").strip() for name in (AUTHORITY_FILE_ENV, AUTHORITY_SHA256_ENV, AUTHORITY_SOURCE_REVISION_ENV)}
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise _error("authority source parameters are incomplete")
    path = Path(values[AUTHORITY_FILE_ENV])
    raw = _regular_file_bytes(path)
    receipt_sha256 = _digest(values[AUTHORITY_SHA256_ENV].lower(), field="authority SHA-256")
    if hashlib.sha256(raw).hexdigest() != receipt_sha256:
        raise _error("authority file digest mismatch")
    source_revision = _revision(values[AUTHORITY_SOURCE_REVISION_ENV].lower(), field="source revision")
    payload = _strict_json(raw)
    if set(payload) != _AUTHORITY_FIELDS:
        raise _error("authority fields are unsupported or incomplete")
    if payload.get("decision") != "APPROVE":
        raise _error("authority decision is not APPROVE")
    if payload.get("authority_scope") != "LIVE":
        raise _error("authority scope is not LIVE")
    if payload.get("continuous_inputs_allowed") is not True:
        raise _error("continuous inputs are not approved")
    target_binding = _validate_target_binding(payload.get("runtime_target"), runtime_target)
    strategy_revision = _revision(strategy_revision or resolve_strategy_revision(), field="strategy revision")
    runner_revision = _revision(runner_revision or resolve_runner_revision(), field="runner revision")
    config_sha256 = _digest((config_sha256 or "").lower(), field="config SHA-256")
    if payload.get("strategy_revision") != strategy_revision:
        raise _error("strategy revision mismatch")
    if payload.get("runner_revision") != runner_revision:
        raise _error("runner revision mismatch")
    if payload.get("config_sha256") != config_sha256:
        raise _error("config digest mismatch")
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise _error("evaluation time has no timezone")
    mandate = _validate_mandate(payload.get("mandate"), now_utc=now.astimezone(timezone.utc))
    return LiveRiskAuthority(
        authority_receipt_sha256=receipt_sha256,
        source_revision=source_revision,
        runtime_target_binding=target_binding,
        strategy_revision=strategy_revision,
        runner_revision=runner_revision,
        config_sha256=config_sha256,
        continuous_inputs_allowed=True,
        mandate=mandate,
    )

def bind_live_risk_authority(
    authority: LiveRiskAuthority,
    *,
    runtime_target: Any,
    input_material: Any,
    config_sha256: str,
    strategy_revision: str | None = None,
    runner_revision: str | None = None,
    now_utc: datetime,
) -> tuple[dict[str, Any], CandidateRiskIdentity]:
    if not isinstance(authority, LiveRiskAuthority):
        raise _error("authority material is missing")
    if _validate_target_binding(authority.runtime_target_binding, runtime_target) != authority.runtime_target_binding:
        raise _error("runtime target mismatch")
    strategy_revision = _revision(strategy_revision or resolve_strategy_revision(), field="strategy revision")
    runner_revision = _revision(runner_revision or resolve_runner_revision(), field="runner revision")
    config_sha256 = _digest(config_sha256.lower(), field="config SHA-256")
    if strategy_revision != authority.strategy_revision:
        raise _error("strategy revision mismatch")
    if runner_revision != authority.runner_revision:
        raise _error("runner revision mismatch")
    if config_sha256 != authority.config_sha256:
        raise _error("config digest mismatch")
    if not authority.continuous_inputs_allowed:
        raise _error("continuous inputs are not approved")
    effective_at = _parse_utc(authority.mandate["effective_at"], field="effective_at")
    expires_at = _parse_utc(authority.mandate["expires_at"], field="expires_at")
    current_time = now_utc.astimezone(timezone.utc)
    if current_time < effective_at or current_time >= expires_at:
        raise _error("authority expired or not yet effective")
    input_digest = canonical_input_digest(input_material)
    candidate = CandidateRiskIdentity(
        strategy_profile=str(getattr(runtime_target, "strategy_profile", "")),
        account_mode=ACCOUNT_MODE,
        strategy_revision=strategy_revision,
        runner_revision=runner_revision,
        config_sha256=config_sha256,
        input_manifest_sha256=input_digest,
        authority_receipt_sha256=authority.authority_receipt_sha256,
    )
    mandate = dict(authority.mandate)
    mandate.update(
        {
            "authority_receipt_sha256": authority.authority_receipt_sha256,
            "authority_scope": "LIVE",
            "source_revision": authority.source_revision,
            "strategy_profile": candidate.strategy_profile,
            "account_mode": ACCOUNT_MODE,
            "strategy_revision": strategy_revision,
            "runner_revision": runner_revision,
            "config_sha256": config_sha256,
            "input_manifest_sha256": input_digest,
            "candidate_identity_sha256": candidate.candidate_sha256,
        }
    )
    return mandate, candidate

def config_sha256(config: Mapping[str, Any]) -> str:
    """Hash manifest defaults merged with approved runtime overrides."""

    return canonical_input_digest(dict(config))
