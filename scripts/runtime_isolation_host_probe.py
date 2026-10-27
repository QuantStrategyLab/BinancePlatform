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
import urllib.error
import socket
import ssl
from datetime import datetime, timezone
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
    strategy_marker = "      - name: 4. Run trading strategy"
    next_marker = "      - name: Remove protected LIVE risk authority source"
    strategy_block = ""
    if workflow.count(strategy_marker) == 1 and workflow.count(next_marker) == 1:
        candidate = workflow[workflow.index(strategy_marker) : workflow.index(next_marker)]
        # The end marker must be the very next step, never a broader job slice.
        if not any(line.startswith("      - ") for line in candidate.splitlines()[1:]):
            strategy_block = candidate
    references_present = key_ref in workflow and secret_ref in workflow
    return {
        "source": "github_actions_environment_secrets" if references_present else "UNVERIFIED",
        "environment_name": "binance-runtime" if "environment: binance-runtime" in workflow else None,
        "broker_secret_references_present": references_present,
        "broker_secret_scope": (
            "strategy_step_only"
            if key_ref in strategy_block and secret_ref in strategy_block
            and workflow.count(key_ref) == 1 and workflow.count(secret_ref) == 1
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_binance_public_api() -> dict[str, Any]:
    """One unauthenticated ping on this host; no redirect, retry or body output."""
    result = {"status": "unknown", "reason": "request_failed", "http_status": None,
              "request_attempts": 1, "authenticated": False,
              "observed_at": datetime.now(timezone.utc).isoformat()}
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request("https://api.binance.com/api/v3/ping", method="GET",
                                     headers={"User-Agent": "binance-runtime-isolation-probe/1"})
    try:
        with opener.open(request, timeout=10) as response:
            result["http_status"] = response.status
            body = response.read(257)
            if response.status == 200 and len(body) <= 256 and json.loads(body) == {}:
                result.update(status="reachable", reason="public_ping_passed")
            else:
                result.update(status="failed", reason="unexpected_response")
    except urllib.error.HTTPError as exc:
        result["http_status"] = exc.code
        result.update(status="failed", reason={
            451: "http_451", 403: "http_403", 429: "rate_limited",
        }.get(exc.code, "redirect_rejected" if 300 <= exc.code < 400 else "http_error"))
        exc.close()
    except (TimeoutError, socket.timeout):
        result["reason"] = "request_timeout"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, socket.gaierror):
            result["reason"] = "dns_failed"
        elif isinstance(exc.reason, ssl.SSLError):
            result["reason"] = "tls_failed"
        elif isinstance(exc.reason, TimeoutError):
            result["reason"] = "request_timeout"
    except (ValueError, UnicodeError):
        result.update(status="failed", reason="unexpected_response")
    except (OSError, ssl.SSLError):
        pass
    return result


def build_report(*, workflow_path: Path, egress_check_url: str, expected_egress_sha256: str,
                 check_public_api: bool = False) -> dict[str, Any]:
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
    report = {
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
    if check_public_api:
        report["binance_public_api"] = check_binance_public_api()
    return report


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
    parser.add_argument("--check-binance-public-api", action="store_true",
                        help="One fixed unauthenticated Binance ping; no broker account data.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if bool(args.egress_check_url) != bool(args.expected_egress_sha256):
        raise SystemExit("egress check URL and expected fingerprint must be configured together")
    report = build_report(
        workflow_path=args.workflow,
        egress_check_url=args.egress_check_url,
        expected_egress_sha256=args.expected_egress_sha256,
        check_public_api=args.check_binance_public_api,
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
