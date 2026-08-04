from __future__ import annotations

import os
import hashlib
import json
import re
from dataclasses import asdict, is_dataclass
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from quant_platform_kit import PortfolioSnapshot, Position, build_strategy_evaluation_inputs
from quant_platform_kit.risk.gate import assess_with_evidence as qpk_assess_with_evidence
from quant_platform_kit.strategy_contracts import (
    StrategyContext,
    StrategyDecision,
    StrategyEntrypoint,
    StrategyRuntimeAdapter,
    build_strategy_context_from_available_inputs,
    resolve_strategy_artifact_contract,
)

from crypto_strategies import get_platform_runtime_adapter
from strategy_loader import load_strategy_entrypoint_for_profile
from strategy_registry import BINANCE_PLATFORM, resolve_strategy_metadata
from trend_pool_support import get_default_live_pool_candidates as tp_get_default_live_pool_candidates


DEFAULT_LOCAL_TREND_POOL_ARTIFACT = Path(__file__).resolve().parent / "artifacts" / "live_pool_legacy.json"
# Ensure artifacts directory exists so local-fallback path never fails with FileNotFoundError
DEFAULT_LOCAL_TREND_POOL_ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
DEFAULT_TREND_POOL_SIZE = 5
BINANCE_RESEARCH_MANDATE_RECEIPT_SHA256 = "246c39b8023b25f913bf1e67dc175005955a7102f3727dfc1bd8e981cf8128ee"
QPK_RISK_SOURCE_REVISION = "b371322b948e4298920a7d8613b155245dcd5f8d"
COMBO_RUNTIME_ENV_OVERRIDES: tuple[tuple[str, str, str], ...] = (
    ("BTC_WEIGHT", "btc_weight", "ratio"),
    ("TREND_WEIGHT", "trend_weight", "ratio"),
    ("DYNAMIC_MODE", "dynamic_mode", "bool"),
    ("DYNAMIC_REGIME_MODE", "dynamic_regime_mode", "regime_mode"),
    ("DYNAMIC_REGIME_OFF_CUT", "dynamic_regime_off_cut", "ratio"),
    ("DYNAMIC_HARD_SMA200_RATIO", "dynamic_hard_sma200_ratio", "positive_float"),
    ("DYNAMIC_HARD_MA200_SLOPE", "dynamic_hard_ma200_slope", "float"),
    ("DYNAMIC_SOFT_SMA200_RATIO", "dynamic_soft_sma200_ratio", "positive_float"),
    ("DYNAMIC_HARD_BTC_WEIGHT", "dynamic_hard_btc_weight", "ratio"),
    ("DYNAMIC_HARD_TREND_WEIGHT", "dynamic_hard_trend_weight", "ratio"),
    ("DYNAMIC_SOFT_BTC_WEIGHT", "dynamic_soft_btc_weight", "ratio"),
    ("DYNAMIC_SOFT_TREND_WEIGHT", "dynamic_soft_trend_weight", "ratio"),
    ("ROTATION_TOP_N", "rotation_top_n", "int"),
    ("TARGET_VOL", "target_vol", "positive_float"),
    ("CIRCUIT_BREAKER_ENABLED", "circuit_breaker_enabled", "bool"),
    ("ZSCORE_EXIT_RISK_REDUCED_EXPOSURE", "zscore_exit_risk_reduced_exposure", "ratio"),
    ("ZSCORE_EXIT_RISK_OFF_EXPOSURE", "zscore_exit_risk_off_exposure", "ratio"),
    ("ZSCORE_EXIT_ALLOW_OUTSIDE_EXECUTION_WINDOW", "zscore_exit_allow_outside_execution_window", "bool"),
)


def _parse_env_bool(name: str, raw: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} must be boolean-like")


