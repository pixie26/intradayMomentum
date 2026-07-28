# AGENTS.md — SPY Intraday Momentum Research

## Project

Reproducible quantitative research on an intraday momentum strategy for SPY
(1-minute bars, 2008–2026), replicating and then stress-testing a
"Beat-the-Market"-style day-trading strategy: breakout of a dynamic intraday
noise band, optional VWAP confirmation, volatility-targeted sizing, flat
overnight, per-share commissions.

Priorities, in order:

1. **Faithful frozen baselines** — the two existing scripts (`naive` and
   `paper`) define the reference behavior. Reproduce and freeze them before
   any improvement.
2. **Tradability, not curves** — an attractive full-sample equity curve is
   not evidence. Net-of-cost, per-regime, out-of-sample robustness is.
3. **No leakage** — every signal must use only information available at the
   minute it is computed, with execution delayed to the next minute.

The current conclusion is: baseline scripts run end-to-end and produce
charts, but no frozen run manifest, statistical validation, or cost
robustness analysis exists yet. Tradability is unproven.

## Core invariants

- Raw data files are immutable inputs. Never edit, re-save, or "clean" them
  in place; all transforms happen in code at load time.
- Signal at minute `t` may only use bars with timestamp `<= t`; PnL accrues
  from `t+1` onward (the existing `shift(1)` convention). Never attribute a
  return to a position entered at the same minute that generated it.
- `sigma_open` must remain shifted by one day: the rolling per-minute band
  uses only prior days' `move_open`. Same-day values in the band are
  leakage.
- `spy_dvol` uses daily returns strictly before the current day (current
  implementation: days `d-15` through `d-2`). Preserve or explicitly
  re-justify this gap.
- VWAP used in signals is the cumulative *intraday* VWAP up to minute `t`,
  reset each day — never a full-day VWAP, and not the raw vendor `vwap`
  column in the parquet (that is per-bar).
- Daily aggregates (e.g. `df_daily`, `ret_spy`) are benchmark/reporting
  material only; they must never feed intraday signals.
- Freeze baseline configs and outputs before optimizing. Never overwrite
  baseline artifacts; experiments write to new paths.
- Change one conceptual component at a time: data, signal, sizing, cost
  model, execution timing, or evaluation — never several at once.
- Never fabricate market data, backtest results, charts, or run status.
  Report what actually ran.
- Reproducibility through explicit parameters, recorded data fingerprints,
  and run metadata — not through notebooks' hidden state.
- Leave user changes and unrelated files intact.

## Current state and important paths

| Path | Role |
|---|---|
| `SPY_1min_2008_202607_merged.parquet` | Primary data: 1,813,340 1-min bars. Columns: `caldt` (timestamp), `timestamp` (string dup), `open/high/low/close/volume`, `trade_count`, `vwap` (per-bar, vendor), `day`. Read-only. |
| `SPY_1min_2008_202607_merged.csv` | Same data as CSV (~172 MB). Prefer the parquet; use the CSV only for interchange. |
| `spy_dividends_full.csv` | 74 quarterly SPY dividends, 2008-03-20 → 2026-06-18 (`Date`, `Dividend`). |
| `backtesting_BeatTheMarket_2008_2026_naive.py` | **Baseline A (naive)**: noise-band breakout state machine, full-notional sizing. |
| `backtesting_BeatTheMarket_2008_2026.py` | **Baseline B (paper)**: band + VWAP filter, vol-target sizing. |
| `spy_momentum_*_{naive,paper}.png` | Generated baseline charts (equity curve, cumulative return). Generated output, not input. |

Not under version control. No tests, configs, or artifact manifests yet —
creating them is expected early work (see Baseline freeze below).

## Baseline semantics (authoritative description)

Both scripts share: RTH filter 09:30–15:59 (minute_of_day 1–390); evaluation
buckets at `min_from_open % trade_freq == 0` (13 buckets/day at
trade_freq=30); flat overnight; AUM recursion `AUM_d = AUM_{d-1} + net_pnl`
from `aum_0 = 100000`.

**Signal construction**
- `move_open(t) = |close(t)/open_day − 1|` per minute.
- `sigma_open(m)` = per-minute-of-day rolling mean of `move_open` over 14
  days (`min_periods=13`), **shifted one day**. First ~15 trading days are
  warmup and produce no trades.
- Bands: `UB = max(open, prev_close_adj)·(1 + band_mult·sigma_open)`,
  `LB = min(open, prev_close_adj)·(1 − band_mult·sigma_open)`, where
  `prev_close_adj = prev_day_close − dividend_today` (dividend assumed known
  at the open on ex-date; it adjusts the band reference only — positions are
  flat overnight so dividends never enter intraday PnL).

**Baseline A (naive)** — persistent state machine at each bucket: enter
long/short on band breakout, reverse on opposite breakout, otherwise hold
previous position (including through zero-signal buckets). Sizing: full
notional, `shares = round(prev_AUM / open)`.

**Baseline B (paper)** — signal-reactive: at each bucket, `+1` if
`close > UB and close > vwap`, `−1` if `close < LB and close < vwap`,
else flat from the next minute (zero signal erases the position; it does
not persist). Sizing: vol-target,
`shares = round(prev_AUM / open · min(target_vol/spy_dvol, max_leverage))`,
falling back to `max_leverage` notional when `spy_dvol` is NaN.

**Execution & costs (both)**
- Bucket marks are forward-filled, then `shift(1)`: the position decided at
  bucket `t` earns returns starting minute `t+1`.
- `gross_pnl = Σ exposure · Δclose_1m · shares`; overnight gap is never
  captured (first minute diff is NaN and excluded).
- `trades_count = Σ|Δexposure|` including the end-of-day flatten; each unit
  is one order of `shares` shares.
