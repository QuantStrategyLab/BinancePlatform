from __future__ import annotations

from crypto_strategies import get_platform_runtime_adapter, get_strategy_catalog

from quant_platform_kit.common.strategies import (
    CRYPTO_DOMAIN,
    PlatformCapabilityMatrix,
    PlatformStrategyPolicy,
    StrategyDefinition,
    build_platform_profile_matrix,
    build_platform_profile_status_matrix,
    derive_enabled_profiles_for_platform,
    derive_eligible_profiles_for_platform,
    get_catalog_strategy_metadata,
    get_enabled_profiles_for_platform,
    resolve_platform_strategy_definition,
)

BINANCE_PLATFORM = "binance"


DEFAULT_STRATEGY_PROFILE = "crypto_leader_rotation"
ROLLBACK_STRATEGY_PROFILE = DEFAULT_STRATEGY_PROFILE

STRATEGY_CATALOG = get_strategy_catalog()
STRATEGY_DEFINITIONS = dict(STRATEGY_CATALOG.definitions)
BINANCE_ROLLOUT_ALLOWLIST = frozenset(STRATEGY_DEFINITIONS)

PLATFORM_SUPPORTED_DOMAINS: dict[str, frozenset[str]] = {
    BINANCE_PLATFORM: frozenset({CRYPTO_DOMAIN}),
}
PLATFORM_CAPABILITY_MATRIX = PlatformCapabilityMatrix(
    platform_id=BINANCE_PLATFORM,
    supported_domains=PLATFORM_SUPPORTED_DOMAINS[BINANCE_PLATFORM],
    supported_target_modes=frozenset({"weight"}),
    supported_inputs=frozenset(
        {
            "market_prices",
            "derived_indicators",
            "benchmark_snapshot",
            "portfolio_snapshot",
            "universe_snapshot",
        }
    ),
    supported_capabilities=frozenset(),
)
ELIGIBLE_STRATEGY_PROFILES = derive_eligible_profiles_for_platform(
    STRATEGY_CATALOG,
    capability_matrix=PLATFORM_CAPABILITY_MATRIX,
    runtime_adapter_loader=lambda profile: get_platform_runtime_adapter(
        profile,
        platform_id=BINANCE_PLATFORM,
    ),
)
BINANCE_ENABLED_PROFILES = derive_enabled_profiles_for_platform(
    STRATEGY_CATALOG,
    capability_matrix=PLATFORM_CAPABILITY_MATRIX,
    runtime_adapter_loader=lambda profile: get_platform_runtime_adapter(
        profile,
        platform_id=BINANCE_PLATFORM,
    ),
    rollout_allowlist=BINANCE_ROLLOUT_ALLOWLIST,
)
PLATFORM_POLICY = PlatformStrategyPolicy(
    platform_id=BINANCE_PLATFORM,
    supported_domains=PLATFORM_SUPPORTED_DOMAINS[BINANCE_PLATFORM],
    enabled_profiles=BINANCE_ENABLED_PROFILES,
    default_profile=DEFAULT_STRATEGY_PROFILE,
    rollback_profile=ROLLBACK_STRATEGY_PROFILE,
)

SUPPORTED_STRATEGY_PROFILES = BINANCE_ENABLED_PROFILES


def get_eligible_profiles_for_platform(platform_id: str) -> frozenset[str]:
    if platform_id != BINANCE_PLATFORM:
        return frozenset()
    return ELIGIBLE_STRATEGY_PROFILES


def get_supported_profiles_for_platform(platform_id: str) -> frozenset[str]:
    return get_enabled_profiles_for_platform(platform_id, policy=PLATFORM_POLICY)


def get_platform_profile_matrix() -> list[dict[str, object]]:
    return build_platform_profile_matrix(STRATEGY_CATALOG, policy=PLATFORM_POLICY)


def get_platform_profile_status_matrix() -> list[dict[str, object]]:
    return build_platform_profile_status_matrix(
        STRATEGY_CATALOG,
        policy=PLATFORM_POLICY,
        eligible_profiles=ELIGIBLE_STRATEGY_PROFILES,
    )


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


def resolve_strategy_metadata(
    raw_value: str | None,
    *,
    platform_id: str,
):
    definition = resolve_strategy_definition(raw_value, platform_id=platform_id)
    return get_catalog_strategy_metadata(STRATEGY_CATALOG, definition.profile)
