# Binance Runtime Isolation Migration

## Status and scope

This document defines a staged migration from the persistent GitHub self-hosted
runner to a short-lived execution boundary. It does **not** authorize a live
cutover, delete the current runner, change broker permissions, or move broker
credentials.

The current `main.yml` workflow remains the production path until a separately
reviewed cutover change is approved. The first migration phase is limited to a
fixed-input, no-order shadow replay on a clean GitHub-hosted runner.

## Current architecture

The current runtime is dispatched through GitHub Actions and runs on the
persistent `binance-quant-runner` self-hosted runner. The broker job checks out
the repository, authenticates to Google Cloud through GitHub OIDC, builds or
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

## Decision

Use an isolated **Cloud Run Job** as the preferred target. A just-in-time
ephemeral GitHub runner is the fallback only if the runtime proves incompatible
with Cloud Run networking or execution constraints.

Cloud Run Job is the lower-complexity fit for this personal deployment because
the runtime is a bounded command, not a long-lived HTTP service. Each execution
runs in a fresh managed task, exits when complete, and writes logs to Cloud
Logging. It also avoids operating Kubernetes solely for GitHub Actions Runner
Controller.

### Target control and data planes

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

## Current GCP gaps to close before deployment

The `binancequant` project currently has the GitHub Workload Identity provider
and a runtime service account with Firestore access, but no reviewed Cloud Run
Job, Artifact Registry repository, runtime Secret Manager entries, dedicated
invoker identity, or fixed-egress Cloud NAT path. Those are deployment
prerequisites, not defects to paper over in workflow YAML.

Binance IP allowlisting is an important constraint. Cloud Run uses a dynamic
outbound IP pool by default. A live job must route all outbound traffic through
Direct VPC egress (or a connector) and Cloud NAT with a reserved static IP before
that IP can be allowlisted at Binance.

## Risk boundaries

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

### Phase 1: fixed-input isolation shadow (this change)

- Run `run_cycle_replay.py` on `ubuntu-latest` using committed fixtures.
- Do not request OIDC and do not reference any GitHub environment or secret.
- Assert `dry_run=true`, `executed_call_count=0`, and at least one suppressed
  side effect.
- Upload only the redacted structured report.

Passing this phase proves that the strategy package can execute in a clean,
short-lived environment. It does not validate Google Cloud, Binance networking,
or live readiness.

### Phase 2: Cloud Run fixture shadow

- Create a shadow-only container entrypoint that runs the same committed fixture.
- Deploy a separate `binance-runtime-shadow` job with no broker secrets.
- Set one task, parallelism one, zero retries, and a bounded timeout.
- Invoke manually through a dedicated GitHub OIDC invoker identity.
- Compare the Cloud Run report digest with the GitHub-hosted Phase 1 report.

### Phase 3: read-only forward shadow

- Add Firestore read/write permissions required for shadow state only.
- If live market/account observations are required, use a separate Binance API
  key without order or withdrawal capability.
- Add Secret Manager resource-level access only for that read-only key.
- Establish Direct VPC egress, Cloud NAT, and a reserved outbound IP.
- Run alongside the old runtime without sending orders and reconcile decisions.

Creating or changing a Binance API key is an explicit operator action and is not
part of the automated migration.

### Phase 4: limited live canary

This phase requires a separate PR and human approval. Before it starts:

- the Cloud Run job must use the existing live risk envelope or a smaller one;
- the old scheduler must be fenced so only one execution path can place orders;
- duplicate-order, partial-fill, timeout, Firestore outage, and Binance outage
  recovery must be rehearsed;
- the execution image digest, job revision, service accounts, IAM bindings,
  secret versions, static IP, and rollback owner must be recorded.

### Phase 5: cutover and retirement

Switch the scheduler only after the canary evidence is accepted. Keep the old
runner installed but disabled during the observation window. Remove it and
rotate credentials only in a later, separately authorized cleanup.

## Deployment preflight

All items below are mandatory before Phase 2 or later:

- [ ] Phase 1 fixture report is deterministic and records zero executed calls.
- [ ] Container entrypoint defaults to no-order; live requires an explicit,
      reviewed runtime target.
- [ ] Image is referenced by digest, not a mutable tag.
- [ ] Shadow and live are different Cloud Run Jobs and identities.
- [ ] GitHub invoker service account has `roles/run.invoker` only on the intended
      job and cannot update the job.
- [ ] Job runtime service account is not the default Compute service account.
- [ ] Runtime service account has no project-wide Owner, Editor, IAM, Cloud Run
      admin, or Secret Manager admin role.
- [ ] Broker secrets are absent from GitHub and plaintext environment variables;
      Secret Manager access is limited to named secrets.
- [ ] Live secret versions are pinned or rotation behavior is explicitly tested.
- [ ] Task count and parallelism are one; task retries are zero; timeout is
      shorter than the scheduling interval.
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

1. Stop the new scheduler/invoker and wait for the current Cloud Run execution to
   reach a terminal state.
2. Reconcile broker open orders, fills, balances, Firestore lease, and the last
   durable execution report. Do not start the old path while ownership is
   ambiguous.
3. Mark the Cloud Run job parked and revoke its invoker binding. Do not delete
   evidence or secret versions during incident response.
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