- `commission = trades_count · max(min_comm_per_order, commission_rate · shares)`;
  `net_pnl = gross_pnl − commission`.

**Frozen baseline parameters**

| Parameter | A (naive) | B (paper) |
|---|---|---|
| sigma window / min_periods | 14 / 13 | 14 / 13 |
| band_mult | 1.0 | 1.0 |
| trade_freq (min) | 30 | 30 |
| sizing | full notional | vol_target: target_vol=0.02, max_leverage=4.0 |
| spy_dvol window | — | 14 daily returns, days d−15…d−2 |
| commission / min per order | $0.0035/share / $0.35 | same |
| aum_0 / period | 100,000 / 2008–2026 | same |

## Baseline freeze and reproduction

Phase 1 — Freeze. Before any experiment:
- Record SHA-256 of the parquet and dividend CSV, Python/pandas/numpy/
  statsmodels/matplotlib versions, and the exact CLI used per baseline.
- Save each baseline's printed stats table and daily `strat` series (ret,
  AUM, ret_spy) to an artifacts folder, plus a run manifest (data hashes,
  parameters, date range, runtime, output paths, code file hashes).
- The four existing PNGs are unfrozen outputs; regenerate them from the
  frozen configs and treat the regenerated set as reference.

Phase 2 — Acceptance. A baseline counts as reproducible when one clean
command regenerates stats and charts from documented inputs and the numbers
match the frozen record. A pretty chart alone is not reproduction.

Phase 3 — Controlled improvement. Test ideas one at a time, each as a
separate config compared against the frozen baseline on identical dates,
cost model, and metric code. Candidate queue (do not combine in a first
experiment):
- A. Tradability: commission sensitivity (0 / 0.5× / 1× / 2×), slippage per
  trade, trade_freq ∈ {5, 15, 30, 60}, capacity/rounding effects.
- B. Ablations: remove VWAP filter; remove vol targeting; band_mult sweep;
  state-machine vs signal-reactive position handling.
- C. Signal variants: entry stop / time stop, overnight holding, alternate
  band definitions (true-range based), vendor per-bar vwap vs computed VWAP.
- D. Regimes: per-year and per-vol-regime performance, drawdown episodes,
  warmup sensitivity.
- E. Universe: QQQ/IWM or ES futures only after A–D are understood on SPY.

## Validation and experiment discipline

- Rule-based strategy: no fitted parameters beyond the frozen windows, so
  the leakage risk is in timing, not training. Still define a development
  window (suggested: 2008–2017) and a holdout (suggested: 2018–2026).
  Parameter/variant selection uses the development window only; run the
  holdout once, after the choice is frozen. Baseline *reproduction* covers
  the full sample for fidelity — selection must not.
- Never pick a variant on one favorable interval. Report per-year Sharpe,
  net/gross return, max drawdown, turnover (trades/day), and worst year;
  full-period aggregates alone are insufficient.
- Comparisons require identical date range, cost assumptions, and metric
  implementation. If two scripts compute stats differently, unify the metric
  code first — that is an engineering-only change.
- Accounting identities must hold every day: `net = gross − costs`;
  `trades_count` includes the EOD flatten; AUM changes only by net PnL.
  Breakage of an identity invalidates the run, not the identity.
- Interpret results as a stylized backtest: minute-close execution, no
  slippage, no borrow/locate, no market impact. Do not claim tradable alpha
  without cost sensitivity, regime stability, and execution-timing
  robustness.
- Prefer the simpler variant unless added complexity shows repeatable
  out-of-sample (holdout or walk-forward) improvement and survives ablation.

## Working rules

- Inspect the relevant function before editing; both scripts share ~80% of
  their code — duplicated logic is known debt, but do not refactor structure
  in the same change as an algorithmic edit. Never mix modernization with
  behavior change.
- Keep loading, feature construction, backtest loop, and evaluation/plotting
  separated by responsibility, as in the current scripts.
- Explicitly sort by timestamp before grouped rolling/shift operations;
  keep explicit date/timestamp dtypes.
- Centralize paths and parameters (argparse or a config); no hardcoded
  absolute paths in reusable code.
- Fit nothing on the full sample: any learned transform (if ever added)
  fits on the development window only.
- Avoid installing dependencies or regenerating full-history outputs unless
  the task requires it.

## Large-file and context discipline

- The parquet is the only practical load (~1.8M rows); avoid the 172 MB CSV
  in iterative work. Load once per process; cache the RTH-filtered frame
  when iterating.
- The per-day Python loop over ~4,600 days is the runtime bottleneck. Smoke
  test on a bounded date range (e.g. 2–5 years) before any full run; state
  the range used in the report.
- Read schemas, samples, and aggregates before whole frames; do not paste
  large data dumps into context.

## Verification

Start with the narrowest check; broaden with risk.
- Timing/leakage edits: unit-check that `sigma_open` excludes the current
  day, bucket signals apply from `t+1`, and `spy_dvol` excludes the current
  day; then a bounded end-to-end run.
- Signal/sizing/cost edits: hand-verify one full day bar-by-bar (bands,
  signal, exposure after shift, trades_count, commission, PnL) against code
  output, plus the accounting identities above; then compare against the
  frozen baseline on identical dates.
- Evaluation/metric edits: verify stats on a small synthetic return series
  with known Sharpe/drawdown.
- Report separately: checks run, checks not necessary, checks not run, and
  residual risk. Never claim an unrun check passed.

## Handoff / completion report

State: what changed and which files; why; commands actually run with their
results (date range, key stats); classification of the change as baseline
reproduction, engineering-only, or strategy experiment; and remaining risks.
For any change that alters historical outputs, explain why and list which
frozen artifacts must be regenerated.
