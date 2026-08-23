# Binance Runtime Isolation Migration

## Status and scope

This document defines a staged migration from the persistent GitHub self-hosted
runner to a short-lived execution boundary. It does **not** authorize a live
cutover, delete the current runner, change broker permissions, or move broker
credentials.

The current `main.yml` workflow remains the production path until a separately
reviewed cutover change is approved. Phase 1 is a fixed-input, no-order shadow
replay on a clean GitHub-hosted runner. Phase 2 identified the existing boundary
and selected an Oracle Cloud disposable-instance direction without provisioning
it.

## Current architecture

The current runtime is dispatched through GitHub Actions and runs on the
`binance-quant-runner` self-hosted runner on a user-owned **Oracle Cloud
Infrastructure Compute instance**. This is an operator-attested deployment
fact. The GitHub repository runner API independently confirms an online
Linux/X64 runner, and the redacted host profile reports a QEMU VM; neither API
nor DMI alone identifies the OCI tenancy or instance OCID. The broker job checks
out the repository, authenticates to Google Cloud through GitHub OIDC, builds or
reuses a local dependency environment, and injects the Binance credentials only
into the strategy step.

Recent hardening already provides useful boundaries:

- the broker job has `contents: read` and cannot write the repository;
- execution-log publication happens in a separate GitHub-hosted job without
  broker credentials;
- third-party actions used by the runtime are pinned to full commit SHAs;
- the Workload Identity provider accepts this repository on `refs/heads/main`;
- `RUNTIME_TARGET_JSON` and `BINANCE_DRY_RUN` must agree or the runtime fails
  closed.

The residual risk is persistence: a compromised dependency, workflow step, or
operator action can leave files or processes on the runner and affect a later
job. GitHub explicitly recommends ephemeral self-hosted runners for autoscaling
and does not recommend persistent runners for that purpose.

## Phase 2 decision gate

Do not select Cloud Run merely because the runtime uses Google Cloud for state.
The current runner is an ordinary Oracle Cloud VPS/VM with an existing network
path. Phase 2 records the host boundary, network-egress match, and secret source
without reading any secret value, then compares Oracle-native isolation with a
cross-cloud Cloud Run migration.

Current evidence is:

| Question | Evidence | Status |
| --- | --- | --- |
| Runner registration | GitHub repository API reports `binance-quant-runner`, Linux/X64, online | Confirmed; API did not attest ephemeral mode |
| Host provider | Operator attests Oracle Cloud; host profile run `32644765084` reports QEMU `Standard PC (i440FX + PIIX, 1996)` | OCI deployment confirmed by operator; exact instance OCID remains unrecorded |
| Network egress | Host profile did not find an operator-configured expected fingerprint | `UNVERIFIED`; raw address was not recorded |
| Broker secret source | `main.yml` obtains Binance credentials from GitHub environment secrets | Confirmed |
| Broker secret scope | Credentials are injected only into the trading strategy step | Confirmed by contract tests |

The manual `Runtime Isolation Host Profile` workflow is read-only: it has no
environment, OIDC permission, or secret references. It records DMI/provider
evidence and, when the operator configures both an HTTPS egress-check endpoint
and the expected allowlisted-address digest, records only `MATCHED` or
`MISMATCHED`. It never records the raw address or its digest.

### Candidate decision matrix

