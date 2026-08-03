# Statistical uncertainty v1

Post-result addendum for the amended economic headline
`corrected_execution × halt_aware × with-dividends × $0.0025/share`.
It reads the immutable formal run; it does not change the frozen v2 spec or
rerun the strategy.

Run from the repository root:

```powershell
python experiments/statistical_uncertainty_v1/test_uncertainty.py
python experiments/statistical_uncertainty_v1/run_uncertainty.py
```

The primary uncertainty estimate is an 8,000-replication circular moving-block
bootstrap with 20-session blocks. Ordinary moving blocks and 1/5/10/20/40/60
session block lengths are sensitivity checks. The supplementary HAC estimate
uses a Bartlett/Newey-West long-run covariance of the Sharpe numerator and
denominator moments, with 20 lags primary and 0/5/10/20/40/60 lags reported.

`P(bootstrap draw <= 0)` is stored as a resampling diagnostic, not called a
p-value. The direct decline test uses a null-centered bootstrap distribution;
HAC additionally reports normal-approximation one- and two-sided p-values.

See `docs/STATISTICAL_UNCERTAINTY_V1_ZH.md` for the audited conclusion.
