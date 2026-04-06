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
    selected_candidates = {
        str(symbol): {
            "weight": float(payload["weight"]),
            "relative_score": float(payload["relative_score"]),
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
    }
