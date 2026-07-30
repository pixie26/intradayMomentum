"""Reproduce the paper's Q24 monthly-return appendix as an isolated experiment."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import im_engine_v4 as engine  # noqa: E402


MONTHS = [
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
]
INCOMPLETE_STATUSES = {
    "warmup_no_prev_close",
    "warmup_no_band",
    "absent_session",
    "unknown_exit",
    "after_unknown_exit",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_meta() -> dict:
    def run(*args: str) -> str | None:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None

    commit = run("rev-parse", "HEAD")
    status = run("status", "--porcelain")
    return {"commit": commit, "dirty": bool(status) if status is not None else None}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "experiments" / "paper_replication_v1" / "config.yml",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def resolve_repo_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def load_reference(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    wide = pd.read_csv(path)
    required = {"year", *MONTHS, "yearly"}
    missing = sorted(required - set(wide.columns))
    if missing:
        raise ValueError(f"Reference table lacks columns: {missing}")
    if wide["year"].duplicated().any():
        raise ValueError("Reference table contains duplicate years.")

    monthly_rows = []
    yearly_rows = []
    for row in wide.itertuples(index=False):
        reported_months = []
        for month, name in enumerate(MONTHS, 1):
            value = getattr(row, name)
            if pd.notna(value):
                monthly_rows.append({
                    "year": int(row.year),
                    "month": month,
                    "paper_return_pct": float(value),
                })
                reported_months.append(month)
        yearly_rows.append({
            "year": int(row.year),
            "last_reported_month": max(reported_months),
            "paper_yearly_return_pct": float(row.yearly),
        })
    return pd.DataFrame(monthly_rows), pd.DataFrame(yearly_rows)


def validate_inputs(config_path: Path) -> tuple[dict, dict]:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    paper_path = resolve_repo_path(config["paper"]["path"])
    reference_path = resolve_repo_path(config["paper"]["reference_table"])
    bundle_path = resolve_repo_path(config["data"]["bundle"])

    actual_paper_sha = sha256_file(paper_path)
    if actual_paper_sha != config["paper"]["sha256"]:
        raise ValueError(
            f"Paper hash mismatch: expected {config['paper']['sha256']}, "
            f"got {actual_paper_sha}.")
    release_manifest_path = bundle_path / "data_manifest.json"
    success_path = bundle_path / "_SUCCESS"
    if not release_manifest_path.exists() or not success_path.exists():
        raise ValueError(f"{bundle_path} is not a published immutable release.")
    release_manifest = json.loads(
        release_manifest_path.read_text(encoding="utf-8"))
    if release_manifest.get("release_id") != config["data"]["required_release_id"]:
        raise ValueError(
            f"Expected release {config['data']['required_release_id']}, "
            f"got {release_manifest.get('release_id')}.")
    success = json.loads(success_path.read_text(encoding="utf-8"))
    release_sha = sha256_file(release_manifest_path)
    if success.get("data_manifest_sha256") != release_sha:
        raise ValueError("Data release manifest does not match _SUCCESS.")

    monthly_ref, yearly_ref = load_reference(reference_path)
    if len(monthly_ref) != 220:
        raise ValueError(
            f"Q24 reference should contain 220 reported months, got "
            f"{len(monthly_ref)}.")
    context = {
        "paper_path": paper_path,
        "reference_path": reference_path,
        "bundle_path": bundle_path,
        "release_manifest_path": release_manifest_path,
        "release_manifest": release_manifest,
        "release_manifest_sha256": release_sha,
        "monthly_ref": monthly_ref,
        "yearly_ref": yearly_ref,
    }
    return config, context


def compound_returns(values: pd.Series) -> float:
    if values.empty:
        return np.nan
    return float(((1.0 + values.fillna(0.0)).prod() - 1.0) * 100.0)


def strategy_performance(result: pd.DataFrame, cfg: engine.Cfg) -> dict:
    """Paper-style strategy statistics on the calendarised evaluation series."""
    values = result.loc[result["is_evaluation"], "ret"].fillna(0.0)
    metrics = engine.stats(result, cfg.rf_annual)
    if len(values) and float(values.std()) > 0:
        metrics["Sharpe_calendar"] = round(float(
            (values.mean() - cfg.rf_annual / 252.0)
            / values.std() * np.sqrt(252.0)), 2)
    return metrics


def full_benchmark_total_returns(
    data: dict,
    cfg: engine.Cfg,
) -> pd.Series:
    """SPY close-to-close total returns on the full validated calendar."""
    _, daily = engine.build_features(
        data["bars"], data["vmin"], data["vsess"], cfg, data["dividends"])
    close_valid = daily["close_valid"].reindex(daily.index).fillna(False)
    prices = daily["close"].where(close_valid)
    dividends = daily["dividend"].reindex(daily.index).fillna(0.0)
    return (prices + dividends) / prices.shift(1) - 1.0


def performance_and_curve(
    data: dict,
    cfg: engine.Cfg,
    result: pd.DataFrame,
    benchmark_total_returns: pd.Series,
    profile: str,
    tier: str,
    dividend_mode: str,
) -> tuple[dict, pd.DataFrame]:
    strategy_metrics = strategy_performance(result, cfg)
    benchmark_metrics = engine.benchmark(data, cfg, result)
    evaluation = result.loc[result["is_evaluation"]].copy()
    strategy_returns = evaluation["ret"].fillna(0.0)
    benchmark_returns = benchmark_total_returns.reindex(evaluation.index)
    benchmark_valid = benchmark_returns.dropna()
    benchmark_equity = (1.0 + benchmark_returns).cumprod()
    benchmark_drawdown = (
        benchmark_equity / benchmark_equity.cummax() - 1.0)
    benchmark_nonzero = benchmark_valid.loc[benchmark_valid.ne(0.0)]

    performance = {
        "profile": profile,
        "tier": tier,
        "dividend_mode": dividend_mode,
        **{f"strategy_{key}": value
           for key, value in strategy_metrics.items()},
        **benchmark_metrics,
        "spy_total_TotRet%": (
            float(((1.0 + benchmark_valid).prod() - 1.0) * 100.0)
            if len(benchmark_valid) else np.nan),
        "spy_total_MDD%": (
            float(benchmark_drawdown.min() * 100.0)
            if benchmark_drawdown.notna().any() else np.nan),
        "spy_total_Hit%": (
            float(benchmark_nonzero.gt(0.0).mean() * 100.0)
            if len(benchmark_nonzero) else np.nan),
    }

    daily_curve = pd.DataFrame({
        "strategy_equity": (1.0 + strategy_returns).cumprod(),
        "spy_equity": benchmark_equity,
    })
    daily_curve["year"] = daily_curve.index.year
    daily_curve["month"] = daily_curve.index.month
    monthly_curve = (
        daily_curve.groupby(["year", "month"], sort=True)
        [["strategy_equity", "spy_equity"]]
        .last()
        .reset_index()
    )
    monthly_curve.insert(0, "dividend_mode", dividend_mode)
    monthly_curve.insert(0, "tier", tier)
    monthly_curve.insert(0, "profile", profile)
    monthly_curve["date"] = pd.to_datetime({
        "year": monthly_curve["year"],
        "month": monthly_curve["month"],
        "day": 1,
    }).dt.to_period("M").dt.to_timestamp("M").dt.strftime("%Y-%m-%d")
    return performance, monthly_curve


def compare_one(
    data: dict,
    profile: str,
    tier: str,
    dividend_mode: str,
    benchmark_total_returns: pd.Series,
    monthly_ref: pd.DataFrame,
    yearly_ref: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, dict, pd.DataFrame]:
    cfg = engine.profile_cfg(
        profile,
        tier=tier,
        ignore_dividends=dividend_mode == "ignore_dividends",
    )
    result = engine.backtest(data, cfg).copy()
    result.index = pd.DatetimeIndex(result.index)
    result["year"] = result.index.year
    result["month"] = result.index.month

    monthly_actual = (
        result.loc[result["is_evaluation"]]
        .groupby(["year", "month"])["ret"]
        .apply(compound_returns)
    )
    status_by_month = (
        result.groupby(["year", "month"])["status"]
        .agg(lambda values: sorted(set(values) & INCOMPLETE_STATUSES))
    )

    monthly_rows = []
    for ref in monthly_ref.itertuples(index=False):
        key = (ref.year, ref.month)
        actual = monthly_actual.get(key, np.nan)
        bad_statuses = status_by_month.get(key, None)
        if key not in status_by_month.index:
            comparable = False
            reason = "no_source_data"
        elif bad_statuses:
            comparable = False
            reason = "incomplete_engine_month:" + "|".join(bad_statuses)
        elif not np.isfinite(actual):
            comparable = False
            reason = "no_evaluation_returns"
        else:
            comparable = True
            reason = ""
        diff = actual - ref.paper_return_pct if np.isfinite(actual) else np.nan
        monthly_rows.append({
            "profile": profile,
            "tier": tier,
            "dividend_mode": dividend_mode,
            "year": int(ref.year),
            "month": int(ref.month),
            "paper_return_pct": ref.paper_return_pct,
            "replication_return_pct": actual,
            "difference_pp": diff,
            "absolute_difference_pp": abs(diff) if np.isfinite(diff) else np.nan,
            "available": bool(np.isfinite(actual)),
            "comparable": comparable,
            "exclusion_reason": reason,
        })
    monthly = pd.DataFrame(monthly_rows)

    yearly_rows = []
    for ref in yearly_ref.itertuples(index=False):
        ref_months = monthly_ref.loc[
            monthly_ref["year"].eq(ref.year), "month"].tolist()
        detail = monthly.loc[monthly["year"].eq(ref.year)]
        ev = result.loc[
            result["year"].eq(ref.year)
            & result["month"].isin(ref_months)
            & result["is_evaluation"],
            "ret",
        ]
        actual = compound_returns(ev)
        comparable = bool(len(detail) == len(ref_months)
                          and detail["comparable"].all())
        diff = actual - ref.paper_yearly_return_pct if np.isfinite(actual) else np.nan
        yearly_rows.append({
            "profile": profile,
            "tier": tier,
            "dividend_mode": dividend_mode,
            "year": int(ref.year),
            "last_reported_month": int(ref.last_reported_month),
            "paper_yearly_return_pct": ref.paper_yearly_return_pct,
            "replication_yearly_return_pct": actual,
            "difference_pp": diff,
            "absolute_difference_pp": abs(diff) if np.isfinite(diff) else np.nan,
            "comparable": comparable,
            "exclusion_reason": "" if comparable else "one_or_more_months_incomplete",
        })
    performance, curve = performance_and_curve(
        data,
        cfg,
        result,
        benchmark_total_returns,
        profile,
        tier,
        dividend_mode,
    )
    return monthly, pd.DataFrame(yearly_rows), performance, curve


def summarize(
    monthly: pd.DataFrame,
    yearly: pd.DataFrame,
    config: dict,
) -> pd.DataFrame:
    rows = []
    thresholds = config["comparison"]["absolute_difference_thresholds_pp"]
    primary = (
        config["matrix"]["primary_profile"],
        config["matrix"]["primary_tier"],
        config["matrix"]["primary_dividend_mode"],
    )
    group_cols = ["profile", "tier", "dividend_mode"]
    for (profile, tier, dividend_mode), frame in monthly.groupby(
            group_cols, sort=False):
        available = frame.loc[frame["available"]].copy()
        eligible = frame.loc[frame["comparable"]].copy()
        annual = yearly.loc[
            yearly["profile"].eq(profile)
            & yearly["tier"].eq(tier)
            & yearly["dividend_mode"].eq(dividend_mode)
            & yearly["comparable"]
        ]
        available_abs_diff = available["absolute_difference_pp"]
        abs_diff = eligible["absolute_difference_pp"]
        annual_abs = annual["absolute_difference_pp"]
        row = {
            "profile": profile,
            "tier": tier,
            "dividend_mode": dividend_mode,
            "is_primary": (profile, tier, dividend_mode) == primary,
            "available_months": int(len(available)),
            "monthly_mae_all_available_pp": available_abs_diff.mean(),
            "comparable_months": int(len(eligible)),
            "monthly_mae_pp": abs_diff.mean(),
            "monthly_median_abs_error_pp": abs_diff.median(),
            "monthly_max_abs_error_pp": abs_diff.max(),
            "comparable_years": int(len(annual)),
            "yearly_mae_pp": annual_abs.mean(),
        }
        for threshold in thresholds:
            label = str(threshold).replace(".", "_")
            row[f"available_months_within_{label}pp_pct"] = (
                float(available_abs_diff.le(threshold).mean() * 100.0)
                if len(available_abs_diff) else np.nan
            )
            row[f"months_within_{label}pp_pct"] = (
                float(abs_diff.le(threshold).mean() * 100.0)
                if len(abs_diff) else np.nan
            )
        rows.append(row)
    return pd.DataFrame(rows).sort_values(
        ["monthly_mae_pp", "yearly_mae_pp"], na_position="last")


def fmt(value: object, decimals: int = 2) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, (float, np.floating)):
        return f"{value:.{decimals}f}"
    return html.escape(str(value))


def table_html(frame: pd.DataFrame, decimals: int = 2) -> str:
    headers = "".join(f"<th>{html.escape(str(c))}</th>" for c in frame.columns)
    rows = []
    for row in frame.itertuples(index=False, name=None):
        cells = "".join(f"<td>{fmt(value, decimals)}</td>" for value in row)
        rows.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def render_report(
    config: dict,
    context: dict,
    monthly: pd.DataFrame,
    yearly: pd.DataFrame,
    summary: pd.DataFrame,
    performance: pd.DataFrame,
    curves: pd.DataFrame,
    provenance: dict,
) -> str:
    def records_json(frame: pd.DataFrame) -> str:
        records = json.loads(frame.to_json(orient="records", double_precision=10))
        return json.dumps(records, ensure_ascii=False, separators=(",", ":"))

    summary_view = summary[[
        "profile", "tier", "dividend_mode", "available_months",
        "monthly_mae_all_available_pp", "comparable_months",
        "monthly_mae_pp", "months_within_0_5pp_pct",
        "months_within_1_0pp_pct", "comparable_years", "yearly_mae_pp",
    ]].copy()
    numeric = summary_view.select_dtypes(include=[np.number]).columns
    summary_view[numeric] = summary_view[numeric].round(3)
    summary_view = summary_view.rename(columns={
        "monthly_mae_all_available_pp": "all_available_MAE_pp",
        "monthly_mae_pp": "comparable_MAE_pp",
        "months_within_0_5pp_pct": "within_0.5pp_%",
        "months_within_1_0pp_pct": "within_1.0pp_%",
        "yearly_mae_pp": "yearly_MAE_pp",
    })

    template = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SPY Intraday Momentum - 论文 Q24 详细复现</title>
<style>
:root { --ink:#17212b; --muted:#617080; --navy:#123a5a; --blue:#1875c1;
  --red:#c7423b; --green:#27835a; --amber:#a96d00; --line:#d9e1e8;
  --paper:#f5f7f9; --card:#fff; }
* { box-sizing:border-box; }
body { margin:0; color:var(--ink); background:#eef2f5;
  font:15px/1.5 Inter,Segoe UI,system-ui,sans-serif; }
header { background:linear-gradient(135deg,#0c2e49,#195d8e); color:#fff;
  padding:34px max(24px,calc((100vw - 1420px)/2)); }
header h1 { margin:0 0 8px; font-size:30px; }
header p { margin:0; color:#dcecf7; max-width:1000px; }
main { max-width:1420px; margin:0 auto; padding:24px; }
h2 { color:var(--navy); margin:34px 0 12px; }
h3 { margin:20px 0 8px; color:#25475f; }
.notice { background:#fff4cf; border-left:5px solid #d59b00;
  padding:12px 16px; margin-bottom:18px; }
.controls { position:sticky; top:0; z-index:5; display:grid;
  grid-template-columns:repeat(4,minmax(160px,1fr)); gap:12px;
  background:rgba(255,255,255,.96); border:1px solid var(--line);
  border-radius:10px; padding:14px; box-shadow:0 5px 18px #173a5520; }
label { color:var(--muted); font-size:12px; font-weight:700; }
select { display:block; width:100%; margin-top:4px; padding:8px 10px;
  border:1px solid #b9c6d0; border-radius:6px; background:#fff; color:var(--ink); }
.cards { display:grid; grid-template-columns:repeat(6,minmax(135px,1fr));
  gap:12px; margin:18px 0; }
.card { background:var(--card); border:1px solid var(--line); border-radius:9px;
  padding:14px; box-shadow:0 2px 7px #173a5510; }
.card .label { color:var(--muted); font-size:12px; }
.card .value { font-size:23px; font-weight:750; color:var(--navy); margin-top:3px; }
.panel { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:18px; margin:14px 0; overflow:hidden; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
.table-wrap { overflow:auto; max-height:620px; border:1px solid var(--line); }
table { border-collapse:collapse; width:100%; font-size:13px; background:#fff; }
th,td { border-bottom:1px solid var(--line); padding:7px 9px; text-align:right;
  white-space:nowrap; }
th { position:sticky; top:0; background:#e9f0f5; color:#264a64; z-index:1; }
th:first-child,td:first-child { text-align:left; }
tr:hover td { background:#f4f8fb; }
.good { color:var(--green); font-weight:700; }
.warn { color:var(--amber); font-weight:700; }
.bad { color:var(--red); font-weight:700; }
.muted { color:var(--muted); }
.legend { display:flex; gap:18px; flex-wrap:wrap; color:var(--muted); font-size:13px; }
.swatch { display:inline-block; width:12px; height:3px; vertical-align:middle;
  margin-right:5px; }
svg { width:100%; height:auto; display:block; background:#fff; }
.footnote { color:var(--muted); font-size:12px; }
details { background:#fff; border:1px solid var(--line); border-radius:8px;
  padding:10px 14px; margin-top:18px; }
pre { overflow:auto; max-height:360px; font-size:11px; }
code { background:#edf2f5; padding:2px 5px; border-radius:4px; }
@media(max-width:950px) {
  .controls,.cards,.grid2 { grid-template-columns:1fr 1fr; }
}
@media(max-width:600px) {
  main { padding:12px; } .controls,.cards,.grid2 { grid-template-columns:1fr; }
  header { padding:24px 16px; } header h1 { font-size:24px; }
}
</style>
</head>
<body>
<header>
  <h1>SPY Intraday Momentum - 论文 Q24 详细复现</h1>
  <p>18 个组合的逐月论文对照、完整 performance 和同区间 SPY benchmark。
  论文版本：__PAPER_REVISION__；数据：data-v1.0。</p>
</header>
<main>
<div class="notice"><strong>独立复现实验。</strong>
本报告不消费、不修改、也不替代冻结的正式经济评价。论文 performance
样本与本地样本区间不同，表中明确列出日期，不能把两者当作完全同区间比较。</div>

<div class="controls">
  <label>Profile<select id="profile"></select></label>
  <label>Tier<select id="tier"></select></label>
  <label>Dividend mode<select id="dividend"></select></label>
  <label>月度表年份<select id="year"></select></label>
</div>

<div class="cards">
  <div class="card"><div class="label">可比月份</div><div class="value" id="cMonths"></div></div>
  <div class="card"><div class="label">月度 MAE</div><div class="value" id="cMae"></div></div>
  <div class="card"><div class="label">误差 ≤ 0.5pp</div><div class="value" id="c05"></div></div>
  <div class="card"><div class="label">策略 CAGR</div><div class="value" id="cCagr"></div></div>
  <div class="card"><div class="label">策略 Sharpe</div><div class="value" id="cSharpe"></div></div>
  <div class="card"><div class="label">SPY CAGR</div><div class="value" id="cSpy"></div></div>
</div>

<section class="panel">
  <h2>1. 每个月：论文 Q24 vs 本地复现</h2>
  <p class="muted">差值 = 复现收益 - 论文收益。颜色按绝对误差：
  绿色 ≤0.5pp，黄色 ≤1.0pp，红色 &gt;1.0pp。不可比月份保留但不进入 MAE。</p>
  <div class="grid2">
    <div>
      <h3 id="barTitle">月度收益对照</h3>
      <svg id="monthlyBars" viewBox="0 0 760 330" role="img"
        aria-label="论文与复现月度收益柱状图"></svg>
      <div class="legend">
        <span><i class="swatch" style="background:#123a5a"></i>论文 Q24</span>
        <span><i class="swatch" style="background:#e56b45"></i>本地复现</span>
      </div>
    </div>
    <div class="table-wrap">
      <table><thead><tr>
        <th>年月</th><th>论文 %</th><th>复现 %</th><th>差值 pp</th>
        <th>绝对误差</th><th>可比</th><th>排除原因</th>
      </tr></thead><tbody id="monthlyBody"></tbody></table>
    </div>
  </div>
</section>

<section class="panel">
  <h2>2. 年度 Q24 对照</h2>
  <p class="muted">2025 是论文表中的 Jan-Aug YTD。年度值用于辅助阅读；
  主匹配标准仍是逐月 MAE。</p>
  <div class="table-wrap" style="max-height:420px">
    <table><thead><tr><th>年份</th><th>论文 %</th><th>复现 %</th>
      <th>差值 pp</th><th>可比</th><th>排除原因</th></tr></thead>
      <tbody id="yearlyBody"></tbody></table>
  </div>
</section>

<section class="panel">
  <h2>3. Performance 与 SPY benchmark</h2>
  <p class="muted" id="periodNote"></p>
  <div class="grid2">
    <div>
      <h3>累计净值（月末，log scale）</h3>
      <svg id="equityChart" viewBox="0 0 760 360" role="img"
        aria-label="策略和 SPY 累计净值"></svg>
      <div class="legend">
        <span><i class="swatch" style="background:#1875c1"></i>Selected strategy</span>
        <span><i class="swatch" style="background:#c7423b"></i>SPY benchmark</span>
      </div>
    </div>
    <div class="table-wrap" style="max-height:none">
      <table><thead><tr><th>指标</th><th>论文策略</th>
        <th>本地策略</th><th>本地 SPY</th></tr></thead>
        <tbody id="performanceBody"></tbody></table>
    </div>
  </div>
  <p class="footnote">论文列来自 Table 3（PDF page __PERFORMANCE_PAGE__）。
  本地 SPY 使用同一分钟文件的有效收盘价；缺失收盘不以前值代替。
  生产级 benchmark 仍应换成独立 daily raw-close + dividend 数据源。
  ignore-dividends 模式下，本地 SPY 列实际是 price-only benchmark。</p>
</section>

<section class="panel">
  <h2>4. 18 组合总览</h2>
  <p class="muted">按严格可比月份 MAE 从低到高排序。exploratory 某些组合
  因 unknown-exit 终止，覆盖月数较少，不应只看排名。</p>
  <div class="table-wrap" style="max-height:none">__SUMMARY_TABLE__</div>
</section>

<details><summary>口径、隔离与 provenance</summary>
<ul>
  <li>月度收益：日净收益复利；包含当前 profile 自身设定的佣金和滑点。</li>
  <li>策略 performance：完整 calendar evaluation series；Sharpe =
  日均超额收益 / 日收益标准差 × √252。</li>
  <li>SPY benchmark：同一 evaluation dates；with-dividends 为 total return，
  ignore-dividends 为 price-only sensitivity。</li>
  <li>融资利率、借券费和 market impact 仍为零/未纳入；这不是正式经济评价。</li>
</ul>
<pre>__PROVENANCE__</pre>
</details>
</main>

<script>
const MONTHLY = __MONTHLY_JSON__;
const YEARLY = __YEARLY_JSON__;
const SUMMARY = __SUMMARY_JSON__;
const PERFORMANCE = __PERFORMANCE_JSON__;
const CURVES = __CURVES_JSON__;
const CONFIG = __CONFIG_JSON__;

const byId = id => document.getElementById(id);
const fmt = (v,d=2) => v === null || v === undefined || Number.isNaN(Number(v))
  ? "—" : Number(v).toFixed(d);
const unique = (rows,key) => [...new Set(rows.map(r => r[key]))];
function fillSelect(id, values, selected) {
  const el=byId(id); el.innerHTML=values.map(v =>
    '<option value="'+v+'"'+(v===selected?' selected':'')+'>'+v+'</option>').join('');
}
fillSelect("profile", CONFIG.matrix.profiles, CONFIG.matrix.primary_profile);
fillSelect("tier", CONFIG.matrix.tiers, CONFIG.matrix.primary_tier);
fillSelect("dividend", CONFIG.matrix.dividend_modes,
  CONFIG.matrix.primary_dividend_mode);

function selectedKey(row) {
  return row.profile===byId("profile").value &&
    row.tier===byId("tier").value &&
    row.dividend_mode===byId("dividend").value;
}
function errorClass(v) {
  if (v===null || Number.isNaN(Number(v))) return "";
  return v<=0.5 ? "good" : (v<=1 ? "warn" : "bad");
}
function refreshYears() {
  const years=unique(MONTHLY.filter(selectedKey),"year").sort();
  const old=byId("year").value;
  const options=["全部",...years.map(String)];
  fillSelect("year",options,options.includes(old)?old:
    (options.includes("2024")?"2024":"全部"));
}
function renderMonthly() {
  const chosen=byId("year").value;
  let rows=MONTHLY.filter(selectedKey).sort((a,b)=>
    a.year-b.year || a.month-b.month);
  const chartRows=chosen==="全部" ? [] :
    rows.filter(r=>String(r.year)===chosen);
  if (chosen!=="全部") rows=chartRows;
  byId("monthlyBody").innerHTML=rows.map(r =>
    '<tr><td>'+r.year+'-'+String(r.month).padStart(2,'0')+'</td>'+
    '<td>'+fmt(r.paper_return_pct,1)+'</td>'+
    '<td>'+fmt(r.replication_return_pct,3)+'</td>'+
    '<td>'+fmt(r.difference_pp,3)+'</td>'+
    '<td class="'+errorClass(r.absolute_difference_pp)+'">'+
      fmt(r.absolute_difference_pp,3)+'</td>'+
    '<td>'+(r.comparable?'Yes':'No')+'</td>'+
    '<td class="muted">'+(r.exclusion_reason||'')+'</td></tr>').join('');
  byId("barTitle").textContent=chosen==="全部" ?
    "选择一个年份查看月度柱状图" : chosen+" 月度收益对照";
  drawBars(chartRows);
}
function renderYearly() {
  const rows=YEARLY.filter(selectedKey).sort((a,b)=>a.year-b.year);
  byId("yearlyBody").innerHTML=rows.map(r =>
    '<tr><td>'+r.year+'</td><td>'+fmt(r.paper_yearly_return_pct,1)+'</td>'+
    '<td>'+fmt(r.replication_yearly_return_pct,3)+'</td>'+
    '<td>'+fmt(r.difference_pp,3)+'</td>'+
    '<td>'+(r.comparable?'Yes':'No')+'</td>'+
    '<td class="muted">'+(r.exclusion_reason||'')+'</td></tr>').join('');
}
function drawBars(rows) {
  const svg=byId("monthlyBars");
  if (!rows.length) { svg.innerHTML='<text x="380" y="165" text-anchor="middle" fill="#617080">请选择单一年份</text>'; return; }
  const values=rows.flatMap(r=>[r.paper_return_pct,r.replication_return_pct])
    .filter(Number.isFinite);
  const max=Math.max(1,...values.map(v=>Math.abs(v)));
  const W=760,H=330,L=48,R=14,T=18,B=38,base=T+(H-T-B)/2;
  const scale=(H-T-B)/2/max;
  let out='<line x1="'+L+'" y1="'+base+'" x2="'+(W-R)+'" y2="'+base+'" stroke="#8fa1ae"/>';
  const group=(W-L-R)/12, bw=Math.min(18,group*.3);
  rows.forEach((r,i)=>{
    const x=L+i*group+group/2;
    [[r.paper_return_pct,"#123a5a",-bw],[r.replication_return_pct,"#e56b45",0]]
      .forEach(([v,c,dx])=>{
        if (!Number.isFinite(v)) return;
        const h=Math.abs(v)*scale, y=v>=0?base-h:base;
        out+='<rect x="'+(x+dx)+'" y="'+y+'" width="'+bw+'" height="'+h+
          '" fill="'+c+'"><title>'+fmt(v,3)+'%</title></rect>';
      });
    out+='<text x="'+x+'" y="'+(H-14)+'" text-anchor="middle" font-size="11" fill="#617080">'+String(r.month).padStart(2,'0')+'</text>';
  });
  out+='<text x="8" y="'+(T+8)+'" font-size="11" fill="#617080">+'+fmt(max,1)+'%</text>'+
    '<text x="8" y="'+(H-B)+'" font-size="11" fill="#617080">-'+fmt(max,1)+'%</text>';
  svg.innerHTML=out;
}
function drawEquity(rows) {
  const svg=byId("equityChart");
  const clean=rows.filter(r=>Number.isFinite(r.strategy_equity) ||
    Number.isFinite(r.spy_equity));
  if (clean.length<2) { svg.innerHTML='<text x="380" y="180" text-anchor="middle">No curve data</text>'; return; }
  const vals=clean.flatMap(r=>[r.strategy_equity,r.spy_equity])
    .filter(v=>Number.isFinite(v)&&v>0);
  const logs=vals.map(Math.log), lo=Math.min(...logs), hi=Math.max(...logs);
  const W=760,H=360,L=58,R=18,T=20,B=42;
  const x=i=>L+i*(W-L-R)/(clean.length-1);
  const y=v=>T+(hi-Math.log(v||1))/(hi-lo||1)*(H-T-B);
  let out="";
  for(let j=0;j<=4;j++){
    const ly=lo+(hi-lo)*j/4, yy=T+(4-j)*(H-T-B)/4;
    out+='<line x1="'+L+'" y1="'+yy+'" x2="'+(W-R)+'" y2="'+yy+
      '" stroke="#e2e8ed"/><text x="'+(L-7)+'" y="'+(yy+4)+
      '" text-anchor="end" font-size="11" fill="#617080">'+fmt(Math.exp(ly),2)+'x</text>';
  }
  function path(key,color){
    let d="",started=false;
    clean.forEach((r,i)=>{const v=r[key]; if(!Number.isFinite(v)||v<=0){started=false;return;}
      d+=(started?"L":"M")+x(i).toFixed(1)+","+y(v).toFixed(1); started=true;});
    return '<path d="'+d+'" fill="none" stroke="'+color+'" stroke-width="2.2"/>';
  }
  out+=path("strategy_equity","#1875c1")+path("spy_equity","#c7423b");
  const step=Math.max(1,Math.floor(clean.length/6));
  clean.forEach((r,i)=>{if(i%step===0||i===clean.length-1)
    out+='<text x="'+x(i)+'" y="'+(H-15)+'" text-anchor="middle" font-size="11" fill="#617080">'+r.date.slice(0,7)+'</text>';});
  svg.innerHTML=out;
}
function renderPerformance(p) {
  const paper=CONFIG.paper.performance_reference;
  const bench=paper.benchmark;
  const rows=[
    ["样本区间",paper.period,p.strategy_First+" to "+p.strategy_Last,
      p.strategy_First+" to "+p.strategy_Last],
    ["Total Return %",paper.total_return_pct,p["strategy_TotRet%"],p["spy_total_TotRet%"]],
    ["CAGR / IRR %",paper.annual_return_pct,p["strategy_CAGR%"],p["spy_total_CAGR%"]],
    ["Annual Volatility %",paper.volatility_pct,p["strategy_Vol%"],p["spy_total_Vol%"]],
    ["Sharpe",paper.sharpe,p.strategy_Sharpe_calendar,p.spy_total_Sharpe],
    ["Hit Ratio %",paper.hit_ratio_pct,p["strategy_Hit%"],p["spy_total_Hit%"]],
    ["Maximum Drawdown %",-paper.mdd_pct,p["strategy_MDD%"],p["spy_total_MDD%"]],
    ["Alpha annualised %",paper.alpha_annualised_pct,p["alpha_annualised%"],null],
    ["Beta vs SPY",paper.beta,p.beta_vs_spy_total,1],
    ["Information Ratio",null,p.InfoRatio,null],
    ["Evaluation sessions",null,p.strategy_EvalSessions,p.benchmark_valid_sessions],
    ["Trade units",null,p.strategy_TradeUnits,null]
  ];
  byId("performanceBody").innerHTML=rows.map(r=>'<tr><td>'+r[0]+'</td>'+
    '<td>'+(typeof r[1]==="number"?fmt(r[1],2):(r[1]??"—"))+'</td>'+
    '<td>'+(typeof r[2]==="number"?fmt(r[2],2):(r[2]??"—"))+'</td>'+
    '<td>'+(typeof r[3]==="number"?fmt(r[3],2):(r[3]??"—"))+'</td></tr>').join('');
  byId("periodNote").textContent="当前选择："+p.profile+" × "+p.tier+" × "+
    p.dividend_mode+"；本地样本 "+p.strategy_First+" 至 "+p.strategy_Last+
    "。论文 Table 3 样本为 "+paper.period+"，只作结构化参照。";
}
function renderAll() {
  const s=SUMMARY.find(selectedKey), p=PERFORMANCE.find(selectedKey);
  if(!s||!p) return;
  byId("cMonths").textContent=s.comparable_months;
  byId("cMae").textContent=fmt(s.monthly_mae_pp,3)+" pp";
  byId("c05").textContent=fmt(s.months_within_0_5pp_pct,1)+"%";
  byId("cCagr").textContent=fmt(p["strategy_CAGR%"],2)+"%";
  byId("cSharpe").textContent=fmt(p.strategy_Sharpe_calendar,2);
  byId("cSpy").textContent=fmt(p["spy_total_CAGR%"],2)+"%";
  renderMonthly(); renderYearly(); renderPerformance(p);
  drawEquity(CURVES.filter(selectedKey).sort((a,b)=>a.date.localeCompare(b.date)));
}
["profile","tier","dividend"].forEach(id=>byId(id).addEventListener("change",()=>{
  refreshYears(); renderAll();
}));
byId("year").addEventListener("change",renderMonthly);
refreshYears();
renderAll();
</script>
</body>
</html>
"""
    replacements = {
        "__PAPER_REVISION__": html.escape(config["paper"]["revision"]),
        "__PERFORMANCE_PAGE__": str(
            config["paper"]["performance_reference"]["page"]),
        "__SUMMARY_TABLE__": table_html(summary_view, decimals=3),
        "__PROVENANCE__": html.escape(json.dumps(
            provenance, indent=2, sort_keys=True)),
        "__MONTHLY_JSON__": records_json(monthly),
        "__YEARLY_JSON__": records_json(yearly),
        "__SUMMARY_JSON__": records_json(summary),
        "__PERFORMANCE_JSON__": records_json(performance),
        "__CURVES_JSON__": records_json(curves),
        "__CONFIG_JSON__": json.dumps(
            config, ensure_ascii=False, separators=(",", ":")),
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return template


def self_test() -> None:
    config_path = ROOT / "experiments" / "paper_replication_v1" / "config.yml"
    config, context = validate_inputs(config_path)
    monthly = context["monthly_ref"]
    yearly = context["yearly_ref"]
    assert len(monthly) == 220
    assert monthly.loc[
        monthly["year"].eq(2017) & monthly["month"].eq(1),
        "paper_return_pct",
    ].item() == -3.3
    assert yearly.loc[yearly["year"].eq(2024),
                      "paper_yearly_return_pct"].item() == 32.2
    assert len(monthly.loc[monthly["year"].eq(2025)]) == 8
    assert config["classification"] == "replication_only_not_economic_evaluation"
    assert config["matrix"]["dividend_modes"] == [
        "with_dividends", "ignore_dividends"]
    assert config["matrix"]["primary_dividend_mode"] in (
        config["matrix"]["dividend_modes"])
    assert config["paper"]["performance_reference"]["total_return_pct"] == 1985.0
    assert config["paper"]["performance_reference"]["benchmark"][
        "annual_return_pct"] == 7.2
    print("PAPER REPLICATION SELF-TEST PASSED (9 checks)")


def main() -> int:
    args = parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.output_dir is None:
        raise ValueError("--output-dir is required unless --self-test is used.")

    config_path = args.config.resolve()
    config, context = validate_inputs(config_path)
    output_dir = args.output_dir.resolve()
    if output_dir == ROOT or ROOT not in output_dir.parents:
        raise ValueError("Output directory must be a child of the repository.")
    if output_dir.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    protected_before = {
        str(path): sha256_file(resolve_repo_path(path))
        for path in config["isolation"]["protected_files"]
    }
    monthly_parts = []
    yearly_parts = []
    performance_rows = []
    curve_parts = []
    dividend_metadata = {}
    for dividend_mode in config["matrix"]["dividend_modes"]:
        ignore_dividends = dividend_mode == "ignore_dividends"
        load_cfg = engine.profile_cfg(
            "paper_spec", ignore_dividends=ignore_dividends)
        data = engine.load_run(context["bundle_path"], load_cfg)
        benchmark_total_returns = full_benchmark_total_returns(data, load_cfg)
        dividend_metadata[dividend_mode] = data["dividend_meta"]
        for profile in config["matrix"]["profiles"]:
            for tier in config["matrix"]["tiers"]:
                print(
                    f"running profile={profile} tier={tier} "
                    f"dividend_mode={dividend_mode}",
                    flush=True,
                )
                monthly, yearly, performance, curve = compare_one(
                    data,
                    profile,
                    tier,
                    dividend_mode,
                    benchmark_total_returns,
                    context["monthly_ref"],
                    context["yearly_ref"],
                )
                monthly_parts.append(monthly)
                yearly_parts.append(yearly)
                performance_rows.append(performance)
                curve_parts.append(curve)
        del data
    monthly_all = pd.concat(monthly_parts, ignore_index=True)
    yearly_all = pd.concat(yearly_parts, ignore_index=True)
    performance_all = pd.DataFrame(performance_rows)
    curves_all = pd.concat(curve_parts, ignore_index=True)
    summary = summarize(monthly_all, yearly_all, config)

    protected_after = {
        str(path): sha256_file(resolve_repo_path(path))
        for path in config["isolation"]["protected_files"]
    }
    if protected_before != protected_after:
        raise RuntimeError("A protected economic-evaluation file changed.")

    provenance = {
        "experiment_id": config["experiment_id"],
        "classification": config["classification"],
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "paper": {
            **config["paper"],
            "actual_sha256": sha256_file(context["paper_path"]),
        },
        "reference_table_sha256": sha256_file(context["reference_path"]),
        "config_sha256": sha256_file(config_path),
        "engine_sha256": sha256_file(ROOT / "im_engine_v4.py"),
        "data_bundle": {
            "type": "immutable_release",
            "path": str(context["bundle_path"]),
            "release_id": context["release_manifest"]["release_id"],
            "data_manifest_sha256": context["release_manifest_sha256"],
        },
        "data_release_manifest_sha256": context["release_manifest_sha256"],
        "source_sha256": context["release_manifest"]["source_sha256"],
        "dividend_modes": dividend_metadata,
        "matrix": config["matrix"],
        "performance_method": {
            "strategy": "calendarised daily net returns",
            "sharpe": "(daily mean - rf/252) / daily std * sqrt(252)",
            "benchmark": "same minute file, valid raw closes, dividends by mode",
            "curve_frequency": "month end",
        },
        "protected_files_before": protected_before,
        "protected_files_after": protected_after,
        "git": git_meta(),
        "command": sys.argv,
    }

    staging = Path(tempfile.mkdtemp(
        prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent))
    try:
        monthly_path = staging / "monthly_comparison.csv"
        yearly_path = staging / "yearly_comparison.csv"
        summary_path = staging / "summary.csv"
        performance_path = staging / "performance_benchmark.csv"
        curves_path = staging / "equity_curves_monthly.csv"
        report_path = staging / "report.html"
        monthly_all.to_csv(monthly_path, index=False, float_format="%.8f")
        yearly_all.to_csv(yearly_path, index=False, float_format="%.8f")
        summary.to_csv(summary_path, index=False, float_format="%.8f")
        performance_all.to_csv(
            performance_path, index=False, float_format="%.8f")
        curves_all.to_csv(curves_path, index=False, float_format="%.8f")
        report_path.write_text(
            render_report(
                config,
                context,
                monthly_all,
                yearly_all,
                summary,
                performance_all,
                curves_all,
                provenance,
            ),
            encoding="utf-8",
        )
        provenance["outputs"] = {
            path.name: {"sha256": sha256_file(path), "bytes": path.stat().st_size}
            for path in [
                monthly_path,
                yearly_path,
                summary_path,
                performance_path,
                curves_path,
                report_path,
            ]
        }
        (staging / "manifest.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.replace(output_dir)
    except Exception:
        if staging.exists() and staging.parent == output_dir.parent:
            shutil.rmtree(staging)
        raise

    primary = summary.loc[summary["is_primary"]].iloc[0]
    print(f"output_dir={output_dir}")
    print(f"primary_monthly_mae_pp={primary['monthly_mae_pp']:.3f}")
    print(f"primary_months_within_1pp_pct="
          f"{primary['months_within_1_0pp_pct']:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
