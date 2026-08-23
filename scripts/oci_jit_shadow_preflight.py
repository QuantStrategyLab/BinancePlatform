#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


CONTRACT_SCHEMA = "qsl.oci_jit_shadow_contract.v1"
ATTESTATION_SCHEMA = "qsl.oci_jit_shadow_attestations.v1"
INVENTORY_SCHEMA = "qsl.oci_jit_shadow_inventory.v1"

REQUIRED_CONTRACT_VALUES: dict[tuple[str, ...], Any] = {
    ("deployment_mode",): "no_order_shadow",
    ("apply_authorized",): False,
    ("network", "private_subnet_required"): True,
    ("network", "assign_public_ip"): False,
    ("network", "nat_gateway_required"): True,
    ("network", "reserved_public_ip_required"): True,
    ("network", "inbound_rules_allowed"): False,
    ("compute", "capacity_type"): "ON_DEMAND",
    ("compute", "runner_assignment"): "ONE_JOB",
    ("compute", "pinned_custom_image_required"): True,
    ("compute", "delete_boot_volume_on_termination"): True,
    ("identity", "instance_principal_required"): True,
    ("identity", "dynamic_group_requires_compartment"): True,
    ("identity", "dynamic_group_requires_defined_tag"): True,
    ("identity", "launcher_scope_defined_tag_only"): True,
    ("identity", "launcher_can_read_broker_secret"): False,
    ("identity", "launcher_can_manage_network"): False,
    ("identity", "launcher_can_manage_iam"): False,
    ("identity", "runtime_can_read_broker_secret"): False,
    ("identity", "runtime_can_write_evidence"): True,
    ("identity", "runtime_can_manage_compute"): False,
    ("identity", "runtime_can_manage_iam"): False,
    ("identity", "runtime_can_manage_vault"): False,
    ("shadow", "broker_secret_allowed"): False,
    ("shadow", "github_oidc_allowed"): False,
    ("shadow", "dry_run_required"): True,
    ("shadow", "executed_call_count_required"): 0,
    ("shadow", "platform_retries"): 0,
    ("cleanup", "delete_boot_volume"): True,
    ("cleanup", "deregister_runner"): True,
}

REQUIRED_ATTESTATIONS: dict[tuple[str, ...], Any] = {
    ("network", "subnet_is_private"): True,
    ("network", "instance_assigns_public_ip"): False,
    ("network", "nat_gateway_state"): "AVAILABLE",
    ("network", "reserved_public_ip_state"): "ASSIGNED",
    ("network", "private_subnet_routes_through_nat"): True,
    ("network", "inbound_rules_present"): False,
    ("identity", "instance_principal_enabled"): True,
    ("identity", "dynamic_group_scoped_to_compartment"): True,
    ("identity", "dynamic_group_scoped_to_defined_tag"): True,
    ("identity", "launcher_scope_is_defined_tag_only"): True,
    ("identity", "launcher_can_read_broker_secret"): False,
    ("identity", "launcher_can_manage_network"): False,
    ("identity", "launcher_can_manage_iam"): False,
    ("identity", "runtime_can_read_broker_secret"): False,
    ("identity", "runtime_can_write_evidence"): True,
    ("identity", "runtime_can_manage_compute"): False,
    ("identity", "runtime_can_manage_iam"): False,
    ("identity", "runtime_can_manage_vault"): False,
    ("compute", "capacity_type"): "ON_DEMAND",
    ("compute", "custom_image_is_pinned"): True,
    ("compute", "runner_assignment"): "ONE_JOB",
    ("compute", "boot_volume_delete_on_termination"): True,
}

