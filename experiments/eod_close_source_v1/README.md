# EOD close-source experiment v1

This is a post-result execution sensitivity experiment. It does not modify the
frozen v2 specifications or their published results.

The primary comparison holds the signal, intraday fills, tier, dividends,
financing curve and base `$0.0025/share` slippage fixed, and changes only the
end-of-session flatten price:

- final scheduled minute close (15:59 on a full session);
- independent Yahoo raw daily close, both before and after an incremental
  0.5 bp / 1 bp auction-cost assumption;
- equal-share TWAPs over the final 5, 10, 15 and 30 scheduled minute closes
  (10 minutes is the primary display, the others are robustness checks).

Run:

```powershell
python experiments/eod_close_source_v1/run.py
```

Rebuildable CSV/parquet outputs go under `results/` and are ignored. The runner
also refreshes the readable tracked reports in `docs/`.

The independent daily close is a cross-source close proxy. It is not evidence
that a real MOC order would fill at that exact value. The TWAP is an equivalent
average-price sensitivity and does not model queue position, partial fills,
participation limits or market impact. It also retains the baseline session-end
financing integral rather than pretending the equal slices are fully modelled
child fills.
