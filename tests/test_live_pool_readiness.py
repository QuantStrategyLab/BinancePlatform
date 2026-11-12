from types import SimpleNamespace

import pytest

from application.execution_receipt_adapter import attach_execution_receipt_from_report
from scripts.validate_runtime_startup import FullCycleValidationError, _live_pool_readiness


def _runtime(source="fresh_upstream", *, fresh=True, injected=False):
    return SimpleNamespace(
        strategy_profile="crypto_live_pool_rotation",
        runtime_target=SimpleNamespace(execution_mode="live"),
        trend_pool_payload={} if injected else None,
        trend_pool_contract={"source_kind": source, "is_fresh": fresh},
    )


def _report():
    report = {
        "platform": "binance",
        "strategy_profile": "crypto_live_pool_rotation",
        "dry_run": True,
        "status": "ok",
        "runtime_target": {"execution_mode": "live"},
        "binance_live_authority": {
            "strategy_revision": "a" * 40, "runtime_revision": "b" * 40, "config_sha256": "c" * 64,
        },
    }
    assert attach_execution_receipt_from_report(report)
    return report


@pytest.mark.parametrize("runtime", [
    _runtime("last_known_good"),
    _runtime(fresh=False),
    _runtime(injected=True),
    SimpleNamespace(
        strategy_profile="crypto_live_pool_rotation",
        runtime_target=SimpleNamespace(execution_mode="live"),
        trend_pool_payload=None,
        trend_pool_contract=None,
    ),
])
def test_cached_stale_injected_or_missing_pool_blocks_live_readiness(runtime):
    with pytest.raises(FullCycleValidationError, match="full_cycle_trend_pool_not_fresh"):
        _live_pool_readiness(runtime, _report())


def test_fresh_live_pool_requires_a_bound_no_submit_receipt():
    report = _report()
    assert _live_pool_readiness(_runtime(), report) == {
        "trend_pool_source": "fresh_upstream", "execution_receipt_checked": True,
    }
    report["binance_live_authority"]["strategy_revision"] = "d" * 40
    with pytest.raises(FullCycleValidationError, match="full_cycle_execution_receipt_invalid"):
        _live_pool_readiness(_runtime(), report)
