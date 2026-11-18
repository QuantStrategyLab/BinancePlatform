"""Per-cycle RunContext for BinancePlatform (B09).

Trend universe and strategy-runtime bind to a cycle-scoped context during
execute_cycle. While a RunContext is bound, resolves do not fall back to module
STRATEGY_RUNTIME / TREND_UNIVERSE. Module symbols remain import-safe seeds and
out-of-cycle patch points only.
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


class RunContextError(RuntimeError):
    """Fail-closed when a bound cycle is missing a required RunContext field."""


def bind_run_context(ctx: RunContext) -> Token:
    return _ACTIVE_RUN_CONTEXT.set(ctx)


def reset_run_context(token: Token) -> None:
    _ACTIVE_RUN_CONTEXT.reset(token)


def get_active_run_context() -> Optional[RunContext]:
    return _ACTIVE_RUN_CONTEXT.get()


def resolve_trend_universe(
    fallback: Mapping[str, dict[str, Any]] | None = None,
) -> Mapping[str, dict[str, Any]]:
    ctx = _ACTIVE_RUN_CONTEXT.get()
    if ctx is not None:
        return ctx.trend_universe
    if fallback is None:
        raise RunContextError("trend_universe_unbound_requires_fallback")
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


def resolve_strategy_runtime(fallback: Any = None) -> Any:
    """Bound cycles must use ctx.strategy_runtime; unbound may use module fallback."""
    ctx = _ACTIVE_RUN_CONTEXT.get()
    if ctx is not None:
        if ctx.strategy_runtime is None:
            raise RunContextError("run_context_missing_strategy_runtime")
        return ctx.strategy_runtime
    if fallback is None:
        raise RunContextError("strategy_runtime_unbound_requires_fallback")
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
