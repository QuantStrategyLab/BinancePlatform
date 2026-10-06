from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "runtime_isolation_host_probe.py"
MAIN_WORKFLOW = ROOT / ".github" / "workflows" / "main.yml"


def load_probe():
    spec = importlib.util.spec_from_file_location("runtime_isolation_host_probe", PROBE)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load runtime isolation host probe")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeIsolationHostProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.probe = load_probe()

    def test_current_secret_source_is_github_environment_and_step_scoped(self) -> None:
        result = self.probe.inspect_secret_source(MAIN_WORKFLOW)

        self.assertEqual(result["source"], "github_actions_environment_secrets")
        self.assertEqual(result["environment_name"], "binance-runtime")
        self.assertTrue(result["broker_secret_references_present"])
        self.assertEqual(result["broker_secret_scope"], "strategy_step_only")
        self.assertFalse(result["secret_values_read"])

    def _inspect_synthetic_workflow(self, text: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workflow.yml"
            path.write_text(text, encoding="utf-8")
            return self.probe.inspect_secret_source(path)

    def test_strategy_secret_boundary_requires_unique_adjacent_step_markers(self) -> None:
        workflow = MAIN_WORKFLOW.read_text(encoding="utf-8")
        start = "      - name: 4. Run trading strategy"
        end = "      - name: Remove protected LIVE risk authority source"
        variants = {
            "missing_start": workflow.replace(start, "      - name: Unrecognized strategy"),
            "missing_end": workflow.replace(end, "      - name: Unrecognized cleanup"),
            "duplicate_start": workflow + "\n" + start + "\n",
            "duplicate_end": workflow + "\n" + end + "\n",
            "intervening_named_step": workflow.replace(end, "      - name: Unrecognized step\n" + end),
            "intervening_unnamed_step": workflow.replace(end, "      - uses: example/action@v1\n" + end),
        }
        for case, text in variants.items():
            with self.subTest(case=case):
                result = self._inspect_synthetic_workflow(text)
                self.assertEqual(result["broker_secret_scope"], "UNVERIFIED")
                self.assertFalse(result["secret_values_read"])

    def test_cross_step_secret_references_are_not_claimed_strategy_only(self) -> None:
        workflow = MAIN_WORKFLOW.read_text(encoding="utf-8")
        key = "          BINANCE_API_KEY: ${{ secrets.BINANCE_API_KEY }}"
        secret = "          BINANCE_API_SECRET: ${{ secrets.BINANCE_API_SECRET }}"
        later_step = "\n      - name: Other synthetic step\n        env:\n" + key + "\n" + secret + "\n"
        for case, text in {
            "moved": workflow.replace(key + "\n", "").replace(secret + "\n", "") + later_step,
            "duplicated": workflow + later_step,
        }.items():
            with self.subTest(case=case):
                result = self._inspect_synthetic_workflow(text)
                self.assertTrue(result["broker_secret_references_present"])
                self.assertEqual(result["broker_secret_scope"], "UNVERIFIED")
                self.assertFalse(result["secret_values_read"])

    def test_provider_inference_does_not_assume_gce(self) -> None:
        self.assertEqual(
            self.probe.infer_host_provider({"sys_vendor": "OracleCloud.com"}),
            "oci",
        )
        self.assertEqual(
            self.probe.infer_host_provider({"product_name": "Google Compute Engine"}),
            "gce",
        )
        self.assertEqual(
            self.probe.infer_host_provider({"product_name": "KVM"}),
            "virtual_machine_unknown_provider",
        )
        self.assertEqual(
            self.probe.infer_host_provider({"sys_vendor": "Tencent Cloud"}),
            "tencent_cloud_vm",
        )
        self.assertEqual(self.probe.infer_host_provider({}), "unknown")

    def test_egress_comparison_never_returns_raw_address_or_digest(self) -> None:
        address = "203.0.113.10"
        expected = hashlib.sha256(address.encode("ascii")).hexdigest()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = address.encode("ascii")
        response.__exit__.return_value = False

        with mock.patch.object(self.probe.urllib.request, "urlopen", return_value=response):
            result = self.probe.check_egress(
                check_url="https://egress-check.example.test/ip",
                expected_sha256=expected,
            )

        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(result["ip_version"], 4)
        self.assertFalse(result["raw_address_recorded"])
        self.assertNotIn(address, json.dumps(result))
        self.assertNotIn(expected, json.dumps(result))

    def test_unconfigured_egress_is_partial_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow = Path(temp_dir) / "main.yml"
            workflow.write_text(MAIN_WORKFLOW.read_text(encoding="utf-8"), encoding="utf-8")
            with mock.patch.object(self.probe, "DMI_FIELDS", {}):
                report = self.probe.build_report(
                    workflow_path=workflow,
                    egress_check_url="",
                    expected_egress_sha256="",
                )

        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual(report["network_egress"]["status"], "UNVERIFIED")
        self.assertTrue(report["no_order"])


if __name__ == "__main__":
    unittest.main()