| Candidate | Isolation | Network egress | Secret boundary | Cost and maintenance | Rollback |
| --- | --- | --- | --- | --- | --- |
| Ephemeral runner on the same Oracle instance | Low/medium: GitHub assigns one job, but the OCI instance, root processes, filesystem, and container daemon persist | Keeps the current OCI public address and Binance allowlist unchanged | Current GitHub environment secret flow can remain, but every run still enters the persistent host | Lowest incremental cost and simplest bootstrap; no meaningful host-compromise containment | Fast operational rollback, but a suspected host compromise requires instance replacement before reuse |
| Independent disposable OCI Compute JIT runner | High when a whole on-demand instance is launched from a pinned custom image and terminated with its boot volume after one job | Put the runner in a private subnet behind an OCI NAT gateway with a reserved public IP; the instance needs no inbound public IP | Use an OCI instance principal in a tightly matched dynamic group to read one OCI Vault secret version; never bake GitHub or Binance credentials into the image | Pay for right-sized Compute and boot volume only while provisioned, plus image/storage/network resources; launcher, log export, timeout cleanup, and orphan detection require automation | Stop creating instances, wait for/terminate the candidate, reconcile, then restore the unchanged old Oracle runner; reserved NAT IP remains stable |
| Cloud Run Job | High managed task isolation with separate GCP invoker/runtime identities | Dynamic by default; fixed egress requires GCP Direct VPC egress, Cloud NAT, and a reserved IP separate from the current OCI path | Google Secret Manager fits naturally, but moves the broker-secret and execution boundary into a second cloud | Per-run compute can be small, but GCP NAT/static networking, image registry, IAM, and cross-cloud operations add fixed complexity | Trigger rollback is simple; job revision, IAM, NAT, secret version, and old OCI ownership still need reconciliation |

### Recommendation

Use an **independent disposable OCI Compute JIT runner** as the primary target.
It is the smallest migration from the current Oracle-hosted Python/GitHub Actions
runtime that also creates a real fresh-host boundary. Build it from an immutable
OCI custom image, launch an on-demand right-sized flexible instance in a private
subnet, register it for one job with GitHub's ephemeral/JIT mode, route outbound
traffic through an OCI NAT gateway with a reserved public IP, forward runner
logs externally, and terminate the instance **and boot volume** after the
terminal report is durable.

Treat same-host ephemeral registration as a short transitional hardening step,
not the final boundary. It prevents a runner from receiving a second GitHub job,
but cannot remove a compromise from the persistent Oracle/QEMU instance.

Keep Cloud Run Job as the managed alternative. Select it only if a digest-pinned
container passes the same fixture and forward shadow, and the fixed-egress
Cloud NAT cost and operational path are explicitly accepted. Existing Firestore
and GitHub OIDC use is not, by itself, a reason to move execution to Cloud Run.

This recommendation remains pre-provisioning: record the OCI tenancy, region,
compartment, current instance/VNIC, route table, and allowlisted egress ownership
outside public artifacts. Confirm the new reserved NAT address and OCI IAM
policy before choosing an image or Vault implementation.

### Phase 2 validation evidence

- Host profile run `32644765084`: `PARTIAL`, QEMU VM, no secret value read,
  broker secret references confirmed strategy-step-only, egress `UNVERIFIED`.
- First parity run `32644766682` safely stopped during temporary-environment
  setup; the fixture and broker path did not execute. PR #162 corrected the
  interpreter target without changing live code.
- Accepted parity run `32644960891`: GitHub-hosted and current-runner reports both
  recorded `status=ok`, `dry_run=true`, `executed_call_count=0`, and
  `suppressed_call_count=11`.
- Both accepted reports produced semantic digest
  `c52a7cf15079ef3346b6f45bbd3b48aef59c539e868e7780ea9f535e17fee1ed`.

### Preferred OCI control and data planes

```text
trusted provisioning controller
  -> OCI launcher identity (launch/get/terminate only; no Vault secret read)
  -> launch one on-demand instance from a pinned custom-image OCID
       -> private subnet, no inbound public IP
       -> defined security tag: BinanceRuntimeCandidate
       -> short-lived GitHub JIT registration, one job only
       -> OCI NAT gateway with a reserved public IP
       -> OCI instance principal / narrowly matched dynamic group
            -> read one named OCI Vault secret bundle version
            -> write external runner/runtime evidence
       -> existing Google WIF path for bounded Firestore access
  -> terminal evidence and reconciliation
  -> runner deregistration
  -> terminate instance with boot volume deletion
```

