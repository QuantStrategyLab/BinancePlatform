"""Per-cycle RunContext for BinancePlatform (B09).

First slice: trend universe leaves module globals during execute_cycle.
Strategy-runtime activation stays on module globals until a follow-up PR.
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
    strategy_runtime: Any | None = None  # reserved for B09 phase 2


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
