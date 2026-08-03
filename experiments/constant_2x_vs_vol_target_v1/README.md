# Constant 2x versus paper volatility targeting v1

This post-result sizing sensitivity compares:

- paper-style sizing: `min(2% / lagged 14-session daily volatility, 4)`;
- constant sizing: `2 × AUM / session open`.

Everything else is fixed at the current primary economic display
(`corrected_execution × halt_aware × with-dividends × $0.0025/share`) with the
point-in-time financing curve and a continuous AUM path.

The primary constant-2x path inherits the paper rule's dvol eligibility, so the
comparison changes only the leverage value. A secondary operational path runs
constant 2x without requiring dvol and discloses the small set of extra trading
sessions. The report includes performance, actual exposure, financing costs,
annual stability, and a relative-return decomposition for sessions where the
paper rule calls for `<2x`, `2x–<4x`, or the `4x cap`.

Run:

```powershell
python experiments/constant_2x_vs_vol_target_v1/run.py
```

Outputs are rebuildable and published atomically under `results/`. Frozen v2
and its published headline are not modified.
