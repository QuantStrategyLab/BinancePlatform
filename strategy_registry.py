from __future__ import annotations

from crypto_strategies import get_strategy_catalog

from quant_platform_kit.common.strategies import (
    CRYPTO_DOMAIN,
    PlatformStrategyPolicy,
    StrategyDefinition,
    build_platform_profile_matrix,
    get_enabled_profiles_for_platform,
    resolve_platform_strategy_definition,
)

BINANCE_PLATFORM = "binance"


DEFAULT_STRATEGY_PROFILE = "crypto_leader_rotation"

STRATEGY_CATALOG = get_strategy_catalog()
STRATEGY_DEFINITIONS = dict(STRATEGY_CATALOG.definitions)

PLATFORM_SUPPORTED_DOMAINS: dict[str, frozenset[str]] = {
    BINANCE_PLATFORM: frozenset({CRYPTO_DOMAIN}),
}
PLATFORM_POLICY = PlatformStrategyPolicy(
    platform_id=BINANCE_PLATFORM,
    supported_domains=PLATFORM_SUPPORTED_DOMAINS[BINANCE_PLATFORM],
    enabled_profiles=frozenset(STRATEGY_DEFINITIONS),
    default_profile=DEFAULT_STRATEGY_PROFILE,
    rollback_profile=DEFAULT_STRATEGY_PROFILE,
)

SUPPORTED_STRATEGY_PROFILES = frozenset(STRATEGY_DEFINITIONS)


def get_supported_profiles_for_platform(platform_id: str) -> frozenset[str]:
    return get_enabled_profiles_for_platform(platform_id, policy=PLATFORM_POLICY)


def get_platform_profile_matrix() -> list[dict[str, object]]:
    return build_platform_profile_matrix(STRATEGY_CATALOG, policy=PLATFORM_POLICY)


def resolve_strategy_definition(
    raw_value: str | None,
    *,
    platform_id: str,
) -> StrategyDefinition:
    return resolve_platform_strategy_definition(
        raw_value,
        platform_id=platform_id,
        strategy_catalog=STRATEGY_CATALOG,
        policy=PLATFORM_POLICY,
    )
