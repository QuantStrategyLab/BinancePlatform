# Portfolio stoploss observation (report only)

The cycle report adds `diagnostics.portfolio_stoploss.pre_trade` and
`post_trade`. These are observations only. They do not authorize execution,
change allocations, add stop orders, send notifications, change the cycle
status, or repair the portfolio stoploss gap described in issue #369.

The observation concerns the legacy portfolio unrealized-PnL stoploss rule
(-20%), not every stop mechanism. Existing strategy-level held-trend stops,
strategy stop diagnostics, daily circuit breakers, and MEMBER gate decisions
remain unchanged.

## Meaning

- `evaluation_status: not_evaluated` with
  `reason_code: legacy_rule_not_in_reviewed_gate` means the reviewed dependency
  path uses `assess_with_evidence`, without applying the legacy portfolio rule.
  This is not a passed risk check, even if cost data are complete and the
  MEMBER assessment says APPROVE.
- `evaluation_status: unknown` with
  `reason_code: gate_evaluation_evidence_unavailable` means the installed
  revisions, catalog entrypoint wrapper, or MEMBER assessment contract could
  not be verified against the reviewed path. No enforcement conclusion follows.
- `evaluation_status: not_evaluated` with
  `reason_code: evaluation_not_reached` means that cycle phase has not reached
  evaluation, for example after an early failure or pre-trade rejection.
- `pnl_input_status` separately describes average-cost coverage: `missing`,
  `partial`, `complete`, `empty`, `invalid`, or `unknown`. Complete coverage is
  an input observation, not evidence a stoploss was evaluated. Empty positions
  do not establish complete cost coverage. No PnL is computed by this helper.

`position_count` and `cost_covered_position_count` count positions in the
strategy-member snapshot after managed-symbol filtering. They are not
whole-account coverage, asset-value percentages, balances, or an account PnL.
Fuel/nonmember assets may be excluded. Unknown/invalid input counts are null.
The current snapshot builder supplies no `average_cost`, so a valid nonempty
snapshot currently reports missing cost inputs. No account identifiers, symbols,
monetary values, PnL values, or arbitrary runtime metadata are added to this
observation. Both metadata-to-allocation and allocation-to-report boundaries
use a strict projection.

## Reviewed scope and future upgrades

The reviewed path is CryptoStrategies
`7fcca8ee0280b3e66245eb673caadda8b8b3b9a3` and QuantPlatformKit
`155d74a615ccdebf7223787939c2dc7151d75efd`, with the catalog crypto entrypoint
and `qsl.risk_gate_assessment.v2` MEMBER assessment. The common wrapper passes
no risk-control state; the gate's extra stop/drawdown mandate fields apply to
specific TQQQ schemas, not this crypto path, and its risk engine uses no
legacy portfolio-stop detector. Review this conclusion again when dependencies
or entrypoint/gate contracts change. Do not infer it from a dependency version
number, from a MEMBER approval, or from missing risk flags.

Installed Git revision provenance is required for the reviewed-path label.
Editable installs without that provenance conservatively report unknown. CI
uses editable dependency overlays, so tests explicitly cover both known and
unknown provenance rather than pretending an editable checkout is verified.

Synthetic tests cover cost coverage, invalid/nonfinite input, strict redaction,
unknown versions/contracts, independent phases, early exit, and unchanged
strategy decisions, cycle action callbacks, notifications, and report status.
No live account, order, notification, deployment, or trading workflow is used.
