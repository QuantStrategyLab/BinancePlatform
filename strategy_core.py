import os

from strategy_loader import load_strategy_component

_CORE_MODULE = load_strategy_component(
    os.getenv("STRATEGY_PROFILE"),
    component_name="core",
)

DEFAULT_POOL_SCORE_WEIGHTS = _CORE_MODULE.DEFAULT_POOL_SCORE_WEIGHTS
allocate_trend_buy_budget = _CORE_MODULE.allocate_trend_buy_budget
build_rotation_pool_ranking = _CORE_MODULE.build_rotation_pool_ranking
build_stable_quality_pool = _CORE_MODULE.build_stable_quality_pool
compute_allocation_budgets = _CORE_MODULE.compute_allocation_budgets
get_dynamic_btc_base_order = _CORE_MODULE.get_dynamic_btc_base_order
get_dynamic_btc_target_ratio = _CORE_MODULE.get_dynamic_btc_target_ratio
is_missing = _CORE_MODULE.is_missing
rank_normalize = _CORE_MODULE.rank_normalize
safe_float = _CORE_MODULE.safe_float
select_rotation_weights = _CORE_MODULE.select_rotation_weights

__all__ = [
    "DEFAULT_POOL_SCORE_WEIGHTS",
    "allocate_trend_buy_budget",
    "build_rotation_pool_ranking",
    "build_stable_quality_pool",
    "compute_allocation_budgets",
    "get_dynamic_btc_base_order",
    "get_dynamic_btc_target_ratio",
    "is_missing",
    "rank_normalize",
    "safe_float",
    "select_rotation_weights",
]
