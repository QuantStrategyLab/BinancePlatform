"""Restore only the existing QPK portfolio-loss rejection at the platform boundary."""

from dataclasses import replace

from quant_platform_kit.risk.gate import _STOP_LOSS_THRESHOLD
from quant_platform_kit.risk.portfolio_diagnostics import compute_unrealized_pnl_pct


def apply_portfolio_loss_stoploss(decision, snapshot):
    """Keep the pinned rule's strict threshold, equity basis and unknown semantics.

    Calling the entire legacy gate would also enable unrelated concentration,
    mandate and loss-streak rules. The MEMBER assessment remains authoritative;
    this additional rejection can only remove execution permission.
    """
    pnl = compute_unrealized_pnl_pct(snapshot)
    if pnl is None or pnl >= _STOP_LOSS_THRESHOLD:
        return decision
    return replace(
        decision,
        positions=(),
        budgets=(),
        risk_flags=tuple(dict.fromkeys((*decision.risk_flags, "rejected:stop_loss"))),
    )
