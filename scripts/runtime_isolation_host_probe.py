#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import platform
import re
import urllib.request
from pathlib import Path
from typing import Any


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DMI_FIELDS = {
    "sys_vendor": Path("/sys/class/dmi/id/sys_vendor"),
    "product_name": Path("/sys/class/dmi/id/product_name"),
    "product_version": Path("/sys/class/dmi/id/product_version"),
}


def _read_field(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()[:160]
    except (OSError, UnicodeError):
        return ""


def infer_host_provider(dmi: dict[str, str]) -> str:
    evidence = " ".join(dmi.values()).lower()
    if "google" in evidence or "compute engine" in evidence:
        return "gce"
    if "oracle" in evidence and "cloud" in evidence:
        return "oci"
    if "amazon" in evidence or "ec2" in evidence:
        return "aws_ec2"
    if "microsoft" in evidence or "azure" in evidence:
        return "azure_vm"
    if "tencent" in evidence:
        return "tencent_cloud_vm"
    if "digitalocean" in evidence:
        return "digitalocean_vm"
    if "hetzner" in evidence:
        return "hetzner_vm"
    if "linode" in evidence or "akamai" in evidence:
        return "linode_vm"
    if any(marker in evidence for marker in ("kvm", "qemu", "vmware", "virtualbox")):
        return "virtual_machine_unknown_provider"
    return "unknown"


def inspect_secret_source(workflow_path: Path) -> dict[str, Any]:
    workflow = workflow_path.read_text(encoding="utf-8")
    key_ref = "BINANCE_API_KEY: ${{ secrets.BINANCE_API_KEY }}"
    secret_ref = "BINANCE_API_SECRET: ${{ secrets.BINANCE_API_SECRET }}"
    strategy_marker = "- name: 4. Run trading strategy"
    next_marker = "- name: 5. Stage execution report"
    strategy_block = ""
    if strategy_marker in workflow and next_marker in workflow:
        strategy_block = workflow[workflow.index(strategy_marker) : workflow.index(next_marker)]
    references_present = key_ref in workflow and secret_ref in workflow
    return {
        "source": "github_actions_environment_secrets" if references_present else "UNVERIFIED",
        "environment_name": "binance-runtime" if "environment: binance-runtime" in workflow else None,
        "broker_secret_references_present": references_present,
        "broker_secret_scope": (
            "strategy_step_only"
            if key_ref in strategy_block and secret_ref in strategy_block
            else "UNVERIFIED"
        ),
        "secret_values_read": False,
    }


def check_egress(*, check_url: str, expected_sha256: str) -> dict[str, Any]:
    if not expected_sha256:
        return {
            "status": "UNVERIFIED",
            "reason": "expected egress fingerprint is not configured",
            "raw_address_recorded": False,
        }
    if not SHA256_RE.fullmatch(expected_sha256):
        raise ValueError("expected egress fingerprint must be 64 lowercase hexadecimal characters")
    if not check_url.startswith("https://"):
        raise ValueError("egress check URL must use https")

    request = urllib.request.Request(check_url, headers={"User-Agent": "binance-runtime-isolation-probe/1"})
    with urllib.request.urlopen(request, timeout=10) as response:
        raw_address = response.read(256).decode("ascii").strip()
    address = ipaddress.ip_address(raw_address)
    observed = hashlib.sha256(address.compressed.encode("ascii")).hexdigest()
    matched = observed == expected_sha256
    return {
        "status": "MATCHED" if matched else "MISMATCHED",
        "ip_version": address.version,
        "raw_address_recorded": False,
    }


def build_report(*, workflow_path: Path, egress_check_url: str, expected_egress_sha256: str) -> dict[str, Any]:
    dmi = {name: value for name, path in DMI_FIELDS.items() if (value := _read_field(path))}
    egress = check_egress(
        check_url=egress_check_url,
        expected_sha256=expected_egress_sha256,
    )
    secret_boundary = inspect_secret_source(workflow_path)
    host_provider = infer_host_provider(dmi)
    status = (
        "READY"
        if egress["status"] == "MATCHED"
        and host_provider != "unknown"
        and secret_boundary["broker_secret_scope"] == "strategy_step_only"
        else "PARTIAL"
    )
    return {
        "schema_version": "runtime_isolation_host_profile.v1",
        "status": status,
        "no_order": True,
        "runner": {
            "name": os.getenv("RUNNER_NAME") or None,
            "os": os.getenv("RUNNER_OS") or platform.system(),
            "arch": os.getenv("RUNNER_ARCH") or platform.machine(),
            "environment": os.getenv("RUNNER_ENVIRONMENT") or None,
            "registration_mode": "UNVERIFIED",
        },
        "host": {
            "provider": host_provider,
            "dmi": dmi,
            "hostname_recorded": False,
        },
        "network_egress": egress,
        "secret_boundary": secret_boundary,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect a redacted runtime isolation host profile.")
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--egress-check-url",
        default=os.getenv("RUNTIME_EGRESS_CHECK_URL", ""),
        help="Operator-controlled HTTPS endpoint that returns only the caller IP.",
    )
    parser.add_argument(
        "--expected-egress-sha256",
        default=os.getenv("RUNTIME_EXPECTED_EGRESS_SHA256", "").strip().lower(),
        help="Expected SHA-256 of the allowlisted public IP; the raw IP is never emitted.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if bool(args.egress_check_url) != bool(args.expected_egress_sha256):
        raise SystemExit("egress check URL and expected fingerprint must be configured together")
    report = build_report(
        workflow_path=args.workflow,
        egress_check_url=args.egress_check_url,
        expected_egress_sha256=args.expected_egress_sha256,
    )
    if report["network_egress"]["status"] == "MISMATCHED":
        raise SystemExit("Observed egress does not match the configured allowlist fingerprint")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        "Runtime isolation host profile written: "
        f"status={report['status']} provider={report['host']['provider']} "
        f"egress={report['network_egress']['status']}"
    )


if __name__ == "__main__":
    main()
