from __future__ import annotations

from crypto_strategies import get_strategy_definitions as get_crypto_strategy_definitions

from quant_platform_kit.common.strategies import (
    CRYPTO_DOMAIN,
    get_supported_profiles_for_platform as qpk_get_supported_profiles_for_platform,
    resolve_strategy_definition as qpk_resolve_strategy_definition,
)

BINANCE_PLATFORM = "binance"


DEFAULT_STRATEGY_PROFILE = "crypto_leader_rotation"

STRATEGY_DEFINITIONS = get_crypto_strategy_definitions()

PLATFORM_SUPPORTED_DOMAINS: dict[str, frozenset[str]] = {
    BINANCE_PLATFORM: frozenset({CRYPTO_DOMAIN}),
}

SUPPORTED_STRATEGY_PROFILES = frozenset(STRATEGY_DEFINITIONS)


def get_supported_profiles_for_platform(platform_id: str) -> frozenset[str]:
    return qpk_get_supported_profiles_for_platform(
        STRATEGY_DEFINITIONS,
        PLATFORM_SUPPORTED_DOMAINS,
        platform_id=platform_id,
    )


def resolve_strategy_definition(
    raw_value: str | None,
    *,
    platform_id: str,
) -> StrategyDefinition:
    return qpk_resolve_strategy_definition(
        raw_value,
        platform_id=platform_id,
        strategy_definitions=STRATEGY_DEFINITIONS,
        platform_supported_domains=PLATFORM_SUPPORTED_DOMAINS,
        default_profile=DEFAULT_STRATEGY_PROFILE,
    )
