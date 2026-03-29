import os

from strategy_loader import load_strategy_component

_ROTATION_MODULE = load_strategy_component(
    os.getenv("STRATEGY_PROFILE"),
    component_name="rotation",
)

get_trend_sell_reason = _ROTATION_MODULE.get_trend_sell_reason
plan_trend_buys = _ROTATION_MODULE.plan_trend_buys
refresh_rotation_pool = _ROTATION_MODULE.refresh_rotation_pool

__all__ = [
    "get_trend_sell_reason",
    "plan_trend_buys",
    "refresh_rotation_pool",
]
