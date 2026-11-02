# Cost history coverage

Inspect Spot trade-history coverage from account opening for every supplied
coin. The collector reads trades; offline replay produces a coverage report
and, with a separate evidence file, a moving weighted average cost preview.
Neither component writes to a store, ledger, settings, or trading runtime.
Cost writes remain forbidden, including when the preview produces cost values.

## Input and scope

Supply a historical manifest with `coins` and `symbols`. Each symbol maps to
its base and quote assets, for example:

```json
{"coins": ["BTC", "USDT", "DOGE"], "symbols": {"BTCUSDT": ["BTC", "USDT"]}}
```

Include all historical pairs, including delisted pairs, and coins received
without trades. There is no coin filter. Base, quote and commission coins are
included automatically. Coins with no pair remain visible with a gap.
Current exchange metadata and current balances cannot establish the complete
historical universe. `account_open_ms` must be the evidenced account opening
time, and `end_ms` is a fixed inclusive cutoff; there is no recent-history
default. These timestamps use milliseconds.

The manifest is supplied evidence, not an authenticated account binding. This
collector does not discover accounts or claim that the manifest is complete.
Spot trade records do not cover deposits, transfers, rewards, Earn, staking,
airdrops, converts or dust. Other account products and unavailable historical
records remain unproven without separate evidence. Those omissions keep cost
status `UNKNOWN`.

## Collector interface

`application.cost_history_collector.collect_history` accepts a reader and
returns an in-memory `cost_history.v1` artifact. It loads no credentials and
performs no persistence. `SpotHistoryGET` wraps an existing SDK client with
exactly one permitted request: signed `GET /api/v3/myTrades`. It rejects other
methods, paths, unsupported parameter combinations and verbose SDK clients,
and disables redirects. There are no order, transfer or exchange write calls.
A caller must use its approved credential and protected storage arrangement;
this feature adds no live collection entrypoint or scheduled job.

| Parameter | Required | Default |
| --- | --- | --- |
| `universe` | Yes | Historical coin and base/quote manifest |
| `account_open_ms` | Yes | No inferred opening time |
| `end_ms` | Yes | No moving cutoff |
| `pagination` | No | `from-id` |
| `max_requests` | No | 100,000 across all symbols, including retries |
| `request_interval` | No | At least 1 second before each GET |
| `weight_ceiling` | No | Pause at 1,000 used request weight per minute |
| `mode` | No | Only `coverage-only` is accepted |

`from-id` starts at ID zero per symbol and advances to the largest returned
ID plus one. Trade IDs need not be consecutive. It stops at a short/empty page
or after crossing the cutoff, then filters to the inclusive opening/cutoff.
An exactly full final page requires another query. IDs are scoped to symbols;
exact duplicate rows are counted once, conflicts and stalled/regressing
cursors fail that symbol without claiming its range covered.

`time-window` queries non-overlapping inclusive day windows. A full window
cannot establish completeness and may contain the newest rows, so collection
restarts that symbol at ID zero and scans its history. This also handles more
than 1,000 fills sharing one millisecond. `fromId` is never combined with time
parameters. These constraints follow the Binance
[account trade endpoint](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/account-endpoints).

The collector honors `Retry-After` on HTTP 429 with at most three retries;
a delay over 60 seconds stops collection with a gap. HTTP 418 stops immediately.
It pauses for 60 seconds at the configured minute-weight ceiling. Budget
exhaustion, permission failures, invalid symbols and malformed responses also
produce explicit gaps, rather than empty successful coverage. No other
endpoint is queried to recover a failed symbol. The adapter and collector
suppress provider exception text and signed URLs. Rate handling follows the
Binance [REST limits](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/limits).
Use a dedicated, non-concurrent client; headers describe IP-wide usage and
cannot guarantee headroom when other clients share the IP.

## Offline coverage report

The standalone CLI reads a previously saved private artifact and prints JSON:

```sh
python scripts/replay_cost_history_coverage.py --input "$HISTORY_JSON"
```

