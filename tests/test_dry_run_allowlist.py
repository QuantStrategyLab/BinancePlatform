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


@pytest.mark.parametrize("profile", ["crypto_equity_combo", "unapproved_profile", "crypto_leader_rotation"])
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


@pytest.mark.parametrize("profile", ["crypto_live_pool_rotation", "crypto_btc_dca", "crypto_trend_rotation"])
@pytest.mark.parametrize("enabled", [None, "", "true", "False"])
def test_dry_run_scope_requires_explicit_disabled_runtime(target_env, monkeypatch, enabled, profile):
    monkeypatch.setenv("RUNTIME_TARGET_JSON", json.dumps(dry_run_target_payload(strategy_profile=profile)))
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


def test_real_cli_forwards_scope_through_builder_activation_and_loader(target_env, monkeypatch, capsys):
    import runpy
    import sys
    from pathlib import Path
    import main

    # No risk authority or broker: successful admission must reach the next
    # existing guard, without replacing any builder, resolver or loader.
    connector = Mock(side_effect=AssertionError("broker must not be reached"))
    monkeypatch.setattr(main, "qpk_connect_client", connector)
    monkeypatch.setattr(sys, "argv", ["validate_runtime_startup.py", "--full-cycle"])
    script = Path(__file__).resolve().parents[1] / "scripts/validate_runtime_startup.py"
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(script), run_name="__main__")
    assert error.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason_code"] == "full_cycle_risk_authority_missing"
    assert main.STRATEGY_RUNTIME.profile == "crypto_live_pool_rotation"
    connector.assert_not_called()


def test_real_default_and_startup_entries_cannot_use_scoped_collection(target_env, monkeypatch):
    import main
    from scripts.validate_runtime_startup import validate_startup
    from strategy_loader import load_strategy_entrypoint_for_profile
    from strategy_runtime import load_strategy_runtime

    monkeypatch.setenv("VALIDATION_SCOPE", FULL_CYCLE_NO_SUBMIT)
    target = build_runtime_target(**target_env, continuity_fingerprint_payload=target_env)
    for call in (
        lambda: main.build_live_runtime(),
        validate_startup,
        lambda: main._activate_execution_strategy_runtime(target.strategy_profile, runtime_target=target),
        lambda: load_strategy_runtime(target.strategy_profile, runtime_target=target),
        lambda: load_strategy_entrypoint_for_profile(target.strategy_profile, runtime_target=target),
    ):
        with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
            call()


@pytest.mark.parametrize("scope", [FULL_CYCLE_NO_SUBMIT, "dry_run"])
def test_loader_cannot_apply_scope_without_explicit_target(scope):
    from strategy_runtime import load_strategy_runtime

    with pytest.raises(ValueError, match="requires_runtime_target|unsupported_strategy_validation_scope"):
        load_strategy_runtime("crypto_live_pool_rotation", validation_scope=scope)


def test_main_builder_rejects_unknown_scope_or_live_target(target_env, monkeypatch):
    import main

    with pytest.raises(ValueError, match="unsupported_strategy_validation_scope"):
        main.build_live_runtime(validation_scope="dry_run")
    payload = dry_run_target_payload(dry_run_only=False)
    monkeypatch.setenv("RUNTIME_TARGET_JSON", json.dumps(payload))
    monkeypatch.setenv("BINANCE_DRY_RUN", "false")
    with pytest.raises(ValueError, match="requires_dry_run_target"):
        main.build_live_runtime(validation_scope=FULL_CYCLE_NO_SUBMIT)