OCID_VARIABLES = {
    "OCI_JIT_COMPARTMENT_OCID",
    "OCI_JIT_SUBNET_OCID",
    "OCI_JIT_NAT_GATEWAY_OCID",
    "OCI_JIT_RESERVED_PUBLIC_IP_OCID",
    "OCI_JIT_IMAGE_OCID",
    "OCI_JIT_RUNTIME_DYNAMIC_GROUP_OCID",
    "OCI_JIT_RUNTIME_POLICY_OCID",
    "OCI_JIT_LAUNCHER_PRINCIPAL_OCID",
}


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _get(payload: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = payload
    for part in path:
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _validate_expected(
    payload: Mapping[str, Any], expected: Mapping[tuple[str, ...], Any]
) -> list[str]:
    failures: list[str] = []
    for path, expected_value in expected.items():
        actual = _get(payload, path)
        if actual != expected_value:
            failures.append(f"{'.'.join(path)} must be {expected_value!r}")
    return failures


def validate_contract(contract: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    if contract.get("schema_version") != CONTRACT_SCHEMA:
        failures.append(f"schema_version must be {CONTRACT_SCHEMA}")
    failures.extend(_validate_expected(contract, REQUIRED_CONTRACT_VALUES))

    variables = contract.get("required_repository_variables")
    if not isinstance(variables, list) or not variables:
        failures.append("required_repository_variables must be a non-empty list")
    elif any(not isinstance(name, str) or not name.startswith("OCI_JIT_") for name in variables):
        failures.append("repository variable names must use the OCI_JIT_ prefix")
    elif len(variables) != len(set(variables)):
        failures.append("required_repository_variables must not contain duplicates")

    max_age = _get(contract, ("compute", "max_instance_age_minutes"))
    if not isinstance(max_age, int) or isinstance(max_age, bool) or not 5 <= max_age <= 180:
        failures.append("compute.max_instance_age_minutes must be between 5 and 180")

    operations = contract.get("planned_operations")
    required_operations = {
        "launch_tagged_private_instance",
        "register_one_job_jit_runner",
        "run_no_order_shadow",
        "terminate_instance_with_boot_volume_deletion",
        "audit_orphans",
    }
    if not isinstance(operations, list) or not required_operations.issubset(set(operations)):
        failures.append("planned_operations must include the bounded launch/shadow/teardown lifecycle")
    return failures


def inspect_repository_variables(
    contract: Mapping[str, Any], environ: Mapping[str, str]
) -> dict[str, Any]:
    names = contract.get("required_repository_variables", [])
    configured: list[str] = []
    missing: list[str] = []
    invalid_format: list[str] = []
    for name in names:
        value = environ.get(name, "").strip()
        if not value:
            missing.append(name)
            continue
        configured.append(name)
        if name in OCID_VARIABLES and not value.startswith("ocid1."):
            invalid_format.append(name)
    return {
        "configured_variable_names": configured,
        "missing_variable_names": missing,
        "invalid_format_variable_names": invalid_format,
        "values_recorded": False,
    }


def validate_attestations(attestations: Mapping[str, Any] | None) -> list[str]:
    if attestations is None:
        return ["read-only OCI attestations were not supplied"]
    failures: list[str] = []
    if attestations.get("schema_version") != ATTESTATION_SCHEMA:
        failures.append(f"attestation schema_version must be {ATTESTATION_SCHEMA}")
    if attestations.get("evidence_source") != "oci_read_only_export":
        failures.append("attestation evidence_source must be oci_read_only_export")
    if attestations.get("collection_complete") is not True:
        failures.append("read-only OCI attestation collection must be complete")
    if attestations.get("resource_identifiers_recorded") is not False:
        failures.append("read-only OCI attestations must not record resource identifiers")
    failures.extend(_validate_expected(attestations, REQUIRED_ATTESTATIONS))
    return failures


def build_preflight_report(
    contract: Mapping[str, Any],
    *,
    environ: Mapping[str, str],
    attestations: Mapping[str, Any] | None,
) -> dict[str, Any]:
    contract_failures = validate_contract(contract)
    variables = inspect_repository_variables(contract, environ)
    attestation_failures = validate_attestations(attestations)
    ready = not (
        contract_failures
        or variables["missing_variable_names"]
        or variables["invalid_format_variable_names"]
        or attestation_failures
    )
    return {
        "schema_version": "qsl.oci_jit_shadow_preflight.v1",
        "status": "READY" if ready else "PARKED",
        "no_order": True,
        "apply_authorized": False,
        "secret_values_read": False,
        "contract_failures": contract_failures,
        "repository_variables": variables,
        "attestation_failures": attestation_failures,
        "provisioning_plan": {
            "capacity_type": _get(contract, ("compute", "capacity_type")),
            "private_subnet": _get(contract, ("network", "private_subnet_required")),
            "assign_public_ip": _get(contract, ("network", "assign_public_ip")),
            "reserved_nat_egress": _get(contract, ("network", "reserved_public_ip_required")),
            "defined_tag_required": True,
            "instance_principal_required": _get(
                contract, ("identity", "instance_principal_required")
            ),
            "runner_assignment": _get(contract, ("compute", "runner_assignment")),
        },
        "termination_plan": {
            "deregister_runner": _get(contract, ("cleanup", "deregister_runner")),
            "terminate_instance": True,
            "delete_boot_volume": _get(contract, ("cleanup", "delete_boot_volume")),
            "audit_orphans": True,
        },
        "planned_operations": contract.get("planned_operations", []),
    }


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 string") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def audit_orphans(contract: Mapping[str, Any], inventory: Mapping[str, Any]) -> dict[str, Any]:
    contract_failures = validate_contract(contract)
    failures: list[str] = []
    if inventory.get("schema_version") != INVENTORY_SCHEMA:
        failures.append(f"inventory schema_version must be {INVENTORY_SCHEMA}")
    if inventory.get("collection_complete") is not True:
        failures.append("OCI and GitHub inventory collection must be complete")
    collected_at = _parse_time(inventory.get("collected_at"), "collected_at")
    max_age = int(_get(contract, ("compute", "max_instance_age_minutes")) or 0)
    orphan_states = set(_get(contract, ("cleanup", "orphan_states")) or [])
    findings: list[dict[str, Any]] = []

    def inspect(resources: Any, resource_type: str) -> None:
        if not isinstance(resources, list):
            failures.append(f"{resource_type} inventory must be a list")
            return
        for resource in resources:
            if not isinstance(resource, Mapping) or resource.get("candidate_tag_matches") is not True:
                continue
            created_at = _parse_time(resource.get("created_at"), f"{resource_type}.created_at")
            age_minutes = int((collected_at - created_at).total_seconds() // 60)
            is_orphan = False
            reason = ""
            if resource_type == "instance":
                state = str(resource.get("lifecycle_state", ""))
                is_orphan = state in orphan_states and age_minutes > max_age
                reason = f"candidate instance remained {state} beyond max age"
            elif resource_type == "boot_volume":
                is_orphan = (
                    resource.get("attached_instance_id") in (None, "")
                    and age_minutes > max_age
                )
                reason = "candidate boot volume is unattached beyond max age"
            elif resource_type == "vnic_attachment":
                is_orphan = (
                    str(resource.get("lifecycle_state", "")) != "ATTACHED"
                    and age_minutes > max_age
                )
                reason = "candidate VNIC attachment is not attached beyond max age"
            elif resource_type == "github_runner":
                is_orphan = age_minutes > max_age
                reason = "candidate GitHub runner registration remained beyond max age"
            if is_orphan:
                findings.append(
                    {
                        "resource_type": resource_type,
                        "resource_fingerprint": _fingerprint(resource.get("id", "missing-id")),
                        "age_minutes": age_minutes,
                        "reason": reason,
                    }
                )

    inspect(inventory.get("instances"), "instance")
    inspect(inventory.get("boot_volumes"), "boot_volume")
    inspect(inventory.get("vnic_attachments"), "vnic_attachment")
    inspect(inventory.get("github_runners"), "github_runner")
    ready = not contract_failures and not failures and not findings
    return {
        "schema_version": "qsl.oci_jit_shadow_orphan_audit.v1",
        "status": "READY" if ready else "PARKED",
        "no_order": True,
        "cleanup_authorized": False,
        "raw_resource_ids_recorded": False,
        "contract_failures": contract_failures,
        "inventory_failures": failures,
        "orphan_count": len(findings),
        "findings": findings,
    }


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the non-applying OCI JIT shadow control-plane contract."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--contract", type=Path, required=True)
    preflight.add_argument("--attestations", type=Path)
    preflight.add_argument(
        "--attestations-env",
        help="Environment variable containing a redacted read-only OCI attestation JSON object.",
    )
    preflight.add_argument("--output", type=Path, required=True)
    preflight.add_argument("--require-ready", action="store_true")

    orphan_audit = subparsers.add_parser("audit-orphans")
    orphan_audit.add_argument("--contract", type=Path, required=True)
    orphan_audit.add_argument("--inventory", type=Path, required=True)
    orphan_audit.add_argument("--output", type=Path, required=True)
    orphan_audit.add_argument("--require-ready", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    contract = _read_json(args.contract)
    if args.command == "preflight":
        if args.attestations and args.attestations_env:
            raise SystemExit("choose either --attestations or --attestations-env")
        if args.attestations:
            attestations = _read_json(args.attestations)
        elif args.attestations_env:
            raw_attestations = os.environ.get(args.attestations_env, "").strip()
            attestations = json.loads(raw_attestations) if raw_attestations else None
            if attestations is not None and not isinstance(attestations, dict):
                raise SystemExit("attestations environment variable must contain a JSON object")
        else:
            attestations = None
        report = build_preflight_report(contract, environ=os.environ, attestations=attestations)
    else:
        report = audit_orphans(contract, _read_json(args.inventory))
    _write_report(args.output, report)
    print(f"OCI JIT shadow {args.command}: status={report['status']} no_order=true")
    if args.require_ready and report["status"] != "READY":
        raise SystemExit("OCI JIT shadow check is PARKED")


if __name__ == "__main__":
    main()