It imports no broker SDK or storage backend, loads no runtime settings, and
has no output-file or apply option. `--mode apply` is rejected. The pure
`replay_coverage(history, mode="coverage-only")` API enforces the same mode
guard before inspecting input and accepts no storage sink. Filesystem and
socket traps, input immutability checks, and import-boundary tests protect
this separation.

For each coin the report includes:

- Successful trade-query ranges by symbol and their intersection across all
  relevant pairs, even when a successful range has zero trades.
- Missing query ranges and collector failures, preserving partial successful
  windows. A missing coin/pair relationship is an explicit gap.
- `UNKNOWN` segments and reasons, including absent opening/nontrade cost
  proof, third-coin fee valuation and missing historical quote-cost evidence.
- Unique trade counts for both base and quote legs, buy/sell counts,
  commission-event counts and duplicate counts.

Replay orders supplied trades chronologically and checks for debits without
observed trade inventory. It uses exact decimal quantities internally, never
assumes an opening acquisition cost, and never returns prices, quantities,
average costs or proposed ledger patches. A fully queried trade range remains
`UNKNOWN` for cost purposes until the other sources and accounting policies
have evidence. A saved artifact's coverage is a supplied claim, not independent
proof of Binance retention or an account's economic history. Same-time
cross-symbol inventory checks are diagnostic, not proof of execution order.

Keep artifacts and reports private: coin names, timestamps and activity counts
can reveal account activity. Only invented fixtures belong in Git. This
feature has no connection to reconciliation recovery or stop-loss activation.

## Offline cost preview

Add `--basis-evidence "$BASIS_EVIDENCE_JSON"` to the same CLI, or call
`application.cost_history_basis.replay_cost_basis(history, evidence)`.
The result is `cost_basis_preview.v1`, with per-coin quantity, total cost,
average cost, acquisition and disposal records, evidence references, UNKNOWN
reasons and the original trade-only coverage report. The nested coverage
report still describes Spot-only proof; it does not incorporate the evidence
file. The mode remains `coverage-only`: both APIs reject apply/write modes
before reading input and accept no store, ledger or settings sink.

All costs and historical prices use the explicit `reporting_asset`. A unit of
that asset is one reporting unit; no USD peg or present-day market price is
assumed. Opening inventory is immediately before `account_open_ms`; every
event from that time through `end_ms`, inclusive, is replayed. Decimal
arithmetic uses 160 significant digits internally; serialized values are
decimal strings. There is no rounding to exchange order precision.

### Accounting policies

- Use moving weighted average across all supplied coins, from account opening.
  Disposals remove quantity at the pool's current average acquisition cost.
  Incoming exchange assets use execution value, rather than the disposed
  asset's historical acquisition cost. Spot execution value is `quoteQty`
  multiplied by the quote asset's historical reporting price. Convert/dust
  execution value uses the reporting leg when present, otherwise the sold
  asset's execution-time reporting price.
- Capitalize third-coin fees into the acquired asset at execution-time value.
  Reduce the fee coin at its own moving average cost. An unvalued fee or
  unproven fee-coin basis leaves the acquired cost UNKNOWN. Fees in the spent
  asset add execution value to acquisition cost; fees withheld from the
  received asset reduce received quantity while retaining gross cost.
- Recognize Earn and staking rewards at receipt-time fair value. Recognize
  airdrops on the same basis; no reliable historical price means UNKNOWN.
  Matched Spot/Earn principal movements preserve existing aggregate basis
  and do not create reward income.
- External deposits/transfers carry documented source acquisition cost after
  matching transfer identity, coin, net quantity, account ownership, both
  account identities and the sending fee. A deposit record alone is
  insufficient. The source cost covers the gross outgoing lot; a same-coin
  sending fee removes its proportional source basis. Unsupported cross-coin
  sending fees and unmatched legs remain UNKNOWN.
- Replay convert/dust execution records as linked sell/buy legs with explicit
  quantities and fees, including an explicit zero fee. Missing execution,
  fee or valuation evidence leaves UNKNOWN; no Spot fill is fabricated.

### Evidence format

`cost_basis_evidence.v1` is a separate private JSON object. It must match the
history's `account_open_ms` and `end_ms`. It contains:

| Field | Meaning |
| --- | --- |
| `reporting_asset` | A coin in the historical manifest; all costs/prices use this unit |
| `universe_source` | Reference proving the complete historical coin/pair universe |
| `opening` | Per coin: `quantity`, `cost` (or null), and a `source` reference |
| `nontrade_sources` | Per coin: evidence reference for complete nontrade records over the full interval |
| `trades` | Optional evidence keyed by `SYMBOL:tradeId`, with `prices` and optional `sequence` |
| `events` | Nontrade and conversion execution records, described below |

Every coin needs opening evidence, including explicitly documented zero
inventory, and complete nontrade evidence even when no nontrade events
occurred. Missing references prevent known final inventory and costs. Any
uncovered Spot query or collector failure prevents known final inventory
and costs for every coin: a missed trade could charge any supplied coin as
its commission asset. Coins without Spot pairs can have
known basis when opening and nontrade evidence is complete. Evidence for
coins outside the manifest or unmatched trade IDs is rejected.

All events have a unique string `id`, `kind`, `time_ms`, and `source` reference.
Receipt/withdrawal/principal events have `coin` and positive `quantity`.
An event's optional `prices` maps coins to objects such as
`{"price": "5", "time_ms": 10, "source": "synthetic-price-record"}`.
Prices must be positive, have a source, and match the exact event timestamp.
Opening and event quantities, costs and prices use nonnegative decimal
strings, with up to 30 integer and 30 fractional digits.

| Event kind | Additional fields |
| --- | --- |
| `earn_reward`, `staking_reward`, `airdrop` | Receipt-time `prices` for the received coin |
| `deposit`, `transfer_in` | `transfer_id`, `from_account`, `to_account`, `owner`, and a matching `source_leg` |
| `internal_transfer` | The same matching fields, `fee_quantity: "0"`, and a matching `source_leg` |
| `withdrawal` | `fee_coin` and `fee_quantity`, with zero fees explicit |
| `convert`, `dust` | `execution_id`, `sold_coin`, `sold_quantity`, `bought_coin`, `bought_quantity`, `fee_coin`, `fee_quantity`, and execution-time `prices` |

An external `source_leg` repeats `transfer_id`, `from_account`, `to_account`,
`owner` and `coin`, and provides gross `quantity`, same-coin `fee_coin`, `fee`,
`source_cost`, `source` and outgoing `time_ms` (no later than receipt).
Gross quantity less fee must equal the received quantity. `source_cost` is
the documented acquisition cost of the gross quantity in `reporting_asset`,
never its current market value. The source leg may precede account opening.
An internal principal `source_leg` repeats the matching fields, quantity and
explicit zero `fee_quantity`, with its own source reference. Both internal
accounts must be in the same aggregate Spot/Earn inventory scope. A matched
internal event changes no aggregate quantity or basis. A withdrawal removes
its principal and any fee from inventory at their own moving averages.

Events sharing a millisecond require distinct, evidenced integer `sequence`
values across both Spot and nontrade records. Missing or duplicate order
evidence leaves affected inventory UNKNOWN. Input order and alphabetical
symbol order never prove execution order. Exact duplicates with the same
event ID are counted once; conflicting duplicates and reuse of a transfer ID
are rejected. Separate evidence cannot introduce a `spot` event.

An acquisition without cost evidence contaminates the mixed pool, including
later known purchases. Its basis can recover only after a proven complete
disposal to zero and a later evidenced acquisition. Missing history or
quantity evidence prevents this recovery. `observed_quantity` is diagnostic;
`quantity`, `total_cost` and `average_cost` are null when completeness is
unproven. Unknown segments retain the historical failure, even after a pool
recovers. Each acquisition retains its event/source links and applicable
historical valuations; each disposal retains removed moving-average cost.

References are supplied provenance claims. This offline calculation cannot
authenticate an account, verify an export or establish that a user-provided
completeness claim is true. It never fetches missing prices or account data.
Keep inputs and cost-valued output in protected private storage. This policy
does not authorize cost persistence, live collection, trading or deployment.
