# QQQ dividend series — provenance and cross-validation

File: `qqq_dividends_crossvalidated_20260804.csv` (columns `Date,Dividend`,
mirroring `spy_dividends_state_street_20260730.csv`).

## Why this file exists

The QQQ first-pass run (`experiments/qqq_first_pass_v1/`) used
`ignore_dividends=True` because no QQQ dividend series was in the workspace.
This file supplies one so the engine can dividend-adjust previous closes and
the benchmark can be QQQ total-return (not price-only).

## Sources (no single official feed covers the full 2007-04-25..2026-07-31
sample at full precision, so three are combined)

1. **Invesco (issuer) official product page** —
   `https://www.invesco.com/us/en/financial-products/etfs/invesco-qqq-trust-series-1.html`,
   "Yields & Distributions" table, captured 2026-08-04. Exact 5-decimal
   amounts for the 5 most-recent ex-dates (2025-06-23 .. 2026-06-22). Used as
   the issuer ground-truth cross-check. The page's "View since inception"
   control could not be expanded through the in-app browser (a persistent
   overlay intercepted pointer events and the React date-filter would not
   reload), so the issuer table could not be scraped in full.

2. **Nasdaq (listing exchange) official quote API** —
   `https://api.nasdaq.com/api/quote/QQQ/dividends?assetclass=etf`.
   5-decimal exact amounts, 2012-06-15 .. 2026-06-22. Note: this feed is
   **missing** the 2012-09-21 and 2012-12-21 distributions (jumps from
   2012-06-15 to 2013-03-15); those two are filled from source 3.

3. **Yahoo Finance chart API** —
   `https://query1.finance.yahoo.com/v8/finance/chart/QQQ?...&events=div`.
   Full coverage 2007-06-15 .. 2026-06-22 (81 ex-dates) but amounts rounded
   to 3 decimals. Used as the only source for 2007-06-15 .. 2012-03-15 (24
   rows) and as the fill for the two Nasdaq-missing 2012 dates.

## Cross-validation (all on the overlapping ex-dates)

- Yahoo vs Nasdaq, 2012-06-15 .. 2026-06-22: every Yahoo ex-date is present
  in Nasdaq (except the two Nasdaq-missing 2012 dates, where Yahoo is the
  source); amounts agree to 3 decimals (Yahoo rounds, Nasdaq is exact).
- Yahoo vs Invesco issuer, 2025-06-23 .. 2026-06-22: all 5 ex-dates match;
  amounts agree to 3 decimals.
- Nasdaq vs Invesco issuer, 2025-06-23 .. 2026-06-22: exact 5-decimal
  agreement on all 5 rows.

Conclusion: the three sources share one ex-date schedule; only precision
differs. Dates are authoritative across all three.

## Precision caveat

Rows sourced from Yahoo (24, all in 2007-06-15 .. 2012-03-15 plus the two
2012 Nasdaq gaps) carry 3-decimal amounts (±$0.0005). In that period QQQ
dividends were $0.03–0.16/share on a $30–60 price, so the rounding error is
negligible for both the total-return benchmark and the ex-date open-gap
adjustment. All 2012-06-15 onward rows are exact 5-decimal from the listing
exchange / issuer.

## Coverage vs the QQQ sample

Sample spans 2007-04-25 .. 2026-07-31. QQQ pays quarterly; the first in-sample
ex-date is 2007-06-15 (the prior ~2027-03-15 ex-date is before the sample
start). The last ex-date in the file is 2026-06-22; no ex-date falls between
2026-06-22 and the sample end 2026-07-31, so coverage is complete for the
sample.

## Build

`Date` = ex-date (YYYY-MM-DD, exchange calendar). `Dividend` = cash
distribution per share (USD). Built 2026-08-04 by
`experiments/qqq_with_dividends_v1/build_dividend_file.py` (embedded in the
session; the CSV itself is the artifact). Rows: 81.
