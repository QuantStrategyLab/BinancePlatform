import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
QPK_SRC = ROOT.parent / "QuantPlatformKit" / "src"
CRYPTO_STRATEGIES_SRC = ROOT.parent / "CryptoStrategies" / "src"
for path in (ROOT, QPK_SRC, CRYPTO_STRATEGIES_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from quant_platform_kit.common.runtime_target import build_runtime_target

from live_risk_authority import (
    LiveRiskAuthorityError,
    bind_live_risk_authority,
    config_sha256,
    load_live_risk_authority,
)


PROFILE = "crypto_live_pool_rotation"
STRATEGY_REVISION = "d" * 40
RUNNER_REVISION = "e" * 40
CONFIG_SHA256 = "f" * 64
SOURCE_REVISION = "a" * 40


def _target():
    return build_runtime_target(
        platform_id="binance",
        strategy_profile=PROFILE,
        dry_run_only=False,
        deployment_selector="default",
        account_selector="crypto-combo-live",
        account_scope="crypto_combo",
        service_name="binance-platform",
    )


def _payload(now: datetime):
    return {
        "decision": "APPROVE",
        "authority_scope": "LIVE",
        "runtime_target": {
            "platform_id": "binance",
            "strategy_profile": PROFILE,
            "account_scope": "crypto_combo",
            "account_selector": ["crypto-combo-live"],
            "deployment_selector": "default",
        },
        "strategy_revision": STRATEGY_REVISION,
        "runner_revision": RUNNER_REVISION,
        "config_sha256": CONFIG_SHA256,
        "continuous_inputs_allowed": True,
        "mandate": {
            "mandate_id": "binance_live_pool_rotation_fixture",
            "mandate_version": "fixture-v1",
            "effective_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            "max_snapshot_age_seconds": 300,
            "effective_exposure_cap": 1.0,
            "loss_budget": 5000.0,
            "product_caps": {"BTCUSDT": 1.0, "ETHUSDT": 1.0, "SOLUSDT": 1.0},
            "nominal_caps": {"BTCUSDT": 1.0, "ETHUSDT": 1.0, "SOLUSDT": 1.0},
            "product_leverage_factors": {"BTCUSDT": 1, "ETHUSDT": 1, "SOLUSDT": 1},
            "allowed_nonzero_assets": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        },
    }


def _dynamic_payload(now: datetime):
    payload = _payload(now)
    mandate = payload["mandate"]
    mandate.pop("loss_budget")
    mandate["budget_policy"] = {"mode": "managed_usdt_dynamic"}
    mandate["validity_mode"] = "until_revoked"
    mandate["expires_at"] = None
    return payload


def _write_authority(tmp_path: Path, payload: dict[str, object]):
    path = tmp_path / "binance-live-authority.json"
    raw = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def _env(path: Path, digest: str):
    return {
        "BINANCE_RISK_AUTHORITY_FILE": str(path),
        "BINANCE_RISK_AUTHORITY_SHA256": digest,
        "BINANCE_RISK_AUTHORITY_SOURCE_REVISION": SOURCE_REVISION,
    }


def test_missing_authority_material_remains_closed():
    assert load_live_risk_authority(env={}, runtime_target=_target()) is None


def test_partial_authority_material_is_a_sanitized_configuration_error(tmp_path):
    with pytest.raises(LiveRiskAuthorityError, match="authority configuration invalid"):
        load_live_risk_authority(
            env={"BINANCE_RISK_AUTHORITY_FILE": str(tmp_path / "missing.json")},
            runtime_target=_target(),
        )


def test_authority_is_bound_to_actual_runtime_and_dynamic_input_digest_changes(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    path, digest = _write_authority(tmp_path, _payload(now))
    with patch.dict(
        "os.environ",
        _env(path, digest),
        clear=False,
    ), patch("live_risk_authority.resolve_strategy_revision", return_value=STRATEGY_REVISION), patch(
        "live_risk_authority.resolve_runner_revision", return_value=RUNNER_REVISION
    ):
        authority = load_live_risk_authority(
            env=_env(path, digest),
            runtime_target=_target(),
            config_sha256=CONFIG_SHA256,
            now_utc=now,
        )
        mandate_a, candidate_a = bind_live_risk_authority(
            authority,
            runtime_target=_target(),
            input_material={"prices": {"BTCUSDT": 60000.0}, "pool": ["ETHUSDT"]},
            config_sha256=CONFIG_SHA256,
            now_utc=now,
        )
        mandate_b, candidate_b = bind_live_risk_authority(
            authority,
            runtime_target=_target(),
            input_material={"prices": {"BTCUSDT": 61000.0}, "pool": ["ETHUSDT"]},
            config_sha256=CONFIG_SHA256,
            now_utc=now,
        )
        _, candidate_c = bind_live_risk_authority(
            authority,
            runtime_target=_target(),
            input_material={
                "prices": {"BTCUSDT": 61000.0},
                "pool": ["ETHUSDT"],
                "execution_controls": {"allow_new_trend_entries": False},
            },
            config_sha256=CONFIG_SHA256,
            now_utc=now,
        )

    assert mandate_a["authority_scope"] == "LIVE"
    assert mandate_a["loss_budget"] == 5000.0
    assert mandate_a["expires_at"] == (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    assert mandate_a["candidate_identity_sha256"] == candidate_a.candidate_sha256
    assert candidate_a.input_manifest_sha256 != candidate_b.input_manifest_sha256
    assert candidate_b.input_manifest_sha256 != candidate_c.input_manifest_sha256
    assert mandate_b["loss_budget"] == mandate_a["loss_budget"]


def test_dynamic_managed_usdt_policy_derives_qpk_budget_and_snapshot_expiry(tmp_path):
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    path, digest = _write_authority(tmp_path, _dynamic_payload(now))
    with patch("live_risk_authority.resolve_strategy_revision", return_value=STRATEGY_REVISION), patch(
        "live_risk_authority.resolve_runner_revision", return_value=RUNNER_REVISION
    ):
        authority = load_live_risk_authority(
            env=_env(path, digest),
            runtime_target=_target(),
            strategy_revision=STRATEGY_REVISION,
            runner_revision=RUNNER_REVISION,
            config_sha256=CONFIG_SHA256,
            now_utc=now,
        )
        mandate, _candidate = bind_live_risk_authority(
            authority,
            runtime_target=_target(),
            input_material={
                "budget_observation": {
                    "managed_usdt": 120.0,
                    "total_equity": 1000.0,
                    "observed_effective_exposure": 0.40,
                },
                "prices": {"BTCUSDT": 60000.0},
            },
            config_sha256=CONFIG_SHA256,
            now_utc=now,
        )

    assert mandate["loss_budget"] == 120.0
    assert mandate["expires_at"] == "2026-09-13T12:05:00Z"


def test_dynamic_managed_usdt_policy_rejects_invalid_budget_observation(tmp_path):
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    path, digest = _write_authority(tmp_path, _dynamic_payload(now))
    with patch("live_risk_authority.resolve_strategy_revision", return_value=STRATEGY_REVISION), patch(
        "live_risk_authority.resolve_runner_revision", return_value=RUNNER_REVISION
    ):
        authority = load_live_risk_authority(
            env=_env(path, digest),
            runtime_target=_target(),
            strategy_revision=STRATEGY_REVISION,
            runner_revision=RUNNER_REVISION,
            config_sha256=CONFIG_SHA256,
            now_utc=now,
        )
        with pytest.raises(LiveRiskAuthorityError, match="budget observation"):
            bind_live_risk_authority(
                authority,
                runtime_target=_target(),
                input_material={
                    "budget_observation": {
                        "managed_usdt": -1.0,
                        "total_equity": 1000.0,
                        "observed_effective_exposure": 0.40,
                    }
                },
                config_sha256=CONFIG_SHA256,
                now_utc=now,
            )


def test_dynamic_managed_usdt_policy_clamps_normal_overexposure_headroom(tmp_path):
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    payload = _dynamic_payload(now)
    payload["mandate"]["effective_exposure_cap"] = 0.5
    path, digest = _write_authority(tmp_path, payload)
    with patch("live_risk_authority.resolve_strategy_revision", return_value=STRATEGY_REVISION), patch(
        "live_risk_authority.resolve_runner_revision", return_value=RUNNER_REVISION
    ):
        authority = load_live_risk_authority(
            env=_env(path, digest),
            runtime_target=_target(),
            strategy_revision=STRATEGY_REVISION,
            runner_revision=RUNNER_REVISION,
            config_sha256=CONFIG_SHA256,
            now_utc=now,
        )
        mandate, _candidate = bind_live_risk_authority(
            authority,
            runtime_target=_target(),
            input_material={
                "budget_observation": {
                    "managed_usdt": 120.0,
                    "total_equity": 1000.0,
                    "observed_effective_exposure": 0.60,
                }
            },
            config_sha256=CONFIG_SHA256,
            now_utc=now,
        )

    assert mandate["loss_budget"] == 0.0


def test_authority_rejects_runtime_target_mismatch(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    path, digest = _write_authority(tmp_path, _payload(now))
    with pytest.raises(LiveRiskAuthorityError, match="runtime target mismatch"):
        load_live_risk_authority(
            env=_env(path, digest),
            runtime_target=build_runtime_target(
                platform_id="binance",
                strategy_profile=PROFILE,
                dry_run_only=False,
                deployment_selector="other",
                account_selector="crypto-combo-live",
                account_scope="crypto_combo",
                service_name="binance-platform",
            ),
            strategy_revision=STRATEGY_REVISION,
            runner_revision=RUNNER_REVISION,
            config_sha256=CONFIG_SHA256,
            now_utc=now,
        )


def test_authority_rejects_digest_and_revision_binding_mismatch(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = _payload(now)
    payload["config_sha256"] = "0" * 64
    path, digest = _write_authority(tmp_path, payload)
    with pytest.raises(LiveRiskAuthorityError, match="config digest mismatch"):
        load_live_risk_authority(
            env=_env(path, digest),
            runtime_target=_target(),
            strategy_revision=STRATEGY_REVISION,
            runner_revision=RUNNER_REVISION,
            config_sha256=CONFIG_SHA256,
            now_utc=now,
        )

    payload = _payload(now)
    payload["strategy_revision"] = "1" * 40
    path, digest = _write_authority(tmp_path, payload)
    with pytest.raises(LiveRiskAuthorityError, match="strategy revision mismatch"):
        load_live_risk_authority(
            env=_env(path, digest),
            runtime_target=_target(),
            strategy_revision=STRATEGY_REVISION,
            runner_revision=RUNNER_REVISION,
            config_sha256=CONFIG_SHA256,
            now_utc=now,
        )


def test_authority_rejects_tampered_bytes(tmp_path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    path, digest = _write_authority(tmp_path, _payload(now))
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(LiveRiskAuthorityError, match="digest mismatch"):
        load_live_risk_authority(
            env=_env(path, digest),
            runtime_target=_target(),
            strategy_revision=STRATEGY_REVISION,
            runner_revision=RUNNER_REVISION,
            config_sha256=CONFIG_SHA256,
            now_utc=now,
        )


@pytest.mark.parametrize(
    ("raw", "reason"),
    [(b'{"decision":"APPROVE","decision":"APPROVE"}', "duplicate authority field"),
     (b'{"decision":NaN}', "non-finite JSON value")],
)
def test_authority_parser_rejects_duplicate_and_nonfinite_json(tmp_path, raw, reason):
    path = tmp_path / "invalid-authority.json"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    with pytest.raises(LiveRiskAuthorityError, match=reason):
        load_live_risk_authority(
            env=_env(path, digest),
            runtime_target=_target(),
            strategy_revision=STRATEGY_REVISION,
            runner_revision=RUNNER_REVISION,
            config_sha256=CONFIG_SHA256,
            now_utc=datetime.now(timezone.utc).replace(microsecond=0),
        )


def test_authority_rejects_research_scope_and_expiry(tmp_path):
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    payload = _payload(now)
    payload["authority_scope"] = "RESEARCH_ONLY"
    path, digest = _write_authority(tmp_path, payload)
    with pytest.raises(LiveRiskAuthorityError, match="authority scope"):
        load_live_risk_authority(
            env=_env(path, digest),
            runtime_target=_target(),
            strategy_revision=STRATEGY_REVISION,
            runner_revision=RUNNER_REVISION,
            config_sha256=CONFIG_SHA256,
            now_utc=now,
        )


@pytest.mark.parametrize("payload_builder", [_payload, _dynamic_payload], ids=["fixed", "managed_usdt_dynamic"])
def test_loaded_authority_reaches_pinned_crypto_qpk_and_mapper(tmp_path, payload_builder):
    from decision_mapper import map_strategy_decision_to_rotation_plan
    from strategy_runtime import load_research_only_strategy_runtime

    now = datetime.now(timezone.utc).replace(microsecond=0)
    runtime = load_research_only_strategy_runtime(PROFILE)
    payload = payload_builder(now)
    effective_config_digest = config_sha256(runtime.effective_runtime_config)
    payload["config_sha256"] = effective_config_digest
    path, digest = _write_authority(tmp_path, payload)
    with patch("live_risk_authority.resolve_strategy_revision", return_value=STRATEGY_REVISION), patch(
        "live_risk_authority.resolve_runner_revision", return_value=RUNNER_REVISION
    ):
        authority = load_live_risk_authority(
            env=_env(path, digest),
            runtime_target=_target(),
            strategy_revision=STRATEGY_REVISION,
            runner_revision=RUNNER_REVISION,
            config_sha256=effective_config_digest,
            now_utc=now,
        )
        evaluation = runtime.evaluate(
            prices={"ETHUSDT": 3000.0, "SOLUSDT": 180.0, "BTCUSDT": 60000.0, "BNBUSDT": 100.0},
            trend_indicators={
                "ETHUSDT": {
                    "close": 3000.0, "sma20": 2800.0, "sma60": 2600.0, "sma200": 2200.0,
                    "roc20": 0.20, "roc60": 0.35, "roc120": 0.60, "vol20": 0.25,
                    "avg_quote_vol_30": 60000000.0, "avg_quote_vol_90": 50000000.0,
                    "avg_quote_vol_180": 45000000.0, "trend_persist_90": 0.80,
                    "age_days": 500, "atr14": 120.0,
                },
                "SOLUSDT": {
                    "close": 180.0, "sma20": 170.0, "sma60": 160.0, "sma200": 120.0,
                    "roc20": 0.28, "roc60": 0.45, "roc120": 0.75, "vol20": 0.30,
                    "avg_quote_vol_30": 42000000.0, "avg_quote_vol_90": 39000000.0,
                    "avg_quote_vol_180": 36000000.0, "trend_persist_90": 0.76,
                    "age_days": 450, "atr14": 8.0,
                },
            },
            btc_snapshot={"regime_on": True, "btc_roc20": 0.08, "btc_roc60": 0.16, "btc_roc120": 0.30},
            account_metrics={"total_equity": 3700.0, "cash_usdt": 2450.0, "trend_value": 0.0, "dca_value": 1200.0},
            trend_universe_symbols=("ETHUSDT",),
            portfolio_trend_universe_symbols=("ETHUSDT", "SOLUSDT"),
            balances={"BTCUSDT": 0.02, "ETHUSDT": 0.0, "SOLUSDT": 0.0, "BNBUSDT": 0.5},
            state={},
            translator=lambda key, **_kwargs: key,
            now_utc=now,
            risk_authority=authority,
            runtime_target=_target(),
            trend_pool_contract={"source_kind": "fixture", "symbols": ["ETHUSDT", "SOLUSDT"]},
            execution_mode="live",
            portfolio_risk_symbols=("BTCUSDT", "BNBUSDT", "ETHUSDT", "SOLUSDT"),
            get_symbol_trade_state_fn=lambda state, symbol: state.get(
                symbol, {"is_holding": False, "entry_price": 0.0, "highest_price": 0.0}
            ),
            set_symbol_trade_state_fn=lambda state, symbol, symbol_state: state.__setitem__(symbol, dict(symbol_state)),
        )

    assessment = evaluation.decision.diagnostics["member_risk_assessment"]
    assert assessment["outcome"] == "APPROVE", assessment["reason_codes"]
    assert map_strategy_decision_to_rotation_plan(evaluation.decision)["execution_permitted"]


def test_build_live_runtime_reads_authority_into_existing_runtime(tmp_path):
    from runtime_config_support import build_live_runtime
    from strategy_runtime import load_research_only_strategy_runtime

    now = datetime.now(timezone.utc).replace(microsecond=0)
    target = _target()
    strategy_runtime = load_research_only_strategy_runtime(PROFILE)
    payload = _payload(now)
    payload["config_sha256"] = config_sha256(strategy_runtime.effective_runtime_config)
    path, digest = _write_authority(tmp_path, payload)
    target_json = {
        "platform_id": "binance",
        "strategy_profile": PROFILE,
        "dry_run_only": False,
        "deployment_selector": "default",
        "account_selector": ["crypto-combo-live"],
        "account_scope": "crypto_combo",
        "service_name": "binance-platform",
        "execution_mode": "live",
        "market": "CRYPTO",
        "market_calendar": "24/7",
        "market_timezone": "UTC",
    }
    env = {
        "STRATEGY_PROFILE": PROFILE,
        "RUNTIME_TARGET_JSON": json.dumps(target_json),
        "BINANCE_DRY_RUN": "false",
        "RUNTIME_TARGET_ENABLED": "true",
        **_env(path, digest),
    }
    import runtime_config_support

    definition = runtime_config_support.resolve_research_strategy_definition(
        PROFILE, platform_id="binance"
    )
    with patch.dict("os.environ", env, clear=True), patch(
        "live_risk_authority.resolve_strategy_revision", return_value=STRATEGY_REVISION
    ), patch("live_risk_authority.resolve_runner_revision", return_value=RUNNER_REVISION), patch(
        "runtime_config_support.resolve_runtime_target_strategy",
        return_value=(target, definition),
    ):
        runtime = build_live_runtime(now_utc=now)

    assert runtime.risk_authority is not None
    assert runtime.risk_authority.authority_receipt_sha256 == digest
    assert runtime.runtime_target.account_scope == "crypto_combo"


def test_build_runtime_main_allocation_and_qpk_gate_reject_limits(tmp_path):
    import main
    from decision_mapper import map_strategy_decision_to_rotation_plan
    from runtime_config_support import build_live_runtime
    from strategy_runtime import load_research_only_strategy_runtime

    now = datetime.now(timezone.utc).replace(microsecond=0)
    target = _target()
    strategy_runtime = load_research_only_strategy_runtime(PROFILE)
    payload = _payload(now)
    payload["config_sha256"] = config_sha256(strategy_runtime.effective_runtime_config)
    path, digest = _write_authority(tmp_path, payload)
    target_json = {
        "platform_id": "binance", "strategy_profile": PROFILE, "dry_run_only": False,
        "deployment_selector": "default", "account_selector": ["crypto-combo-live"],
        "account_scope": "crypto_combo", "service_name": "binance-platform",
        "execution_mode": "live", "market": "CRYPTO", "market_calendar": "24/7",
        "market_timezone": "UTC",
    }
    env = {
        "STRATEGY_PROFILE": PROFILE, "RUNTIME_TARGET_JSON": json.dumps(target_json),
        "BINANCE_DRY_RUN": "false", "RUNTIME_TARGET_ENABLED": "true", **_env(path, digest),
    }
    import runtime_config_support

    definition = runtime_config_support.resolve_research_strategy_definition(
        PROFILE, platform_id="binance"
    )
    indicators = {
        "ETHUSDT": {
            "close": 3000.0, "sma20": 2800.0, "sma60": 2600.0, "sma200": 2200.0,
            "roc20": 0.20, "roc60": 0.35, "roc120": 0.60, "vol20": 0.25,
            "avg_quote_vol_30": 60000000.0, "avg_quote_vol_90": 50000000.0,
            "avg_quote_vol_180": 45000000.0, "trend_persist_90": 0.80, "age_days": 500, "atr14": 120.0,
        },
        "SOLUSDT": {
            "close": 180.0, "sma20": 170.0, "sma60": 160.0, "sma200": 120.0,
            "roc20": 0.28, "roc60": 0.45, "roc120": 0.75, "vol20": 0.30,
            "avg_quote_vol_30": 42000000.0, "avg_quote_vol_90": 39000000.0,
            "avg_quote_vol_180": 36000000.0, "trend_persist_90": 0.76, "age_days": 450, "atr14": 8.0,
        },
    }
    prices = {"ETHUSDT": 3000.0, "SOLUSDT": 180.0, "BTCUSDT": 60000.0, "BNBUSDT": 100.0}
    balances = {"BTCUSDT": 0.02, "ETHUSDT": 0.0, "SOLUSDT": 0.0, "BNBUSDT": 0.5}
    with patch.dict("os.environ", env, clear=True), patch(
        "live_risk_authority.resolve_strategy_revision", return_value=STRATEGY_REVISION
    ), patch("live_risk_authority.resolve_runner_revision", return_value=RUNNER_REVISION), patch(
        "runtime_config_support.resolve_runtime_target_strategy", return_value=(target, definition)
    ):
        runtime = build_live_runtime(now_utc=now)
        runtime.trend_pool_contract = {"source_kind": "fixture", "symbols": ["ETHUSDT", "SOLUSDT"]}
        old_strategy_runtime = main.STRATEGY_RUNTIME
        main.STRATEGY_RUNTIME = strategy_runtime
        try:
            kwargs = {
                "runtime": runtime, "state": {},
                "runtime_trend_universe": {"ETHUSDT": {"base_asset": "ETH"}, "SOLUSDT": {"base_asset": "SOL"}},
                "trend_indicators": indicators,
                "btc_snapshot": {"regime_on": True, "btc_roc20": 0.08, "btc_roc60": 0.16, "btc_roc120": 0.30},
                "prices": prices, "balances": balances, "u_total": 2450.0, "fuel_val": 50.0,
                "allow_new_trend_entries": True, "allow_pool_refresh": True,
            }
            evaluation = main._resolve_strategy_evaluation(**kwargs)
            plan = map_strategy_decision_to_rotation_plan(evaluation.decision)
            assessment = evaluation.decision.diagnostics["member_risk_assessment"]
            assert assessment["outcome"] == "APPROVE", assessment["reason_codes"]
            assert plan["execution_permitted"] is True

            asset_limited = replace(
                runtime.risk_authority,
                mandate={**runtime.risk_authority.mandate, "allowed_nonzero_assets": ["BTCUSDT"]},
            )
            runtime.risk_authority = asset_limited
            rejected_asset = main._resolve_strategy_evaluation(**kwargs)
            asset_assessment = rejected_asset.decision.diagnostics["member_risk_assessment"]
            assert asset_assessment["outcome"] == "REJECT"
            assert "asset_not_authorized" in asset_assessment["reason_codes"]

            budget_limited = replace(
                runtime.risk_authority,
                mandate={**runtime.risk_authority.mandate, "loss_budget": 1.0},
            )
            runtime.risk_authority = budget_limited
            rejected_budget = main._resolve_strategy_evaluation(**kwargs)
            budget_assessment = rejected_budget.decision.diagnostics["member_risk_assessment"]
            assert budget_assessment["outcome"] == "REJECT"
            assert "budget_authority_exceeded" in budget_assessment["reason_codes"]
        finally:
            main.STRATEGY_RUNTIME = old_strategy_runtime

    payload = _payload(now)
    payload["mandate"]["expires_at"] = (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    path, digest = _write_authority(tmp_path, payload)
    with pytest.raises(LiveRiskAuthorityError, match="authority expired"):
        load_live_risk_authority(
            env=_env(path, digest),
            runtime_target=_target(),
            strategy_revision=STRATEGY_REVISION,
            runner_revision=RUNNER_REVISION,
            config_sha256=CONFIG_SHA256,
            now_utc=now,
        )
