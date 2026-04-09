from __future__ import annotations

import unittest

from crypto_strategies import get_platform_runtime_adapter, get_strategy_definitions

from strategy_registry import (
    BINANCE_PLATFORM,
    BINANCE_ENABLED_PROFILES,
    BINANCE_ROLLOUT_ALLOWLIST,
    ELIGIBLE_STRATEGY_PROFILES,
    PLATFORM_CAPABILITY_MATRIX,
    STRATEGY_DEFINITIONS,
)


class ContractGovernanceTests(unittest.TestCase):
    def test_registry_profiles_follow_catalog_and_rollout_allowlist(self) -> None:
        catalog_profiles = frozenset(get_strategy_definitions())
        self.assertEqual(frozenset(STRATEGY_DEFINITIONS), catalog_profiles)
        self.assertLessEqual(BINANCE_ENABLED_PROFILES, BINANCE_ROLLOUT_ALLOWLIST)
        self.assertLessEqual(BINANCE_ENABLED_PROFILES, ELIGIBLE_STRATEGY_PROFILES)

    def test_capability_matrix_matches_crypto_runtime_contract(self) -> None:
        for profile, definition in get_strategy_definitions().items():
            adapter = get_platform_runtime_adapter(profile, platform_id=BINANCE_PLATFORM)
            with self.subTest(profile=profile):
                self.assertIn(definition.domain, PLATFORM_CAPABILITY_MATRIX.supported_domains)
                self.assertIn(definition.target_mode, PLATFORM_CAPABILITY_MATRIX.supported_target_modes)
                self.assertLessEqual(definition.required_inputs, adapter.available_inputs)
                self.assertLessEqual(adapter.available_inputs, PLATFORM_CAPABILITY_MATRIX.supported_inputs)
                if "portfolio_snapshot" in definition.required_inputs:
                    self.assertEqual(adapter.portfolio_input_name, "portfolio_snapshot")


if __name__ == "__main__":
    unittest.main()
