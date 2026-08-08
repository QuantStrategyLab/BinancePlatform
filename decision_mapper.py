from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from quant_platform_kit.risk.contracts import CandidateRiskIdentity
from quant_platform_kit.risk.gate import (
    _FALLBACK_MAX_SNAPSHOT_AGE_SECONDS_V1,
    _canonical_digest,
    _decision_metrics,
    _parse_utc_timestamp,
)
from quant_platform_kit.strategy_contracts import StrategyDecision


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _approved_scoped_assessment(
    value: Any,
    *,
    scope: str,
    now: datetime,
) -> Mapping[str, Any] | None:
    """Accept only serialized QPK approval evidence; never infer or recompute risk authority."""
    if not isinstance(value, Mapping):
        return None
    reason_codes = value.get("reason_codes")
    if (
        value.get("scope") != scope
        or value.get("outcome") != "APPROVE"
        or not isinstance(reason_codes, (list, tuple))
        or reason_codes
    ):
        return None
    for field in (
        "mandate_authority_receipt_sha256",
        "candidate_identity_sha256",
        "decision_digest_sha256",
        "portfolio_snapshot_digest_sha256",
        "assessment_sha256",
    ):
        if not isinstance(value.get(field), str) or not _SHA256_PATTERN.fullmatch(value[field]):
            return None
    for field in ("contract_version", "evaluated_at", "policy_id", "policy_version"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            return None
    evaluated_at = _parse_utc_timestamp(value["evaluated_at"])
    if evaluated_at is None:
        return None
    age_seconds = (now - evaluated_at).total_seconds()
    if not 0.0 <= age_seconds <= _FALLBACK_MAX_SNAPSHOT_AGE_SECONDS_V1:
        return None
    return value


def has_execution_authority(decision: StrategyDecision | None) -> bool:
    """Require matching QPK RiskEngine, MEMBER, and ACCOUNT approval evidence."""
    if not isinstance(decision, StrategyDecision):
        return False
    diagnostics = decision.diagnostics
    if not isinstance(diagnostics, Mapping) or diagnostics.get("risk_gate") != "APPROVE":
        return False
    if any(str(flag).startswith("rejected:") for flag in decision.risk_flags):
        return False
    now = datetime.now(timezone.utc)
    member = _approved_scoped_assessment(
        diagnostics.get("member_risk_assessment"),
        scope="MEMBER",
        now=now,
    )
    account = _approved_scoped_assessment(
        diagnostics.get("account_risk_assessment"),
        scope="ACCOUNT",
        now=now,
    )
    if member is None or account is None:
        return False
    candidate_identity = diagnostics.get("candidate_risk_identity")
    if not isinstance(candidate_identity, CandidateRiskIdentity):
        return False
    try:
        decision_payload, _, _ = _decision_metrics(decision, total_equity=None)
        decision_digest = _canonical_digest(decision_payload)
    except (TypeError, ValueError):
        return False
    return (
        member["candidate_identity_sha256"]
        == account["candidate_identity_sha256"]
        == candidate_identity.candidate_sha256
        and member["decision_digest_sha256"]
        == account["decision_digest_sha256"]
        == decision_digest
    )


def _budget_map(decision: StrategyDecision) -> dict[str, float]:
    values: dict[str, float] = {}
    for budget in decision.budgets:
        if budget.amount is not None:
            values[budget.name] = float(budget.amount)
    return values


def _position_weight_map(decision: StrategyDecision) -> dict[str, float]:
    values: dict[str, float] = {}
    for position in decision.positions:
        if position.target_weight is not None:
            values[position.symbol] = float(position.target_weight)
    return values


def map_strategy_decision_to_allocation(
    decision: StrategyDecision,
    *,
    account_metrics: Mapping[str, Any],
) -> dict[str, float]:
    diagnostics = dict(decision.diagnostics)
    authorized = has_execution_authority(decision)
    budgets = _budget_map(decision) if authorized else {}
    positions = _position_weight_map(decision) if authorized else {}
    trend_target_ratio = (
        float(
            diagnostics.get(
                "trend_target_ratio",
                sum(weight for symbol, weight in positions.items() if symbol != "BTCUSDT"),
            )
        )
        if authorized
        else 0.0
    )
    return {
        "total_equity": float(account_metrics["total_equity"]),
        "trend_val": float(account_metrics["trend_value"]),
        "dca_val": float(account_metrics["dca_value"]),
        "btc_target_ratio": (
            float(diagnostics.get("btc_target_ratio", positions.get("BTCUSDT", 0.0)))
            if authorized
            else 0.0
        ),
        "trend_target_ratio": trend_target_ratio,
        "trend_usdt_pool": float(budgets.get("trend_rotation_pool", 0.0)),
        "dca_usdt_pool": float(budgets.get("btc_core_dca_pool", 0.0)),
        "btc_base_order_usdt": (
            float(diagnostics.get("btc_base_order_usdt", 0.0)) if authorized else 0.0
        ),
    }


def map_strategy_decision_to_rotation_plan(decision: StrategyDecision) -> dict[str, Any]:
    diagnostics = dict(decision.diagnostics)
    authorized = has_execution_authority(decision)
    metadata = diagnostics.get("metadata") if isinstance(diagnostics.get("metadata"), Mapping) else {}
    combo_meta = metadata.get("combo") if isinstance(metadata.get("combo"), Mapping) else {}
    selected_candidates = {
        str(symbol): {
            "weight": float(payload.get("weight", 0.0)),
            "relative_score": float(payload.get("relative_score", 0.0)),
            "abs_momentum": float(payload.get("abs_momentum", 0.0)),
        }
        for symbol, payload in dict(diagnostics.get("rotation_candidates", {})).items()
    } if authorized else {}
    planned_trend_buys = {
        str(symbol): float(amount)
        for symbol, amount in dict(diagnostics.get("planned_trend_buys", {})).items()
    } if authorized else {}
    sell_reasons = {
        str(symbol): str(reason)
        for symbol, reason in dict(diagnostics.get("sell_reasons", {})).items()
        if str(reason)
    } if authorized else {}
    return {
        "active_trend_pool": list(diagnostics.get("trend_pool", ())),
        "selected_candidates": selected_candidates,
        "eligible_buy_symbols": (
            [str(symbol) for symbol in diagnostics.get("eligible_buy_symbols", ())]
            if authorized
            else []
        ),
        "planned_trend_buys": planned_trend_buys,
        "sell_reasons": sell_reasons,
        "rotation_pool_source_version": diagnostics.get("rotation_pool_source_version"),
        "rotation_pool_source_as_of_date": diagnostics.get("rotation_pool_source_as_of_date"),
        "rotation_pool_last_month": diagnostics.get("rotation_pool_last_month"),
        "artifact_contract": dict(diagnostics.get("artifact_contract", {})),
        "risk_flags": tuple(str(flag) for flag in decision.risk_flags),
        "combo_diagnostics": {
            "base_btc_weight": float(combo_meta.get("base_btc_weight", 0.0) or 0.0),
            "base_trend_weight": float(combo_meta.get("base_trend_weight", 0.0) or 0.0),
            "effective_btc_weight": float(combo_meta.get("btc_weight", 0.0) or 0.0),
            "effective_trend_weight": float(combo_meta.get("trend_weight", 0.0) or 0.0),
            "dynamic_regime_mode": str(combo_meta.get("dynamic_regime_mode", "")),
            "regime_tier": str(combo_meta.get("regime_tier", "")),
            "regime_off": bool(metadata.get("regime_off", False)),
            "btc_sma200_ratio": metadata.get("btc_sma200_ratio"),
            "ma200_slope": metadata.get("ma200_slope"),
            "gross_exposure": float(metadata.get("gross_exposure", 0.0) or 0.0),
        },
    }
