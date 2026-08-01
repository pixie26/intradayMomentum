# No-leverage-cap sensitivity v1

This is a post-result sizing sensitivity experiment. It does not modify the
frozen v2 specification or either published headline.

The comparison fixes the current primary economic display:

- `corrected_execution`;
- `halt_aware`;
- with dividends;
- `$0.0025/share` slippage;
- the frozen point-in-time cash, funding and borrow-rate curve;
- a continuous AUM path from 2008-01-22 through 2026-07-09.

Only the sizing formula changes:

- baseline: `min(2% / lagged 14-session daily volatility, 4)`;
- uncapped: `2% / lagged 14-session daily volatility`.

Run:

```powershell
python experiments/no_leverage_cap_v1/run.py
```

Rebuildable results are written atomically under `results/`. The runner checks
the baseline against the existing formal 72-cell run, verifies the accounting
identities, and records both portfolio and same-path trading-only CAGR. Market
impact, participation limits, margin rules, forced liquidation and partial
fills remain unmodelled, so the uncapped case is a mechanical stress test, not
a tradable recommendation.
