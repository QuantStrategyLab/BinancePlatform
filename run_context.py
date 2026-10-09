"""Per-cycle RunContext for BinancePlatform (B09).

Trend universe (PR-1) and strategy-runtime handle (PR-2) bind to a cycle-scoped
context during execute_cycle. Module STRATEGY_RUNTIME / TREND_UNIVERSE remain
import-safe fallbacks and out-of-cycle patch points.
Does not change order-submission or state-owner semantics in runtime_support.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Callable, Mapping, MutableMapping, Optional


@dataclass
class RunContext:
    """Cycle-scoped mutable view. Not a replacement for ExecutionRuntime."""

    trend_universe: MutableMapping[str, dict[str, Any]]
    strategy_runtime: Any | None = None


_ACTIVE_RUN_CONTEXT: ContextVar[Optional[RunContext]] = ContextVar(
    "binance_active_run_context",
    default=None,
)


def bind_run_context(ctx: RunContext) -> Token:
    return _ACTIVE_RUN_CONTEXT.set(ctx)


def reset_run_context(token: Token) -> None:
    _ACTIVE_RUN_CONTEXT.reset(token)


def get_active_run_context() -> Optional[RunContext]:
    return _ACTIVE_RUN_CONTEXT.get()


def resolve_trend_universe(
    fallback: Mapping[str, dict[str, Any]],
) -> Mapping[str, dict[str, Any]]:
    ctx = _ACTIVE_RUN_CONTEXT.get()
    if ctx is not None:
        return ctx.trend_universe
    return fallback


def set_active_trend_universe(
    resolved: MutableMapping[str, dict[str, Any]],
    *,
    fallback_setter: Callable[[MutableMapping[str, dict[str, Any]]], None],
) -> None:
    ctx = _ACTIVE_RUN_CONTEXT.get()
    if ctx is not None:
        ctx.trend_universe = resolved
        return
    fallback_setter(resolved)


def resolve_strategy_runtime(fallback: Any) -> Any:
    """Prefer cycle-bound strategy runtime; else module/import-safe fallback."""
    ctx = _ACTIVE_RUN_CONTEXT.get()
    if ctx is not None and ctx.strategy_runtime is not None:
        return ctx.strategy_runtime
    return fallback


def set_active_strategy_runtime(
    activated: Any,
    *,
    fallback_setter: Callable[[Any], None],
) -> None:
    ctx = _ACTIVE_RUN_CONTEXT.get()
    if ctx is not None:
        ctx.strategy_runtime = activated
        return
    fallback_setter(activated)
