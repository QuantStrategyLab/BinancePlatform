"""Synthetic regressions for the approved Spot-only strategy boundary."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from infra.binance_runtime import ensure_asset_available_runtime
from runtime_support import ExecutionIntegrityError, runtime_call_client, build_execution_report, ExecutionRuntime


def test_spot_shortfall_never_redeems_earn():
    client = Mock()
    client.get_asset_balance.return_value = {"free": "2", "locked": "0"}
    client.get_simple_earn_flexible_product_position.return_value = {"rows": [{"productId": "synthetic", "totalAmount": "100"}]}
    submit = Mock()
    result = ensure_asset_available_runtime(
        SimpleNamespace(client=client, dry_run=False), {"redemption_subscription_intents": []}, "BTC", 3, [],
        runtime_call_client_fn=submit, append_log_fn=Mock(), runtime_notify_fn=Mock(),
        translate_fn=lambda key, **kwargs: key, sleep_fn=Mock(),
    )
    assert result is False
    submit.assert_not_called()
    client.get_simple_earn_flexible_product_position.assert_not_called()


@pytest.mark.parametrize("method,effect", [
    ("subscribe_simple_earn_flexible_product", "earn_subscribe"),
    ("redeem_simple_earn_flexible_product", "earn_redeem"),
    ("subscribe_simple_earn_flexible_product", "other"),
])
def test_direct_earn_submission_is_rejected_before_state_or_broker_access(method, effect):
    client, load, write = Mock(), Mock(), Mock()
    runtime = ExecutionRuntime(client=client, dry_run=False, state_loader=load, state_writer=write)
    with pytest.raises(ExecutionIntegrityError, match="earn_outside_strategy_scope"):
        runtime_call_client(runtime, build_execution_report(runtime), method_name=method,
                            effect_type=effect, payload={"productId": "synthetic", "amount": 1})
    assert client.mock_calls == []
    load.assert_not_called()
    write.assert_not_called()


@pytest.mark.parametrize("free,locked", [("NaN", "0"), ("1", "Infinity"), ("-1", "0"), ("1", "-1"), ("1e400", "0")])
def test_invalid_spot_quantities_cannot_be_used_for_budget(free, locked):
    from runtime_support import get_spot_balance
    client = Mock()
    client.get_asset_balance.return_value = {"free": free, "locked": locked}
    with pytest.raises(ExecutionIntegrityError, match="spot_balance_lookup_failed"):
        get_spot_balance(client, "USDT")
    client.get_simple_earn_flexible_product_position.assert_not_called()


def test_spot_valuation_includes_locked_but_available_funds_do_not():
    from runtime_support import get_spot_balance
    client = Mock()
    client.get_asset_balance.return_value = {"asset": "USDT", "free": "2", "locked": "3"}
    assert get_spot_balance(client, "USDT") == 5
    assert get_spot_balance(client, "USDT", free_only=True) == 2
    client.get_simple_earn_flexible_product_position.assert_not_called()


@pytest.mark.parametrize("scope", [None, "spot_plus_earn", "", "SPOT"])
def test_legacy_scope_is_rejected_before_normalization_or_any_state_write(scope):
    from application.state_service import load_cycle_state
    raw = {"balance_scope": scope, "last_balance_snapshot": {"BTC": 5},
           "accounting_rebase": {"historical_difference_unresolved": True}}
    import copy
    before = copy.deepcopy(raw)
    hooks = {name: Mock() for name in (
        "resolve_runtime_trend_pool", "normalize_trade_state", "update_trend_pool_state",
        "runtime_set_trade_state", "get_runtime_trend_universe", "append_report_error", "trend_universe_setter")}
    with pytest.raises(ExecutionIntegrityError, match="spot_scope_migration_required"):
        load_cycle_state(SimpleNamespace(), {}, False, state_loader=lambda **_: raw, **hooks)
    assert raw == before
    assert all(not hook.called for hook in hooks.values())


def test_normalization_preserves_explicit_scope_but_never_upgrades_legacy():
    from trade_state_support import normalize_trade_state
    kwargs = dict(trend_universe={}, last_good_payload_key="last_good", action_history_key="actions", retired_positions_key="retired")
    assert normalize_trade_state({"balance_scope": "spot"}, **kwargs)["balance_scope"] == "spot"
    assert normalize_trade_state({"last_balance_snapshot": {"BTC": 5}}, **kwargs)["balance_scope"] == ""
