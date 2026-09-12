import os
import hashlib
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from unittest.mock import patch

import pytest


def test_startup_validation_cannot_run_with_execution_enabled(monkeypatch):
    from scripts.validate_runtime_startup import validate_startup
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "true")
    with pytest.raises(ValueError, match="requires_disabled_runtime"):
        validate_startup()


def test_startup_validation_loads_without_broker_credentials_or_execution(monkeypatch):
    from scripts.validate_runtime_startup import validate_startup
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "false")
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    calls = []
    def build():
        calls.append("build")
        assert not os.getenv("BINANCE_API_KEY") and not os.getenv("BINANCE_API_SECRET")
        return SimpleNamespace(standard_execution_permitted=False, strategy_profile="fixture")
    def load_cycle_state(runtime, report, allow_new):
        calls.append("state_load")
        assert runtime.state_writer({"normalized": True}) is True
        assert report == {"state_write_intents": []}
        assert allow_new is False
        return (
            {},
            {"symbols": ["ETHUSDT", "ZECUSDT"]},
            {
                "ETHUSDT": {"base_asset": "ETH"},
                "SOLUSDT": {"base_asset": "SOL", "valuation_only": True},
            },
            True,
        )
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(
        build_live_runtime=build,
        build_execution_report=lambda _runtime: {"state_write_intents": []},
        _load_cycle_state=load_cycle_state,
    ))
    result = validate_startup()
    assert calls == ["build", "state_load"]
    assert result["status"] == "passed"
    assert result["execution_permitted"] is False
    assert result["risk_materials_present"] is False
    assert result["validation_scope"] == "startup_only"
    assert result["recovery_ready"] is False
    assert result["state_load_checked"] is True
    assert result["source_pool_symbol_count"] == 2
    assert result["managed_asset_count"] == 5
    assert result["strategy_candidate_count"] == 1
    assert result["valuation_only_count"] == 1


def test_startup_validation_rejects_broker_credentials(monkeypatch):
    from scripts.validate_runtime_startup import validate_startup
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "false")
    monkeypatch.setenv("BINANCE_API_KEY", "synthetic")
    with pytest.raises(ValueError, match="broker_credentials_forbidden"):
        validate_startup()


def test_full_cycle_uses_live_builder_and_closes_all_write_ports(monkeypatch, tmp_path):
    import main
    import run_cycle_replay
    from live_risk_authority import config_sha256
    from quant_platform_kit.common.runtime_target import build_runtime_target
    from strategy_runtime import load_research_only_strategy_runtime
    import strategy_registry
    from scripts.validate_runtime_startup import validate_full_cycle

    now = datetime(2026, 3, 15, tzinfo=timezone.utc)
    target = build_runtime_target(
        platform_id="binance",
        strategy_profile="crypto_live_pool_rotation",
        dry_run_only=False,
        deployment_selector="fixture",
        account_selector="fixture-account",
        account_scope="fixture-scope",
        service_name="binance-platform",
    )
    loaded = load_research_only_strategy_runtime(target.strategy_profile)
    runner_revision = "b" * 40
    payload = {
        "decision": "APPROVE",
        "authority_scope": "LIVE",
        "runtime_target": {
            "platform_id": "binance",
            "strategy_profile": "crypto_live_pool_rotation",
            "account_scope": "fixture-scope",
            "account_selector": ["fixture-account"],
            "deployment_selector": "fixture",
        },
        "strategy_revision": "d" * 40,
        "runner_revision": runner_revision,
        "config_sha256": config_sha256({**loaded.merged_runtime_config, **loaded.runtime_overrides}),
        "continuous_inputs_allowed": True,
        "mandate": {
            "mandate_id": "synthetic_full_cycle_fixture",
            "mandate_version": "fixture-v1",
            "effective_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            "max_snapshot_age_seconds": 300,
            "effective_exposure_cap": 1.0,
            "loss_budget": 1000.0,
            "product_caps": {symbol: 1.0 for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "LTCUSDT", "BCHUSDT")},
            "nominal_caps": {symbol: 1.0 for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "LTCUSDT", "BCHUSDT")},
            "product_leverage_factors": {symbol: 1 for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "LTCUSDT", "BCHUSDT")},
            "allowed_nonzero_assets": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "LTCUSDT", "BCHUSDT"],
        },
    }
    authority_path = tmp_path / "authority.json"
    raw = (json.dumps(payload, sort_keys=True) + "\n").encode()
    authority_path.write_bytes(raw)
    authority_env = {
        "BINANCE_RISK_AUTHORITY_FILE": str(authority_path),
        "BINANCE_RISK_AUTHORITY_SHA256": hashlib.sha256(raw).hexdigest(),
        "BINANCE_RISK_AUTHORITY_SOURCE_REVISION": "a" * 40,
    }
    monkeypatch.setattr(
        strategy_registry,
        "PLATFORM_POLICY",
        replace(strategy_registry.PLATFORM_POLICY, enabled_profiles=frozenset({"crypto_live_pool_rotation"})),
    )
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "false")
    monkeypatch.setenv("STRATEGY_PROFILE", "crypto_live_pool_rotation")
    monkeypatch.setenv("BINANCE_DRY_RUN", "false")
    monkeypatch.setenv("DEPLOYMENT_SELECTOR", "fixture")
    monkeypatch.setenv("ACCOUNT_SELECTOR", "fixture-account")
    monkeypatch.setenv("ACCOUNT_SCOPE", "fixture-scope")
    monkeypatch.setenv("SERVICE_NAME", "binance-platform")
    monkeypatch.delenv("RUNTIME_TARGET_JSON", raising=False)
    monkeypatch.setenv("BINANCE_RISK_AUTHORITY_FILE", authority_env["BINANCE_RISK_AUTHORITY_FILE"])
    monkeypatch.setenv("BINANCE_RISK_AUTHORITY_SHA256", authority_env["BINANCE_RISK_AUTHORITY_SHA256"])
    monkeypatch.setenv("BINANCE_RISK_AUTHORITY_SOURCE_REVISION", authority_env["BINANCE_RISK_AUTHORITY_SOURCE_REVISION"])

    replay_runtime, replay_client, state_store, _ = run_cycle_replay.build_replay_runtime(
        run_id="full-cycle-live-builder-fixture", dry_run=True, now_utc=now,
    )

    def build():
        runtime = main.build_live_runtime(now_utc=now)
        runtime.state_loader = state_store.load
        runtime.trend_pool_payload = replay_runtime.trend_pool_payload
        runtime.btc_market_snapshot = replay_runtime.btc_market_snapshot
        runtime.trend_indicator_snapshots = replay_runtime.trend_indicator_snapshots
        return runtime

    with patch("live_risk_authority.resolve_strategy_revision", return_value="d" * 40), patch(
        "live_risk_authority.resolve_runner_revision", return_value=runner_revision
    ), patch("quant_platform_kit.risk.gate._utc_now", return_value=now):
        result = validate_full_cycle(runtime_builder=build, client_connector=lambda *_args, **_kwargs: replay_client)

    assert result["status"] == "passed"
    assert result["cycle_complete"] is True
    assert result["risk_outcome"] == "APPROVE"
    assert result["execution_permitted"] is False
    assert result["broker_read_count"] > 0
    assert result["suppressed_strategy_record_count"] > 0
    assert state_store.write_calls == []


