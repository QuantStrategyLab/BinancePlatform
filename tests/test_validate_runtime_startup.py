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


@pytest.mark.parametrize(("profile", "scenario"), [
    ("crypto_live_pool_rotation", "default"),
    ("crypto_live_pool_rotation", "strategy_revision_mismatch"),
    ("crypto_live_pool_rotation", "runner_revision_mismatch"),
    ("crypto_live_pool_rotation", "malformed_strategy_revision"),
    ("crypto_btc_dca", "ordinary_buy"),
    ("crypto_btc_dca", "stale_btc_history"),
    ("crypto_btc_dca", "malformed_btc_history"),
    ("crypto_btc_dca", "invalid_last_btc_close"),
    ("crypto_btc_dca", "uncomputable_last_btc_indicators"),
    ("crypto_btc_dca", "injected_btc_snapshot"),
    ("crypto_btc_dca", "synthetic_smart"),
    ("crypto_btc_dca", "missing_smart_input"),
    ("crypto_btc_dca", "future_cycle_input"),
    ("crypto_btc_dca", "stale_cycle_input"),
    ("crypto_btc_dca", "conflicting_cycle_input"),
    ("crypto_btc_dca", "snapshot_removed"),
    ("crypto_btc_dca", "missing_flags"),
    ("crypto_btc_dca", "invalid_smart_flag"),
    ("crypto_btc_dca", "invalid_cycle_flag"),
    ("crypto_btc_dca", "smart_without_cycle"),
    ("crypto_btc_dca", "outside_window"),
    ("crypto_btc_dca", "insufficient_cash"),
    ("crypto_trend_rotation", "default"),
])
def test_full_cycle_uses_live_builder_and_closes_all_write_ports(monkeypatch, tmp_path, capsys, profile, scenario):
    import main
    import run_cycle_replay
    from live_risk_authority import config_sha256
    from quant_platform_kit.common.runtime_target import build_runtime_target
    from strategy_runtime import load_research_only_strategy_runtime
    from quant_platform_kit.common.live_continuity import runtime_target_fingerprint
    from scripts.validate_runtime_startup import FullCycleValidationError, validate_full_cycle

    # The real builder activates an unbound module-level handle and pool knobs.
    # Restore them after each synthetic profile so later replay tests keep their
    # original runtime rather than inheriting this fixture's last profile.
    for name in (
        "STRATEGY_RUNTIME",
        "TREND_POOL_SIZE",
        "DEFAULT_LIVE_POOL_LEGACY_PATH",
        "DEFAULT_TREND_POOL_MAX_AGE_DAYS",
        "DEFAULT_TREND_POOL_ACCEPTABLE_MODES",
    ):
        monkeypatch.setattr(main, name, getattr(main, name))

    now = datetime(2026, 3, 15 if scenario == "outside_window" or profile != "crypto_btc_dca" else 25, tzinfo=timezone.utc)
    target = build_runtime_target(
        platform_id="binance",
        strategy_profile=profile,
        dry_run_only=True,
        deployment_selector="fixture",
        account_selector="fixture-account",
        account_scope="fixture-scope",
        service_name="binance-platform",
    )
    btc_overrides = {}
    if profile == "crypto_btc_dca":
        # Ordinary cases explicitly disable smart and exit inputs; the smart case
        # supplies separate synthetic indicator and normal exit data below.
        btc_overrides = {"smart_multiplier_enabled": False, "zscore_exit_enabled": False}
        if scenario in {"synthetic_smart", "missing_smart_input", "future_cycle_input",
                         "stale_cycle_input", "conflicting_cycle_input", "snapshot_removed"}:
            btc_overrides["smart_multiplier_enabled"] = True
            btc_overrides["zscore_exit_enabled"] = True
            btc_overrides["zscore_exit_context"] = {"route": "normal"}
        if scenario == "missing_flags":
            btc_overrides = {"zscore_exit_enabled": False}
        if scenario == "invalid_smart_flag":
            btc_overrides["smart_multiplier_enabled"] = None
        if scenario == "invalid_cycle_flag":
            btc_overrides["cycle_indicator_enabled"] = "true"
        if scenario == "smart_without_cycle":
            btc_overrides.update(smart_multiplier_enabled=True, cycle_indicator_enabled=False)
        if scenario == "insufficient_cash":
            btc_overrides["cash_reserve_usd"] = 1000.0
        import strategy_runtime

        build_loaded = strategy_runtime._build_loaded_strategy_runtime

        def build_with_synthetic_config(entrypoint):
            actual = build_loaded(entrypoint)
            if entrypoint.manifest.profile == "crypto_btc_dca":
                return replace(actual, runtime_overrides=btc_overrides)
            return actual

        # Both the authority digest and the real gated activation use the same
        # synthetic config; neither loader nor the authority check is bypassed.
        monkeypatch.setattr(strategy_runtime, "_build_loaded_strategy_runtime", build_with_synthetic_config)
    loaded = load_research_only_strategy_runtime(target.strategy_profile)
    runner_revision = "b" * 40
    payload = {
        "decision": "APPROVE",
        "authority_scope": "LIVE",
        "runtime_target": {
            "platform_id": "binance",
            "strategy_profile": profile,
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
    if scenario == "strategy_revision_mismatch":
        payload["strategy_revision"] = "c" * 40
    elif scenario == "runner_revision_mismatch":
        payload["runner_revision"] = "c" * 40
    elif scenario == "malformed_strategy_revision":
        payload["strategy_revision"] = "private-sentinel/account/secret"
    authority_path = tmp_path / "authority.json"
    raw = (json.dumps(payload, sort_keys=True) + "\n").encode()
    authority_path.write_bytes(raw)
    authority_env = {
        "BINANCE_RISK_AUTHORITY_FILE": str(authority_path),
        "BINANCE_RISK_AUTHORITY_SHA256": hashlib.sha256(raw).hexdigest(),
        "BINANCE_RISK_AUTHORITY_SOURCE_REVISION": "a" * 40,
    }
    target_payload = {
        "platform_id": target.platform_id,
        "strategy_profile": target.strategy_profile,
        "dry_run_only": True,
        "deployment_selector": "fixture",
        "account_selector": ["fixture-account"],
        "account_scope": "fixture-scope",
        "service_name": "binance-platform",
    }
    target_payload["live_continuity"] = {
        "state": "RECONCILE_ONLY",
        "baseline_kind": "legacy_authorized",
        "baseline_id": "synthetic-no-submit",
        "baseline_target_sha256": runtime_target_fingerprint(target_payload),
        "captured_at": "2026-03-15",
    }
    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "false")
    monkeypatch.setenv("BINANCE_DRY_RUN", "true")
    monkeypatch.setenv("RUNTIME_TARGET_JSON", json.dumps(target_payload))
    monkeypatch.setenv("BINANCE_RISK_AUTHORITY_FILE", authority_env["BINANCE_RISK_AUTHORITY_FILE"])
    monkeypatch.setenv("BINANCE_RISK_AUTHORITY_SHA256", authority_env["BINANCE_RISK_AUTHORITY_SHA256"])
    monkeypatch.setenv("BINANCE_RISK_AUTHORITY_SOURCE_REVISION", authority_env["BINANCE_RISK_AUTHORITY_SOURCE_REVISION"])

    if scenario in {"strategy_revision_mismatch", "runner_revision_mismatch", "malformed_strategy_revision"}:
        monkeypatch.setattr(sys, "argv", ["validate_runtime_startup.py", "--full-cycle"])
        script = Path(__file__).resolve().parents[1] / "scripts/validate_runtime_startup.py"
        with patch("live_risk_authority.resolve_strategy_revision", return_value="d" * 40), \
             patch("live_risk_authority.resolve_runner_revision", return_value=runner_revision), \
             pytest.raises(SystemExit) as exit_error:
            runpy.run_path(str(script), run_name="__main__")
        assert exit_error.value.code == 1
        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["status"] == "failed" and result["failure_stage"] == "build"
        assert result["reason_code"] == ("runner_revision_mismatch" if scenario == "runner_revision_mismatch"
                                         else "strategy_revision_mismatch")
        assert result["version_binding_observation"] == {
            "declared_strategy_revision": None if scenario == "malformed_strategy_revision" else payload["strategy_revision"],
            "observed_strategy_revision": "d" * 40,
            "declared_runner_revision": payload["runner_revision"],
            "observed_runner_revision": runner_revision,
            "interpretation": "comparison_observation_remaining_checks_not_passed",
        }
        assert "private-sentinel" not in captured.out + captured.err
        assert str(authority_path) not in captured.out + captured.err
        assert payload["config_sha256"] not in captured.out + captured.err
        return

    replay_runtime, replay_client, state_store, _ = run_cycle_replay.build_replay_runtime(
        run_id="full-cycle-live-builder-fixture", dry_run=True, now_utc=now,
    )
    historical_calls = []

    def historical_klines(symbol, interval, lookback):
        assert interval == "1d"
        historical_calls.append((symbol, interval, lookback))
        days = int(lookback.split()[0])
        cutoff = int(now.timestamp() * 1000)
        rows = [
            [cutoff - (days - index) * 86_400_000, 100 + index, 101 + index,
             99 + index, 100 + index, 10,
             cutoff - (days - index - 1) * 86_400_000 - 1]
            for index in range(days)
        ]
        if symbol == "BTCUSDT" and lookback == "700 days ago UTC":
            if scenario == "stale_btc_history":
                return rows[:-1]
            if scenario == "malformed_btc_history":
                rows[-1][6] -= 1
            if scenario == "invalid_last_btc_close":
                rows[-1][4] = float("nan")
            if scenario == "uncomputable_last_btc_indicators":
                for row in rows[-200:]:
                    row[1:5] = [100, 101, 99, 100]
        return rows

    replay_client.get_historical_klines = historical_klines
    smart_scenarios = {"synthetic_smart", "missing_smart_input", "future_cycle_input",
                       "stale_cycle_input", "conflicting_cycle_input", "snapshot_removed"}
    consumed_inputs = []
    if scenario in smart_scenarios:
        from crypto_strategies.strategies import crypto_btc_dca

        compute = crypto_btc_dca.compute_signals

        def capture_signals(*args, **kwargs):
            consumed_inputs.append((kwargs.get("derived_indicators"), kwargs.get("as_of")))
            if scenario == "snapshot_removed" and len(consumed_inputs) == 1:
                cycle_path.unlink()
            return compute(*args, **kwargs)

        monkeypatch.setattr(crypto_btc_dca, "compute_signals", capture_signals)
        cycle_path = tmp_path / "synthetic-cycle.json"
        monkeypatch.setenv("BTC_CYCLE_INDICATORS_PATH", str(cycle_path))
        monkeypatch.setenv("BTC_CYCLE_INPUT_MAX_AGE_SECONDS", "7200")
        if scenario != "missing_smart_input":
            observed = now - timedelta(hours=1)
            available = now - timedelta(minutes=30)
            if scenario == "future_cycle_input":
                available = now + timedelta(seconds=1)
            if scenario == "stale_cycle_input":
                observed = now - timedelta(hours=3)
            cycle_path.write_text(json.dumps({
                "contract_version": "btc-cycle-input/v1", "source": "synthetic-test",
                "source_version": "fixture-v1", "symbol": "BTCUSDT",
                "observed_at": observed.isoformat(), "available_at": available.isoformat(), "ahr999": 0.7,
            }))
        if scenario == "conflicting_cycle_input":
            resolve_indicators = main.resolve_runtime_trend_indicators

            def conflicting_indicators(runtime, *, validation_scope="standard"):
                indicators = resolve_indicators(runtime, validation_scope=validation_scope)
                indicators["BTCUSDT"] = {"ahr999": 0.8}
                return indicators

            monkeypatch.setattr(main, "resolve_runtime_trend_indicators", conflicting_indicators)

    if scenario in {"missing_flags", "invalid_smart_flag", "invalid_cycle_flag"}:
        from crypto_strategies.strategies import crypto_btc_dca

        def unexpected_compute(*args, **kwargs):
            consumed_inputs.append(True)
            raise AssertionError("unvalidated BTC input reached strategy")

        monkeypatch.setattr(crypto_btc_dca, "compute_signals", unexpected_compute)
    if scenario == "smart_without_cycle":
        resolve_indicators = main.resolve_runtime_trend_indicators

        def noncycle_indicators(runtime, *, validation_scope="standard"):
            indicators = resolve_indicators(runtime, validation_scope=validation_scope)
            indicators["BTCUSDT"] = {"close": 60000.0, "sma200": 55000.0,
                                     "high252": 65000.0, "rsi14": 50.0}
            return indicators

        monkeypatch.setattr(main, "resolve_runtime_trend_indicators", noncycle_indicators)
    if scenario in {"missing_flags", "invalid_smart_flag", "invalid_cycle_flag", "smart_without_cycle"}:
        monkeypatch.setenv("BTC_CYCLE_INDICATORS_PATH", str(tmp_path / "missing-cycle.json"))
        monkeypatch.delenv("BTC_CYCLE_INPUT_MAX_AGE_SECONDS", raising=False)

    def build(*, validation_scope):
        assert validation_scope == "full_cycle_no_submit"
        runtime = main.build_live_runtime(now_utc=now, validation_scope=validation_scope)
        assert runtime.strategy_runtime.profile == profile
        if btc_overrides:
            assert runtime.strategy_runtime.runtime_overrides == btc_overrides
        assert runtime.runtime_target.live_continuity.state == "RECONCILE_ONLY"
        assert runtime.standard_execution_permitted is False
        runtime.state_loader = state_store.load
        runtime.trend_pool_payload = replay_runtime.trend_pool_payload
        assert runtime.btc_market_snapshot is None
        if scenario == "injected_btc_snapshot":
            runtime.btc_market_snapshot = {"ma200": 100.0}
        assert runtime.trend_indicator_snapshots is None
        return runtime

    reports = []
    observed_exit_plans = []
    if scenario == "synthetic_smart":
        import infra.btc_no_submit as btc_no_submit

        approve_amount = btc_no_submit.approved_buy_amount

        def capture_exit_plan(decision, metrics):
            amount = approve_amount(decision, metrics)
            exit_plan = decision.diagnostics["zscore_exit"]
            observed_exit_plans.append((exit_plan["route"], exit_plan["applied"]))
            return amount

        monkeypatch.setattr(btc_no_submit, "approved_buy_amount", capture_exit_plan)
    execute_cycle = main.execute_cycle

    def capture_cycle(*args, **kwargs):
        report = execute_cycle(*args, **kwargs)
        reports.append(report)
        return report

    monkeypatch.setattr(main, "execute_cycle", capture_cycle)
    with patch("live_risk_authority.resolve_strategy_revision", return_value="d" * 40), patch(
        "live_risk_authority.resolve_runner_revision", return_value=runner_revision
    ), patch("quant_platform_kit.risk.gate._utc_now", return_value=now):
        try:
            result = validate_full_cycle(runtime_builder=build, client_connector=lambda *_args, **_kwargs: replay_client)
        except FullCycleValidationError as exc:
            if scenario in {"missing_smart_input", "future_cycle_input", "stale_cycle_input",
                            "conflicting_cycle_input", "missing_flags", "invalid_smart_flag", "invalid_cycle_flag"}:
                assert exc.failure_stage == "portfolio_allocation"
                assert exc.reason_code == "btc_cycle_input_unavailable"
                assert exc.error_type == "btc_cycle_input_error"
                assert not consumed_inputs
                assert ("BTCUSDT", "1d", "700 days ago UTC") in historical_calls
                assert replay_client.side_effect_calls == []
                assert state_store.write_calls == []
                return
            if scenario in {"stale_btc_history", "malformed_btc_history", "invalid_last_btc_close",
                            "uncomputable_last_btc_indicators", "injected_btc_snapshot"}:
                assert exc.failure_stage == "market_snapshot"
                assert replay_client.side_effect_calls == []
                assert state_store.write_calls == []
                return
            pytest.fail(
                f"synthetic full-cycle failure: reason={exc.reason_code}, "
                f"stage={exc.failure_stage}, error_type={exc.error_type}",
                pytrace=False,
            )

    if scenario in {"missing_smart_input", "future_cycle_input", "stale_cycle_input",
                    "conflicting_cycle_input", "missing_flags", "invalid_smart_flag", "invalid_cycle_flag",
                    "stale_btc_history", "malformed_btc_history",
                    "invalid_last_btc_close", "uncomputable_last_btc_indicators", "injected_btc_snapshot"}:
        pytest.fail("expected full-cycle validation rejection", pytrace=False)
    assert result["status"] == "passed"
    assert result["cycle_complete"] is True
    assert result["risk_outcome"] == "APPROVE"
    assert result["execution_permitted"] is False
    assert result["broker_read_count"] > 0
    assert result["suppressed_strategy_record_count"] > 0
    if profile == "crypto_btc_dca":
        assert ("BTCUSDT", "1d", "700 days ago UTC") in historical_calls
        proposals = reports[0]["btc_dca_intents"]
        if scenario in {"ordinary_buy", "synthetic_smart", "snapshot_removed", "smart_without_cycle"}:
            assert len(proposals) == 1
            assert proposals[0]["action"] == "buy"
            assert proposals[0]["proposal_only"] is True
            assert proposals[0]["budget"] > 0
            if scenario in {"synthetic_smart", "snapshot_removed"}:
                # Pre/post allocation and both real trend-plan evaluations use the same input.
                assert len(consumed_inputs) >= 4
                assert all(inputs["BTCUSDT"]["ahr999"] == 0.7 and clock == now
                           for inputs, clock in consumed_inputs)
                if scenario == "synthetic_smart":
                    assert observed_exit_plans
                    assert all(plan == ("normal", True) for plan in observed_exit_plans)
                if scenario == "snapshot_removed":
                    assert len(consumed_inputs) >= 2
                    with pytest.raises(FullCycleValidationError) as error:
                        with patch("live_risk_authority.resolve_strategy_revision", return_value="d" * 40), patch(
                            "live_risk_authority.resolve_runner_revision", return_value=runner_revision
                        ), patch("quant_platform_kit.risk.gate._utc_now", return_value=now):
                            validate_full_cycle(runtime_builder=build,
                                                client_connector=lambda *_args, **_kwargs: replay_client)
                    assert error.value.reason_code == "btc_cycle_input_unavailable"
                    assert error.value.failure_stage == "portfolio_allocation"
        else:
            assert proposals == []
    if profile == "crypto_live_pool_rotation":
        assert ("BTCUSDT", "1d", "700 days ago UTC") in historical_calls
        assert all(call[2] == "420 days ago UTC" for call in historical_calls if call[0] != "BTCUSDT")
    assert replay_client.side_effect_calls == []
    assert state_store.write_calls == []


def test_full_cycle_without_live_authority_fails_closed(monkeypatch):
    from scripts.validate_runtime_startup import FullCycleValidationError, validate_full_cycle

    monkeypatch.delenv("BINANCE_RISK_AUTHORITY_FILE", raising=False)
    monkeypatch.delenv("BINANCE_RISK_AUTHORITY_SHA256", raising=False)
    monkeypatch.delenv("BINANCE_RISK_AUTHORITY_SOURCE_REVISION", raising=False)
    runtime = SimpleNamespace(standard_execution_permitted=False, risk_authority=None)
    with pytest.raises(FullCycleValidationError) as error:
        validate_full_cycle(runtime_builder=lambda **_kwargs: runtime)
    assert error.value.reason_code == "full_cycle_risk_authority_missing"
    assert error.value.failure_stage == "build"


def test_full_cycle_failure_projection_keeps_reject_and_abort_distinct():
    import main
    import run_cycle_replay
    from scripts.validate_runtime_startup import _full_cycle_failure_metadata, _full_cycle_failure_reason

    risk_rejected = run_cycle_replay.run_replay_cycle(
        run_id="full-cycle-risk-rejected", dry_run=True,
    )["report"]
    aborted_runtime, _client, _state_store, _notifier = run_cycle_replay.build_replay_runtime(
        run_id="full-cycle-aborted", dry_run=True,
    )
    aborted_runtime.state_loader = lambda *, normalize=False: None
    cycle_aborted = main.execute_cycle(aborted_runtime)

    assert _full_cycle_failure_reason(risk_rejected) == "full_cycle_risk_rejected", (
        f"synthetic replay failure metadata: {_full_cycle_failure_metadata(risk_rejected)}"
    )
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


def test_full_cycle_broker_proxy_allows_only_bounded_bnb_dividend_get():
    from scripts.validate_runtime_startup import _ReadOnlyBroker

    class Client:
        def __init__(self):
            self.calls = []

        def _request_margin_api(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return {"total": 0, "rows": []}

    client = Client()
    broker = _ReadOnlyBroker(client, symbols={"BNBUSDT"})
    result = broker._request_margin_api(
        "get", "asset/assetDividend", signed=True,
        data={"asset": "BNB", "startTime": 1000, "endTime": 2000, "limit": 500},
    )

    assert result == {"total": 0, "rows": []}
    assert len(client.calls) == 1
    with pytest.raises(RuntimeError, match="dividend_window_forbidden"):
        broker._request_margin_api(
            "get", "asset/assetDividend", signed=True,
            data={"asset": "USDT", "startTime": 1000, "endTime": 2000, "limit": 500},
        )


def test_full_cycle_broker_proxy_allows_main_qpk_btc_snapshot_window():
    import main
    from scripts.validate_runtime_startup import _ReadOnlyBroker

    class Client:
        def __init__(self):
            self.calls = []

        def get_historical_klines(self, symbol, interval, lookback):
            self.calls.append((symbol, interval, lookback))
            return [
                [index * 86_400_000, 100 + index, 101 + index, 99 + index, 100 + index, 10]
                for index in range(700)
            ]

    client = Client()
    broker = _ReadOnlyBroker(client, symbols={"BTCUSDT"})
    logs = []

    snapshot = main.fetch_btc_market_snapshot(broker, 800.0, log_buffer=logs)

    assert snapshot is not None
    assert client.calls == [("BTCUSDT", "1d", "700 days ago UTC")]
    assert broker.read_counts == {"get_historical_klines": 1}
    assert logs == []


def test_full_cycle_broker_proxy_keeps_trend_window_bounded():
    from scripts.validate_runtime_startup import _ReadOnlyBroker

    broker = _ReadOnlyBroker(object(), symbols={"BTCUSDT", "ETHUSDT"})
    with pytest.raises(RuntimeError, match="market_window_forbidden"):
        broker.get_historical_klines("ETHUSDT", "1d", "421 days ago UTC")
    with pytest.raises(RuntimeError, match="market_window_forbidden"):
        broker.get_historical_klines("BTCUSDT", "1d", "701 days ago UTC")


def test_full_cycle_failure_projection_keeps_allowlisted_report_metadata():
    from scripts.validate_runtime_startup import _full_cycle_failure_metadata

    report = {
        "diagnostics": {
            "cycle_failure": {
                "stage": "market_snapshot",
                "error_type": "client_call_error",
            }
        }
    }
    assert _full_cycle_failure_metadata(report) == ("market_snapshot", "client_call_error")
    assert _full_cycle_failure_metadata({
        "diagnostics": {"cycle_failure": {"stage": "private", "error_type": "secret"}}
    }) == ("result", "RuntimeError")


def test_full_cycle_failure_projection_preserves_safe_daily_state_reason_code():
    from scripts.validate_runtime_startup import _full_cycle_failure_reason

    report = {
        "status": "error",
        "diagnostics": {
            "cycle_failure": {
                "stage": "daily_state",
                "error_type": "execution_integrity_error",
                "reason_code": "earn_counter_reset",
            }
        },
    }
    assert _full_cycle_failure_reason(report) == "earn_counter_reset"
    report["diagnostics"]["cycle_failure"]["reason_code"] = "provider account=secret-token"
    assert _full_cycle_failure_reason(report) == "full_cycle_cycle_error"


@pytest.mark.parametrize("reason", [
    "earn_checkpoint_required_for_receipt", "earn_forward_accounting_unverified",
    "balance_change_unexplained", "external_cash_flow_reconciliation_uncertain",
    "daily_accounting_unverifiable", "daily_trend_accounting_migration_unverifiable",
    "daily_trend_accounting_unverifiable", "external_cash_flow_cursor_missing",
    "external_cash_flow_late_completion", "external_cash_flow_balance_mismatch",
    "external_withdrawal_accounting_unsupported", "external_cash_flow_scope_unsupported",
])
def test_full_cycle_preserves_fixed_daily_integrity_reason(reason):
    from scripts.validate_runtime_startup import _full_cycle_failure_reason
    report = {"status": "error", "diagnostics": {"cycle_failure": {
        "stage": "daily_state", "error_type": "execution_integrity_error", "reason_code": reason,
    }}}
    assert _full_cycle_failure_reason(report) == reason
    report["diagnostics"]["cycle_failure"]["stage"] = "market_snapshot"
    assert _full_cycle_failure_reason(report) == "full_cycle_broker_read_failed"


@pytest.mark.parametrize("reason", ["PRIVATE_ACCOUNT", "custom_error", {}, [], None, False])
def test_full_cycle_unknown_daily_reason_stays_generic(reason):
    from scripts.validate_runtime_startup import _full_cycle_failure_reason
    assert _full_cycle_failure_reason({"status": "error", "diagnostics": {"cycle_failure": {
        "stage": "daily_state", "reason_code": reason,
    }}}) == "full_cycle_cycle_error"


@pytest.mark.parametrize('message, expected', [
    ('runtime_recovery_not_active', 'runtime_recovery_not_active'),
    ('recovery_control_state_invalid', 'recovery_control_state_invalid'),
    ('recovery_active_binding_invalid', 'recovery_active_binding_invalid'),
    ('recovery_control_read_failed', 'recovery_control_read_failed'),
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
    assert 'version_binding_observation' not in json.loads(captured.out)
    assert json.loads(captured.out) == {
        'status': 'failed', 'stage': 'runtime_startup_validation',
        'error_type': 'ValueError', 'reason_code': expected,
    }


def test_startup_cli_reports_reason_from_safe_runtime_exception(monkeypatch, capsys):
    from application.reconciliation_recovery import RecoveryControlReadError

    monkeypatch.setenv('RUNTIME_TARGET_ENABLED', 'false')
    monkeypatch.delenv('BINANCE_API_KEY', raising=False)
    monkeypatch.delenv('BINANCE_API_SECRET', raising=False)

    def build():
        raise RecoveryControlReadError()

    monkeypatch.setitem(sys.modules, 'main', SimpleNamespace(build_live_runtime=build))
    script = Path(__file__).resolve().parents[1] / 'scripts/validate_runtime_startup.py'
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(script), run_name='__main__')
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out)['reason_code'] == 'recovery_control_read_failed'

@pytest.mark.parametrize(
    "authority_message, expected_reason",
    [
        ("live risk authority configuration invalid: runner revision mismatch", "runner_revision_mismatch"),
        ("live risk authority configuration invalid: runner checkout has tracked modifications", "runner_checkout_dirty"),
        ("live risk authority configuration invalid: config digest mismatch", "config_digest_mismatch"),
    ],
)
def test_full_cycle_cli_preserves_safe_builder_authority_reason(
    monkeypatch, capsys, authority_message, expected_reason,
):
    from live_risk_authority import LiveRiskAuthorityError

    monkeypatch.setenv("RUNTIME_TARGET_ENABLED", "false")
    monkeypatch.setitem(
        sys.modules,
        "main",
        SimpleNamespace(
            build_live_runtime=lambda **_kwargs: (_ for _ in ()).throw(
                LiveRiskAuthorityError(authority_message)
            )
        ),
    )
    monkeypatch.setattr(sys, "argv", ["validate_runtime_startup.py", "--full-cycle"])
    script = Path(__file__).resolve().parents[1] / "scripts/validate_runtime_startup.py"
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(script), run_name="__main__")

    assert error.value.code == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert "version_binding_observation" not in result
    assert result == {
        "status": "failed",
        "stage": "full_cycle_validation",
        "failure_stage": "build",
        "error_type": "ValueError",
        "reason_code": expected_reason,
    }
    assert authority_message not in captured.out + captured.err


def test_revision_observation_is_not_carried_from_other_phases():
    from live_risk_authority import LiveRiskAuthorityError
    from scripts.validate_runtime_startup import _wrap_full_cycle_error

    error = LiveRiskAuthorityError("strategy revision mismatch")
    error.version_binding_observation = {"declared_strategy_revision": "a" * 40}
    wrapped = _wrap_full_cycle_error(error, failure_stage="cycle", fallback_reason="full_cycle_cycle_failed")
    assert wrapped.reason_code == "strategy_revision_mismatch"
    assert not hasattr(wrapped, "version_binding_observation")


def test_btc_cycle_input_reason_projection_is_exact_and_scoped():
    from scripts.validate_runtime_startup import _full_cycle_failure_reason

    failure = {"stage": "portfolio_allocation", "error_type": "btc_cycle_input_error",
               "reason_code": "btc_cycle_input_unavailable"}
    def project(**changes):
        return _full_cycle_failure_reason({"status": "error", "diagnostics": {
            "cycle_failure": {**failure, **changes}}})
    assert project() == "btc_cycle_input_unavailable"
    assert project(stage="market_snapshot") == "full_cycle_broker_read_failed"
    assert project(error_type="value_error") == "full_cycle_cycle_error"
    assert project(reason_code="private-provider-message") == "full_cycle_cycle_error"
