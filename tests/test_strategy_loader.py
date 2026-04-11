import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
QPK_SRC = PROJECT_ROOT.parent / "QuantPlatformKit" / "src"
CRYPTO_STRATEGIES_SRC = PROJECT_ROOT.parent / "CryptoStrategies" / "src"
for path in (PROJECT_ROOT, QPK_SRC, CRYPTO_STRATEGIES_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


class StrategyLoaderTests(unittest.TestCase):
    def test_load_strategy_entrypoint_for_profile_returns_unified_entrypoint(self):
        try:
            from strategy_loader import load_strategy_entrypoint_for_profile
        except ModuleNotFoundError as exc:
            if exc.name == "pandas":
                self.skipTest("pandas is not installed")
            raise

        entrypoint = load_strategy_entrypoint_for_profile("crypto_leader_rotation")

        self.assertEqual(entrypoint.manifest.profile, "crypto_leader_rotation")
        self.assertEqual(entrypoint.manifest.domain, "crypto")
        self.assertIn("market_prices", entrypoint.manifest.required_inputs)
        self.assertIn("portfolio_snapshot", entrypoint.manifest.required_inputs)


if __name__ == "__main__":
    unittest.main()
