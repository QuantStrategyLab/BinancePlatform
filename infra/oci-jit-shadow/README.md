# OCI JIT shadow deployment preflight

This directory is the first implementation batch for the disposable Oracle
Cloud Compute runner selected in `docs/runtime_isolation_migration.md`. It is a
**non-applying control-plane contract**. It does not call OCI, register a GitHub
runner, read a secret, change the Binance allowlist, or alter the current live
runner.

`contract.json` records the reviewed launch and termination invariants. The
preflight script validates those invariants, reports whether the operator-owned
OCI inputs are configured, consumes a redacted read-only attestation, and emits
only variable names and readiness findings. It never emits variable values or
resource OCIDs.

## Repository variables the operator must provide later

Configure these as GitHub repository variables only after the corresponding OCI
resources have been reviewed. None is a broker credential.

| Variable | Required meaning |
| --- | --- |
| `OCI_JIT_REGION` | OCI region containing the candidate runtime |
| `OCI_JIT_COMPARTMENT_OCID` | Dedicated candidate compartment |
| `OCI_JIT_AVAILABILITY_DOMAIN` | Availability domain for the on-demand instance |
| `OCI_JIT_SUBNET_OCID` | Private subnet with no instance public IP |
| `OCI_JIT_NAT_GATEWAY_OCID` | NAT gateway used by that private subnet |
| `OCI_JIT_RESERVED_PUBLIC_IP_OCID` | Reserved public IP attached to the NAT gateway |
| `OCI_JIT_IMAGE_OCID` | Reviewed immutable custom image |
| `OCI_JIT_SHAPE` | Right-sized on-demand flexible shape |
| `OCI_JIT_RUNNER_GROUP` | Dedicated GitHub runner group that accepts only the reviewed workflow |
| `OCI_JIT_DEFINED_TAG_NAMESPACE` | Defined-tag namespace for candidate ownership |
| `OCI_JIT_DEFINED_TAG_KEY` | Defined-tag key matched by IAM and orphan audit |
| `OCI_JIT_DEFINED_TAG_VALUE` | Defined-tag value for the Binance candidate runtime |
| `OCI_JIT_RUNTIME_DYNAMIC_GROUP_OCID` | Dynamic group limited by compartment and defined tag |
| `OCI_JIT_RUNTIME_POLICY_OCID` | Runtime policy with no Compute/IAM/Vault administration |
| `OCI_JIT_LAUNCHER_PRINCIPAL_OCID` | Separate launcher identity, unable to read broker secrets |
| `OCI_JIT_EVIDENCE_BUCKET_NAME` | External destination for redacted runner and terminal evidence |
| `OCI_JIT_ATTESTATIONS_JSON` | Redacted JSON matching `preflight-attestations.example.json` |

Keep the exact OCIDs and the reserved public address out of repository files and
workflow artifacts. Do not store `BINANCE_API_KEY`, `BINANCE_API_SECRET`, GitHub
JIT registration tokens, OCI API private keys, or Vault secret material in any
of these variables. Broker credentials remain prohibited in the fixture-shadow
phase.

## What the attestation means

The example attestation is a schema example, not evidence that any OCI resource
exists. Before setting `OCI_JIT_ATTESTATIONS_JSON`, use the OCI Console or a
read-only OCI identity to verify all represented facts:

- the subnet is private, assigns no public IP, and routes outbound traffic
  through the expected NAT gateway;
- the NAT gateway is available and owns the reviewed reserved public IP;
- no inbound security rule is required for the runner;
- the dynamic-group rule includes both the dedicated compartment and defined
  tag;
- the no-order instance principal can write only the evidence destination and
  cannot read the broker secret or manage Compute, IAM, dynamic groups,
  policies, Vault, or secret versions;
- the launcher is limited to tagged candidates and cannot read the broker
  secret or manage network/IAM resources;
- the image is pinned, capacity is on-demand, runner assignment is one job, and
  instance termination deletes the boot volume.

The preflight validates the redacted statements. It does not replace an OCI IAM
policy review and does not prove the raw OCI resources by itself.

## Safe preflight and orphan audit

The manual `OCI JIT Shadow Preflight` workflow has `contents: read`, no OIDC,
environment, or secret references. It renders a redacted launch/terminate plan,
exercises the orphan-audit engine with a clean fixture, and runs the fixed-input
no-order strategy replay. With `require_ready=false`, missing OCI inputs produce
a `PARKED` terminal report while the no-order replay still runs. Use
`require_ready=true` only after all variables and the read-only attestation are
configured.

To check a real inventory without mutating it, export a JSON object matching
`qsl.oci_jit_shadow_inventory.v1` from read-only OCI and GitHub API queries, then
run:

```bash
python scripts/oci_jit_shadow_preflight.py audit-orphans \
  --contract infra/oci-jit-shadow/contract.json \
  --inventory /secure/path/oci-jit-inventory.json \
  --output reports/oci_jit_shadow_orphan_audit.json \
  --require-ready
```

Only resources matching the candidate defined tag belong in that inventory.
The report replaces resource identifiers with short one-way fingerprints. It
does not delete anything: a stale instance, unattached boot volume, detached
VNIC attachment, lingering GitHub runner registration, incomplete collection,
or malformed evidence results in `PARKED`.

Actual OCI launch/termination, JIT-token delivery, instance-principal secret
retrieval, and automated cleanup remain a later reviewed phase. No live cutover
is authorized by a successful preflight.
