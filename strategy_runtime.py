from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from quant_platform_kit import PortfolioSnapshot, Position, build_strategy_evaluation_inputs
from quant_platform_kit.strategy_contracts import (
    StrategyContext,
    StrategyDecision,
    StrategyEntrypoint,
    StrategyRuntimeAdapter,
    build_strategy_context_from_available_inputs,
)

from crypto_strategies import get_platform_runtime_adapter
from strategy_loader import load_strategy_entrypoint_for_profile
from strategy_registry import BINANCE_PLATFORM, resolve_strategy_metadata
from trend_pool_support import get_default_live_pool_candidates as tp_get_default_live_pool_candidates


DEFAULT_LOCAL_TREND_POOL_ARTIFACT = Path(__file__).resolve().parent / "artifacts" / "live_pool_legacy.json"


@dataclass(frozen=True)
class StrategyEvaluationResult:
    decision: StrategyDecision
    account_metrics: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)


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
        return int(self.merged_runtime_config["trend_pool_size"])

    @property
    def artifact_contract(self) -> dict[str, Any]:
        return {
            "version": str(self.merged_runtime_config.get("artifact_contract_version", "")),
            "max_age_days": int(self.merged_runtime_config.get("artifact_max_age_days", 45)),
            "acceptable_modes": tuple(self.merged_runtime_config.get("artifact_acceptable_modes", ())),
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
        ctx = StrategyContext(
            as_of=ctx.as_of,
            market_data=ctx.market_data,
            portfolio=ctx.portfolio,
            state=ctx.state,
            runtime_config=ctx.runtime_config,
            capabilities=ctx.capabilities,
            artifacts={"trend_pool_contract": self.artifact_contract},
        )
        decision = self.entrypoint.evaluate(ctx)
        return StrategyEvaluationResult(
            decision=decision,
            account_metrics=dict(account_metrics),
            metadata={
                "strategy_profile": self.profile,
                "strategy_display_name": resolve_strategy_metadata(
                    self.profile,
                    platform_id=BINANCE_PLATFORM,
                ).display_name,
            },
        )


def load_strategy_runtime(raw_profile: str | None) -> LoadedStrategyRuntime:
    entrypoint = load_strategy_entrypoint_for_profile(raw_profile)
    runtime_adapter = get_platform_runtime_adapter(
        entrypoint.manifest.profile,
        platform_id=BINANCE_PLATFORM,
    )
    merged_runtime_config = dict(entrypoint.manifest.default_config)
    local_artifact_candidates = tuple(
        Path(path) for path in tp_get_default_live_pool_candidates(DEFAULT_LOCAL_TREND_POOL_ARTIFACT)
    )
    return LoadedStrategyRuntime(
        entrypoint=entrypoint,
        runtime_adapter=runtime_adapter,
        merged_runtime_config=merged_runtime_config,
        local_artifact_candidates=local_artifact_candidates,
    )
