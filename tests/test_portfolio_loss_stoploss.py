from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from quant_platform_kit import PortfolioSnapshot, Position
from quant_platform_kit.common.strategy_contracts import BudgetIntent, PositionTarget, StrategyDecision
from quant_platform_kit.risk.gate import _STOP_LOSS_THRESHOLD

from portfolio_loss_stoploss import apply_portfolio_loss_stoploss


def snapshot(pnl, *, cost=100.0):
    return PortfolioSnapshot(
        as_of=datetime.now(timezone.utc), total_equity=100.0,
        positions=(Position(symbol="BTCUSDT", quantity=1.0, market_value=100.0 + pnl * 100,
                            average_cost=cost),),
    )


def decision():
    return StrategyDecision(
        positions=(PositionTarget(symbol="BTCUSDT", target_weight=0.2),),
        budgets=(BudgetIntent(name="btc_core_dca_pool", amount=20.0),),
        risk_flags=("existing:flag",),
        diagnostics={"member_risk_assessment": {"outcome": "APPROVE"},
                     "strategy_stop_evaluation": {"outcome": "CLEAR"}},
    )


@pytest.mark.parametrize("pnl", [-0.19, -0.20, 0.0, 0.20])
def test_at_or_above_original_threshold_preserves_exact_decision(pnl):
    original = decision()
    assert apply_portfolio_loss_stoploss(original, snapshot(pnl)) is original


def test_below_original_threshold_rejects_without_replacing_member_evidence():
    original = decision()
    result = apply_portfolio_loss_stoploss(original, snapshot(-0.21))
    assert result.positions == result.budgets == ()
    assert result.risk_flags == ("existing:flag", "rejected:stop_loss")
    assert result.diagnostics == original.diagnostics
    assert original.positions and original.budgets
    from decision_mapper import map_strategy_decision_to_allocation
    allocation = map_strategy_decision_to_allocation(result, account_metrics={
        "total_equity": 100, "trend_value": 0, "dca_value": 79,
    })
    assert allocation["execution_permitted"] is False


def test_missing_cost_keeps_existing_unknown_semantics():
    original = decision()
    assert apply_portfolio_loss_stoploss(original, snapshot(-0.50, cost=None)) is original


def test_existing_threshold_is_reused_and_other_legacy_checks_are_not_enabled():
    assert _STOP_LOSS_THRESHOLD == -0.20
    original = decision()
    # This flag would reject via the whole legacy gate; only stoploss is wired.
    original.diagnostics["consecutive_losses"] = 100
    with patch("portfolio_loss_stoploss.compute_unrealized_pnl_pct", return_value=None):
        assert apply_portfolio_loss_stoploss(original, snapshot(0)) is original
