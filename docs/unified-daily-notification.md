# Unified daily notification (code only)

Routine cycles and verified order notifications are persisted for the central
daily digest regardless of legacy flags. Unknown order outcomes, risk, persistence
and authentication errors remain immediate. A candidate write failure sends a
standalone coverage error, not the routine message.
Dry-run previews remain suppressed and are not counted as live trading runs in
the production digest; their existing local and archived checks remain available.

QPK source reference: `2b04882058666ebc81d1650d93fa2105ec4ba3d8` (notification modules only).

The notification-only QPK source snapshot in `notifications/digest_snapshot/` avoids an
unrelated trading dependency upgrade. Keep these three modules byte-identical
to the reviewed QPK notification modules. Existing QPK/strategy pins, order
execution, funding, risk, account enablement and durable report writes are unchanged.

Before deploying the policy, validate the exact active target's private archived
report, candidate store and QRS receiver. Existing cloud monitor wiring uses
`RUNTIME_TARGET_ENABLED=true`, exact protected `RUNTIME_TARGET_JSON`,
`EXECUTION_REPORT_GCS_URI`, `DIGEST_CANDIDATES_GCS_URI`, `DIGEST_STORE_TARGET`,
`DIGEST_OPAQUE_ACCOUNT_UID` and `DIGEST_TARGET_ID`. Missing configuration stops
projection; disabled targets stay disabled. Do not infer private identities.
The producer aggregates every unique cycle in the completed 06:00 SGT window;
missing evidence is unknown, never zero. No new collector or scheduler is added.

No deployment, variable application, IAM, secret or live message is authorized
by this PR. Source adoption and delivery require separate approved release work.

For multiple active targets, protected `DIGEST_TARGET_BINDINGS_JSON` supplies a
list of per-target environment mappings. `DIGEST_NOTIFICATION_SCHEDULE_JSON`
can reuse the existing heartbeat scheduler/calendar declaration to prove a
window was not due. Missing due reports stay errors; closed configured sessions
produce an `actually_ran=false` marker without inventing zero trading counts.

Fill counts require individual execution identifiers. A completed order response
proves an order completed, not how many fills it received. Missing fill counts
remain unknown; confirmed routine orders still appear without a false diagnostic.