def _parse_runtime_env_value(name: str, raw: str, kind: str) -> Any:
    if kind == "bool":
        return _parse_env_bool(name, raw)
    if kind == "regime_mode":
        normalized = raw.strip().lower().replace("-", "_")
        if normalized == "legacy":
            return "legacy"
        if normalized in {"dual", "dual_leg", "tiered", "cash_cap"}:
            return "dual_leg"
        raise ValueError(f"{name} must be legacy or dual_leg")
    if kind == "int":
        value = int(raw)
        if value < 1:
            raise ValueError(f"{name} must be >= 1")
        return value
    value = float(raw)
    if kind == "float":
        return value
    if kind == "ratio":
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be between 0 and 1")
        return value
    if kind == "positive_float":
        if value <= 0.0:
            raise ValueError(f"{name} must be > 0")
        return value
    raise ValueError(f"unsupported runtime env parser: {kind}")


def _load_combo_runtime_overrides() -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for env_name, config_key, kind in COMBO_RUNTIME_ENV_OVERRIDES:
        raw = os.getenv(env_name)
        if raw is None or not raw.strip():
            continue
        overrides[config_key] = _parse_runtime_env_value(env_name, raw, kind)
    return overrides


@dataclass(frozen=True)
class StrategyEvaluationResult:
    decision: StrategyDecision
    account_metrics: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccountGateResult:
    decision: StrategyDecision
    member_risk_assessment: Mapping[str, Any]
    account_risk_assessment: Mapping[str, Any]
    cap_assessment: Mapping[str, Any]
    order_authorization: Mapping[str, Any]


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_binance_research_mandate() -> dict[str, Any]:
    """Return the immutable zero-cap authority object; it can never authorize an order."""
    return {
        "mandate_id": "binance_crypto_research_only_v1",
        "mandate_version": "2026-08-04.1",
        "authority_receipt_sha256": BINANCE_RESEARCH_MANDATE_RECEIPT_SHA256,
        "authority_scope": "RESEARCH_ONLY",
        "strategy_profile": "crypto_live_pool_rotation",
        "account_mode": "single_strategy_account_v1",
        "effective_at": "2026-08-04T04:27:55Z",
        "expires_at": "2026-09-03T15:59:59Z",
        "max_snapshot_age_seconds": 300,
        "effective_exposure_cap": 0.0,
        "loss_budget": 0.0,
        "product_caps": 0.0,
        "nominal_caps": 0.0,
        "product_leverage_factors": {},
        "allowed_nonzero_assets": [],
        "source_revision": QPK_RISK_SOURCE_REVISION,
    }


