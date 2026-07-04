from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from quant_platform_kit.strategy_contracts import StrategyDecision


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
    budgets = _budget_map(decision)
    positions = _position_weight_map(decision)
    trend_target_ratio = float(
        diagnostics.get(
            "trend_target_ratio",
            sum(weight for symbol, weight in positions.items() if symbol != "BTCUSDT"),
        )
    )
    return {
        "total_equity": float(account_metrics["total_equity"]),
        "trend_val": float(account_metrics["trend_value"]),
        "dca_val": float(account_metrics["dca_value"]),
        "btc_target_ratio": float(diagnostics.get("btc_target_ratio", positions.get("BTCUSDT", 0.0))),
        "trend_target_ratio": trend_target_ratio,
        "trend_usdt_pool": float(budgets.get("trend_rotation_pool", 0.0)),
        "dca_usdt_pool": float(budgets.get("btc_core_dca_pool", 0.0)),
        "btc_base_order_usdt": float(diagnostics.get("btc_base_order_usdt", 0.0)),
    }


def map_strategy_decision_to_rotation_plan(decision: StrategyDecision) -> dict[str, Any]:
    diagnostics = dict(decision.diagnostics)
    metadata = diagnostics.get("metadata") if isinstance(diagnostics.get("metadata"), Mapping) else {}
    combo_meta = metadata.get("combo") if isinstance(metadata.get("combo"), Mapping) else {}
    selected_candidates = {
        str(symbol): {
            "weight": float(payload.get("weight", 0.0)),
            "relative_score": float(payload.get("relative_score", 0.0)),
            "abs_momentum": float(payload.get("abs_momentum", 0.0)),
        }
        for symbol, payload in dict(diagnostics.get("rotation_candidates", {})).items()
    }
    planned_trend_buys = {
        str(symbol): float(amount)
        for symbol, amount in dict(diagnostics.get("planned_trend_buys", {})).items()
    }
    sell_reasons = {
        str(symbol): str(reason)
        for symbol, reason in dict(diagnostics.get("sell_reasons", {})).items()
        if str(reason)
    }
    return {
        "active_trend_pool": list(diagnostics.get("trend_pool", ())),
        "selected_candidates": selected_candidates,
        "eligible_buy_symbols": [str(symbol) for symbol in diagnostics.get("eligible_buy_symbols", ())],
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
