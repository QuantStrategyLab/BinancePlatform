"""Project schema-checked authority metadata without loading business services.

This reads the environment Secret in memory. It does not attest the current
runner, installed strategy, mandate validity, target binding or business state.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
from datetime import datetime, timedelta

_FIELDS = {
    "decision", "authority_scope", "runtime_target", "strategy_revision",
    "runner_revision", "config_sha256", "continuous_inputs_allowed", "mandate",
}
_TARGET_FIELDS = {
    "platform_id", "strategy_profile", "account_scope", "account_selector", "deployment_selector",
}
_MANDATE_REQUIRED = {
    "mandate_id", "mandate_version", "effective_at", "expires_at",
    "max_snapshot_age_seconds", "effective_exposure_cap", "product_caps",
    "nominal_caps", "product_leverage_factors", "allowed_nonzero_assets",
}
_MANDATE_OPTIONAL = {
    "product_effective_caps", "max_nonzero_assets", "budget_policy", "validity_mode", "loss_budget",
}


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("authority_projection_invalid")


def _hex(value: object, length: int) -> str:
    _require(type(value) is str and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None)
    return value


def _pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        _require(key not in result)
        result[key] = value
    return result


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _text(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _utc_text(value: object) -> bool:
    if not _text(value):
        return False
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


def _finite_float(value: str) -> float:
    number = float(value)
    _require(math.isfinite(number))
    return number


def _invalid_constant(_value: str) -> None:
    raise ValueError("authority_projection_invalid")


def _validate_schema(payload: object) -> None:
    _require(type(payload) is dict and set(payload) == _FIELDS)
    _require(payload["decision"] == "APPROVE" and payload["authority_scope"] == "LIVE")
    _require(payload["continuous_inputs_allowed"] is True)
    _hex(payload["strategy_revision"], 40)
    _hex(payload["runner_revision"], 40)
    _hex(payload["config_sha256"], 64)
    target = payload["runtime_target"]
    _require(type(target) is dict and set(target) == _TARGET_FIELDS)
    _require(all(_text(target[key]) for key in _TARGET_FIELDS - {"account_selector"}))
    _require(target["platform_id"] == "binance")
    selectors = target["account_selector"]
    _require(type(selectors) is list and bool(selectors) and all(_text(item) for item in selectors))
    mandate = payload["mandate"]
    _require(type(mandate) is dict and _MANDATE_REQUIRED <= set(mandate))
    _require(not set(mandate) - (_MANDATE_REQUIRED | _MANDATE_OPTIONAL))
    _require(all(_text(mandate[key]) for key in ("mandate_id", "mandate_version", "effective_at")))
    _require(_utc_text(mandate["effective_at"]))
    if mandate.get("validity_mode", "fixed_expiry") == "until_revoked":
        _require(mandate["expires_at"] is None)
    else:
        _require(_utc_text(mandate["expires_at"]))
    _require(_number(mandate["max_snapshot_age_seconds"]) and mandate["max_snapshot_age_seconds"] > 0)
    _require(_number(mandate["effective_exposure_cap"]) and 0 <= mandate["effective_exposure_cap"] <= 1)
    for key in ("product_caps", "nominal_caps", "product_leverage_factors", "product_effective_caps"):
        if key in mandate:
            value = mandate[key]
            _require(_number(value) or (type(value) is dict and all(
                _text(asset) and _number(cap) for asset, cap in value.items()
            )))
    assets = mandate["allowed_nonzero_assets"]
    _require(type(assets) is list and bool(assets) and all(_text(asset) for asset in assets))
    if "max_nonzero_assets" in mandate:
        _require(type(mandate["max_nonzero_assets"]) is int and mandate["max_nonzero_assets"] > 0)
    if "loss_budget" in mandate:
        _require(mandate["loss_budget"] is None or (_number(mandate["loss_budget"]) and mandate["loss_budget"] >= 0))
    if "budget_policy" in mandate:
        _require(mandate["budget_policy"] == {"mode": "managed_usdt_dynamic"})
        _require(mandate.get("loss_budget") is None)
    else:
        _require("loss_budget" in mandate and _number(mandate["loss_budget"]))
    if "validity_mode" in mandate:
        _require(mandate["validity_mode"] in ("fixed_expiry", "until_revoked"))


def project(raw_text: str, expected_digest: str, source_revision: str) -> dict[str, str]:
    raw = raw_text.encode("utf-8")
    _require(0 < len(raw) <= 65_536)
    _hex(expected_digest, 64)
    _hex(source_revision, 40)
    digest = hashlib.sha256(raw).hexdigest()
    _require(hmac.compare_digest(digest, expected_digest))
    payload = json.loads(raw, object_pairs_hook=_pairs, parse_float=_finite_float,
                         parse_constant=_invalid_constant)
    _validate_schema(payload)
    return {
        "status": "projected",
        "strategy_revision": payload["strategy_revision"],
        "runner_revision": payload["runner_revision"],
        "config_sha256": payload["config_sha256"],
        "authority_sha256": digest,
        "authority_source_revision": source_revision,
    }


def main() -> int:
    try:
        result = project(
            os.environ.pop("BINANCE_RISK_AUTHORITY_JSON", ""),
            os.environ.get("BINANCE_RISK_AUTHORITY_SHA256", ""),
            os.environ.get("BINANCE_RISK_AUTHORITY_SOURCE_REVISION", ""),
        )
    except Exception:
        # Never print exception messages, input-dependent strings or tracebacks.
        print('{"status":"failed","reason_code":"authority_projection_invalid"}')
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
