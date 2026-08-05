"""Build an exploratory QQQ attribution report reusing the SPY generator.

This is a *derived, exploratory* artifact: it runs the intraday-momentum engine
on an exploratory QQQ data run and feeds the resulting daily ledger through the
audited, instrument-agnostic machinery in ``make_attribution_report`` (``ar``)
and ``run_evaluation`` (``runner``) - the exact code that produces the frozen
SPY attribution report.

QQQ uses financing-rates-v2 (2007-04-25..2026-07-31, a pure superset of the
frozen v1): cash earns SOFR/LIBOR-proxy - 50bp, leveraged funding +100bp,
borrow 25bp. The benchmark is QQQ total return computed from the SAME
minute file used by the backtest (a same-source benchmark, *not* an
independent daily close series) plus the crossvalidated cash dividends.
Both facts are disclosed in the report header and notes; the report is
labelled exploratory, not formal.

Run:
    python evaluation/make_qqq_attribution_report.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import im_engine_v4 as engine  # noqa: E402
from evaluation import make_attribution_report as ar  # noqa: E402
from evaluation import run_evaluation as runner  # noqa: E402

# Silence the engine's pct_change(fill_method) FutureWarning so the printed
# summary stays readable. Behaviour is unchanged.
warnings.filterwarnings("ignore", category=FutureWarning)


RUN_DIR = ROOT / "data" / "candidates" / "qqq_v1_observed" / "runs" / "20260804T143705Z"
DIVIDEND_CSV = ROOT / "data" / "reference" / "qqq_dividends_crossvalidated_20260804.csv"
OUTPUT_DIR = ROOT / "experiments" / "qqq_with_dividends_v1" / "attribution"
PUBLISH_HTML = ROOT / "experiments" / "qqq_with_dividends_v1" / "QQQ_ATTRIBUTION.html"
SOURCE_SUBDIR = "_source_run"
BENCHMARK_CSV_NAME = "qqq_benchmark_daily.csv"

EVALUATION_START = "2024-05-01"
FINANCING_RATES_CSV = (
    ROOT / "data" / "reference" / "financing_rates_v2" / "financing_rates_daily.csv")


def build_spec() -> dict:
    """Spec dict accepted by ar.headline_cell_id / headline_label_zh / prepare_daily."""
    return {
        "spec_version": "qqq_v1",
        "evaluation_start": EVALUATION_START,
        "headline": {
            "profile": "corrected_execution",
            "tier": "halt_aware",
            "dividend_mode": "with_dividends",
            "slippage_per_share": 0.0025,
        },
    }


def write_source_artifacts(source_dir: Path, cid: str, spec: dict,
                           result: pd.DataFrame,
                           ledger: dict[str, pd.DataFrame]) -> None:
    """Write the formal-run-style artifacts ar.prepare_daily expects to read.

    The decomposition is built from the in-memory ``result`` because
    ``runner.decomposition_rows`` reads ``result.attrs["daily_features"]`` (the
    ``dvol`` series) to form the frozen pre-publication volatility quintiles;
    that attr is stripped when the daily frame is written to parquet.
    """
    data_start = str(result.index.min().date())
    data_end = str(result.index.max().date())
    periods = runner.build_periods(spec, data_start, data_end)
    cell = runner.Cell(
        spec["headline"]["profile"], spec["headline"]["tier"],
        spec["headline"]["dividend_mode"],
        float(spec["headline"]["slippage_per_share"]))
    decomp_rows: list[dict] = []
    for period in periods:
        decomp_rows.extend(runner.decomposition_rows(cell, period, result, ledger))
    pd.DataFrame(decomp_rows).to_csv(source_dir / "decomposition.csv", index=False)

    daily_df = result.reset_index()
    daily_df.attrs = {}  # parquet cannot serialise DataFrame-valued attrs
    daily_df.insert(0, "cell_id", cid)
    daily_df.to_parquet(source_dir / "daily_results.parquet", index=False)

    for name in ("round_trips", "signals"):
        frame = ledger.get(name, pd.DataFrame()).copy()
        if not frame.empty:
            frame.insert(0, "cell_id", cid)
        frame.to_parquet(source_dir / f"{name}.parquet", index=False)


def build_benchmark_csv(source_dir: Path, data: dict, cfg) -> Path:
    """QQQ total-return benchmark from the SAME minute file (same-source).

    The close is taken where ``close_valid``; sessions with no valid close are
    dropped so total returns are computed between consecutive *valid* closes
    (a return therefore spans any internal data gap, e.g. the absent
    2007-06-01 session). The series is initialised at the first valid close:
    a one-day anchor equal to that close is prepended so the first session's
    return is 0 by convention (there is no prior QQQ level in the minute file).
    This is disclosed in the report notes; it is the same-source benchmark
    equivalent of the SPY report's independent pre-sample benchmark history.
    """
    _, daily_feat = engine.build_features(
        data["bars"], data["vmin"], data["vsess"], cfg, data["dividends"])
    close = daily_feat["close"].where(daily_feat["close_valid"]).dropna()
    first_date = close.index[0]
    anchor = pd.Series(
        [float(close.iloc[0])],
        index=pd.DatetimeIndex([first_date - pd.Timedelta(days=1)]))
    close = pd.concat([anchor, close])
    benchmark_path = source_dir / BENCHMARK_CSV_NAME
    pd.DataFrame({
        "session_date": close.index,
        "close": close.to_numpy(),
    }).to_csv(benchmark_path, index=False)
    return benchmark_path


def postprocess(report: str) -> str:
    """Swap SPY->QQQ labelling and mark the report as exploratory/derived."""
    out = report
    out = out.replace("SPY 日内动量", "QQQ 日内动量")
    out = out.replace(
        "正式主口径派生分析 · 仅报告点估计",
        "探索性派生分析（非正式口径） · 仅报告点估计")
    # Net-value label first, then the remaining standalone "SPY 总回报".
    out = out.replace("SPY 总回报净值", "QQQ 总回报净值")
    out = out.replace("SPY 总回报", "QQQ 总回报")
    out = out.replace("SPY 最差季度", "QQQ 最差季度")
    out = out.replace("SPY 最好季度", "QQQ 最好季度")
    out = out.replace("SPY 借券成本", "QQQ 借券成本")
    out = out.replace(
        "独立日频 SPY 未调整收盘价加 State Street 现金分红",
        "同分钟文件 QQQ 收盘价加交叉验证现金分红（非独立日频源）")
    out = out.replace("2026-07-09", "2026-07-31")
    # Header meta: correct the "frozen formal run" claim and add the QQQ
    # exploratory / financing-v2 / same-source-benchmark disclosure.
    out = out.replace(
        "本报告由冻结正式运行的逐日账本派生，不改变策略、正式经济评价或原始 v2 报告。"
        "事件名称是描述性标签，不构成单一因果判断。",
        "本报告由一次探索性 QQQ 运行的逐日账本派生（非冻结正式评价）；不改变策略或原始运行。"
        "事件名称是描述性标签，不构成单一因果判断。"
        "<br><b>QQQ 探索性披露：</b>本报告派生自一次探索性 QQQ 运行（非冻结正式评价），"
        "融资采用 financing-rates-v2（2007-04-25..2026-07-31，冻结 v1 的纯超集：现金 SOFR/LIBOR 代理减 50bp、"
        "杠杆融资加 100bp、借券 25bp），基准为同分钟文件 QQQ 总回报"
        "（非独立日频源），因此相关性与基准对比不等同于 SPY 正式报告的独立基准口径。")
    # Cash-comparison note describes the LIBOR/SOFR-50bp carry. QQQ now uses
    # financing-rates-v2 with the same -50bp cash spread, so the original note
    # is accurate and needs no correction.
    return out


def main() -> int:
    spec = build_spec()
    cid = runner.headline_cell_id(spec)

    cfg = engine.profile_cfg(
        "corrected_execution", tier="halt_aware", slip_per_share=0.0025,
        ignore_dividends=False, require_dividends=True,
        cash_rate_annual=0.0, funding_rate_annual=0.0, borrow_rate_annual=0.0)
    data = engine.load_run(RUN_DIR, cfg)
    financing_rates = runner.load_financing_rates(FINANCING_RATES_CSV)
    result = engine.backtest(
        data, cfg, collect_ledger=True, financing_rates=financing_rates)
    ledger = result.attrs["ledger"]

    data_manifest = json.loads(
        (RUN_DIR / "reports" / "data_manifest.json").read_text(encoding="utf-8"))

    staging = Path(tempfile.mkdtemp(prefix="qqq_attr_src_"))
    try:
        write_source_artifacts(staging, cid, spec, result, ledger)
        benchmark_csv = build_benchmark_csv(staging, data, cfg)

        daily, checks = ar.prepare_daily(staging, spec, benchmark_csv, DIVIDEND_CSV)
        daily, trips = ar.load_headline_ledgers(staging, spec, daily)
        decomposition = ar.load_headline_decomposition(staging, spec)

        evaluation_start = pd.Timestamp(spec["evaluation_start"])
        pre_metrics = ar.reporting_metrics(daily[daily.index < evaluation_start])
        post_metrics = ar.reporting_metrics(daily[daily.index >= evaluation_start])
        checks.update({f"pre_{k}": v for k, v in pre_metrics.items()})
        checks.update({f"post_{k}": v for k, v in post_metrics.items()})

        close = runner.load_daily_benchmark(benchmark_csv)
        dividends = ar.load_dividends(DIVIDEND_CSV)
        benchmark = ar.full_benchmark(
            close, dividends, daily.index.min(), daily.index.max())
        quarters, annual = ar.period_tables(daily, close, dividends)

        manifest = {
            "run_id": RUN_DIR.name,
            "classification": "exploratory_derived_attribution",
            "spec_sha256": hashlib.sha256(
                json.dumps(spec, sort_keys=True).encode()).hexdigest(),
            "git": runner.git_state(),
            "data_release": {
                "run_dir": str(RUN_DIR),
                "run_id": data_manifest.get("run_id"),
                "expected_start": data_manifest.get("expected_start"),
                "expected_end": data_manifest.get("expected_end"),
                "observed_start": data_manifest.get("observed_start"),
                "observed_end": data_manifest.get("observed_end"),
            },
            "benchmark": {
                # Records the final persisted path (written after publish).
                "path": str(OUTPUT_DIR / SOURCE_SUBDIR / BENCHMARK_CSV_NAME),
                "sha256": ar.sha256(benchmark_csv),
                "same_source_minute_file": True,
                "independent": False,
            },
            "financing_rates": {
                "release_id": "financing-rates-v2",
                "path": str(FINANCING_RATES_CSV.relative_to(ROOT)),
                "sha256": ar.sha256(FINANCING_RATES_CSV),
                "range": "2007-04-25..2026-07-31",
                "cash_spread_bps": -50.0,
                "funding_spread_bps": 100.0,
                "borrow_bps": 25.0,
            },
        }

        report = ar.render_html(
            manifest, spec, daily, trips, decomposition, quarters, annual,
            benchmark, checks)
        report_qqq = postprocess(report)

        if OUTPUT_DIR.exists():
            shutil.rmtree(OUTPUT_DIR)
        report_path = ar.publish(
            OUTPUT_DIR, PUBLISH_HTML, report_qqq, daily, trips, decomposition,
            quarters, annual, manifest, checks)
        # Persist the source artifacts (incl. the benchmark CSV) inside the
        # output dir so the provenance path recorded above resolves.
        shutil.copytree(staging, OUTPUT_DIR / SOURCE_SUBDIR)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    print(f"written: {report_path}")
    print(f"publish_html: {PUBLISH_HTML} ({PUBLISH_HTML.stat().st_size} bytes)")
    print(json.dumps({
        "headline_cell": cid,
        "sessions": len(daily),
        "complete_quarters": int(quarters["complete"].sum()),
        "pre_portfolio_cagr": pre_metrics["portfolio_cagr"],
        "post_portfolio_cagr": post_metrics["portfolio_cagr"],
        "pre_trading_only_cagr": pre_metrics["trading_only_cagr"],
        "post_trading_only_cagr": post_metrics["trading_only_cagr"],
        "pre_cash_interest_annualized": pre_metrics["cash_interest_annualized"],
        "post_cash_interest_annualized": post_metrics["cash_interest_annualized"],
        "max_abs_net_identity_residual": checks["max_abs_net_identity_residual"],
        "max_abs_return_identity_residual": checks["max_abs_return_identity_residual"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