Launcher and runtime permissions must be separate. The launcher may create,
inspect, and terminate only tagged candidate instances and attach approved
network/image resources. It must not read the Binance secret. The instance
principal may read only the named secret bundle and required evidence/state
resources; it must not create instances, edit dynamic groups or policies, change
the NAT gateway, or manage Vault secrets.

Use an OCI dynamic-group matching rule constrained by the dedicated compartment
and a defined security tag, not all instances in the tenancy. The custom image
must contain no runner registration, GitHub token, OCI user key, Binance key, or
secret material. Deliver only the short-lived GitHub JIT registration to the new
instance bootstrap; retrieve the broker secret at runtime with the instance
principal.

OCI supports reserved public IPs on NAT gateways. This lets disposable private
instances keep a stable Binance-visible source address without exposing SSH or a
public address on each runner. A new NAT address must remain no-order until a
human verifies it and updates the Binance allowlist. Never let old and new live
paths place orders merely because both addresses are temporarily allowlisted.

Use on-demand capacity for the trading candidate. OCI preemptible instances can
be reclaimed and provide only a short termination warning, which is unsuitable
for an order/reconciliation boundary. Right-size a flexible shape and terminate
it promptly; explicitly request boot-volume deletion because OCI otherwise
preserves the boot volume by default. Secret Management itself is listed as
free, but Vault/key choices, custom images, boot volumes, Compute, and networking
must be checked with the tenancy's OCI cost estimator before provisioning.

### Cloud Run candidate control and data planes

```text
external scheduler
  -> dispatch-only GitHub workflow (GitHub-hosted runner, no broker secrets)
  -> GitHub OIDC / Workload Identity Federation
  -> dedicated Cloud Run invoker service account
  -> run one pre-created Cloud Run Job
  -> dedicated job runtime service account
       -> Firestore state
       -> narrowly scoped Secret Manager secrets
       -> Binance through a fixed outbound IP
       -> append-only execution evidence
```

Build/deploy authority and runtime invoke authority must remain separate:

- the normal scheduler may invoke one reviewed job but may not update its image,
  environment, service account, or secrets;
- deployment uses a separate identity and a digest-pinned container image;
- the Cloud Run runtime identity may read only the required Firestore data and
  named secrets; it may not administer IAM, Cloud Run, or Secret Manager;
- the GitHub workflow never receives `BINANCE_API_KEY` or
  `BINANCE_API_SECRET`.

## Cloud Run candidate gaps to close before deployment

Repository configuration proves the existing GitHub Workload Identity contract
and Firestore use, but it does not prove a reviewed Cloud Run Job, Artifact
Registry image, dedicated invoker identity, or fixed-egress Cloud NAT path.
Treat every missing attestation as `UNVERIFIED`; do not infer a resource from a
project name or create it as part of discovery.

Binance IP allowlisting is an important constraint. Cloud Run uses a dynamic
outbound IP pool by default. A live job must route all outbound traffic through
Direct VPC egress (or a connector) and Cloud NAT with a reserved static IP before
that IP can be allowlisted at Binance.

## Cloud Run candidate risk mapping

| Boundary | Required rule | Failure behavior |
| --- | --- | --- |
| GitHub workflow | No broker secret references; `contents: read`; OIDC only in the invoke job | Do not invoke the Cloud Run Job |
| OIDC trust | Repository and `refs/heads/main` constrained; dedicated invoker service account | Authentication denied |
| Runtime image | Immutable image digest, reviewed source revision recorded | Job remains parked |
| Cloud Run execution | One task, parallelism one, timeout bounded, automatic task retries zero | Terminal failed execution; operator alert |
| Broker secret | Secret Manager resource-level access; withdrawals disabled; static-IP allowlist | Job cannot start or broker rejects request |
| Order idempotency | Stable run/execution ID plus Firestore lease and order intent keys | Duplicate/overlapping cycle aborts |
| Evidence | Append-only report with source SHA, image digest, mode, and executed/suppressed counts | Missing evidence is `PARKED`, never `READY` |
| Live authority | Existing risk envelope only; no automatic expansion of assets, leverage, or capital | Human decision required |

