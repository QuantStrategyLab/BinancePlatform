# Cost history coverage

Inspect Spot trade-history coverage from account opening for every supplied
coin. The collector reads trades; offline replay produces only a coverage
report. Neither component calculates acquisition costs or writes to a store,
ledger, settings, or trading runtime. Moving weighted average is the selected
method for a later cost reconstruction; it does not authorize applying costs.

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
Trade records do not cover deposits, transfers, rewards, Earn, staking,
airdrops, converts or dust. Other account products and unavailable historical
records remain unproven. Those omissions keep cost status `UNKNOWN`.

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

## Offline report

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
