from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math
from typing import Any

from quant_platform_kit import PortfolioSnapshot
from quant_platform_kit.risk.contracts import CandidateRiskIdentity, RiskGateAssessment
from quant_platform_kit.risk.gate import assess_with_evidence
from quant_platform_kit.strategy_contracts import StrategyDecision


_MAX_AUTHORITY_AGE_SECONDS = 300.0
_MAX_ASSESSMENT_SKEW_SECONDS = 5.0


@dataclass(frozen=True)
class ExecutionAuthority:
    decision: StrategyDecision
    portfolio_snapshot: PortfolioSnapshot
    candidate_identity: CandidateRiskIdentity
    member_assessment: RiskGateAssessment
    account_assessment: RiskGateAssessment


def _validated_assessment(value: Any) -> RiskGateAssessment | None:
    if type(value) is not RiskGateAssessment:
        return None
    try:
        payload = asdict(value)
        supplied_digest = payload.pop("assessment_sha256")
        rebuilt = RiskGateAssessment(**payload)
    except (TypeError, ValueError):
        return None
    if rebuilt.assessment_sha256 != supplied_digest:
        return None
    return rebuilt


def _assessment_time(value: str) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def is_execution_authority_valid(
    authority: Any,
    *,
    decision: StrategyDecision | None = None,
) -> bool:
    if type(authority) is not ExecutionAuthority:
        return False
    if type(authority.decision) is not StrategyDecision:
        return False
    if decision is not None and authority.decision is not decision:
        return False
    if type(authority.portfolio_snapshot) is not PortfolioSnapshot:
        return False
    if type(authority.candidate_identity) is not CandidateRiskIdentity:
        return False

    member = _validated_assessment(authority.member_assessment)
    account = _validated_assessment(authority.account_assessment)
    if member is None or account is None:
        return False
    if member.scope != "MEMBER" or account.scope != "ACCOUNT":
        return False
    if member.outcome != "APPROVE" or account.outcome != "APPROVE":
        return False
    if member.reason_codes or account.reason_codes:
        return False
    candidate_digest = authority.candidate_identity.candidate_sha256
    if member.candidate_identity_sha256 != candidate_digest:
        return False
    if account.candidate_identity_sha256 != candidate_digest:
        return False
    if member.decision_digest_sha256 != account.decision_digest_sha256:
        return False
    if member.portfolio_snapshot_digest_sha256 != account.portfolio_snapshot_digest_sha256:
        return False
    if member.contract_version != account.contract_version:
        return False
    if (member.policy_id, member.policy_version) != (account.policy_id, account.policy_version):
        return False
    if (
        member.qpk_source_revision,
        member.mandate_id,
        member.mandate_version,
        member.mandate_authority_receipt_sha256,
        member.mandate_scope,
    ) != (
        account.qpk_source_revision,
        account.mandate_id,
        account.mandate_version,
        account.mandate_authority_receipt_sha256,
        account.mandate_scope,
    ):
        return False
    if member.mandate_authority_receipt_sha256 != authority.candidate_identity.authority_receipt_sha256:
        return False

    member_time = _assessment_time(member.evaluated_at)
    account_time = _assessment_time(account.evaluated_at)
    if member_time is None or account_time is None:
        return False
    now = datetime.now(timezone.utc)
    for evaluated_at in (member_time, account_time):
        age_seconds = (now - evaluated_at).total_seconds()
        if age_seconds < -_MAX_ASSESSMENT_SKEW_SECONDS or age_seconds > _MAX_AUTHORITY_AGE_SECONDS:
            return False
    return abs((member_time - account_time).total_seconds()) <= _MAX_ASSESSMENT_SKEW_SECONDS