Cloud Run's default task retry count is not appropriate for order execution: a
platform retry could repeat a partially completed cycle. The live job must use
`maxRetries: 0`; application-level recovery must first reconcile Firestore,
broker orders, fills, and the stable execution ID.

## Staged migration

### Phase 1: fixed-input isolation shadow (completed)

- Run `scripts/run_isolation_shadow_fixture.py` on `ubuntu-latest` using
  committed fixtures and a fixed replay clock.
- Do not request OIDC and do not reference any GitHub environment or secret.
- Assert `dry_run=true`, `executed_call_count=0`, and at least one suppressed
  side effect.
- Upload only the redacted structured report.

Passing this phase proves that the strategy package can execute in a clean,
short-lived environment. It does not validate Google Cloud, Binance networking,
or live readiness.

### Phase 2: deployment-neutral discovery and fixture parity (this change)

- Collect a redacted current-runner profile without OIDC or secret access.
- Record the operator-attested Oracle deployment separately from machine
  evidence. Keep the exact OCI resource identity, runner registration mode, and
  egress `UNVERIFIED` until each is attested without publishing sensitive data.
- Run the same portable fixed-input fixture on the GitHub-hosted runner and,
  optionally, the current self-hosted runner.
- Require `dry_run=true`, zero executed calls, no state writes, and a matching
  semantic report digest. Deployment identity fields are excluded from the
  digest; strategy decisions, intents, gates, and suppressed effects remain.
- Compare same-host ephemeral, disposable VPS/VM, and Cloud Run using the matrix
  above. Do not create a runner, VM, container registry, Cloud Run Job, IAM
  binding, network, or secret.

If Cloud Run is selected later, its shadow job must use a digest-pinned image,
dedicated runtime and invoker identities, one task, parallelism one, zero
platform retries, a bounded timeout, and no broker secret. If a disposable VM
runner is selected, the equivalent controls are one job per VM, no runner reuse,
a pinned machine/container image, verified teardown, and external logs.

### Recommended migration sequence

1. Inventory the existing Oracle instance, VNIC, subnet, route table, public IP,
   OCI compartment, and Binance allowlist ownership. Configure the expected
   egress fingerprint and rerun the redacted host profile; do not publish the raw
   address or OCIDs in workflow artifacts.
2. Define a separate OCI candidate compartment/private subnet, reserved NAT
   public IP, NSG/security-list rules, launcher identity, tagged dynamic group,
   runtime policy, Vault secret contract, and external log/evidence destination
   as reviewed IaC. Do not apply it in the documentation phase.
3. Build a pinned OCI custom image without secrets or runner registration.
   Launch one on-demand instance and deliver a short-lived GitHub JIT token. The
   candidate must have no inbound public IP and no broker secret for fixture
   shadow.
4. Run the fixed-input no-order fixture and require the accepted semantic digest
   above. Forward runner diagnostics, terminate the instance with boot-volume
   deletion, and verify the registration cannot accept a second job.
5. Run a read-only forward shadow through the reserved OCI NAT address with a
   separate no-order/no-withdrawal Binance credential retrieved by instance
   principal from OCI Vault. Reconcile decisions against the current runtime.
6. Rehearse missing report, timeout, duplicate dispatch, state lease, launcher
   failure, teardown failure, orphan instance/volume, NAT outage, Vault outage,
   and runner deregistration failure. Platform retries remain disabled.
7. Only after evidence review, have a human add/confirm the candidate NAT address
   in the Binance allowlist and approve an existing-envelope canary. Fence the
   old scheduler before any candidate can place an order.
8. Roll back by disabling new provisioning, terminating the candidate after
   reconciliation, and restoring the unchanged old Oracle path. Do not reassign
   addresses or rotate secrets during incident evidence collection.
9. Retire the persistent runner, remove the old allowlisted address, and rotate
   credentials only in a later cleanup after the observation window closes.

### Phase 3: read-only forward shadow

