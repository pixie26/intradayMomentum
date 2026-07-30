# AGENTS.md — SPY Intraday Momentum Research

Reproducible research on the "Beat the Market" SPY intraday-momentum strategy
(1-minute bars, 2008–2026). The project has moved past the two original
baseline scripts: it is now a two-layer framework — a validated data pipeline
plus a three-profile backtest engine — whose open question is whether the
signal still earns more than its costs after the paper's publication
(2024-05-01), per the pre-registered spec in `config/`.

Priorities: (1) faithful, attributable reproduction; (2) tradability — net of
cost, per-regime, post-publication — not pretty curves; (3) no leakage.

## Documentation

All rationale, history and detailed semantics live in `docs/`. Read in this
order before non-trivial work:

1. `docs/PROJECT_WORK_LOG_ZH.md` — full research narrative and current state
   (`..._DETAILED.md` adds per-topic explanations).
2. `docs/PAPER_SAMPLE_CURRENT_COMPARISON_ZH.md` — paper vs authors' sample
   code vs this implementation, item by item.
3. `docs/README_DATA.md` / `docs/README_ENGINE.md` — layer contracts.
4. `config/evaluation_spec_v1.yml` — frozen post-publication evaluation.
5. `config/data_release_v1.yml` — candidate data-v1.0 boundary, timestamp,
   duplicate and environment contract.
6. `docs/HISTORICAL_V3_REVIEW.md` — data-layer review history.

Run reports (tied to a specific `data/processed/runs/<run_id>/`):
- `docs/DATA_AUDIT_20260729_ZH.md` — first local full-sample data audit:
  every problem class found in the real data, with evidence and handling.

## Layout

| Path | Role |
|---|---|
| `SPY_1min_2008_202607_merged.parquet` | Raw 1-min bars (1.8M rows). Read-only input. |
| `spy_dividends_full.csv` | Legacy rounded 74-row dividend input. Preserve for provenance; not the data-v1.0 candidate. |
| `data/reference/spy_dividends_state_street_20260730.csv` | Exact 74-row State Street dividend input for the data-v1.0 candidate; metadata and extraction script are tracked beside/in `scripts/`. |
| `prepare_spy_data.py` | Data layer v5 candidate: explicit release boundaries, duplicate conflict classes, return-gap audit, dependency lock, component validity, and atomic publish. `--self-test` = 28 checks. |
| `im_engine_v4.py` | Engine: three profiles (`official_sample_compatible`, `paper_spec`, `corrected_execution`). Working copy. |
| `test_engine.py` | 62 engine checks. Run: `python test_engine.py`. |
| `experiments/paper_replication_v1/` | Isolated Q24 monthly-return replication; never a substitute for the frozen economic evaluation. |
| `config/evaluation_spec_v1.yml` | Pre-registered evaluation; do not edit in response to results. |
| `config/data_release_v1.yml` | Candidate data-v1.0 contract. Currently blocked by 13 missing leading XNYS sessions. |
| `docs/` | All documentation (see above). |
| `previous_research/` | Archived original baseline scripts + charts. Historical reference only; do not extend them. |
| `manifest/`, `original_uploads/` | 2026-07-29 delivery record (hashes + original filenames). Historical only. |
| `data/processed/` | Generated pipeline runs. Not yet created in this workspace. |

## Core invariants

- Raw data is immutable. Never forward-fill, impute, or "clean" bars in
  place; all transforms happen in code at load time.
- Features are computed on the **full exchange calendar** from
  `spy_1min_clean.parquet`; the session tier is applied **last**, as a trading
  mask. Filtering to a tier first silently redefines previous-close and the
  rolling windows.
- The engine rebuilds its decision mask from parameter-free primitives via
  `config_validity()`. Never consume `signal_valid_default_config` — it is a
  default-configuration diagnostic only.
- No leakage: a signal at bar `t` uses only information available at that
  bar's close; `corrected_execution` fills at the next executable open.
  `sigma_open` / `dvol` windows use only prior eligible sessions; a missing
  observation consumes a window slot — never reach further back to make up
  the count.
- VWAP is the cumulative intraday VWAP from the open (halt minutes exempt),
  not the vendor per-bar column.
- Halt semantics: a position held through a halt earns the reopening gap in
  full; an unfilled order earns nothing; no decisions inside a halt.
- Accounting identities must hold every day: `net = gross − costs
  (+ financing)`; a reversal is two traded units; the EOD flatten is counted;
  AUM changes only by net PnL; unknown exits do not compound into AUM.
- Benchmark: invalid closes are NaN (never the last available bar), anchored
  one session before the evaluation start. Report the full metric set **per
  tier** — never mix CAGR from one tier with MDD from another.
- Never fabricate market data, backtest results, charts, or run status.
  Report what actually ran.
- Change one conceptual component at a time (data, signal, sizing, costs,
  execution, evaluation). Never overwrite frozen artifacts; experiments write
  to new paths.
- The post-2024-05-01 window is a **post-publication evaluation period**, not
  untouched OOS. No parameter fitting on it; the decision rule (gross edge
  per traded share vs costs) is fixed in the spec.

## Current state

Frozen: data-v1.0 begins on 2008-01-22, the raw source's first observed and
complete XNYS session; the documented re-scope is in
`docs/DATA_V1_START_DATE_DECISION_ZH.md`. Also frozen: engine (three profiles)
and evaluation spec v1. Mechanics baseline (zero
dividends/financing, full sample): official
17.0% CAGR / 1.15 Sharpe; paper_spec 16.8% / 1.16; corrected_execution
14.2% / 1.01 — see `docs/PROJECT_WORK_LOG_ZH.md` §6 for the full table.

Pending, in priority order:

1. Real-dividend double run (with / ignore); headlines use with-dividends.
2. Executable evaluation runner driving `profile × tier × dividend × cost`.
3. Signals / fills / round-trip ledger for the pre-registered decomposition.
4. Financing time-integral (cash / borrowed cash / long / short notional
   separately; current `avg_signed_notional` nets longs against shorts).
5. Independent daily SPY raw-close benchmark.
6. Evaluation spec v2, then the single post-publication report.

Explicitly deferred: parameter optimisation, Qlib, machine learning, live
deployment.

## Working rules

- Dependencies: `pip install -r requirements.txt`; it resolves to the exact
  `requirements.lock`, which the data pipeline verifies before publication.
- After engine edits run `python test_engine.py`; after data-layer edits run
  `python prepare_spy_data.py --self-test`. Report what actually ran and the
  date range used; never claim an unrun check passed.
- The pipeline takes ~60s on the full sample; the engine's per-day loop is
  the bottleneck. Smoke-test on a bounded date range first.
- Load the parquet, not the 172 MB CSV. Load once per process. Do not paste
  large data dumps into context; inspect schemas and aggregates.
- Inspect the relevant function before editing. Follow the existing style;
  reuse rather than duplicate. No unrelated refactors or formatting-only
  changes. Engineering-only changes must not alter historical outputs.

## Completion report

State: what changed and which files; why; commands actually run with results;
classification (engineering-only vs strategy-affecting); remaining risks and
checks not run. For output-altering changes, list which frozen artifacts must
be regenerated.
