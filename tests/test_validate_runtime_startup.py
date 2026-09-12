import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

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