- Give the selected candidate only the Firestore permissions required for
  shadow state.
- If live market/account observations are required, use a separate Binance API
  key without order or withdrawal capability.
- Limit access to the named read-only credential at the selected runtime
  boundary; do not inject it into discovery or fixture workflows.
- Preserve and verify the current fixed egress for a disposable VM candidate.
  For Cloud Run, establish Direct VPC egress, Cloud NAT, and a reserved outbound
  IP first.
- Run alongside the old runtime without sending orders and reconcile decisions.

Creating or changing a Binance API key is an explicit operator action and is not
part of the automated migration.

### Phase 4: limited live canary

This phase requires a separate PR and human approval. Before it starts:

- the selected candidate must use the existing live risk envelope or a smaller
  one;
- the old scheduler must be fenced so only one execution path can place orders;
- duplicate-order, partial-fill, timeout, Firestore outage, and Binance outage
  recovery must be rehearsed;
- the execution image digest, runner/job revision, identities, IAM bindings,
  secret versions, static IP, and rollback owner must be recorded.

### Phase 5: cutover and retirement

Switch the scheduler only after the canary evidence is accepted. Keep the old
runner installed but disabled during the observation window. Remove it and
rotate credentials only in a later, separately authorized cleanup.

## Deployment preflight

All common items below are mandatory before provisioning any candidate:

- [ ] Phase 1 fixture report is deterministic and records zero executed calls.
- [ ] Current host provider and runner persistence are attested, not inferred.
- [ ] Current public egress matches the separately configured allowlist
      fingerprint without publishing the address.
- [ ] Candidate produces the same semantic digest as the GitHub-hosted fixture.
- [ ] Candidate runs one execution at a time with platform retries disabled.
- [ ] Candidate starts from a pinned image and is destroyed after one execution.
- [ ] Shadow and live use different jobs/runners and identities.
- [ ] Invoke identity cannot deploy, update IAM, or change runtime configuration.
- [ ] Runtime identity is dedicated and is not a default compute identity.
- [ ] Runtime identity has no project-wide Owner, Editor, IAM, runtime-platform,
      or Secret Manager administrator role.
- [ ] Every shadow candidate receives no broker secret. Any later live design
      documents its secret source and limits access to the single execution
      boundary and named secret resources.
- [ ] Live secret versions are pinned or rotation behavior is explicitly tested.
- [ ] Cloud Run, if selected, uses a digest-pinned image, task count one,
      parallelism one, `maxRetries: 0`, and a bounded timeout.
- [ ] Disposable VM, if selected, uses a one-job JIT/ephemeral registration,
      external runner logs, and verified VM destruction.
- [ ] OCI candidate is an on-demand instance launched from a pinned custom-image
      OCID in a private subnet; preemptible capacity is prohibited.
- [ ] OCI candidate egresses only through a NAT gateway with a reviewed reserved
      public IP; the runner itself has no inbound public IP.
- [ ] OCI launcher and runtime instance-principal policies are separate; the
      runtime dynamic group is constrained by compartment and defined tag.
- [ ] OCI custom image, bootstrap metadata, logs, and terminal artifact contain
      no GitHub, Binance, OCI user-key, or Vault secret value.
- [ ] OCI termination deletes the candidate boot volume and an orphan scanner
      detects instances, volumes, VNICs, and runner registrations left behind.
- [ ] Firestore lease prevents overlapping old/new runtime cycles.
- [ ] Static outbound IP is observed from the job and allowlisted at Binance.
- [ ] Binance key has withdrawals disabled and the smallest required trade scope.
- [ ] Execution report reaches durable storage without granting the runtime job
      repository write permission.
- [ ] Alerts cover start failure, timeout, missing terminal report, reconciliation
      mismatch, and circuit-breaker activation.
- [ ] Old live path remains unchanged and available for rollback until cutover is
      separately approved.

## Rollback

Before live canary, rollback means deleting or disabling only the shadow trigger;
the existing runtime is unaffected.

During live canary or cutover:

