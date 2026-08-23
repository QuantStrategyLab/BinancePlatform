from __future__ import annotations

import importlib.util
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "oci_jit_shadow_preflight.py"
CONTRACT = ROOT / "infra" / "oci-jit-shadow" / "contract.json"
ATTESTATIONS = ROOT / "infra" / "oci-jit-shadow" / "preflight-attestations.example.json"
FIXTURES = ROOT / "tests" / "fixtures" / "oci_jit_shadow"
WORKFLOW = ROOT / ".github" / "workflows" / "oci-jit-shadow-preflight.yml"
README = ROOT / "infra" / "oci-jit-shadow" / "README.md"
FULL_SHA_ACTION = re.compile(r"(?:-\s+)?uses:\s+[^\s@]+@[0-9a-f]{40}(?:\s+#\s+v\d+)?$")


def load_module():
    spec = importlib.util.spec_from_file_location("oci_jit_shadow_preflight", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load OCI JIT shadow preflight module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OciJitShadowPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()
        cls.contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
        cls.attestations = json.loads(ATTESTATIONS.read_text(encoding="utf-8"))

    def configured_environment(self) -> dict[str, str]:
        environment: dict[str, str] = {}
        for name in self.contract["required_repository_variables"]:
            environment[name] = f"configured-{name.lower()}"
        for name in self.module.OCID_VARIABLES:
            environment[name] = f"ocid1.example.oc1..{name.lower()}"
        return environment

    def test_contract_is_non_applying_no_order_and_has_bounded_teardown(self) -> None:
        self.assertEqual(self.module.validate_contract(self.contract), [])
        self.assertFalse(self.contract["apply_authorized"])
        self.assertFalse(self.contract["shadow"]["broker_secret_allowed"])
        self.assertFalse(self.contract["network"]["assign_public_ip"])
        self.assertTrue(self.contract["cleanup"]["delete_boot_volume"])
        self.assertTrue(self.contract["cleanup"]["deregister_runner"])

    def test_preflight_is_ready_only_with_variables_and_attestations(self) -> None:
        ready = self.module.build_preflight_report(
            self.contract,
            environ=self.configured_environment(),
            attestations=self.attestations,
        )
        self.assertEqual(ready["status"], "READY")
        self.assertFalse(ready["apply_authorized"])
        self.assertFalse(ready["secret_values_read"])
        self.assertFalse(ready["repository_variables"]["values_recorded"])

        parked = self.module.build_preflight_report(
            self.contract,
            environ={},
            attestations=None,
        )
        self.assertEqual(parked["status"], "PARKED")
        self.assertEqual(
            set(parked["repository_variables"]["missing_variable_names"]),
            set(self.contract["required_repository_variables"]),
        )
        self.assertTrue(parked["attestation_failures"])

    def test_network_or_permission_regression_parks_preflight(self) -> None:
        attestations = json.loads(json.dumps(self.attestations))
        attestations["network"]["instance_assigns_public_ip"] = True
        attestations["identity"]["launcher_can_read_broker_secret"] = True

        report = self.module.build_preflight_report(
            self.contract,
            environ=self.configured_environment(),
            attestations=attestations,
        )

        self.assertEqual(report["status"], "PARKED")
        self.assertIn(
            "network.instance_assigns_public_ip must be False",
            report["attestation_failures"],
        )
        self.assertIn(
            "identity.launcher_can_read_broker_secret must be False",
            report["attestation_failures"],
        )

    def test_orphan_audit_is_fail_closed_and_redacts_resource_ids(self) -> None:
        clean_inventory = json.loads(
            (FIXTURES / "clean_inventory.json").read_text(encoding="utf-8")
        )
        clean = self.module.audit_orphans(self.contract, clean_inventory)
        self.assertEqual(clean["status"], "READY")
        self.assertEqual(clean["orphan_count"], 0)

        orphan_inventory = json.loads(
            (FIXTURES / "orphan_inventory.json").read_text(encoding="utf-8")
        )
        orphan = self.module.audit_orphans(self.contract, orphan_inventory)
        serialized = json.dumps(orphan)
        self.assertEqual(orphan["status"], "PARKED")
        self.assertEqual(orphan["orphan_count"], 3)
        self.assertFalse(orphan["cleanup_authorized"])
        self.assertNotIn("ocid1.instance.oc1.example.orphan", serialized)
        self.assertNotIn("ocid1.bootvolume.oc1.example.orphan", serialized)
        self.assertTrue(all(len(item["resource_fingerprint"]) == 12 for item in orphan["findings"]))

    def test_workflow_is_manual_read_only_secretless_and_pinned(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        action_lines = [line.strip() for line in workflow.splitlines() if "uses:" in line]

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("contents: read", workflow)
        self.assertNotIn("id-token: write", workflow)
        self.assertNotIn("environment:", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("BINANCE_API_KEY", workflow)
        self.assertNotIn("BINANCE_API_SECRET", workflow)
        self.assertNotIn("oci compute instance launch", workflow)
        self.assertNotIn("oci compute instance terminate", workflow)
        self.assertIn("run_isolation_shadow_fixture.py", workflow)
        self.assertIn("audit-orphans", workflow)
        self.assertTrue(action_lines)
        self.assertTrue(all(FULL_SHA_ACTION.fullmatch(line) for line in action_lines))

    def test_operator_documentation_lists_every_required_variable(self) -> None:
        documentation = README.read_text(encoding="utf-8")
        for name in self.contract["required_repository_variables"]:
            self.assertIn(f"`{name}`", documentation)
        self.assertIn("does not delete anything", documentation)
        self.assertIn("No live cutover", documentation)


if __name__ == "__main__":
    unittest.main()
