import unittest


class StrategyLoaderTests(unittest.TestCase):
    def test_load_strategy_component_resolves_crypto_modules(self):
        try:
            from strategy_loader import load_strategy_component

            core_module = load_strategy_component(
                "crypto_leader_rotation",
                component_name="core",
            )
            rotation_module = load_strategy_component(
                "crypto_leader_rotation",
                component_name="rotation",
            )
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed")
            raise

        self.assertEqual(
            core_module.__name__,
            "crypto_strategies.strategies.crypto_leader_rotation.core",
        )
        self.assertEqual(
            rotation_module.__name__,
            "crypto_strategies.strategies.crypto_leader_rotation.rotation",
        )


if __name__ == "__main__":
    unittest.main()
