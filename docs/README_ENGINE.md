# Intraday-momentum engine v3

```
primitives → config-specific validity → signal → pending order → fill
          → filled position → mark-to-market → costs → calendarised report
```

## Three profiles, not two

"The paper" and "the authors' notebook" are different artefacts. With only two
modes every discrepancy is unattributable.

| profile | what it is |
|---|---|
| `official_sample_compatible` | reproduces the notebook's **conventions** on valid complete sessions: `min_periods=13`, the `d-15..d-2` window, `round()` share sizing, the NaN-vol → 4× fallback, no slippage, row-based rolling, raw daily returns. This is convention parity, **not line-by-line parity** — the calendar, tiering and AUM handling are this engine's. A real parity test must reconcile shares, signal, exposure, trade units, commission, gross, net and AUM day by day. |
| `paper_spec` | the paper as written: 14-session windows, $0.0035/share commission **and** $0.001/share slippage, dividend-adjusted previous close |
| `corrected_execution` | `paper_spec` plus honest execution: fills at the next executable open, a real order/fill state machine, halt-aware marking, separated cost components, calendarised statistics |

```python
from im_engine_v4 import profile_cfg, load_run, backtest, stats, benchmark, report
cfg  = profile_cfg("corrected_execution")
tbl  = report("data/processed/runs/<run_id>", cfg)
print(tbl.to_string()); print(tbl.attrs["provenance"])
```

## Two things this engine refuses to do

**1. Consume `signal_valid_default_config`.** That field is baked at the data
layer's `--trade-freq` / `--sigma-window` and assumes VWAP + vol targeting.
Reusing it means `Cfg(trade_freq=15)` still decides only on 30-minute buckets,
silently. `config_validity()` rebuilds the mask from the parameter-free
primitives at the current configuration. `Cfg(require_config_match=True)`
additionally hard-fails on a mismatch.

**2. Shift exposure by data rows.** That hands a reopening gap to an order that
never filled.

## Halt semantics

| situation | earns the reopening gap? |
|---|---|
| position established before the halt | **yes** — the holder bore that risk |
| signal on the last pre-halt bar, `corrected_execution` | **no** — the order was due inside the halt and is cancelled |
| same signal, `queue_until_executable` | **no** — it fills at the reopening price, P&L starts after |
| decision inside the halt | not permitted |
| `paper_spec` / `signal_bar_close` | **yes** — the paper's own convention fills at the signal bar's close, so the position exists before the halt. This is optimistic and is left visible rather than hidden. |

Halt bars supplied under `--halt-bar-policy allow_present` are excluded from the
cumulative VWAP and from the mark-to-market path.

## Accounting

- **Quantities.** A reversal is **two traded units**, not one: `0 → +1 → −1 → 0`
  is four units on three fill events. Costs are charged on units, and the
  reported `TradeUnits` / `SharesTraded` / `CostPerTradedShare_c` make the
  turnover explicit. `reversal_order_model` selects whether the minimum
  commission applies once (a single 2N-share order) or twice.
- **Share sizing** honours `share_rounding` (`round` for the notebook profile,
  `floor` for the paper). A `Cfg`-field usage audit in the test suite fails if
  any declared field is never read by the engine — this exists because
  `share_rounding` was silently ignored for a full round of results.
- **Unknown exits do not compound.** `unknown_exit_policy` is explicit:
  `terminate` (default — the equity curve is invalid from that day on),
  `exclude_session_and_freeze_aum` (assume zero P&L that day; disclose it), or
  `impute_last_observed` (sensitivity only). Marking the day's return `NaN`
  while letting its guessed P&L into AUM would still contaminate every later
  position size.
- **Financing is a cash account**, not a flat rate on equity: `cash_rate_annual`
  on equity (the book is flat overnight), `funding_rate_annual` on borrowed
  cash, `borrow_rate_annual` on short notional, pro-rated by
  `financing_daycount_fraction` and actual holding time. All default to 0.
  Crediting rf on all of equity and then deducting a separate leverage fee
  double-counts.
