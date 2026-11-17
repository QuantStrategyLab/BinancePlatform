# Unknown-Outcome Query-Before-Submit Contract

English | [简体中文](unknown-outcome-submission-contract.zh-CN.md)

## Purpose and Scope

This contract prevents crash recovery or exception handling around order submission from issuing duplicate submits. It constrains only durable `order_submission` state immediately before and after the broker client call; it does not alter receipt reducers, `quoteOrderQty`, funds or position state, retry/backoff policies, strategies, providers, replay, broker adapters, deployment, Scheduler, QPK, or dependencies.

## State Table

| Durable State | Permitted Action | Entry Condition | Exit Condition |
| --- | --- | --- | --- |
| `RESERVED` | Only state allowed to submit | Default state, or reset for the next logical order after verified terminal | Before any client call, durable write of `SUBMISSION_UNKNOWN` succeeds |
| `SUBMISSION_UNKNOWN` | `get_order` query/reconcile only; never submit | Identity digest and symbol written durably | Transitions to `TERMINAL` only after receiving and persisting a verified broker terminal response |
| `TERMINAL` | Never submit using previous order state | Verified broker `FILLED`, `CANCELED`, `EXPIRED`, or `REJECTED` persisted durably | Durably reset to `RESERVED` for the next logical order |

`SUBMISSION_UNKNOWN` stores only `state`, `identity_sha256`, and `symbol`; raw order IDs, quantities, prices, and strategy dates must never be persisted there. Broker queries derive the client order ID from the digest without storing raw order IDs in state records.

## Exception Classification and Fail-Closed Rules

| Category | Condition | Behavior |
| --- | --- | --- |
| Pre-send local failure | Request is proven not sent **before** `SUBMISSION_UNKNOWN` durable write (e.g., symbol/identity validation failure or UNKNOWN write failure) | Do not invoke client; stay in `RESERVED` |
| Post-UNKNOWN client exception | Any exception after `SUBMISSION_UNKNOWN` durable write succeeds (HTTP, transport, provider-specific, or unclassified) | Retain `SUBMISSION_UNKNOWN`; query/reconcile only; never write `TERMINAL` or retry submit |
| Query not found, exception, or non-terminal | Query returns empty, unparseable, error, or non-terminal status (`NEW` / `PARTIALLY_FILLED`) | Fail closed; keep `SUBMISSION_UNKNOWN` and fail the current cycle so later runs still only query/reconcile |
| Verified terminal | Broker query or direct response confirms `FILLED`, `CANCELED`, `EXPIRED`, or `REJECTED` | Only case permitting durable write of `TERMINAL`; failed terminal write retains `SUBMISSION_UNKNOWN` |

Any crash recovery reads durable state first. If state is `SUBMISSION_UNKNOWN`, recovery ignores current request payloads and may only query/reconcile; without a verified broker terminal response, it must neither submit nor write `TERMINAL`.

## Compatibility and Residual Risks

Legacy state lacking `order_submission` is treated as `RESERVED`. This contract depends on single-write durability in state storage; if persistence is unavailable before calling the client, execution fails closed. Concurrent writers and broker eventual consistency are outside scope and likewise retain `SUBMISSION_UNKNOWN`, blocking resubmission until a verifiable terminal response arrives.
