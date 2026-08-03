# No-leverage sensitivity v1

This experiment compares the current 4x-capped economic display with a 1x-capped
version. "No leverage" means opening-price share sizing cannot exceed AUM:

- baseline: `min(2% / lagged 14-session daily volatility, 4)`;
- no leverage: `min(2% / lagged 14-session daily volatility, 1)`.

The 1x path may hold less than 100% notional when the volatility target calls
for it. Because `corrected_execution` fills later, actual fill and marked
notional can drift slightly above 1x; this experiment does not add dynamic
deleveraging. Signal, execution, tier, dividends, per-share costs,
point-in-time financing inputs and the continuous AUM path are otherwise
unchanged.

Run:

```powershell
python experiments/no_leverage_v1/run.py
```

Results are written atomically under `results/`. This is a post-result sizing
sensitivity and does not modify frozen v2 or its published headlines.
