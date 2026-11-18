"""Report-only observation of the legacy portfolio stoploss, never an order gate.

Cost coverage is counted over the strategy-member snapshot, not account value.
A MEMBER approval is not evidence that the legacy -20% PnL rule ran. Reviewed
pinned dependencies use assess_with_evidence without that legacy detector;
unknown dependencies or assessment contracts must not inherit that conclusion.
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, distribution


_REVIEWED_REVISIONS = {
    "quant-platform-kit": "155d74a615ccdebf7223787939c2dc7151d75efd",
    "crypto-strategies": "7fcca8ee0280b3e66245eb673caadda8b8b3b9a3",
}
_PROFILES = frozenset({"crypto_live_pool_rotation", "crypto_equity_combo",
                       "crypto_btc_dca", "crypto_trend_rotation"})


def _reviewed_member_gate(entrypoint, assessment) -> bool:
    if not isinstance(assessment, Mapping) or (
        assessment.get("contract_version") != "qsl.risk_gate_assessment.v2"
        or assessment.get("scope") != "MEMBER"
        or assessment.get("outcome") not in ("APPROVE", "REJECT")
    ):
        return False
    try:
        for package, revision in _REVIEWED_REVISIONS.items():
            provenance = json.loads(distribution(package).read_text("direct_url.json") or "{}")
            if provenance.get("vcs_info", {}).get("commit_id") != revision:
                return False
        # The runtime loader wraps a catalog CallableStrategyEntrypoint once.
        # Custom entrypoints and changed wrapper contracts remain unknown.
        from crypto_strategies import entrypoints
        from quant_platform_kit.common.strategy_contracts import CallableStrategyEntrypoint

        profile = entrypoint.manifest.profile
        if profile not in _PROFILES or type(entrypoint) is not CallableStrategyEntrypoint:
            return False
        inner = getattr(entrypoint._evaluate, "__self__", None)
        return (type(inner) is CallableStrategyEntrypoint
                and inner._evaluate is getattr(entrypoints, f"evaluate_{profile}", None))
    except (PackageNotFoundError, ValueError, TypeError, AttributeError, OSError):
        return False


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def _input_coverage(snapshot):
    metadata = getattr(snapshot, "metadata", None)
    positions = getattr(snapshot, "positions", None)
    equity = _finite_number(getattr(snapshot, "total_equity", None))
    if (not isinstance(metadata, Mapping) or metadata.get("snapshot_validation_errors")
            or not isinstance(positions, (tuple, list)) or equity is None or equity <= 0):
        return "invalid", None, None
    covered = 0
    for position in positions:
        quantity = _finite_number(getattr(position, "quantity", None))
        value = _finite_number(getattr(position, "market_value", None))
        raw_cost = getattr(position, "average_cost", None)
        cost = _finite_number(raw_cost)
        if (quantity is None or quantity <= 0 or value is None or value < 0
                or (raw_cost is not None and (cost is None or cost <= 0))):
            return "invalid", None, None
        covered += cost is not None
    count = len(positions)
    status = ("empty" if not count else "complete" if covered == count
              else "partial" if covered else "missing")
    return status, count, covered


def _unknown_observation():
    return {
        "evaluation_status": "unknown",
        "reason_code": "gate_evaluation_evidence_unavailable",
        "pnl_input_status": "unknown",
        "coverage_basis": "strategy_member_position_count",
        "position_count": None,
        "cost_covered_position_count": None,
    }


def unevaluated_stoploss_observation():
    """A cycle phase that has not reached strategy evaluation."""
    result = _unknown_observation()
    result.update(evaluation_status="not_evaluated", reason_code="evaluation_not_reached")
    return result


def observe_portfolio_stoploss(snapshot, *, entrypoint, assessment):
    """Return only bounded labels/counts; do not evaluate PnL or change decisions."""
    result = _unknown_observation()
    if _reviewed_member_gate(entrypoint, assessment):
        result.update(evaluation_status="not_evaluated", reason_code="legacy_rule_not_in_reviewed_gate")
    status, count, covered = _input_coverage(snapshot)
    result.update(pnl_input_status=status, position_count=count, cost_covered_position_count=covered)
    return result


def public_stoploss_observation(value):
    """Whitelist at report boundaries; malformed metadata cannot affect trading."""
    try:
        return _project_stoploss_observation(value)
    except Exception:  # noqa: BLE001 - no raw errors or reporting failures escape
        return _unknown_observation()


def _project_stoploss_observation(value):
    fallback = _unknown_observation()
    if not isinstance(value, Mapping):
        return fallback
    projected = {key: value.get(key) for key in fallback}
    if any(type(projected[key]) is not str for key in (
        "evaluation_status", "reason_code", "pnl_input_status", "coverage_basis"
    )):
        return fallback
    if ((projected["evaluation_status"], projected["reason_code"]) not in {
        ("unknown", "gate_evaluation_evidence_unavailable"),
        ("not_evaluated", "legacy_rule_not_in_reviewed_gate"),
        ("not_evaluated", "evaluation_not_reached"),
    } or projected["coverage_basis"] != fallback["coverage_basis"]
            or projected["pnl_input_status"] not in {"unknown", "invalid", "empty", "missing", "partial", "complete"}):
        return fallback
    count, covered = projected["position_count"], projected["cost_covered_position_count"]
    if projected["pnl_input_status"] in {"unknown", "invalid"}:
        return projected if count is None and covered is None else fallback
    if type(count) is not int or type(covered) is not int or not 0 <= covered <= count:
        return fallback
    expected = ("empty" if not count else "complete" if covered == count
                else "partial" if covered else "missing")
    return projected if projected["pnl_input_status"] == expected else fallback
