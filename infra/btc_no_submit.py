"""Consume an approved standalone BTC plan without contacting an order port."""

from collections.abc import Mapping
from math import isclose, isfinite
from decimal import Decimal


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        raise ValueError("btc_validation_plan_invalid")
    return float(value)


def approved_buy_amount(decision, account_metrics, *, usdt_parts):
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
    managed_usdt = _number(account_metrics.get("cash_usdt"))
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
    if Decimal(str(amount)) > Decimal(str(managed_usdt)):
        raise ValueError("btc_validation_insufficient_managed_usdt")
    if amount > 0:
        from infra.btc_attribution import AttributionBlocked, _amount

        if not isinstance(usdt_parts, Mapping) or set(usdt_parts) != {"spot_free", "spot_locked", "earn", "managed_total"}:
            raise ValueError("btc_validation_usdt_split_unavailable")
        try:
            free = _amount(usdt_parts["spot_free"])
            _amount(usdt_parts["spot_locked"])
            _amount(usdt_parts["earn"])
            if _number(usdt_parts["managed_total"]) != managed_usdt:
                raise ValueError("btc_validation_usdt_split_unavailable")
        except (AttributionBlocked, OverflowError):
            raise ValueError("btc_validation_usdt_split_unavailable") from None
        if Decimal(str(amount)) > free:
            raise ValueError("btc_validation_insufficient_spot_usdt")
    return amount


def approved_exit_proposal(decision, account_metrics, *, price, account_btc, evidence, as_of,
                           account_scope, account_selector, broker_parts=None):
    """Build a declared-ledger preview; never grant authority or execution readiness."""
    from decimal import Decimal, InvalidOperation, ROUND_DOWN
    from infra.btc_attribution import reconstruct

    def decimal_number(value):
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError("btc_validation_exit_unsupported")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError("btc_validation_exit_unsupported") from None
        if not result.is_finite() or result < 0 or result > Decimal("1000000000"):
            raise ValueError("btc_validation_exit_unsupported")
        return result

    if evidence is None:
        raise ValueError("btc_validation_exit_unsupported")
    owned = reconstruct(evidence, as_of=as_of, account_scope=account_scope, account_selector=account_selector)
    diag = decision.diagnostics
    if not isinstance(diag, Mapping) or not isinstance(account_metrics, Mapping):
        raise ValueError("btc_validation_exit_unsupported")
    exit_plan = diag.get("zscore_exit")
    if (not isinstance(exit_plan, Mapping) or exit_plan.get("applied") is not True
            or exit_plan.get("route") not in {"risk_reduced", "risk_off"} or "no_execute" in decision.risk_flags):
        raise ValueError("btc_validation_exit_unsupported")
    current = decimal_number(diag.get("btc_value_usd"))
    target = decimal_number(diag.get("target_btc_value_usd"))
    equity = decimal_number(account_metrics.get("total_equity"))
    ratio = decimal_number(diag.get("btc_target_ratio"))
    price = decimal_number(price)
    balance = decimal_number(account_btc)
    if not isinstance(broker_parts, Mapping) or set(broker_parts) != {"spot_free", "spot_locked", "earn"}:
        raise ValueError("btc_validation_exit_unsupported")
    broker_free = decimal_number(broker_parts["spot_free"])
    broker_locked = decimal_number(broker_parts["spot_locked"])
    broker_earn = decimal_number(broker_parts["earn"])
    positions = [position for position in decision.positions if position.symbol == "BTCUSDT"]
    budgets = [budget for budget in decision.budgets if budget.name == "btc_dca_pool" and budget.symbol == "BTCUSDT"]
    if (price <= 0 or equity <= 0 or ratio > 1 or not 0 <= target < current or len(positions) != 1
            or len(budgets) != 1 or decimal_number(budgets[0].amount) != 0
            or decimal_number(diag.get("planned_investment_usd")) != 0
            or abs(decimal_number(positions[0].target_weight) - ratio) > Decimal("0.000000001")
            or abs(decimal_number(account_metrics.get("dca_value")) - current) > Decimal("0.000001")
            or abs(equity * ratio - target) > Decimal("0.000001")
            or broker_locked != 0 or broker_free != owned.spot or broker_earn != owned.earn
            or balance != broker_free + broker_locked + broker_earn
            or abs((owned.spot + owned.earn) * price - current) > Decimal("0.000001")):
        raise ValueError("btc_validation_exit_unsupported")
    try:
        quantity = ((current - target) / price).quantize(Decimal("0.000000000000000001"), rounding=ROUND_DOWN)
    except (InvalidOperation, OverflowError, ZeroDivisionError):
        raise ValueError("btc_validation_exit_unsupported") from None
    if quantity <= 0 or quantity > owned.spot or quantity * price > current - target:
        raise ValueError("btc_validation_exit_unsupported")
    return {"action": "sell", "symbol": "BTCUSDT", "quantity": format(quantity.normalize(), "f"),
            "proposal_only": True, "execution_ready": False,
            "source_authenticated": False, "completeness_authenticated": False}
