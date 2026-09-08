import os
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
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(build_live_runtime=build))
    result = validate_startup()
    assert calls == ["build"]
    assert result["status"] == "passed"
    assert result["execution_permitted"] is False


def test_startup_validation_rejects_broker_credentials(monkeypatch):
    from scripts.validate_runtime_startup import validate_startup
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "false")
    monkeypatch.setenv("BINANCE_API_KEY", "synthetic")
    with pytest.raises(ValueError, match="broker_credentials_forbidden"):
        validate_startup()
