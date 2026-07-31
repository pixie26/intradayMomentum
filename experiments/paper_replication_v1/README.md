# Paper replication experiment v1

This experiment compares the engine's compounded monthly net returns with the
monthly and yearly performance table in Q24 of `Intraday-momentum.pdf`
(2025-09-22 revision, description on PDF page 41, table on page 42).

It is deliberately isolated from the frozen economic evaluation:

- it does not consume or write `config/evaluation_spec_v1.yml`; it only hashes
  the file before and after the run as an isolation guard;
- it writes only to a new, explicitly supplied result directory;
- it changes no profile defaults and fits no parameter;
- every output is labelled `replication_only_not_economic_evaluation`.

The matrix runs all three profiles, all three data tiers, and both
`with_dividends` / `ignore_dividends` modes for attribution. The preselected
appendix comparison is `paper_spec × halt_aware × with_dividends`, using the
State Street dividend file carried by `data-v1.0`. This selection was
registered after a preliminary visual comparison, so it is a transparent
replication diagnostic, not a blind pre-registration.

The paper starts in May 2007, while `data-v1.0` starts on 2008-01-22. Months
containing engine warm-up or unavailable source sessions remain in the detailed
CSV but are marked non-comparable and excluded from match-rate summaries.
The 2025 yearly figure is treated as January-August YTD because those are the
months reported in Q24.

Run:

```bash
python experiments/paper_replication_v1/run.py --self-test
python experiments/paper_replication_v1/run.py \
  --config experiments/paper_replication_v1/config.yml \
  --output-dir experiments/paper_replication_v1/results/<new_run_name>
```

The runner refuses to overwrite an existing output directory and publishes it
atomically. Outputs:

- `monthly_comparison.csv`
- `yearly_comparison.csv`
- `summary.csv`
- `performance_benchmark.csv`
- `equity_curves_monthly.csv`
- `report.html`
- `manifest.json`

## Current verified run

`results/data-v1.0_q24_detailed_report_20260730_v2/` was generated from the
immutable `data-v1.0` bundle. The primary
`paper_spec × halt_aware × with_dividends` comparison reports both all
available paper months and the stricter fully comparable subset. See
`results/data-v1.0_q24_detailed_report_20260730_v2/report.html`.

Across the 18 cells, that primary combination has the lowest strict
comparable-month MAE: 0.3059 percentage points over 206 months, versus
0.3539 points when dividends are ignored. With-dividends has lower monthly
MAE than ignore-dividends in every matched profile/tier pair.

## Add-on: report2 (interactive date-window view)

`report2.html` sits next to `report.html` in the same result directory and is
generated separately by `make_report2.py` (it reads only existing artifacts
and never re-runs the engine or modifies the run outputs). It contains one
interactive section: the reader picks a start and an end date, and the
month-end log-scale cumulative-NAV chart (paper Q24 strategy / local
strategy / local SPY) and a metrics table (指标 | 论文策略 | 本地 SPY |
本地策略) are recomputed live for that window. Windowed metrics use a
monthly methodology (vol = monthly std × √12, Sharpe with rf = 0, MDD on
month-end points); the SPY column is the local SPY month-end series from
the same equity file and the same profile/tier/dividend selection (the
paper publishes no monthly benchmark series), while the paper's Table 3
SPY buy-and-hold values remain as a fixed reference line below the table.
Regenerate:

```bash
python experiments/paper_replication_v1/make_report2.py \
  --results-dir experiments/paper_replication_v1/results/data-v1.0_q24_detailed_report_20260730_v2
```
