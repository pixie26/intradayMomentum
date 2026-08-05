# AGENTS.md — Intraday Momentum Research

This repository is an audited intraday-momentum research framework. SPY is the frozen formal research line (`2008-01-22` through `2026-07-09`); QQQ is a later exploratory extension and must not be presented as preregistered. Priorities are faithful reproduction, point-in-time safety, economic tradability and reproducible provenance.

## Read before non-trivial work

1. `README.md` — current conclusions, evidence layers and roadmap.
2. `docs/README.md` — documentation index and authority hierarchy.
3. For data work: `docs/README_DATA.md` and `config/data_release_v1.yml`.
4. For engine work: `docs/README_ENGINE.md` and `docs/PAPER_SAMPLE_CURRENT_COMPARISON_ZH.md`.
5. For formal SPY evaluation: `config/evaluation_spec_v2.yml`, `docs/POST_PUBLICATION_EVALUATION_V2_ZH.md` and `docs/STATISTICAL_UNCERTAINTY_V1_ZH.md`.
6. For a specific experiment, read that experiment's `README.md` and config before editing its runner.

Historical reviews and discussions are indexed in `docs/README.md`; load them only when relevant.

## Repository map

| Path | Role |
|---|---|
| `prepare_spy_data.py` | Multi-symbol data audit/publish pipeline; `--self-test` runs 28 synthetic checks. |
| `im_engine_v4.py` | Three-profile signal, execution, accounting and financing engine. |
| `evaluation/` | Frozen matrix runner and report/attribution builders. |
| `config/` | Frozen data and evaluation contracts; never edit in response to results. |
| `data/raw/` | Immutable vendor inputs. The tracked SPY Parquet is the formal source; QQQ CSV is local/ignored. |
| `data/reference/` | Dividends, PIT financing releases, Section 31 schedule and provenance. |
| `data_release_v1/`, `benchmark_release_v1/` | Local immutable SPY bundles; generated and ignored. |
| `data/candidates/`, `data/processed/`, `evaluation/results/` | Rebuildable local runs; generated and ignored. |
| `experiments/` | Isolated replication and post-result sensitivity work. |
| `docs/` | Contracts, audits, conclusions, reports and evidence index. |
| `previous_research/` | Read-only original baselines; do not extend. |

## Non-negotiable invariants

- Raw data is immutable. Never forward-fill, impute or silently adjust bars in place.
- Build features and rolling state on the full exchange calendar; apply session tiers last as trading masks.
- Rebuild decision validity from parameter-free primitives via `config_validity()`; never trade from `signal_valid_default_config`.
- A signal at bar `t` uses only information available at that close. `corrected_execution` fills at the next executable open. Rolling `sigma_open` and `dvol` use prior eligible sessions; missing observations consume window slots.
- VWAP is cumulative intraday VWAP from the open, with explicit halt treatment—not the vendor per-bar VWAP field.
- A position held through a halt earns the full reopening gap; an unfilled order earns none; no decisions occur inside a halt.
- Daily identities must hold: `net = gross - explicit costs - slippage + financing`; reversals trade two units; EOD flatten is counted; AUM compounds only known net P&L.
- Benchmarks use independent daily data where specified, invalid closes stay NaN, and periods anchor one session before their start. Never combine metrics from different tiers.
- Same-day range, close direction, VIX change and other end-of-day variables are diagnostics, not no-lookahead filters.
- Never fabricate market data, run status, results or charts. Report only executed checks and exact date ranges.

## Evidence and publication rules

- Change one conceptual component at a time: data, signal, sizing, execution, costs, financing or evaluation.
- Never overwrite frozen artifacts. Publish new run IDs and write `_SUCCESS` last after completeness and hash checks.
- Preserve three labels: original frozen result; post-result reporting amendment/sensitivity; exploratory cross-asset work.
- The period beginning `2024-05-01` is post-publication evaluation, not untouched OOS. Do not fit parameters on it.
- Commit source, configs, concise tables/manifests and readable HTML. Ignore rebuildable large intermediates unless their inclusion is explicitly justified.
- Reader-facing reports are Chinese; retain necessary profile names, abbreviations and raw audit fields in English.

## Current interpretation

The original SPY frozen headline remains `corrected_execution × paper_ready × with_dividends × $0.005/share`. The halt-aware `$0.0025/share` display is a post-result amendment and must be shown with trading-only performance because cash carry is not strategy alpha. A later HAC/block-bootstrap addendum finds the observed pre/post Sharpe decline imprecise; do not call it statistically significant.

QQQ results are exploratory. They use a later financing release and cross-validated dividends, but still lack a frozen QQQ evaluation spec, clean deterministic rerun, second minute source and final invalid-row adjudication.

Open economic work: market impact/capacity, official closing-auction evidence, queue/partial fills and account-specific financing. Parameter optimization, Qlib, machine learning and live deployment remain deferred.

## Working and verification rules

- Install with `python -m pip install -r requirements.txt`; the formal pipeline checks the exact `requirements.lock` environment.
- Search narrowly. Do not recursively read Parquet/binary files as text; inspect them with structured readers.
- Load the large Parquet once per process. Smoke-test bounded ranges before full runs.
- After data changes: `python prepare_spy_data.py --self-test`.
- After engine changes: `python test_engine.py`.
- After docs/navigation changes: `python scripts/check_markdown_links.py`.
- Evaluation and statistical formal-run tests require local ignored bundles; do not imply that GitHub CI ran them.
- Inspect the relevant function before editing; preserve unrelated user changes and avoid unrelated refactors.

## Completion report

State what changed and why; list commands actually run and results; classify the change as documentation/engineering/strategy-affecting/post-result experiment; list output artifacts that must be regenerated; disclose remaining risks and checks not run.
