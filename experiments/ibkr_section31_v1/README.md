# Historical Section 31 cost sensitivity v1

This is a post-result transaction-cost sensitivity. It does not modify either
frozen evaluation spec or the published frozen/amended headline.

The experiment holds signal, execution prices, tier, dividends, financing,
borrow and `$0.0025/share` slippage fixed. It compares:

- the frozen legacy explicit-cost convention (`$0.0035/share`, `$0.35/order`);
- the same convention plus the SEC Section 31 rate effective on each sell date.

Section 31 applies to sell notional, including a short-sale opening fill and
the sell side of a long-to-short reversal. It does not apply to a short-cover
buy. The schedule is `data/reference/sec_section31_rates.csv` and is validated
for continuous calendar-date coverage, rate arithmetic and SEC source URLs.

Run:

```powershell
python experiments/ibkr_section31_v1/run.py
```

Rebuildable outputs under `results/` are ignored. The runner refuses to
overwrite an existing run and records source/config/data/engine hashes.

This is deliberately not called an all-in IBKR model. Historical TAF, CAT,
clearing, pass-through, venue fees/rebates, customer-statement rounding,
monthly commission tiers and market impact remain unmodelled until their
history or an explicit scenario proxy is frozen.
