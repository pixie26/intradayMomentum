"""QQQ rerun with the cross-validated dividend series.

Same strategy spec and the same 7 cells as qqq_first_pass_v1, but the data run
now carries `spy_dividends_clean.csv` (81 ex-dates, 2007-06-15..2026-06-22;
see data/reference/qqq_dividends_crossvalidated_20260804_PROVENANCE.md), so the
engine runs with ignore_dividends=False / require_dividends=True:

- previous closes are dividend-adjusted (prev_close_adj = prev_close - div);
- the benchmark is QQQ *total-return* buy&hold (price return + reinvested
  dividend at the ex-date close), not the price-only benchmark of v1.

Everything else matches v1 and is still disclosed vs the frozen SPY v2
evaluation: financing-rates-v2 (2007-04-25..2026-07-31, a pure superset of
the frozen v1; cash earns SOFR/LIBOR-proxy - 50bp, leveraged funding +100bp,
borrow 25bp) replaces the earlier "cash earns 0" assumption, legacy cost
model, post-publication cut at 2024-05-01, 4 L1-halt sessions excluded as
halt_anomaly.

This script never touches frozen SPY artifacts or the v1 no-dividend run; it
reads the new dividend-carrying data run and writes beside itself.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import im_engine_v4 as engine  # noqa: E402
from evaluation.run_evaluation import (  # noqa: E402
    Period, build_periods, load_financing_rates, performance_metrics,
    slice_result)

RUN_DIR = ROOT / "data" / "candidates" / "qqq_v1_observed" / "runs" / "20260804T143705Z"
OUT_DIR = Path(__file__).resolve().parent
EVAL_START = "2024-05-01"
FINANCING_RATES_CSV = (
    ROOT / "data" / "reference" / "financing_rates_v2" / "financing_rates_daily.csv")

CELLS = [
    ("corrected_execution", "paper_ready", 0.005,
     "comparable to the original SPY v1 headline economics"),
    ("corrected_execution", "paper_ready", 0.0025, "slippage sensitivity"),
    ("corrected_execution", "paper_ready", 0.001, "slippage sensitivity"),
    ("corrected_execution", "paper_ready", 0.010, "slippage sensitivity"),
    ("corrected_execution", "halt_aware", 0.0025,
     "comparable to the amended SPY halt_aware headline economics"),
    ("paper_spec", "paper_ready", 0.001, "paper's own fill/cost assumptions"),
    ("official_sample_compatible", "exploratory", 0.0,
     "authors' sample-code assumptions"),
]


def benchmark_total_returns(data: dict, cfg) -> pd.Series:
    """QQQ total-return buy&hold daily returns from the same minute file.

    Reinvests each ex-date dividend into the position at that session's valid
    close, so the benchmark = price return + reinvested dividend. NaN where a
    session has no valid close (never the last available bar). Anchored one
    session before the evaluation start, same rule as engine.benchmark()."""
    _d, daily = engine.build_features(data["bars"], data["vmin"], data["vsess"],
                                      cfg, data["dividends"])
    close_valid = daily["close_valid"].reindex(daily.index).fillna(False)
    px = daily["close"].where(close_valid)
    div = daily["dividend"].reindex(daily.index).fillna(0.0)
    # total-return factor: (1 + px.pct_change) * (1 + div/prev_close) on ex-dates
    prev_px = px.shift(1)
    price_ret = px.pct_change(fill_method=None)
    div_ret = (div / prev_px).where(prev_px.gt(0) & div.gt(0), 0.0)
    tr = (1 + price_ret) * (1 + div_ret) - 1
    return tr


def main() -> None:
    spec_like = {"evaluation_start": EVAL_START}
    data_start, data_end = "2007-04-25", "2026-07-31"
    periods = build_periods(spec_like, data_start, data_end)

    financing_rates = load_financing_rates(FINANCING_RATES_CSV)

    results = []
    returns_dir = OUT_DIR / "daily_returns"
    returns_dir.mkdir(exist_ok=True)

    for profile, tier, slip, note in CELLS:
        t0 = time.time()
        cfg = engine.profile_cfg(
            profile, tier=tier, slip_per_share=slip,
            ignore_dividends=False, require_dividends=True)
        data = engine.load_run(RUN_DIR, cfg)
        r = engine.backtest(data, cfg, financing_rates=financing_rates)
        cell_id = f"{profile}__{tier}__slip_{f'{slip:.4f}'.replace('.', 'p')}"
        r.to_csv(returns_dir / f"{cell_id}.csv")
        bench_ret = benchmark_total_returns(data, cfg)
        for period in periods:
            sliced = slice_result(r, period)
            m = performance_metrics(sliced, cfg.rf_annual)
            idx = sliced.index
            all_idx = bench_ret.index
            first_loc = int(all_idx.get_loc(idx.min()))
            anchor = all_idx[max(0, first_loc - 1):]
            b = bench_ret.reindex(anchor).reindex(idx)
            years = max((idx.max() - idx.min()).days / 365.2425, 1 / 365.2425)
            b_total = float((1 + b.dropna()).prod())
            m.update({
                "cell_id": cell_id, "profile": profile, "tier": tier,
                "slippage_per_share": slip, "subperiod": period.name,
                "note": note,
                "benchmark_total_return_cagr": b_total ** (1 / years) - 1,
                "benchmark_valid_sessions": int(b.notna().sum()),
                "benchmark_from_same_minute_file": True,
                "dividends_used": True,
            })
            results.append(m)
        print(f"[done] {cell_id} in {time.time() - t0:.0f}s", flush=True)

    out = {
        "experiment": "qqq_with_dividends_v1",
        "data_run_dir": str(RUN_DIR.relative_to(ROOT)),
        "evaluation_start": EVAL_START,
        "periods": [asdict(p) for p in periods],
        "dividends": {
            "source": "data/reference/qqq_dividends_crossvalidated_20260804.csv",
            "provenance": "data/reference/qqq_dividends_crossvalidated_20260804_PROVENANCE.md",
            "ex_dates": 81, "first": "2007-06-15", "last": "2026-06-22",
        },
        "disclosures": [
            "ignore_dividends=False; benchmark is QQQ total-return buy&hold",
            "financing-rates-v2 (2007-04-25..2026-07-31): cash SOFR/LIBOR-proxy - 50bp, funding +100bp, borrow 25bp; v2 is a pure superset of frozen v1",
            "legacy cost model; no Section 31 schedule",
            "extended-hours vendor rows excluded by the data pipeline",
            "1 invalid RTH row dropped (2008-03-11 14:21) under --invalid-row-policy drop",
            "4 L1-halt sessions (2020-03) excluded as halt_anomaly",
        ],
        "financing": {
            "release_id": "financing-rates-v2",
            "path": str(FINANCING_RATES_CSV.relative_to(ROOT)),
            "range": "2007-04-25..2026-07-31",
            "cash_spread_bps": -50.0,
            "funding_spread_bps": 100.0,
            "borrow_bps": 25.0,
        },
        "results": results,
    }
    (OUT_DIR / "results.json").write_text(
        json.dumps(out, indent=2, default=str), encoding="utf-8")

    df = pd.DataFrame(results)
    show = ["cell_id", "subperiod", "cagr", "sharpe_calendar", "max_drawdown",
            "active_sessions", "signal_count", "net_pnl",
            "benchmark_total_return_cagr", "first", "last"]
    print(df[show].to_string(index=False))


if __name__ == "__main__":
    main()
