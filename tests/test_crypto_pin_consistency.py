import unittest

from scripts.check_crypto_pin_consistency import PinError, check_pins


class CryptoPinConsistencyTest(unittest.TestCase):
    def declarations(self, project_revision="a" * 40, lock_revision="a" * 40,
                     locked_revision="a" * 40, qsl_revision="a" * 40):
        project = {"project": {"dependencies": [
            f"crypto-strategies @ git+https://github.com/QuantStrategyLab/CryptoStrategies.git@{project_revision}"
        ]}}
        lock = {"manifest": {"overrides": []}, "package": [
            {"name": "binance-platform", "metadata": {"requires-dist": [{"name": "crypto-strategies",
                "git": f"https://github.com/QuantStrategyLab/CryptoStrategies.git?rev={project_revision}"}]}},
            {"name": "crypto-strategies", "source": {"git":
                f"https://github.com/QuantStrategyLab/CryptoStrategies.git?rev={lock_revision}#{locked_revision}"}}]}
        qsl = {"qsl": {"requires": {"crypto_strategies": qsl_revision}}}
        return project, lock, qsl

    def test_matching_full_shas_pass(self):
        self.assertEqual(check_pins(*self.declarations()), "a" * 40)

    def test_each_mismatch_or_invalid_revision_fails(self):
        for index in range(4):
            values = ["a" * 40] * 4
            values[index] = "b" * 40
            with self.subTest(index=index), self.assertRaises(PinError):
                check_pins(*self.declarations(*values))
        with self.assertRaises(PinError):
            check_pins(*self.declarations(qsl_revision="short"))

    def test_duplicate_or_missing_source_fails(self):
        project, lock, qsl = self.declarations()
        lock["package"].append(lock["package"][0])
        with self.assertRaises(PinError):
            check_pins(project, lock, qsl)
        project, lock, qsl = self.declarations()
        project["project"]["dependencies"] = []
        with self.assertRaises(PinError):
            check_pins(project, lock, qsl)

    def test_lock_requirement_and_manifest_override_cannot_drift(self):
        project, lock, qsl = self.declarations()
        lock["package"][0]["metadata"]["requires-dist"][0]["git"] = (
            "https://github.com/QuantStrategyLab/CryptoStrategies.git?rev=" + "b" * 40
        )
        with self.assertRaises(PinError):
            check_pins(project, lock, qsl)
        project, lock, qsl = self.declarations()
        lock["manifest"]["overrides"] = [{"name": "crypto-strategies", "git":
            "https://github.com/QuantStrategyLab/CryptoStrategies.git?rev=" + "b" * 40}]
        with self.assertRaises(PinError):
            check_pins(project, lock, qsl)
        project, lock, qsl = self.declarations()
        lock["package"][0]["metadata"]["requires-dist"].append(
            lock["package"][0]["metadata"]["requires-dist"][0])
        with self.assertRaises(PinError):
            check_pins(project, lock, qsl)


if __name__ == "__main__":
    unittest.main()