def _assessment_payload(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    return dict(vars(value))


def apply_account_risk_gate(
    decision: StrategyDecision,
    *,
    portfolio_snapshot: Any,
    release_identity_sha256: str,
    run_id: str,
    mandate_provenance: Mapping[str, Any],
    market_data: Mapping[str, Any],
) -> AccountGateResult:
    """Call the QPK ACCOUNT gate and bind its receipt to the current release/run."""
    member = dict(decision.diagnostics.get("member_risk_assessment", {}))
    result = qpk_assess_with_evidence(
        decision,
        portfolio_snapshot,
        scope="ACCOUNT",
        mandate_provenance=mandate_provenance,
        market_data=market_data,
    )
    account = _assessment_payload(result.assessment)
    decision_digest = str(account.get("decision_digest_sha256", ""))
    snapshot_digest = str(account.get("portfolio_snapshot_digest_sha256", ""))
    release_digest_valid = bool(re.fullmatch(r"[0-9a-f]{64}", str(release_identity_sha256)))
    exact_binding = (
        member.get("scope") == "MEMBER"
        and member.get("outcome") == "APPROVE"
        and account.get("scope") == "ACCOUNT"
        and account.get("outcome") == "APPROVE"
        and member.get("decision_digest_sha256") == decision_digest
        and release_digest_valid
        and bool(snapshot_digest)
    )
    capital_authority = (
        mandate_provenance.get("authority_scope") in {"PAPER", "LIVE"}
        and float(mandate_provenance.get("effective_exposure_cap", 0.0) or 0.0) > 0.0
        and bool(mandate_provenance.get("allowed_nonzero_assets"))
    )
    outcome = "APPROVE" if exact_binding and capital_authority else "REJECT"
    cap_assessment = {
        "outcome": outcome,
        "mandate_id": str(mandate_provenance.get("mandate_id", "")),
        "mandate_version": str(mandate_provenance.get("mandate_version", "")),
        "mandate_authority_receipt_sha256": str(mandate_provenance.get("authority_receipt_sha256", "")),
        "mandate_scope": str(mandate_provenance.get("authority_scope", "")),
        "effective_exposure_cap": float(mandate_provenance.get("effective_exposure_cap", 0.0) or 0.0),
        "decision_digest_sha256": decision_digest,
        "release_identity_sha256": str(release_identity_sha256),
        "account_snapshot_sha256": snapshot_digest,
        "account_assessment_sha256": str(account.get("assessment_sha256", "")),
    }
    authorization = {
        "contract_version": "qsl.binance_order_authorization.v1",
        "outcome": outcome,
        "run_id": str(run_id),
        "decision_digest_sha256": decision_digest,
        "release_identity_sha256": str(release_identity_sha256),
        "account_snapshot_sha256": snapshot_digest,
        "member_assessment_sha256": str(member.get("assessment_sha256", "")),
        "account_assessment_sha256": str(account.get("assessment_sha256", "")),
        "mandate_authority_receipt_sha256": str(mandate_provenance.get("authority_receipt_sha256", "")),
        "mandate_scope": str(mandate_provenance.get("authority_scope", "")),
    }
    authorization["authorization_sha256"] = _canonical_sha256(authorization)
    cap_assessment["qpk_source_revision"] = str(mandate_provenance.get("source_revision", ""))
    cap_assessment["order_authorization_sha256"] = authorization["authorization_sha256"]
    gated_decision = StrategyDecision(
        positions=result.decision.positions if outcome == "APPROVE" else (),
        budgets=result.decision.budgets if outcome == "APPROVE" else (),
        risk_flags=tuple(result.decision.risk_flags or ()) + (("rejected:account_gate",) if outcome == "REJECT" else ()),
        diagnostics={
            **dict(result.decision.diagnostics or {}),
            "member_risk_assessment": member,
            "account_risk_assessment": account,
            "cap_assessment": cap_assessment,
            "order_authorization": authorization,
        },
    )
    return AccountGateResult(
        decision=gated_decision,
        member_risk_assessment=member,
        account_risk_assessment=account,
        cap_assessment=cap_assessment,
        order_authorization=authorization,
    )


@dataclass(frozen=True)
class LoadedStrategyRuntime:
    entrypoint: StrategyEntrypoint
    runtime_adapter: StrategyRuntimeAdapter
    runtime_overrides: Mapping[str, Any] = field(default_factory=dict)
    merged_runtime_config: Mapping[str, Any] = field(default_factory=dict)
    local_artifact_candidates: tuple[Path, ...] = ()

    @property
    def profile(self) -> str:
        return self.entrypoint.manifest.profile

    @property
    def trend_pool_size(self) -> int:
        return int(self.merged_runtime_config.get("trend_pool_size", DEFAULT_TREND_POOL_SIZE))

    @property
    def artifact_contract(self) -> dict[str, Any]:
        contract = resolve_strategy_artifact_contract(self.runtime_adapter)
        return {
            "version": str(
                contract.snapshot_contract_version
                or self.merged_runtime_config.get("artifact_contract_version", "")
            ),
            "max_age_days": int(self.merged_runtime_config.get("artifact_max_age_days", 45)),
            "acceptable_modes": tuple(self.merged_runtime_config.get("artifact_acceptable_modes", ())),
            "requires_artifacts": bool(contract.requires_snapshot_artifacts),
            "requires_manifest": bool(contract.requires_snapshot_manifest_path),
            "config_source_policy": str(contract.config_source_policy),
            "default_local_candidates": tuple(str(path) for path in self.local_artifact_candidates),
        }

    @property
    def default_local_artifact_path(self) -> Path:
        if self.local_artifact_candidates:
            return self.local_artifact_candidates[0]
        return DEFAULT_LOCAL_TREND_POOL_ARTIFACT

    def compute_account_metrics(
        self,
        runtime_trend_universe,
        balances,
        prices,
        u_total,
        fuel_val,
    ) -> dict[str, float]:
        trend_value = sum(float(balances[symbol]) * float(prices[symbol]) for symbol in runtime_trend_universe)
        dca_value = float(balances["BTCUSDT"]) * float(prices["BTCUSDT"])
        total_equity = float(u_total) + float(fuel_val) + trend_value + dca_value
        return {
            "cash_usdt": float(u_total),
            "trend_value": trend_value,
            "dca_value": dca_value,
            "total_equity": total_equity,
        }

    def build_portfolio_snapshot(
        self,
        *,
        account_metrics: Mapping[str, Any],
        balances: Mapping[str, Any] | None,
        prices: Mapping[str, Any],
        trend_universe_symbols: tuple[str, ...],
        as_of: datetime,
    ) -> PortfolioSnapshot:
        positions: list[Position] = []
        normalized_symbols = ("BTCUSDT",) + tuple(str(symbol) for symbol in trend_universe_symbols)
        balances_map = dict(balances or {})
        for symbol in normalized_symbols:
            quantity = float(balances_map.get(symbol, 0.0) or 0.0)
            last_price = float(prices.get(symbol, 0.0) or 0.0)
            market_value = quantity * last_price
            if quantity <= 0.0 and market_value <= 0.0:
                continue
            positions.append(
                Position(
                    symbol=symbol,
                    quantity=quantity,
                    market_value=market_value,
                )
            )
        return PortfolioSnapshot(
            as_of=as_of,
            total_equity=float(account_metrics["total_equity"]),
            buying_power=float(account_metrics["cash_usdt"]),
            cash_balance=float(account_metrics["cash_usdt"]),
            positions=tuple(positions),
            metadata={
                "account_metrics": dict(account_metrics),
                "cash_available_for_trading": float(account_metrics["cash_usdt"]),
                "trend_value": float(account_metrics["trend_value"]),
                "dca_value": float(account_metrics["dca_value"]),
                "observed_effective_exposure": (
                    (float(account_metrics["trend_value"]) + float(account_metrics["dca_value"]))
                    / float(account_metrics["total_equity"])
                    if float(account_metrics["total_equity"]) > 0.0
                    else 0.0
                ),
            },
        )

    def evaluate(
        self,
        *,
        prices,
        trend_indicators,
        btc_snapshot,
        account_metrics,
        trend_universe_symbols,
        state,
        translator: Callable[..., str],
        balances: Mapping[str, Any] | None = None,
        now_utc=None,
        allow_new_trend_entries: bool = True,
        allow_rotation_refresh: bool = True,
        get_symbol_trade_state_fn: Callable[..., Any] | None = None,
        set_symbol_trade_state_fn: Callable[..., Any] | None = None,
        release_identity: Mapping[str, Any] | None = None,
        release_identity_sha256: str = "",
        run_id: str = "",
    ) -> StrategyEvaluationResult:
        runtime_config = dict(self.runtime_overrides)
        runtime_config.update(
            {
                "translator": translator,
                "allow_new_trend_entries": bool(allow_new_trend_entries),
                "allow_rotation_refresh": bool(allow_rotation_refresh),
                "now_utc": now_utc,
            }
        )
        if get_symbol_trade_state_fn is not None:
            runtime_config["get_symbol_trade_state_fn"] = get_symbol_trade_state_fn
        if set_symbol_trade_state_fn is not None:
            runtime_config["set_symbol_trade_state_fn"] = set_symbol_trade_state_fn
        runtime_now = now_utc or datetime.now(timezone.utc)
        portfolio_snapshot = self.build_portfolio_snapshot(
            account_metrics=account_metrics,
            balances=balances,
            prices=prices,
            trend_universe_symbols=tuple(trend_universe_symbols),
            as_of=runtime_now,
        )
        from quant_platform_kit.strategy_lifecycle.live_equity import stamp_consecutive_losses_on_snapshot

        portfolio_snapshot = stamp_consecutive_losses_on_snapshot(
            portfolio_snapshot,
            strategy_profile=self.profile,
            domain="crypto",
            logger=getattr(self, "logger", None),
        )
        evaluation_inputs = build_strategy_evaluation_inputs(
            available_inputs=self.runtime_adapter.available_inputs,
            market_inputs={
                "market_prices": prices,
                "derived_indicators": trend_indicators,
                "benchmark_snapshot": btc_snapshot,
                "universe_snapshot": tuple(trend_universe_symbols),
            },
            portfolio_snapshot=portfolio_snapshot,
        )
        ctx = build_strategy_context_from_available_inputs(
            entrypoint=self.entrypoint,
            runtime_adapter=self.runtime_adapter,
            as_of=runtime_now,
            available_inputs=evaluation_inputs,
            state=state,
            runtime_config=runtime_config,
            capabilities={"platform": BINANCE_PLATFORM},
        )
        mandate_provenance = build_binance_research_mandate()
        ctx = StrategyContext(
            as_of=ctx.as_of,
            market_data=ctx.market_data,
            portfolio=ctx.portfolio,
            state=ctx.state,
            runtime_config=ctx.runtime_config,
            capabilities=ctx.capabilities,
            artifacts={
                "trend_pool_contract": self.artifact_contract,
                "runtime_evidence_identity": dict(release_identity or {}),
                "release_identity_sha256": str(release_identity_sha256),
                "mandate_provenance": mandate_provenance,
            },
        )
        decision = self.entrypoint.evaluate(ctx)
        account_gate = apply_account_risk_gate(
            decision,
            portfolio_snapshot=portfolio_snapshot,
            release_identity_sha256=release_identity_sha256,
            run_id=run_id,
            mandate_provenance=mandate_provenance,
            market_data=dict(ctx.market_data or {}),
        )
        return StrategyEvaluationResult(
            decision=account_gate.decision,
            account_metrics=dict(account_metrics),
            metadata={
                "strategy_profile": self.profile,
                "strategy_display_name": resolve_strategy_metadata(
                    self.profile,
                    platform_id=BINANCE_PLATFORM,
                ).display_name,
                "member_risk_assessment": dict(account_gate.member_risk_assessment),
                "account_risk_assessment": dict(account_gate.account_risk_assessment),
                "cap_assessment": dict(account_gate.cap_assessment),
                "order_authorization": dict(account_gate.order_authorization),
            },
        )


def load_strategy_runtime(raw_profile: str | None) -> LoadedStrategyRuntime:
    entrypoint = load_strategy_entrypoint_for_profile(raw_profile)
    runtime_adapter = get_platform_runtime_adapter(
        entrypoint.manifest.profile,
        platform_id=BINANCE_PLATFORM,
    )
    merged_runtime_config = dict(entrypoint.manifest.default_config)
    runtime_overrides: dict[str, Any] = {}
    if entrypoint.manifest.profile == "crypto_equity_combo":
        runtime_overrides.update(_load_combo_runtime_overrides())
    local_artifact_candidates = tuple(
        Path(path) for path in tp_get_default_live_pool_candidates(DEFAULT_LOCAL_TREND_POOL_ARTIFACT)
    )
    return LoadedStrategyRuntime(
        entrypoint=entrypoint,
        runtime_adapter=runtime_adapter,
        runtime_overrides=runtime_overrides,
        merged_runtime_config=merged_runtime_config,
        local_artifact_candidates=local_artifact_candidates,
    )