def build_execution_authority(
    decision: StrategyDecision,
    *,
    portfolio_snapshot: PortfolioSnapshot,
    mandate_provenance: Mapping[str, Any] | None,
    candidate_identity: CandidateRiskIdentity | None,
    market_data: Mapping[str, Any],
) -> ExecutionAuthority | None:
    if type(decision) is not StrategyDecision:
        return None
    if type(portfolio_snapshot) is not PortfolioSnapshot:
        return None
    if not isinstance(mandate_provenance, Mapping):
        return None
    if type(candidate_identity) is not CandidateRiskIdentity:
        return None
    member = assess_with_evidence(
        decision,
        portfolio_snapshot,
        scope="MEMBER",
        mandate_provenance=mandate_provenance,
        market_data=market_data,
        candidate_identity=candidate_identity,
    ).assessment
    account = assess_with_evidence(
        decision,
        portfolio_snapshot,
        scope="ACCOUNT",
        mandate_provenance=mandate_provenance,
        market_data=market_data,
        candidate_identity=candidate_identity,
    ).assessment
    authority = ExecutionAuthority(
        decision=decision,
        portfolio_snapshot=portfolio_snapshot,
        candidate_identity=candidate_identity,
        member_assessment=member,
        account_assessment=account,
    )
    return authority if is_execution_authority_valid(authority, decision=decision) else None


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
    execution_authority: ExecutionAuthority | None = None,
) -> dict[str, float]:
    authorized = is_execution_authority_valid(execution_authority, decision=decision)
    diagnostics = dict(decision.diagnostics) if authorized else {}
    budgets = _budget_map(decision) if authorized else {}
    positions = _position_weight_map(decision) if authorized else {}
    trend_target_ratio = sum(weight for symbol, weight in positions.items() if symbol != "BTCUSDT")
    btc_base_order = float(diagnostics.get("btc_base_order_usdt", 0.0) or 0.0)
    if not math.isfinite(btc_base_order) or btc_base_order < 0.0:
        btc_base_order = 0.0
    btc_base_order = min(btc_base_order, budgets.get("btc_core_dca_pool", 0.0))
    return {
        "total_equity": float(account_metrics["total_equity"]),
        "trend_val": float(account_metrics["trend_value"]),
        "dca_val": float(account_metrics["dca_value"]),
        "btc_target_ratio": float(positions.get("BTCUSDT", 0.0)),
        "trend_target_ratio": trend_target_ratio,
        "trend_usdt_pool": float(budgets.get("trend_rotation_pool", 0.0)),
        "dca_usdt_pool": float(budgets.get("btc_core_dca_pool", 0.0)),
        "btc_base_order_usdt": btc_base_order,
    }


def map_strategy_decision_to_rotation_plan(
    decision: StrategyDecision,
    *,
    execution_authority: ExecutionAuthority | None = None,
) -> dict[str, Any]:
    if not is_execution_authority_valid(execution_authority, decision=decision):
        return {
            "active_trend_pool": [],
            "selected_candidates": {},
            "eligible_buy_symbols": [],
            "planned_trend_buys": {},
            "sell_reasons": {},
            "rotation_pool_source_version": None,
            "rotation_pool_source_as_of_date": None,
            "rotation_pool_last_month": None,
            "artifact_contract": {},
            "risk_flags": tuple(str(flag) for flag in decision.risk_flags),
            "combo_diagnostics": {},
        }
    diagnostics = dict(decision.diagnostics)
    metadata = diagnostics.get("metadata") if isinstance(diagnostics.get("metadata"), Mapping) else {}
    combo_meta = metadata.get("combo") if isinstance(metadata.get("combo"), Mapping) else {}
    target_weights = _position_weight_map(decision)
    target_weights.pop("BTCUSDT", None)
    selected_candidates = {
        str(symbol): {
            "weight": target_weights[str(symbol)],
            "relative_score": float(payload.get("relative_score", 0.0)),
            "abs_momentum": float(payload.get("abs_momentum", 0.0)),
        }
        for symbol, payload in dict(diagnostics.get("rotation_candidates", {})).items()
        if str(symbol) in target_weights
    }
    current_values = {
        str(position.symbol): float(position.market_value or 0.0)
        for position in (() if execution_authority is None else execution_authority.portfolio_snapshot.positions)
    }
    total_equity = 0.0 if execution_authority is None else float(execution_authority.portfolio_snapshot.total_equity)
    remaining_budget = _budget_map(decision).get("trend_rotation_pool", 0.0)
    planned_trend_buys: dict[str, float] = {}
    for symbol, target_weight in target_weights.items():
        buy_amount = max(0.0, target_weight * total_equity - current_values.get(symbol, 0.0))
        buy_amount = min(buy_amount, remaining_budget)
        if buy_amount > 0.0:
            planned_trend_buys[symbol] = buy_amount
            remaining_budget -= buy_amount
    eligible_buy_symbols = list(planned_trend_buys)
    sell_reasons = {
        symbol: str(dict(diagnostics.get("sell_reasons", {})).get(symbol) or "strategy_target_exit")
        for symbol, current_value in current_values.items()
        if symbol != "BTCUSDT" and current_value > 0.0 and target_weights.get(symbol, 0.0) <= 0.0
    }
    return {
        "active_trend_pool": list(diagnostics.get("trend_pool", ())),
        "selected_candidates": selected_candidates,
        "eligible_buy_symbols": eligible_buy_symbols,
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
