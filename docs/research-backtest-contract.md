# Research backtest timing and cost contract

Status: **synthetic causal/cost acceptance; DEVELOPMENT_ONLY**. Related issue:
[#366](https://github.com/QuantStrategyLab/BinancePlatform/issues/366).
Implementation baseline: `ef2324db`. PR #371 was inspected first; its portfolio
stoploss observations do not change this research simulator.

This repair changes `research/backtest.py`, its offline tests and this contract.
It does not change runtime decisions, orders, risk budgets, account settings,
notifications, dependency pins or deployment. No real returns were computed.

## Information and execution boundaries

- Binance kline `time` is the bar open/event time. The inclusive API close-time
  field is retained as `available_at = close_time + 1 millisecond`.
- Synthetic daily/hourly bars without availability metadata use the explicit
  zero-latency assumptions `open + 1 UTC day` / `open + 1 hour`.
- Daily indicators retain `indicator_event_time` and `indicator_available_at`.
  A backward as-of join admits them only at or after their availability.
  Conservative detail: a close-derived hourly signal uses the daily indicators
  already available at that hourly bar's **open**, so a daily update arriving
  during the hour is deferred to the next hourly signal.
- Signals use the preceding completed hourly row. A signal available at time T
  may fill at the next hourly bar's actual open at T. It cannot use that new
  bar's close. Delayed or missing prior quotes cannot become close signals.
- Every symbol must have exactly the BTC timeline; mismatched positional input
  raises rather than silently reading a future row.
- `start`/`end` bound available observations. A final hourly close available
  after `end` is excluded. With no closed bars the result is initial capital.
- Hourly close carry-forward is for marks only. Opens are never forward-filled;
  a missing, nonfinite or nonpositive actual open rejects execution, including
  exits. A missing signal is not an instruction to liquidate a trend holding.
  Missing exit quotes leave positions intact for a later eligible real quote.
- Pre-trade allocations and circuit-breaker checks mark at the current actual
  open, falling back to a previously available close in a gap. Post-bar equity
  uses only closes available by that observation time. Mark carry-forward
  does not assert that an outage position can be liquidated at its last mark.
- Circuit-breaker checks observe the current open and may liquidate there;
  their ledger signal/availability timestamps are the current open. Other
  decisions use the previous completed hourly row.
  A triggered risk exit with no actual open remains pending and fills only
  when a later real open permits liquidation. Pending exits survive the daily
  risk reset; the existing daily entry block and minimum-notional rule remain.

These are declared historical simulation assumptions, not a reconstruction of
exchange latency, spread, market impact or actual order arrival. A2's shared
execution model remains separate work.

## Costs and cash conservation

`simulate_window(..., fee_rate=..., slippage_rate=...)` accepts finite per-side
fractions in `[0, 1)`. Rates are caller-supplied research assumptions. The script
has no account fee tier or calibrated slippage input and does not invent one.

For reference open `p`, quantity `q`, per-side fee `f`, slippage `s`:

| Side | Execution price | Cash delta | Explicit slippage cost |
| --- | --- | --- | --- |
| Buy | `p * (1 + s)` | `-q * execution_price * (1 + f)` | `q * p * s` |
| Sell | `p * (1 - s)` | `q * execution_price * (1 - f)` | `q * p * s` |

Trend and BTC buy quantities fit **fees plus notional** within the existing
`budget * 0.985` spending allowance. The remaining 1.5% stays cash; it is not
reported as a fee. Trend exits, circuit-breaker exits and BTC partial reductions
all debit the same explicit fee/slippage model.

The detailed `trade_ledger` records signal time, signal availability, fill time,
symbol, side, reason, reference/execution price, quantity, notional, fee,
slippage cost and signed cash delta. Summary fields include `total_fees`,
`total_costs = fees + slippage`, and `turnover_notional` in quote-currency units.
All trades contribute to totals even when details are not captured.

If either cost input is omitted, it defaults to zero and the result is labelled
**UNSPECIFIED_ZERO_COST**. Supplying both inputs, including explicit zeros,
produces **EXPLICIT_RESEARCH_RATES**, which still does not assert real-account
coverage. All results retain **DEVELOPMENT_ONLY**. The default script prints
that unspecified zero-cost results cannot support promotion or budget changes.

## Equity and repeatable acceptance

The equity curve begins with pre-execution initial capital at `start` and then
records closed-hour marks at their availability times. Drawdown includes the
initial anchor, so the first execution cost cannot disappear into `cummax()`.
CAGR uses the same observed time span; an unannualizable zero-span result is
NaN, and extreme short synthetic gains may produce positive infinity rather
than aborting the timing test with floating-point overflow.

Offline checks with the repository's pinned QPK and CryptoStrategies sources:

```sh
python -m unittest discover -s tests -p test_research_backtest_contract.py -v
python -m unittest discover -s tests -v
PYTHONPATH=. python -m pytest -q tests
ruff check research/backtest.py tests/test_research_backtest_contract.py
```

The acceptance tests call the real preparation, alignment and simulator code,
replace only the universe/ranking/budget helpers with deterministic fixtures,
and guard `build_client` to fail on any exchange access. They cover:

- Daily final-close perturbations cannot affect earlier hours; delayed daily
  availability is respected.
- Previous closed-hour signals fill at the next actual open, not a same-row
  close; missing next opens reject fills.
- Carried marks cannot create trades; missing exit quotes defer liquidation;
  missing signal information does not imply an exit.
- Trend round-trip cash conservation, buy/sell slippage, BTC buy/partial-sell
  fees and circuit-breaker exit fees, including deferred exits after missing
  quotes and across the next daily risk reset.
- Initial drawdown anchor, unfinished final bars, empty windows, invalid rates,
  future-perturbation invariance, delayed hourly closes, timeline mismatch and
  truthful unspecified-cost status.

Synthetic rates and prices are test oracles, not investment performance or
actual account assumptions. No locked real-data result, calibrated execution
friction, cost-stress backtest, strategy ranking validation or live parity is
claimed. Issue #366 should be evaluated against these limits before closure.
