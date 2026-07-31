# Executable evaluation runner

This directory turns the frozen YAML matrix into one reproducible entry point.
It does not modify `config/evaluation_spec_v1.yml`.

Start with the read-only preflight:

```powershell
python evaluation/run_evaluation.py --plan-only
```

The plan must show:

- 3 profiles × 3 tiers × 2 dividend modes × 4 slippage assumptions = 72 cells;
- full, pre-publication and post-publication = 216 summary rows;
- the immutable `data-v1.0` release ID, bounds and hashes;
- the spec, engine, data, dividend, source and Git provenance;
- any conditions that still prevent a formal publication.

The frozen v2 preflight is:

```powershell
python evaluation/run_evaluation.py --plan-only `
  --spec config/evaluation_spec_v2.yml `
  --benchmark-daily benchmark_release_v1/spy_daily_raw_close.csv `
  --financing-rates data/reference/financing_rates_v1
```

A single-cell engineering smoke run is explicit and cannot be confused with a
formal evaluation:

```powershell
python evaluation/run_evaluation.py `
  --smoke-cell paper_spec__halt_aware__with_dividends__slip_0p0010 `
  --allow-dirty-smoke
```

Smoke outputs are labelled `non_formal_smoke`. A full execution refuses to run
until the formal v2 assumptions are frozen, the Git worktree is clean and the
independent daily benchmark and point-in-time financing release required by v2
are supplied. The financing curve uses the previous completed ECB monthly
3-month USD LIBOR proxy before the transition and the latest SOFR observation
strictly before each session afterwards. Publication is atomic, refuses to
overwrite an existing directory, writes `_SUCCESS` last, checks the exact
expected row count, and creates a self-contained `report.html` plus annual and
monthly headline tables.

Formal execution uses the same paths without `--plan-only`:

```powershell
python evaluation/run_evaluation.py `
  --spec config/evaluation_spec_v2.yml `
  --benchmark-daily benchmark_release_v1/spy_daily_raw_close.csv `
  --financing-rates data/reference/financing_rates_v1
```

Generated results under `evaluation/results/` are rebuildable and ignored.
The runner source and this readable guide belong in Git.
