# SPY minute-data preparation — v4

## Run

```bash
python prepare_spy_data.py --self-test        # 18 checks, no data needed

python prepare_spy_data.py \
  --input SPY_1min_2008_202607_merged.parquet \
  --dividends spy_dividends_full.csv \
  --output-dir data/processed \
  --input-timezone America/New_York \
  --bar-label start \
  --source-split 2016-01-04
```

`--input-timezone` has no default. A tz-naive input without it is a hard error.

## The data → engine contract

**Do not filter bars to a tier and compute features afterwards.** That silently
redefines the strategy. Measured on this sample: dropping 2009-07-27 (opens
11:15) makes `close.shift(1)` hand 2009-07-28 a previous close from 07-24
(98.07) instead of 07-27 (98.35), and a "previous 14 sessions" window covers 17
calendar days instead of 19.

The engine must:

1. read `spy_1min_clean.parquet` and compute every feature over the **full
   exchange calendar**;
2. read `feature_validity_minute.parquet` and rebuild the decision mask from
   the **parameter-free primitives** at its own configuration
   (`im_engine_v4.config_validity`);
3. apply the session tier **last**, as a trading mask.

`im_engine_v4.py` implements this.

`signal_valid_default_config` is diagnostic only. It is baked at the
`--trade-freq` / `--sigma-window` given to this pipeline and assumes VWAP plus
volatility targeting, so an engine consuming it would run `Cfg(trade_freq=15)`
on a 30-minute decision grid without any error. Use it for default-configuration
parity checks and nothing else; `Cfg(require_config_match=True)` hard-fails on a
mismatch.

### Component validity, and why one flag is not enough

Components have different dependency structures:

| component | depends on | an interior gap at 11:19 … |
|---|---|---|
| `move_open_obs_valid` | session open + this bar | still valid at 12:00 |
| `vwap_valid` | every required minute from the open through m | false from 11:19 onward |
| `close_valid` / next `prev_close_valid` | that session's last scheduled minute | unaffected |

So a leading-truncated session is `open_valid=False, close_valid=True` — it
cannot contribute a `move_open` observation but it is still a perfectly good
previous close for the next session. A trailing-truncated session is the
opposite and is worse: 2019-08-12 kills its own daily return *and* 2019-08-13's
previous close.

Halt minutes are exempt from `vwap_valid`: no volume traded, so nothing is
missing.

`sigma_history_valid` and `daily_vol_history_valid` are derived at the
`--trade-freq` / `--sigma-window` passed on the command line. They are a
convenience. The primitives (`bar_present`, `open_valid`, `close_valid`,
`move_open_obs_valid`, `vwap_valid`, `is_halt_minute`, `is_executable_minute`)
are parameter-free; recompute the history flags in the engine when sweeping.

## Halts

No decision, no order, position frozen. **The reopening gap is booked in full** —
the holder bore that risk. On 2020-03-09 the halt spans 09:35–09:48 and SPY
reopens +1.03%; deleting that move would remove real P&L and is exactly why
circuit-breaker sessions matter for tail risk. Because P&L is the diff of
consecutive *available* bars, the gap is captured automatically.

Four MWCB halts are built in. Extend with `--halts` (session_date, start_local,
end_local; inclusive, exchange-local). Overlapping windows are unioned before
counting. `--halt-bar-policy` controls whether the vendor is expected to omit
halt minutes (`absent`, default) or may carry bars through them.

## Reporting

Report the **full metric set per tier**. Never take CAGR from one tier and MDD
from another — they are different equity curves, so the resulting Calmar is not
a defined quantity.

| tier | role |
|---|---|
| `paper_ready` | official-sample compatibility and data-integrity baseline |
| `halt_aware` | primary economic result once the engine handles halts |
| `exploratory` | sensitivity analysis only |

Tightening the session set moved results in the flattering direction on this
sample, so print the tier alongside every number.

Do not hand-maintain results in this file. Generate them:

```python
from im_engine_v4 import profile_cfg, report
tbl = report("data/processed/runs/<run_id>", profile_cfg("corrected_execution"))
print(tbl.to_string()); print(tbl.attrs["provenance"])
```

`provenance` carries `data_run_id`, `source_sha256`, `data_script_sha256`,
`engine_config`, and the sample bounds. Note `DataSessions` (sessions in the
file) and `ReturnObs` (daily returns produced) differ — the first session has no
previous close, and vol warm-up consumes more. Quote both.

## Output layout

```
data/processed/
  latest.json                       # pointer to the newest published run
  runs/<run_id>/
    _SUCCESS
    spy_1min_clean.parquet          # features are computed from this
    spy_1min_paper_ready.parquet
    spy_1min_halt_aware.parquet
    spy_1min_exploratory.parquet
    feature_validity_session.parquet
    feature_validity_minute.parquet # the trading mask
    spy_dividends_clean.csv
    reports/
      data_manifest.json            # all paths RELATIVE to the run directory
      session_quality.csv  minute_coverage.csv  halt_minutes.csv
      extreme_return_rows.csv  longest_zero_volume_runs.csv
      longest_stale_bar_runs.csv
```

Staged in a temp directory and moved into place atomically. A failure at any
point before the move leaves nothing and does not touch `latest.json`. A failure
in the narrow window *after* the move but before the pointer update can leave an
orphan run directory carrying `_SUCCESS`; it is inert — consumers resolve runs
through `latest.json` — but it is not literally "no artefacts".

## Invariants

- Raw OHLC is never adjusted or forward-filled; no missing bar is imputed.
- Row conservation is asserted.
- Session grading compares **minute sets**, not bar counts.
- Minute coverage counts **usable** observations, not bar presence: a session
  opening at 11:15 has a 15:59 bar, but its `move_open` anchors on the 11:15
  price, so it must not enter the band history.
- `band_warm` requires a full `sigma_window` of usable history.
- Minute-bucket eligibility respects early closes (39 in this sample).

## Known limits

- `candidate_format_regime` is a heuristic hint about decimal granularity, not
  vendor identification. Prefer a real `source` column or `--source-split`.
- The raw-vs-adjusted test is reported as `evidence_supports_raw_prices`.
- `--dividend-duplicate-policy sum` requires either a `dividend_type` column
  showing distinct types on the conflicting ex-dates, or `--confirm-dividend-sum`.
- Full-sample runtime ~60s, dominated by the 1.8M-row scheduled-minute grid.