def test_pinned_crypto_dependency_exposes_separate_collection():
    import tomllib
    from pathlib import Path
    from crypto_strategies.runtime_allowlist import DRY_RUN_RUNTIME_ALLOWLIST, RUNTIME_SELECTABLE_ALLOWLIST_V1

    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    lock = tomllib.loads((root / "uv.lock").read_text())
    revision = tomllib.loads((root / "qsl.toml").read_text())["qsl"]["requires"]["crypto_strategies"]
    requirement = next(item for item in project["project"]["dependencies"] if item.startswith("crypto-strategies @"))
    assert requirement.endswith("@" + revision)
    package = next(item for item in lock["package"] if item["name"] == "crypto-strategies")
    assert package["source"]["git"].endswith("?rev=" + revision + "#" + revision)
    assert DRY_RUN_RUNTIME_ALLOWLIST == frozenset({"crypto_live_pool_rotation", "crypto_btc_dca", "crypto_trend_rotation"})
    assert RUNTIME_SELECTABLE_ALLOWLIST_V1 == frozenset()


def test_default_cycle_does_not_inherit_scope_from_built_runtime(target_env):
    import main

    runtime = main.build_live_runtime(validation_scope=FULL_CYCLE_NO_SUBMIT)
    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        main.execute_cycle(runtime)
    with pytest.raises(ValueError, match="unsupported_strategy_validation_scope"):
        main.execute_cycle(runtime, validation_scope="dry_run")


@pytest.mark.parametrize("profile", ["crypto_btc_dca", "crypto_trend_rotation"])
def test_scoped_profiles_use_real_resolver_and_loader_without_production_grant(target_env, monkeypatch, profile):
    import main
    from strategy_loader import load_strategy_entrypoint_for_profile

    payload = dry_run_target_payload(strategy_profile=profile)
    monkeypatch.setenv("RUNTIME_TARGET_JSON", json.dumps(payload))
    target = build_runtime_target(**payload, continuity_fingerprint_payload=payload)
    settings = load_cycle_execution_settings(validation_scope=FULL_CYCLE_NO_SUBMIT)
    assert settings.strategy_profile == profile
    assert settings.runtime_target_enabled is False
    entrypoint = load_strategy_entrypoint_for_profile(profile, runtime_target=target, validation_scope=FULL_CYCLE_NO_SUBMIT)
    assert entrypoint.manifest.profile == profile
    runtime = main.build_live_runtime(validation_scope=FULL_CYCLE_NO_SUBMIT)
    assert runtime.strategy_runtime.profile == profile
    assert runtime.standard_execution_permitted is False
    if profile == "crypto_trend_rotation":
        runtime.trend_pool_payload = {}
        connector = Mock(side_effect=AssertionError("broker must not be called"))
        monkeypatch.setattr(main, "qpk_connect_client", connector)
        with pytest.raises(ValueError, match="Injected trend_pool_payload failed validation"):
            main.resolve_runtime_trend_pool(runtime, {})
        connector.assert_not_called()
    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        resolve_strategy_definition(profile, platform_id="binance")
    with pytest.raises(ValueError, match="Unsupported STRATEGY_PROFILE"):
        load_cycle_execution_settings()
    with pytest.raises(ValueError, match="runtime_strategy_target_mismatch"):
        load_strategy_entrypoint_for_profile("crypto_live_pool_rotation", runtime_target=target, validation_scope=FULL_CYCLE_NO_SUBMIT)
    with pytest.raises(ValueError, match="Unsupported platform_id"):
        resolve_runtime_target_strategy(replace(target, platform_id="other"), validation_scope=FULL_CYCLE_NO_SUBMIT)
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "true")
    with pytest.raises(ValueError, match="requires_disabled_runtime"):
        load_cycle_execution_settings(validation_scope=FULL_CYCLE_NO_SUBMIT)
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "false")
    monkeypatch.setenv("BINANCE_DRY_RUN", "false")
    payload["dry_run_only"] = False
    payload.pop("live_continuity")
    monkeypatch.setenv("RUNTIME_TARGET_JSON", json.dumps(payload))
    with pytest.raises(ValueError, match="requires_dry_run_target"):
        load_cycle_execution_settings(validation_scope=FULL_CYCLE_NO_SUBMIT)