1. Stop the new scheduler/invoker and wait for the current candidate execution
   to reach a terminal state.
2. Reconcile broker open orders, fills, balances, Firestore lease, and the last
   durable execution report. Do not start the old path while ownership is
   ambiguous.
3. Mark the candidate parked and revoke its invoker binding or runner
   registration. For OCI, block new launches and terminate the disposable
   instance only after runner/runtime logs are durable. Do not delete evidence or
   secret versions during incident response.
4. Re-enable the old dispatch path only after the execution lease is cleared and
   the broker state matches the expected portfolio.
5. Record the rollback reason and require a new canary decision before retrying.

Rollback must never run both paths concurrently. Restoring the old runner is not
permission to bypass a triggered circuit breaker or expand the live envelope.

## Rejected alternatives

- Treating `config.sh --ephemeral` on the existing persistent host as complete
  isolation: runner deregistration does not erase a compromised machine.
- Using an OCI preemptible instance for the live execution boundary: OCI may
  reclaim it with only a short warning during order or reconciliation work.
- Baking the GitHub JIT token, OCI user API key, or Binance credential into the
  OCI custom image or cloud-init metadata.
- Reassigning the old runner's public IP as an automatic cutover mechanism:
  address ownership does not fence schedulers or reconcile broker state.
- Reusing the persistent VPS but deleting the workspace after each run: cleanup
  cannot reliably remove a compromised process, runner service, or host secret.
- Selecting Cloud Run only because Firestore and GCP OIDC already exist: that
  ignores container compatibility, fixed-egress NAT, and additional IAM cost.
- Moving broker secrets to a GitHub-hosted runner: the runner is clean, but its
  outbound IP is unsuitable for a stable Binance allowlist and GitHub would still
  become the broker-secret boundary.
- Deploying Actions Runner Controller: production-grade, but Kubernetes adds an
  unnecessary control plane for one bounded personal runtime.
- Switching live directly to Cloud Run: it skips idempotency, networking,
  reconciliation, and rollback evidence.
- Building broker credentials into a VM/container image, passing them to fixture
  jobs, using mutable image tags, enabling platform retries, or running old and
  new live schedulers concurrently.

## Official references

- [GitHub self-hosted runners reference](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)
- [GitHub secure use reference](https://docs.github.com/en/actions/reference/security/secure-use)
- [GitHub compromised runners](https://docs.github.com/en/actions/concepts/security/compromised-runners)
- [OCI Compute instances](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/instances.htm)
- [OCI custom images](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/managingcustomimages.htm)
- [OCI instance termination and boot volumes](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/terminatinginstance.htm)
- [OCI preemptible instances](https://docs.oracle.com/en-us/iaas/Content/Compute/Concepts/preemptible.htm)
- [OCI reserved public IP addresses](https://docs.oracle.com/en-us/iaas/Content/Network/Tasks/managingpublicIPs.htm)
- [OCI NAT gateway creation](https://docs.oracle.com/en-us/iaas/Content/Network/Tasks/nat-create.htm)
- [OCI instance principals and dynamic groups](https://docs.oracle.com/en-us/iaas/Content/Identity/Tasks/callingservicesfrominstances.htm)
- [OCI Secret Management](https://docs.oracle.com/en-us/iaas/Content/secret-management/Concepts/manage-secrets.htm)
- [OCI Secret Management pricing](https://www.oracle.com/security/cloud-security/secrets/)
- [Google Cloud Run jobs](https://cloud.google.com/run/docs/create-jobs)
- [Execute Cloud Run jobs](https://cloud.google.com/run/docs/execute/jobs)
- [Cloud Run Job secrets](https://cloud.google.com/run/docs/configuring/jobs/secrets)
- [Cloud Run Job service identity](https://cloud.google.com/run/docs/configuring/jobs/service-identity)
- [Cloud Run static outbound IP](https://cloud.google.com/run/docs/configuring/static-outbound-ip)
- [Workload Identity Federation for deployment pipelines](https://cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)
