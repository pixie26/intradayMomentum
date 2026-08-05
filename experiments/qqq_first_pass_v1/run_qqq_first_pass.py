"""QQQ first-pass backtest: run the frozen strategy spec on the new QQQ data.

Data input: data/candidates/qqq_v1_observed/runs/20260804T013228Z (prepared by
prepare_spy_data.py from QQQ_1min_20260731.csv; XNAS calendar; extended-hours
rows excluded by the pipeline; 1 invalid RTH row dropped under
--invalid-row-policy drop; no dividend file).

Scope disclosures (vs the frozen SPY v2 evaluation):
- ignore_dividends=True: no QQQ dividend series in the workspace yet, so the
  benchmark is QQQ *price* buy&hold, not total return. The strategy itself is
  flat overnight, so dividends only touch the benchmark and ex-date opens.
- No financing release: financing-rates-v1 covers 2008-01-22..2026-07-09, the
  QQQ run spans 2007-04-25..2026-07-31, so the point-in-time curve cannot
  cover the full range. Cash earns 0, like the original v1 headline economics.
- Legacy cost model (commission $0.0035/share + per-share slippage), no
  Section 31 schedule.
- Everything else follows the frozen spec: continuous full run then slice
  returns (full_sample / pre_publication / post_publication at 2024-05-01),
  per-tier metric sets, never mixed across tiers.

This script never touches frozen SPY artifacts; outputs land beside it.
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
    Period, build_periods, performance_metrics, slice_result)

RUN_DIR = ROOT / "data" / "candidates" / "qqq_v1_observed" / "runs" / "20260804T013228Z"
OUT_DIR = Path(__file__).resolve().parent
EVAL_START = "2024-05-01"  # frozen post-publication cut, same as spec v2

CELLS = [
    # (profile, tier, slippage_per_share, note)
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


def benchmark_price_returns(data: dict, cfg) -> pd.Series:
    """QQQ price buy&hold daily returns from the same minute file, NaN where a
    session has no valid close (never the last available bar). Same anchoring
    rule as engine.benchmark(): the caller slices from one session before the
    evaluation start. No dividends: no QQQ series in the workspace yet."""
    _d, daily = engine.build_features(data["bars"], data["vmin"], data["vsess"],
                                      cfg, data["dividends"])
    close_valid = daily["close_valid"].reindex(daily.index).fillna(False)
    px = daily["close"].where(close_valid)
    return px.pct_change(fill_method=None)


def main() -> None:
    spec_like = {"evaluation_start": EVAL_START}
    data_start, data_end = "2007-04-25", "2026-07-31"
    periods = build_periods(spec_like, data_start, data_end)

    results = []
    returns_dir = OUT_DIR / "daily_returns"
    returns_dir.mkdir(exist_ok=True)

    for profile, tier, slip, note in CELLS:
        t0 = time.time()
        cfg = engine.profile_cfg(
            profile, tier=tier, slip_per_share=slip,
            ignore_dividends=True, require_dividends=False)
        data = engine.load_run(RUN_DIR, cfg)
        r = engine.backtest(data, cfg)
        cell_id = f"{profile}__{tier}__slip_{f'{slip:.4f}'.replace('.', 'p')}"
        r.to_csv(returns_dir / f"{cell_id}.csv")
        bench_ret = benchmark_price_returns(data, cfg)
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
                "benchmark_price_cagr": b_total ** (1 / years) - 1,
                "benchmark_valid_sessions": int(b.notna().sum()),
                "benchmark_from_same_minute_file": True,
                "dividends_used": False,
            })
            results.append(m)
        print(f"[done] {cell_id} in {time.time() - t0:.0f}s", flush=True)

    out = {
        "experiment": "qqq_first_pass_v1",
        "data_run_dir": str(RUN_DIR.relative_to(ROOT)),
        "evaluation_start": EVAL_START,
        "periods": [asdict(p) for p in periods],
        "disclosures": [
            "ignore_dividends=True; benchmark is QQQ price buy&hold, not total return",
            "no financing release coverage for 2007-04-25..2026-07-31; cash earns 0",
            "legacy cost model; no Section 31 schedule",
            "extended-hours vendor rows excluded by the data pipeline",
            "1 invalid RTH row dropped (2008-03-11 14:21) under --invalid-row-policy drop",
            "4 L1-halt sessions (2020-03) carry 1 stray in-halt print each -> halt_anomaly, excluded from paper_ready/halt_aware",
        ],
        "results": results,
    }
    (OUT_DIR / "results.json").write_text(
        json.dumps(out, indent=2, default=str), encoding="utf-8")

    df = pd.DataFrame(results)
    show = ["cell_id", "subperiod", "cagr", "sharpe_calendar", "max_drawdown",
            "active_sessions", "signal_count", "net_pnl", "benchmark_price_cagr",
            "first", "last"]
    print(df[show].to_string(index=False))


if __name__ == "__main__":
    main()
