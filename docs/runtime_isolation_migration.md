# Binance Runtime Isolation Migration

## Status and scope

This document defines a staged migration from the persistent GitHub self-hosted
runner to a short-lived execution boundary. It does **not** authorize a live
cutover, delete the current runner, change broker permissions, or move broker
credentials.

The current `main.yml` workflow remains the production path until a separately
reviewed cutover change is approved. Phase 1 is a fixed-input, no-order shadow
replay on a clean GitHub-hosted runner. Phase 2 is deployment-neutral discovery
and replay parity: it must identify the existing boundary before choosing an
ephemeral runner host or Cloud Run.

## Current architecture

The current runtime is dispatched through GitHub Actions and runs on the
`binance-quant-runner` self-hosted runner. The GitHub repository runner API
confirms only that it is an online Linux/X64 runner; it does not expose whether
the host is GCE, OCI, or another VPS/VM. The repository rename checklist records
the deployment as Oracle/VPS, but that remains documentation evidence rather
than a fresh host attestation. The broker job checks out the repository,
authenticates to Google Cloud through GitHub OIDC, builds or reuses a local
dependency environment, and injects the Binance credentials only into the
strategy step.

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
The current runner may be an ordinary VPS with an already allowlisted stable
egress address. Phase 2 first records the host provider, runner registration
mode, network-egress match, and secret source without reading any secret value.

Current evidence is:

| Question | Evidence | Status |
| --- | --- | --- |
| Runner registration | GitHub repository API reports `binance-quant-runner`, Linux/X64, online | Confirmed; API did not attest ephemeral mode |
| Host provider | Rename checklist says Oracle/VPS; runner API does not expose provider | `UNVERIFIED` until host profile runs |
| Network egress | Runbook expects an allowlisted runner address; no checked-in fingerprint exists | `UNVERIFIED` until an operator-configured fingerprint matches |
| Broker secret source | `main.yml` obtains Binance credentials from GitHub environment secrets | Confirmed |
| Broker secret scope | Credentials are injected only into the trading strategy step | Confirmed by contract tests |

The manual `Runtime Isolation Host Profile` workflow is read-only: it has no
environment, OIDC permission, or secret references. It records DMI/provider
evidence and, when the operator configures both an HTTPS egress-check endpoint
and the expected allowlisted-address digest, records only `MATCHED` or
`MISMATCHED`. It never records the raw address or its digest.

### Candidate decision matrix

| Candidate | Isolation gained | Stable egress | Operational cost | Main residual risk | Selection condition |
| --- | --- | --- | --- | --- | --- |
| Ephemeral runner on the same persistent host | One GitHub job per registration, but not a fresh host | Preserves current egress | Low | Host processes, filesystem, Docker daemon, or root compromise can survive runner deregistration | Transitional only; host is rebuilt or runner executes inside a genuinely disposable VM boundary |
| Independent disposable VPS/VM runner | Fresh VM per execution; keeps GitHub runner compatibility | Reserved/fixed IP is straightforward | Medium | Image/bootstrap, runner-token delivery, log forwarding, and VM teardown must be automated | Prefer when the current runtime needs VM semantics or Binance IP allowlisting dominates |
| Cloud Run Job | Fresh managed task with separate invoke/runtime identities | Requires Direct VPC egress plus Cloud NAT/static IP | Medium | Container compatibility, NAT cost/configuration, and GCP IAM become new dependencies | Prefer only after no-order container parity and fixed-egress feasibility are proven |

No final target is selected in Phase 2. A same-host ephemeral registration is
not equivalent to an ephemeral machine and is not the end state. The lowest-risk
choice is the candidate that passes the same no-order digest, has a verified
stable egress path, separates invoke/runtime identity, and can be destroyed
after one execution.

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
- Mark host provider, registration mode, or egress as `UNVERIFIED` rather than
  guessing from the GCP project or legacy documentation.
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
   registration. Do not delete evidence or secret versions during incident
   response.
4. Re-enable the old dispatch path only after the execution lease is cleared and
   the broker state matches the expected portfolio.
5. Record the rollback reason and require a new canary decision before retrying.

Rollback must never run both paths concurrently. Restoring the old runner is not
permission to bypass a triggered circuit breaker or expand the live envelope.

## Rejected alternatives

- Reusing the persistent VPS but deleting the workspace after each run: cleanup
  cannot reliably remove a compromised process, runner service, or host secret.
- Moving broker secrets to a GitHub-hosted runner: the runner is clean, but its
  outbound IP is unsuitable for a stable Binance allowlist and GitHub would still
  become the broker-secret boundary.
- Deploying Actions Runner Controller: production-grade, but Kubernetes adds an
  unnecessary control plane for one bounded personal runtime.
- Switching live directly to Cloud Run: it skips idempotency, networking,
  reconciliation, and rollback evidence.

## Official references

- [GitHub self-hosted runners reference](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)
- [GitHub secure use reference](https://docs.github.com/en/actions/reference/security/secure-use)
- [Google Cloud Run jobs](https://cloud.google.com/run/docs/create-jobs)
- [Execute Cloud Run jobs](https://cloud.google.com/run/docs/execute/jobs)
- [Cloud Run Job secrets](https://cloud.google.com/run/docs/configuring/jobs/secrets)
- [Cloud Run static outbound IP](https://cloud.google.com/run/docs/configuring/static-outbound-ip)
- [Workload Identity Federation for deployment pipelines](https://cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)
