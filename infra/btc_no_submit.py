"""Consume an approved standalone BTC plan without contacting an order port."""

from collections.abc import Mapping
from math import isclose, isfinite


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        raise ValueError("btc_validation_plan_invalid")
    return float(value)


def approved_buy_amount(decision, account_metrics):
    """Validate the planned increment; callers must first enforce risk approval.

    This is a validation proposal, not an order or an execution simulation.
    Exit plans are unsupported and must not be interpreted as no-op plans.
    """
    diagnostics = decision.diagnostics
    if not isinstance(diagnostics, Mapping) or not isinstance(account_metrics, Mapping):
        raise ValueError("btc_validation_plan_invalid")
    exit_plan = diagnostics.get("zscore_exit")
    if not isinstance(exit_plan, Mapping) or type(exit_plan.get("applied")) is not bool:
        raise ValueError("btc_validation_plan_invalid")
    amount = _number(diagnostics.get("planned_investment_usd"))
    equity = _number(account_metrics.get("total_equity"))
    current = _number(account_metrics.get("dca_value"))
    ratio = _number(diagnostics.get("btc_target_ratio"))
    reported_current = _number(diagnostics.get("btc_value_usd"))
    reported_target = _number(diagnostics.get("target_btc_value_usd"))
    route = exit_plan.get("route")
    if not isinstance(route, str) or route not in {"", "normal", "risk_on", "risk_reduced", "risk_off"}:
        raise ValueError("btc_validation_plan_invalid")
    if route in {"risk_reduced", "risk_off"} or reported_target < reported_current:
        raise ValueError("btc_validation_exit_unsupported")
    if exit_plan["applied"]:
        exposure = _number(exit_plan.get("target_btc_exposure"))
        if route not in {"normal", "risk_on"} or exposure != 1.0:
            raise ValueError("btc_validation_exit_unsupported")
    if equity <= 0 or ratio > 1:
        raise ValueError("btc_validation_plan_invalid")
    positions = [p for p in decision.positions if p.symbol == "BTCUSDT"]
    budgets = [b for b in decision.budgets if b.name == "btc_dca_pool" and b.symbol == "BTCUSDT"]
    if len(positions) != 1 or len(budgets) != 1:
        raise ValueError("btc_validation_plan_invalid")
    target = _number(positions[0].target_weight)
    budget = _number(budgets[0].amount)
    if (not isclose(target, ratio, rel_tol=1e-9, abs_tol=1e-9)
            or not isclose(budget, amount, rel_tol=1e-9, abs_tol=1e-6)
            or not isclose(reported_current, current, rel_tol=1e-9, abs_tol=1e-6)
            or not isclose(reported_target, equity * ratio, rel_tol=1e-9, abs_tol=1e-6)
            or not isclose(equity * ratio, current + amount, rel_tol=1e-9, abs_tol=1e-6)
            or (("no_execute" in decision.risk_flags) != (amount == 0.0))):
        raise ValueError("btc_validation_plan_invalid")
    return amount
