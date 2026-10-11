# Research backtest timing and cost contract

Status: **characterization only; model defects remain open**. Related issue:
[#366](https://github.com/QuantStrategyLab/BinancePlatform/issues/366).
Baseline inspected: `44a0cd6e54c12cabf8944b7fbed9080840451297`.
This change adds synthetic tests and documentation only. It does not correct the
simulator, generate real signals, select a strategy, or change runtime, orders,
risk decisions, or notifications.

## Current behavior versus required acceptance

| Area | Observed in `research/backtest.py` | Required before treating results as comparable evidence |
| --- | --- | --- |
| Daily information boundary | `prepare_*_daily_indicators` includes the final close of day D; `align_symbol_data` joins on D's date, exposing it at D 00:00 and subsequent hours. | Persist bar open/close and availability timestamps; a decision cannot read an unfinished daily bar. Specify the UTC cutoff, boundary inclusion and source finality/latency. |
| Hourly signal and fill | The hourly close is used to form the snapshot, select weights, and fill buys/sells on that same row. Binance kline `time` is parsed from open time; close time is discarded. | Declare signal availability and execution timestamp/price. A next-bar contract needs an actually available next quote; a same-close model must explicitly declare that idealization and cannot be silently compared with next-open fills. |
| Missing data | Hourly closes are forward-filled after the first observed quote, with no quote-age flag. Leading gaps stay NaN. Filled prices can open or liquidate positions. | Distinguish mark-only carry-forward from executable quotes, reject stale fills, specify maximum age, outages, delistings and calendar coverage. |
| Buy costs | Both trend and BTC DCA calculate quantity as `budget * 0.985 / price`, deduct only `quantity * price` and retain the buffer as cash. | Deduct explicit per-side fees and execution friction, with declared rates and provenance. The buffer is not a 150-bps fee. |
| Sell costs | Trend exits, circuit-breaker liquidation and BTC DCA reductions credit `quantity * price` without costs. | Apply the chosen cost contract on every exit path as well as entry; preserve an auditable cash/quantity/fee ledger. |
| Drawdown anchor | Curve records only post-processing rows; `cummax()` starts at the first recorded value. Initial capital is used for return but is not a separate pre-fill curve anchor. | Include initial capital before the first execution so first-fill costs/losses count in drawdown. Specify observation frequency and cash-flow treatment. |
| Cost stress | No fee/slippage inputs, turnover series, or recomputed cost scenarios are returned. | Supply audited turnover and explicit base assumptions before computing 1×/2×/3× scenarios. Do not invent fee tiers or substitute cash buffers. |

Current output must be labelled **未计成本 / costs not included; timing not
validated**. Corrected net returns, cost-stress results, and causal timing
validation are **not computed**. Existing headline return/drawdown output is not
promotion evidence or a valid same-window net-performance comparison. The tests
below do not change the script's printed labels; downstream use must honor this
restriction until separately authorized implementation fixes the model.

## Executable characterization

Run offline after installing the repository's pinned dependencies:

```sh
python -m unittest discover -s tests -p test_research_backtest_contract.py -v
python -m unittest discover -s tests -v
PYTHONPATH=. python -m pytest -q tests
```

`tests/test_research_backtest_contract.py` calls the real alignment, indicator
preparation and simulation functions. It replaces universe, weight selection and
budget helpers with deterministic fixtures to isolate accounting and timing.
No exchange client is created; `build_client` is guarded to fail. It does not
validate strategy rankings or dependency allocation policy.

The tests deliberately PASS on current limitations:

- Changing only the final daily close changes that day's first-hour SMA20 and
  BTC MA200; the previous hour is unchanged.
- A selected first-row buy is filled at that row's close.
- A missing-hour quote carried forward can execute a new buy.
- A flat-price trend buy/sell round trip leaves all initial equity intact.
- A 1,000-unit budget buys 9.85 units at 100, leaving 9,015 cash; selling at 110
  gives 10,098.5. These are arbitrary fixture units, not investment returns or
  a real cost assumption.
- BTC DCA has the same cash-buffer behavior.
- The curve contains only simulation-row timestamps and no pre-fill capital
  observation. The zero-cost fixture does not manufacture a first-fill loss.

These assertions are tripwires for explicit contract changes, not acceptance
criteria for a correct backtester. When a future model repair changes them,
replace the affected characterization with causal/cost acceptance tests rather
than preserving the defect. There are no skipped or expected-failure tests
masquerading as model validation.

## Future acceptance fixtures (not implemented or claimed passing)

1. Daily boundary: changing D's final close must not change decisions before its
   finalized availability time. Test the instant before, at and after that time,
   plus the final unfinished daily bar.
2. Next-fill timing: a close-derived signal cannot fill at that close under a
   next-bar contract. Test a price jump and missing next quote. Retain separate,
   explicitly labelled cases if a same-close approximation is chosen.
3. Stale market: allow separately labelled valuation carry-forward if intended,
   but no execution on a fabricated quote. Cover both entry and all exit paths.
4. Cost conservation: with arbitrary test-only fee `f`, a flat-price notional
   `q * p` round trip loses exactly `2 * q * p * f` when that is the declared
   per-side fee rule and the budget is sufficient. Test fee units, spread and
   slippage separately, both trend and BTC, including circuit-breaker exits.
   This algebra is a proposed test oracle, not today's model or an account rate.
5. Initial loss: starting equity 100 and a first-fill cost 1 imply first observed
   equity 99 and at least -1% drawdown relative to the initial anchor, even if
   all later observations stay at 99. Current zero-cost fills cannot exercise
   this acceptance case; it is not marked fixed by the curve-shape test.
6. Cost scenarios and comparability: verify turnover units and per-side cost
   accounting before recomputing 1×/2×/3× stress. Require matching data, period,
   universe, availability, execution, valuation, initial capital and costs.

Implementation of these fixtures against a corrected model is separate work.
Issue #366 remains open; this document does not authorize a redesign or live
configuration changes.
