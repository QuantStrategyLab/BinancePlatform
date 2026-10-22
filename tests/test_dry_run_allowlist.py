import json
from dataclasses import replace
from unittest.mock import Mock

import pytest

from quant_platform_kit.common.live_continuity import (
    runtime_target_fingerprint,
    runtime_target_permits_standard_execution,
)
from quant_platform_kit.common.runtime_target import build_runtime_target
from runtime_config_support import (
    assert_standard_execution_entry_permitted,
    build_live_runtime,
    load_cycle_execution_settings,
)
from strategy_registry import (
    BINANCE_ENABLED_PROFILES,
    FULL_CYCLE_NO_SUBMIT,
    PLATFORM_POLICY,
    resolve_runtime_target_strategy,
    resolve_strategy_definition,
)


def dry_run_target_payload(**overrides):
    payload = {
        "platform_id": "binance",
        "strategy_profile": "crypto_live_pool_rotation",
        "dry_run_only": True,
        "deployment_selector": "fixture",
        "account_selector": ["fixture-account"],
        "account_scope": "fixture-scope",
        "service_name": "binance-platform",
    }
    payload.update(overrides)
    payload["live_continuity"] = {
        "state": "RECONCILE_ONLY",
        "baseline_kind": "legacy_authorized",
        "baseline_id": "synthetic-no-submit",
        "baseline_target_sha256": runtime_target_fingerprint(payload),
        "captured_at": "2026-10-22",
    }
    return payload


@pytest.fixture
def target_env(monkeypatch):
    # No credentials, authority files, or inherited account configuration.
    monkeypatch.setattr("os.environ", {})
    payload = dry_run_target_payload()
    monkeypatch.setenv("RUNTIME_TARGET_JSON", json.dumps(payload))
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "false")
    monkeypatch.setenv("BINANCE_DRY_RUN", "true")
    return payload


def test_full_cycle_config_resolves_only_approved_profile_without_production_grant(target_env):
    settings = load_cycle_execution_settings(validation_scope=FULL_CYCLE_NO_SUBMIT)
    assert settings.strategy_profile == "crypto_live_pool_rotation"
    assert settings.runtime_target.live_continuity.state == "RECONCILE_ONLY"
    assert settings.runtime_target_enabled is False
    assert not runtime_target_permits_standard_execution(settings.runtime_target)
    assert BINANCE_ENABLED_PROFILES == PLATFORM_POLICY.enabled_profiles == frozenset()
    runtime = build_live_runtime(validation_scope=FULL_CYCLE_NO_SUBMIT)
    assert runtime.dry_run is True
    assert runtime.standard_execution_permitted is False


def test_activation_uses_separate_policy_and_does_not_activate_recovery(target_env, monkeypatch):
    recovery = Mock(side_effect=AssertionError("recovery must not be read or activated"))
    monkeypatch.setattr("application.reconciliation_recovery.load_activated_target", recovery)
    target = build_runtime_target(**target_env, continuity_fingerprint_payload=target_env)
    resolved, definition = resolve_runtime_target_strategy(target, validation_scope=FULL_CYCLE_NO_SUBMIT)
    assert resolved is target
    assert definition.profile == "crypto_live_pool_rotation"
    assert resolved.live_continuity.state == "RECONCILE_ONLY"
    recovery.assert_not_called()
    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        resolve_runtime_target_strategy(target)


@pytest.mark.parametrize("profile", ["crypto_btc_dca", "crypto_trend_rotation", "crypto_equity_combo", "unapproved_profile", "crypto_leader_rotation"])
def test_unapproved_dry_run_profiles_are_rejected(target_env, monkeypatch, profile):
    payload = dry_run_target_payload(strategy_profile=profile)
    monkeypatch.setenv("RUNTIME_TARGET_JSON", json.dumps(payload))
    with pytest.raises(ValueError, match="profile_not_allowed"):
        load_cycle_execution_settings(validation_scope=FULL_CYCLE_NO_SUBMIT)


def test_standard_and_live_resolvers_do_not_consume_dry_run_grant(target_env, monkeypatch):
    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        load_cycle_execution_settings()
    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        assert_standard_execution_entry_permitted()
    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        resolve_strategy_definition("crypto_live_pool_rotation", platform_id="binance")
    payload = dry_run_target_payload(dry_run_only=False)
    monkeypatch.setenv("RUNTIME_TARGET_JSON", json.dumps(payload))
    monkeypatch.setenv("BINANCE_DRY_RUN", "false")
    with pytest.raises(ValueError, match="requires_dry_run_target"):
        load_cycle_execution_settings(validation_scope=FULL_CYCLE_NO_SUBMIT)
    # An ordinary live target with no continuity recovery grant remains denied.
    payload.pop("live_continuity")
    monkeypatch.setenv("RUNTIME_TARGET_JSON", json.dumps(payload))
    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        load_cycle_execution_settings()


@pytest.mark.parametrize("enabled", [None, "", "true", "False"])
def test_dry_run_scope_requires_explicit_disabled_runtime(target_env, monkeypatch, enabled):
    if enabled is None:
        monkeypatch.delenv("RUNTIME_TARGET_ENABLED")
    else:
        monkeypatch.setenv("RUNTIME_TARGET_ENABLED", enabled)
    with pytest.raises(ValueError, match="requires_disabled_runtime"):
        load_cycle_execution_settings(validation_scope=FULL_CYCLE_NO_SUBMIT)


def test_environment_or_missing_target_cannot_select_dry_run_grant(target_env, monkeypatch):
    monkeypatch.setenv("VALIDATION_SCOPE", FULL_CYCLE_NO_SUBMIT)
    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        load_cycle_execution_settings()
    monkeypatch.delenv("RUNTIME_TARGET_JSON")
    with pytest.raises(ValueError, match="requires_runtime_target"):
        load_cycle_execution_settings(validation_scope=FULL_CYCLE_NO_SUBMIT)


def test_unknown_scope_and_other_platform_cannot_select_dry_run_grant(target_env):
    with pytest.raises(ValueError, match="unsupported_strategy_validation_scope"):
        load_cycle_execution_settings(validation_scope="dry_run")
    target = build_runtime_target(**target_env, continuity_fingerprint_payload=target_env)
    with pytest.raises(ValueError, match="Unsupported platform_id"):
        resolve_runtime_target_strategy(replace(target, platform_id="other"), validation_scope=FULL_CYCLE_NO_SUBMIT)
    with pytest.raises(ValueError, match="requires_dry_run_target"):
        resolve_runtime_target_strategy(replace(target, dry_run_only="true"), validation_scope=FULL_CYCLE_NO_SUBMIT)


@pytest.mark.parametrize("method", [
    "order_market_buy", "order_market_sell", "create_order", "cancel_order",
    "transfer", "withdraw", "subscribe_simple_earn_flexible_product",
    "redeem_simple_earn_flexible_product",
])
def test_no_submit_broker_rejects_order_cancel_and_funding_calls(method):
    from scripts.validate_runtime_startup import _ReadOnlyBroker

    raw_client = Mock()
    broker = _ReadOnlyBroker(raw_client, symbols={"BTCUSDT"})
    with pytest.raises(RuntimeError, match="full_cycle_broker_method_forbidden"):
        getattr(broker, method)()
    assert raw_client.mock_calls == []