- **Costs are separable.** `commission = Σ max(min_comm, comm_per_share × qty)`,
  `slippage = Σ slip_per_share × qty`. The minimum applies to commission only.
  `shares <= 0` produces no order and no cost.
- **No final-bar round trip.** A position is never opened on the last scheduled
  bar; it would be liquidated at the same price moments later for zero P&L and
  two charges.
- **Strict eligible-session windows.** `sigma_open` rolls over the full
  scheduled minute grid, so a session merely missing that minute still consumes
  a slot. Daily returns spanning an invalid close are excluded from the vol
  window outright, never back-filled with older returns.
- **Calendarised.** Every calendar session gets a row with a `status`
  (`active`, `no_signal`, `warmup_no_prev_close`, `warmup_no_band`,
  `tier_excluded`, `invalid_feature_dvol`, `zero_size`, `unknown_exit`).
  Sessions are never deleted from the time axis. `CAGR` uses elapsed calendar
  time. **`Sharpe_calendar` is the Sharpe ratio.** Active-only returns are
  reported as un-annualised conditional moments (`ActiveMeanRet_bp`,
  `ActiveStd_bp`), because scaling them by √252 when only ~153 sessions a year
  carry a position manufactures a higher number that collapses back onto
  `Sharpe_calendar` once rescaled by √(252 × active/calendar).
- **Dividends load automatically** from the run's manifest. `paper_spec` and
  `corrected_execution` refuse to run without them; `Cfg(ignore_dividends=True)`
  is required to override and is recorded in provenance.
- **Trailing-truncated sessions** are flagged `unknown_exit` **only if a
  position was still open when the tape stopped** — being flat before the gap
  keeps the P&L known. `unknown_exit` sessions leave the evaluation window
  entirely (`ret = NaN`, `is_evaluation = False`), so they contribute to no
  headline statistic.

## Benchmark

`benchmark()` computes SPY inside this engine on exactly the same sessions,
calendar, dividend file and warm-up cutoff as the strategy. Sessions without a
valid close contribute `NaN`, never the last available bar, and the series is
anchored one session before the evaluation start so the strategy's first
trading day has a matching benchmark return. `benchmark_valid_sessions`,
`benchmark_missing_close_sessions` and `regression_aligned_sessions` are
reported. For production, prefer an independent daily raw-close + dividend
series with its own provenance — a minute file with a truncated tail cannot
recover the true daily close. It returns
`spy_price_CAGR%`, `spy_total_CAGR%`, `excess_CAGR%`, `beta_vs_spy_total`,
`alpha_annualised%` and `InfoRatio`. Do not compare against an external
annualised approximation.

Without a dividend file the benchmark's total return collapses onto its price
return and is understated by roughly 1.8%/yr, which **overstates excess return
by the same amount**. Dividends matter more to the benchmark than to the
strategy, where they only shift the band anchor on ~74 ex-dates.

## Reporting

Report the **full metric set per tier**. Never take CAGR from one tier and MDD
from another — they are different equity curves, so the resulting Calmar is not
a defined quantity.

`provenance` carries `profile`, `engine_script_sha256`, `git.commit`,
`git.dirty`, `data_run_id`, `data_script_sha256`, `source_sha256`, the dividend
file's SHA and event count, and the full engine config.

## Tests

`python test_engine.py` — 57 checks covering halt fill semantics, reversal unit
accounting and minimum-commission conventions, the final-bar round-trip guard,
`exec_lag_minutes` fill timing, strict eligible-session rolling, validity
filtering of the volatility window, `ignore_dividends`, unknown-exit exclusion,
parameter plumbing (`trade_freq`, `sigma_window`, `use_vwap`, `sizing`),
dividend band adjustment, cost decomposition and the calendar time axis.
`python prepare_spy_data.py --self-test` — 28 data-layer checks.

## Not yet implemented

Leverage financing, short-borrow cost, market impact, and a queue-position model
for the passive fill assumption. `corrected_execution` charges a flat
$0.005/share and no financing, so it is still optimistic for a strategy that
runs at 2.5× average leverage.