def test_full_cycle_without_live_authority_fails_closed(monkeypatch):
    from scripts.validate_runtime_startup import validate_full_cycle

    monkeypatch.delenv("BINANCE_RISK_AUTHORITY_FILE", raising=False)
    monkeypatch.delenv("BINANCE_RISK_AUTHORITY_SHA256", raising=False)
    monkeypatch.delenv("BINANCE_RISK_AUTHORITY_SOURCE_REVISION", raising=False)
    runtime = SimpleNamespace(standard_execution_permitted=False, risk_authority=None)
    with pytest.raises(ValueError, match="risk_authority_missing"):
        validate_full_cycle(runtime_builder=lambda: runtime)


def test_full_cycle_failure_projection_keeps_reject_and_abort_distinct():
    import main
    import run_cycle_replay
    from scripts.validate_runtime_startup import _full_cycle_failure_reason

    risk_rejected = run_cycle_replay.run_replay_cycle(
        run_id="full-cycle-risk-rejected", dry_run=True,
    )["report"]
    aborted_runtime, _client, _state_store, _notifier = run_cycle_replay.build_replay_runtime(
        run_id="full-cycle-aborted", dry_run=True,
    )
    aborted_runtime.state_loader = lambda *, normalize=False: None
    cycle_aborted = main.execute_cycle(aborted_runtime)

    assert _full_cycle_failure_reason(risk_rejected) == "full_cycle_risk_rejected"
    assert _full_cycle_failure_reason(cycle_aborted) == "full_cycle_aborted"


def test_full_cycle_broker_proxy_rejects_unknown_methods_and_mutations():
    from scripts.validate_runtime_startup import _ReadOnlyBroker

    class Client:
        def order_market_buy(self, **_kwargs):
            raise AssertionError("must never be reached")

        def get_asset_balance(self, *, asset):
            return {"asset": asset, "free": "0", "locked": "0"}

    broker = _ReadOnlyBroker(Client(), symbols={"BTCUSDT"})
    with pytest.raises(RuntimeError, match="method_forbidden"):
        broker.order_market_buy(symbol="BTCUSDT", quoteOrderQty=1)
    with pytest.raises(RuntimeError, match="asset_forbidden"):
        broker.get_asset_balance(asset="DOGE")


@pytest.mark.parametrize('message, expected', [
    ('runtime_recovery_not_active', 'runtime_recovery_not_active'),
    ('recovery_control_state_invalid', 'recovery_control_state_invalid'),
    ('recovery_active_binding_invalid', 'recovery_active_binding_invalid'),
    ('STRATEGY_PROFILE does not match RUNTIME_TARGET_JSON.strategy_profile', 'startup_strategy_profile_conflict'),
    ('BINANCE_DRY_RUN does not match RUNTIME_TARGET_JSON.dry_run_only', 'startup_dry_run_conflict'),
    ('runtime_recovery_not_active: DO_NOT_LOG_THIS_SYNTHETIC_TOKEN', 'runtime_startup_validation_failed'),
    ('DO_NOT_LOG_THIS_SYNTHETIC_TOKEN', 'runtime_startup_validation_failed'),
])
def test_startup_cli_reports_only_exact_safe_reasons_and_still_fails(monkeypatch, capsys, message, expected):
    import json

    monkeypatch.setenv('RUNTIME_TARGET_ENABLED', 'false')
    monkeypatch.delenv('BINANCE_API_KEY', raising=False)
    monkeypatch.delenv('BINANCE_API_SECRET', raising=False)

    def build():
        raise ValueError(message)

    monkeypatch.setitem(sys.modules, 'main', SimpleNamespace(build_live_runtime=build))
    script = Path(__file__).resolve().parents[1] / 'scripts/validate_runtime_startup.py'
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(script), run_name='__main__')
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert 'DO_NOT_LOG_THIS_SYNTHETIC_TOKEN' not in captured.out + captured.err
    assert json.loads(captured.out) == {
        'status': 'failed', 'stage': 'runtime_startup_validation',
        'error_type': 'ValueError', 'reason_code': expected,
    }
