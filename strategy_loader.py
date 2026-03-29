from __future__ import annotations

from types import ModuleType

from quant_platform_kit.common.strategies import load_strategy_component_module

from strategy_registry import BINANCE_PLATFORM, resolve_strategy_definition


def load_strategy_component(raw_profile: str | None, *, component_name: str) -> ModuleType:
    definition = resolve_strategy_definition(
        raw_profile,
        platform_id=BINANCE_PLATFORM,
    )
    return load_strategy_component_module(
        definition,
        component_name=component_name,
    )
