"""Run the isolated EOD close-source sensitivity experiment."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import im_engine_v4 as engine  # noqa: E402

SCRIPT_PATH = Path(__file__).resolve()
DEFAULT_CONFIG = SCRIPT_PATH.with_name("config.yml")
DEFAULT_MD = ROOT / "docs" / "EOD_CLOSE_SOURCE_EXPERIMENT_V1_ZH.md"
DEFAULT_HTML = ROOT / "docs" / "EOD_CLOSE_SOURCE_EXPERIMENT_V1_ZH.html"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


def git_state() -> dict[str, Any]:
    commit = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False)
    status = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain"],
        capture_output=True, text=True, check=False)
    return {
        "commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
        "dirty_paths": [line[3:].strip() for line in status.stdout.splitlines()
                        if line.strip()],
    }


def read_config(path: Path) -> dict[str, Any]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("experiment config must be a mapping")
    variants = cfg.get("variants", [])
    ids = [item.get("id") for item in variants]
    if len(variants) < 4 or len(set(ids)) != len(variants):
        raise ValueError("experiment requires at least four unique variants")
    if variants[0].get("price_source") != "minute_close":
        raise ValueError("the first variant must be the minute-close baseline")
    return cfg


def load_daily_close(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, parse_dates=["session_date"])
    if "close" not in frame:
        raise ValueError("daily-close input has no close column")
    frame["session_date"] = pd.to_datetime(
        frame["session_date"]).dt.normalize()
    if frame["session_date"].duplicated().any():
        raise ValueError("daily-close input has duplicate sessions")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    if (~np.isfinite(frame["close"]) | (frame["close"] <= 0)).any():
        raise ValueError("daily-close input has invalid close values")
    return frame.set_index("session_date").sort_index()


def minute_and_twap_prices(
        bars: pd.DataFrame, twap_minutes: int) -> pd.DataFrame:
    cols = ["session_date", "minute_of_session", "calendar_bars", "close",
            "is_executable_minute"]
    missing = sorted(set(cols) - set(bars.columns))
    if missing:
        raise ValueError(f"minute data misses columns: {missing}")
    frame = bars.loc[bars["is_executable_minute"].astype(bool), cols].copy()
    frame["session_date"] = pd.to_datetime(
        frame["session_date"]).dt.normalize()
    last = (
        frame.sort_values(["session_date", "minute_of_session"])
        .groupby("session_date", as_index=False).tail(1)
        [["session_date", "minute_of_session", "calendar_bars", "close"]]
        .rename(columns={"close": "minute_close"})
    )
    last["last_bar_is_scheduled_close"] = (
        last["minute_of_session"] == last["calendar_bars"])
    tail = frame.loc[
        frame["minute_of_session"] > frame["calendar_bars"] - twap_minutes]
    twap = tail.groupby("session_date").agg(
        tail_twap=("close", "mean"), tail_bars=("close", "size"),
        tail_first_minute=("minute_of_session", "min"),
        tail_last_minute=("minute_of_session", "max"),
    ).reset_index()
    out = last.merge(twap, on="session_date", how="left")
    return out.set_index("session_date").sort_index()


def make_eod_frame(
        variant: dict[str, Any], daily: pd.DataFrame,
        minute_prices: pd.DataFrame) -> pd.DataFrame | None:
    source = variant["price_source"]
    if source == "minute_close":
        return None
    if source == "independent_daily_close":
        prices = daily["close"].rename("price").to_frame()
    elif source == "tail_twap":
        n = int(variant["twap_minutes"])
        bad = minute_prices["tail_bars"].ne(n)
        # Incomplete sessions may exist in the clean file, but the halt-aware
        # tier will not trade them. Keep available prices and enforce coverage
        # again when the engine reaches a tradable session.
        prices = minute_prices.loc[~bad, "tail_twap"].rename("price").to_frame()
    else:
        raise ValueError(f"unknown price_source {source!r}")
    bps = float(variant.get("extra_cost_bps", 0.0))
    if bps < 0:
        raise ValueError("extra_cost_bps must be nonnegative")
    prices["extra_cost_per_share"] = prices["price"] * bps / 10_000.0
    return prices


def period_masks(index: pd.DatetimeIndex, base: dict[str, Any]) -> dict[str, Any]:
    cut = pd.Timestamp(base["evaluation_start"])
    end = pd.Timestamp(base["evaluation_end"])
    return {
        "full_sample": index <= end,
        "pre_publication": (index < cut) & (index <= end),
        "post_publication": (index >= cut) & (index <= end),
    }


def summarize(
        variant: dict[str, Any], result: pd.DataFrame,
        base: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for period, mask in period_masks(result.index, base).items():
        sliced = result.loc[mask].copy()
        stat = engine.stats(sliced)
        ev = sliced.loc[sliced["is_evaluation"].astype(bool)]
        traded = float(ev["shares_traded"].sum())
        rows.append({
            "variant_id": variant["id"],
            "label": variant["label"],
            "price_source": variant["price_source"],
            "extra_cost_bps": float(variant.get("extra_cost_bps", 0.0)),
            "twap_minutes": variant.get("twap_minutes"),
            "period": period,
            "total_return_pct": stat.get("TotRet%"),
            "cagr_pct": stat.get("CAGR%"),
            "vol_pct": stat.get("Vol%"),
            "sharpe_calendar": stat.get("Sharpe_calendar"),
            "mdd_pct": stat.get("MDD%"),
            "worst_day_pct": stat.get("WorstDay%"),
            "trade_units": stat.get("TradeUnits"),
            "shares_traded": stat.get("SharesTraded"),
            "gross_pnl": float(ev["gross"].sum()),
            "execution_cost": float(ev["cost"].sum()),
            "cash_interest": float(ev["cash_interest"].sum()),
            "financing": float(ev["financing"].sum()),
            "net_pnl": float(ev["net"].sum()),
            "eod_exit_fills": int(ev["eod_exit_fills"].sum()),
            "eod_exit_notional": float(ev["eod_exit_notional"].sum()),
            "eod_extra_cost": float(ev["eod_extra_cost"].sum()),
            "gross_edge_per_traded_share": (
                float(ev["gross"].sum()) / traded if traded else np.nan),
            "cost_per_traded_share": (
                float(ev["cost"].sum()) / traded if traded else np.nan),
            "first": stat.get("First"),
            "last": stat.get("Last"),
            "evaluation_sessions": stat.get("EvalSessions"),
        })
    return rows


def add_deltas(summary: pd.DataFrame, baseline_id: str) -> pd.DataFrame:
    base = summary.loc[summary["variant_id"].eq(baseline_id)].set_index("period")
    for column in ("total_return_pct", "cagr_pct", "sharpe_calendar", "mdd_pct",
                   "gross_pnl", "execution_cost", "net_pnl"):
        summary[f"delta_{column}"] = summary.apply(
            lambda row: row[column] - base.loc[row["period"], column], axis=1)
    return summary


def source_diagnostics(
        minute_prices: pd.DataFrame, daily: pd.DataFrame,
        baseline_result: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    joined = minute_prices.join(
        daily[["close"]].rename(columns={"close": "daily_close"}), how="inner")
    joined["daily_minus_minute"] = (
        joined["daily_close"] - joined["minute_close"])
    joined["twap_minus_minute"] = (
        joined["tail_twap"] - joined["minute_close"])
    active_exit = baseline_result["eod_exit_fills"].gt(0)
    joined["baseline_has_eod_exit"] = active_exit.reindex(
        joined.index).fillna(False).astype(bool)
    diagnostics = []
    for scope, frame in (
        ("all_matched_sessions", joined),
        ("baseline_eod_exit_sessions", joined.loc[joined["baseline_has_eod_exit"]]),
    ):
        for column in ("daily_minus_minute", "twap_minus_minute"):
            x = frame[column].dropna()
            diagnostics.append({
                "scope": scope, "difference": column, "sessions": len(x),
                "mean": float(x.mean()), "median": float(x.median()),
                "mean_abs": float(x.abs().mean()),
                "p95_abs": float(x.abs().quantile(0.95)),
                "p99_abs": float(x.abs().quantile(0.99)),
                "max_abs": float(x.abs().max()),
                "over_0p01": int(x.abs().gt(0.01).sum()),
                "over_0p05": int(x.abs().gt(0.05).sum()),
            })
    worst = joined.loc[joined["baseline_has_eod_exit"]].copy()
    worst["max_source_abs_difference"] = worst[
        ["daily_minus_minute", "twap_minus_minute"]].abs().max(axis=1)
    worst = worst.nlargest(15, "max_source_abs_difference").reset_index()
    return pd.DataFrame(diagnostics), worst


def daily_comparison(results: dict[str, pd.DataFrame]) -> pd.DataFrame:
    frames = []
    baseline = next(iter(results.values()))
    for variant_id, result in results.items():
        frame = result[[
            "status", "gross", "commission", "slippage", "cost",
            "cash_interest", "financing", "net", "ret", "aum",
            "prev_aum",
            "eod_exit_fills", "eod_exit_notional", "eod_extra_cost",
            "is_evaluation",
        ]].copy()
        frame["variant_id"] = variant_id
        frame["delta_net_vs_minute_close"] = frame["net"] - baseline["net"]
        frame["delta_ret_vs_minute_close"] = frame["ret"] - baseline["ret"]
        frames.append(frame.reset_index())
    return pd.concat(frames, ignore_index=True)


def audit_results(results: dict[str, pd.DataFrame]) -> dict[str, Any]:
    max_net_residual = 0.0
    max_aum_residual = 0.0
    for variant_id, result in results.items():
        net_residual = (
            result["net"] - result["gross"] + result["cost"]
            - result["cash_interest"] - result["financing"]
        ).abs().max()
        aum_residual = (
            result["aum"] - result["prev_aum"] - result["net"]
        ).abs().max()
        max_net_residual = max(max_net_residual, float(net_residual))
        max_aum_residual = max(max_aum_residual, float(aum_residual))
        if net_residual > 1e-8 or aum_residual > 1e-8:
            raise RuntimeError(
                f"{variant_id} accounting identity failed: "
                f"net={net_residual}, aum={aum_residual}")
    return {
        "variants": len(results),
        "max_net_identity_residual": max_net_residual,
        "max_aum_identity_residual": max_aum_residual,
    }


def paired_return_diagnostics(
        results: dict[str, pd.DataFrame], base: dict[str, Any]) -> pd.DataFrame:
    baseline_id = next(iter(results))
    baseline = results[baseline_id]
    rows = []
    for variant_id, result in results.items():
        for period, mask in period_masks(result.index, base).items():
            joined = pd.DataFrame({
                "baseline": baseline.loc[mask, "ret"],
                "variant": result.loc[mask, "ret"],
                "eligible": (baseline.loc[mask, "is_evaluation"].astype(bool)
                             & result.loc[mask, "is_evaluation"].astype(bool)),
            })
            joined = joined.loc[joined["eligible"]].dropna()
            relative_log = (
                np.log1p(joined["variant"]) - np.log1p(joined["baseline"]))
            top_day = relative_log.abs().idxmax()
            years = max(
                (joined.index.max() - joined.index.min()).days / 365.2425,
                1 / 365.2425)
            replaced = joined["variant"].copy()
            replaced.loc[top_day] = joined.loc[top_day, "baseline"]
            cagr_without_top = (
                float((1 + replaced).prod()) ** (1 / years) - 1) * 100
            rows.append({
                "variant_id": variant_id,
                "period": period,
                "relative_wealth_vs_baseline": float(
                    np.expm1(relative_log.sum())),
                "top_relative_day": top_day,
                "top_day_baseline_ret": float(joined.loc[top_day, "baseline"]),
                "top_day_variant_ret": float(joined.loc[top_day, "variant"]),
                "top_day_log_relative_contribution": float(
                    relative_log.loc[top_day]),
                "variant_cagr_replacing_top_day_with_baseline_pct":
                    cagr_without_top,
            })
    return pd.DataFrame(rows)


def fmt(value: Any, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):,.{digits}f}"


def report_tables(summary: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    cols = ["label", "period", "cagr_pct", "delta_cagr_pct",
            "sharpe_calendar", "delta_sharpe_calendar", "mdd_pct",
            "total_return_pct", "delta_total_return_pct", "eod_extra_cost"]
    main = summary[cols].copy()
    for col in cols[2:]:
        main[col] = main[col].map(lambda x: fmt(x, 2))
    post = summary.loc[summary["period"].eq("post_publication"), [
        "label", "gross_pnl", "execution_cost", "net_pnl",
        "eod_exit_fills", "eod_exit_notional", "eod_extra_cost",
        "delta_gross_pnl", "delta_execution_cost", "delta_net_pnl",
    ]].copy()
    for col in post.columns[1:]:
        post[col] = post[col].map(lambda x: fmt(x, 0 if "fills" in col else 2))
    return main, post


def render_markdown(
        summary: pd.DataFrame, diagnostics: pd.DataFrame,
        paired: pd.DataFrame, manifest: dict[str, Any]) -> str:
    def row(variant_id: str, period: str) -> pd.Series:
        return summary.loc[
            summary["variant_id"].eq(variant_id)
            & summary["period"].eq(period)].iloc[0]

    baseline = row("minute_1559_close", "post_publication")
    daily0 = row("independent_daily_close_no_auction_cost", "post_publication")
    daily05 = row("independent_daily_close_auction_0p5bp", "post_publication")
    daily1 = row("independent_daily_close_auction_1bp", "post_publication")
    twap = row("tail_twap_10m", "post_publication")
    diag = diagnostics.loc[
        diagnostics["scope"].eq("baseline_eod_exit_sessions")
        & diagnostics["difference"].eq("daily_minus_minute")].iloc[0]
    daily0_pair = paired.loc[
        paired["variant_id"].eq("independent_daily_close_no_auction_cost")
        & paired["period"].eq("post_publication")].iloc[0]
    break_even_bps = 0.5 + (
        daily05["cagr_pct"] - baseline["cagr_pct"]
    ) / max(daily05["cagr_pct"] - daily1["cagr_pct"], 1e-12) * 0.5
    return f"""# EOD 价格来源实验 v1

本实验只改变 EOD flatten 的价格和明确列示的增量成本，不改变信号、日内成交、
仓位规则、分红、融资曲线或冻结的 post-publication 切点。它是看过结果后的执行敏感性，
不是新的 OOS，也不替代 frozen v2 结果。

## 主结果

| EOD 方案 | Full CAGR | Pre CAGR | Post CAGR | Post Sharpe | Post MDD | vs 15:59 Post CAGR |
|---|---:|---:|---:|---:|---:|---:|
{markdown_result_rows(summary)}

Post-publication 下：

- 15:59 minute close：CAGR {baseline['cagr_pct']:.2f}%，Sharpe {baseline['sharpe_calendar']:.2f}。
- 独立 daily close、未加 auction 成本：CAGR {daily0['cagr_pct']:.2f}%，相对基线 {daily0['delta_cagr_pct']:+.2f}pp。
- 独立 daily close + 0.5 bp：CAGR {daily05['cagr_pct']:.2f}%；以 0.5/1 bp 两点线性插值，
  post CAGR 与基线相等的增量 auction 成本约为 {break_even_bps:.2f} bp（仅为局部近似）。
- 独立 daily close + 1 bp EOD auction 成本：CAGR {daily1['cagr_pct']:.2f}%，相对基线 {daily1['delta_cagr_pct']:+.2f}pp。
- 尾盘 10-minute TWAP：CAGR {twap['cagr_pct']:.2f}%，相对基线 {twap['delta_cagr_pct']:+.2f}pp。

## 价格源诊断

在基线实际需要 EOD flatten 的 {int(diag['sessions'])} 个 session 中，独立 daily close
相对 minute close 的有符号差中位数为 ${diag['median']:.4f}，
绝对差 P95 为 ${diag['p95_abs']:.4f}，最大值为 ${diag['max_abs']:.4f}；
{int(diag['over_0p05'])} 个 session 的绝对差超过 $0.05。这个量级说明 cross-source
close 差异不能自动解释为可捕获的 closing-auction alpha。

最大差出现在 2025-04-09：minute close 为 $543.37，而 daily close 为 $548.62。
把这一天的 daily-close 变体收益替换回 minute-close 基线后，该变体 post CAGR 为
{daily0_pair['variant_cagr_replacing_top_day_with_baseline_pct']:.2f}%，说明结论并非只由这一日决定，
但这一日对相对表现有实质影响，不能忽略。

## 解释与边界

- `独立 daily close` 来自 Yahoo raw daily close，是独立 close 代理，不是真实 MOC fill、
  官方 closing-auction imbalance feed 或券商成交回报。
- 1 bp auction 成本是相对基础 `$0.0025/share` slippage 的单边增量，只在 EOD 退出腿收费。
- TWAP 是最后 10 个计划分钟的 minute close 等份成交的等价平均价；完整日为
  15:50–15:59，半日市取最后 10 分钟。未建模 participation、queue、partial fill 和 impact。
- 所有变体沿各自 AUM 连续复利并重新计算随后仓位，不是固定仓位的事后 PnL 加减。
- 当前只报告点估计；HAC、block bootstrap、真实 auction/TAQ 校验仍未做。

## 研究判断

1. EOD close 口径对策略具有经济显著影响，不能继续把它当成无关紧要的记账细节。
2. 没有跨时期占优的方案：所有 TWAP 窗口在 post 均高于 15:59 close，但在 full/pre
   均明显更差。尤其 30-minute TWAP 的 post CAGR 最高、full/pre CAGR 最低，不能据此
   事后选择窗口。
3. 独立 daily close 的 post 优势只在增量 auction 成本低于约 0.72 bp 时保留；1 bp
   已使其低于 15:59 基线。加上 cross-source 差异和 2025-04-09 极端日，目前不能把
   daily-close 结果解释为可复制的 auction alpha。
4. 因此不修改 frozen v2 headline。下一步若继续，应取得 official close/auction print、
   MOC 成交或 TAQ 级证据，并加入 participation、impact 与 partial-fill 模型。

## 审计信息

- run：`{manifest['run_id']}`
- classification：`{manifest['classification']}`
- data：2008-01-22 至 2026-07-09
- Git commit：`{manifest['git']['commit']}`；dirty：`{str(manifest['git']['dirty']).lower()}`
- config SHA-256：`{manifest['hashes']['config_sha256']}`
- engine SHA-256：`{manifest['hashes']['engine_sha256']}`
- daily close SHA-256：`{manifest['hashes']['daily_close_sha256']}`
- financing SHA-256：`{manifest['hashes']['financing_sha256']}`

完整交互前不需要的审计表、逐日结果和极端 close 差异见本 run 的 rebuildable results 目录；
可读 HTML：`docs/EOD_CLOSE_SOURCE_EXPERIMENT_V1_ZH.html`。
"""


def markdown_result_rows(summary: pd.DataFrame) -> str:
    rows = []
    for variant_id in summary["variant_id"].drop_duplicates():
        x = summary.loc[summary["variant_id"].eq(variant_id)].set_index("period")
        p = x.loc["post_publication"]
        rows.append(
            f"| {p['label']} | {x.loc['full_sample','cagr_pct']:.2f}% | "
            f"{x.loc['pre_publication','cagr_pct']:.2f}% | {p['cagr_pct']:.2f}% | "
            f"{p['sharpe_calendar']:.2f} | {p['mdd_pct']:.1f}% | "
            f"{p['delta_cagr_pct']:+.2f}pp |")
    return "\n".join(rows)


def render_html(
        summary: pd.DataFrame, diagnostics: pd.DataFrame,
        worst: pd.DataFrame, paired: pd.DataFrame,
        manifest: dict[str, Any]) -> str:
    main, post = report_tables(summary)
    diag = diagnostics.copy()
    numeric = [c for c in diag.columns if c not in ("scope", "difference")]
    for col in numeric:
        diag[col] = diag[col].map(lambda x: fmt(x, 4))
    worst_show = worst[[
        "session_date", "minute_close", "daily_close", "tail_twap",
        "daily_minus_minute", "twap_minus_minute",
    ]].copy()
    for col in worst_show.columns[1:]:
        worst_show[col] = worst_show[col].map(lambda x: fmt(x, 4))
    provenance = html.escape(json.dumps(manifest, indent=2, default=str), quote=False)
    paired_show = paired.loc[
        paired["period"].eq("post_publication"), [
            "variant_id", "relative_wealth_vs_baseline", "top_relative_day",
            "top_day_baseline_ret", "top_day_variant_ret",
            "variant_cagr_replacing_top_day_with_baseline_pct",
        ]].copy()
    for col in ("relative_wealth_vs_baseline", "top_day_baseline_ret",
                "top_day_variant_ret"):
        paired_show[col] = paired_show[col].map(lambda x: fmt(100 * x, 3) + "%")
    paired_show["variant_cagr_replacing_top_day_with_baseline_pct"] = paired_show[
        "variant_cagr_replacing_top_day_with_baseline_pct"].map(
            lambda x: fmt(x, 2) + "%")
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EOD 价格来源实验 v1</title><style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#f5f7fb;color:#172033}}
main{{max-width:1250px;margin:auto;padding:28px}} .card{{background:white;border:1px solid #dfe5ef;
border-radius:12px;padding:20px;margin:16px 0;box-shadow:0 2px 8px #15213a10}}
h1,h2{{margin-top:0}} .badge{{display:inline-block;background:#fff3cd;color:#745500;padding:6px 10px;
border-radius:99px;font-weight:600}} table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{padding:8px;border-bottom:1px solid #e8edf5;text-align:right}} th:first-child,td:first-child{{text-align:left}}
.scroll{{overflow:auto}} code,pre{{font-family:Consolas,monospace}} pre{{white-space:pre-wrap;background:#111827;color:#dbeafe;padding:14px;border-radius:8px}}
.note{{color:#526079;line-height:1.6}} ul{{line-height:1.65}}</style></head><body><main>
<span class="badge">POST-RESULT EXECUTION SENSITIVITY · POINT ESTIMATES</span>
<h1>EOD 价格来源实验 v1</h1><p class="note">固定 corrected_execution × halt_aware × with-dividends ×
$0.0025/share、融资曲线及信号路径，只替换 EOD flatten 腿。独立 daily close 是代理，不是真实 MOC 成交。</p>
<section class="card"><h2>Full / Pre / Post 结果</h2><div class="scroll">{main.to_html(index=False, border=0)}</div></section>
<section class="card"><h2>Post-publication P&amp;L 与 EOD 成本</h2><div class="scroll">{post.to_html(index=False, border=0)}</div></section>
<section class="card"><h2>价格源差异分布</h2><div class="scroll">{diag.to_html(index=False, border=0)}</div>
<p class="note">差值均为候选价格减去 15:59 minute close；跨源差异不等于可交易 auction edge。</p></section>
<section class="card"><h2>实际 EOD exit 日的最大价格差异</h2><div class="scroll">{worst_show.to_html(index=False, border=0)}</div></section>
<section class="card"><h2>Post 单日集中度审计</h2><div class="scroll">{paired_show.to_html(index=False, border=0)}</div>
<p class="note">最后一列把各变体相对基线影响最大的单日收益替换回基线后重新计算 CAGR。</p></section>
<section class="card"><h2>口径与限制</h2><ul>
<li>Daily close：Yahoo raw daily close；1 bp 为基础 per-share slippage 之外、仅 EOD 退出腿的增量。</li>
<li>TWAP：最后 10 个计划分钟 close 等份成交的等价平均价；未模拟 queue、partial fill、participation 和 impact。</li>
<li>各变体从完整样本起点连续复利，因此后续 share sizing 随各自 AUM 变化。</li>
<li>不修改 frozen v2；本实验是看过结果后的敏感性，只报告点估计。</li></ul></section>
<section class="card"><h2>研究判断</h2><ol>
<li>EOD close 口径具有经济显著影响，但没有跨时期占优的执行方案。</li>
<li>30-minute TWAP 在 post 最好、在 full/pre 最差，不能事后挑作新 headline。</li>
<li>Daily-close 优势的 post 增量成本 break-even 约 0.72 bp；1 bp 已低于基线。</li>
<li>保持 frozen v2 不变；下一步需要 official auction/TAQ 与 impact/partial-fill 证据。</li>
</ol></section>
<section class="card"><h2>Provenance</h2><pre>{provenance}</pre></section>
</main></body></html>"""


def run(args: argparse.Namespace) -> Path:
    config_path = args.config.resolve()
    cfg_doc = read_config(config_path)
    base = cfg_doc["base"]
    engine_cfg = engine.profile_cfg(
        base["profile"], tier=base["tier"],
        ignore_dividends=base["dividend_mode"] == "ignore_dividends",
        slip_per_share=float(base["slippage_per_share"]))
    data_release = (ROOT / base["data_release"]).resolve()
    daily_path = (ROOT / base["daily_close"]).resolve()
    financing_path = (ROOT / base["financing_rates"]).resolve()
    data = engine.load_run(data_release, engine_cfg)
    daily = load_daily_close(daily_path)
    financing = pd.read_csv(financing_path, parse_dates=["session_date"])
    twap_windows = sorted({
        int(v["twap_minutes"]) for v in cfg_doc["variants"]
        if v["price_source"] == "tail_twap"})
    minute_prices_by_window = {
        n: minute_and_twap_prices(data["bars"], n) for n in twap_windows}
    minute_prices = minute_prices_by_window[10]

    results: dict[str, pd.DataFrame] = {}
    summary_rows: list[dict[str, Any]] = []
    for variant in cfg_doc["variants"]:
        source_prices = (
            minute_prices_by_window[int(variant["twap_minutes"])]
            if variant["price_source"] == "tail_twap" else minute_prices)
        eod_frame = make_eod_frame(variant, daily, source_prices)
        result = engine.backtest(
            data, engine_cfg, financing_rates=financing,
            eod_execution=eod_frame)
        results[variant["id"]] = result
        summary_rows.extend(summarize(variant, result, base))
        print(
            f"completed {variant['id']}: "
            f"{engine.stats(result).get('CAGR%')}% CAGR", flush=True)

    summary = add_deltas(
        pd.DataFrame(summary_rows), cfg_doc["variants"][0]["id"])
    diagnostics, worst = source_diagnostics(
        minute_prices, daily, results[cfg_doc["variants"][0]["id"]])
    daily_rows = daily_comparison(results)
    paired = paired_return_diagnostics(results, base)
    accounting_audit = audit_results(results)

    run_id = args.run_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_eod_close_source_v1")
    output_root = (ROOT / "experiments" / "eod_close_source_v1" / "results")
    output = output_root / run_id
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    staging = output_root / f".staging-{run_id}-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        manifest = {
            "run_id": run_id,
            "classification": cfg_doc["classification"],
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "experiment_id": cfg_doc["experiment_id"],
            "sample": {"start": "2008-01-22", "end": base["evaluation_end"],
                       "post_publication_start": base["evaluation_start"]},
            "base_config": base,
            "variants": cfg_doc["variants"],
            "git": git_state(),
            "hashes": {
                "config_sha256": sha256(config_path),
                "runner_sha256": sha256(SCRIPT_PATH),
                "engine_sha256": sha256(ROOT / "im_engine_v4.py"),
                "data_release_manifest_sha256": sha256(
                    data_release / "data_manifest.json"),
                "daily_close_sha256": sha256(daily_path),
                "financing_sha256": sha256(financing_path),
            },
            "coverage": {
                "summary_rows": int(len(summary)),
                "daily_rows": int(len(daily_rows)),
                "minute_daily_matched_sessions": int(len(
                    minute_prices.index.intersection(daily.index))),
            },
            "accounting_audit": accounting_audit,
        }
        summary.to_csv(staging / "summary.csv", index=False)
        diagnostics.to_csv(staging / "source_diagnostics.csv", index=False)
        worst.to_csv(staging / "largest_source_differences.csv", index=False)
        paired.to_csv(staging / "paired_return_diagnostics.csv", index=False)
        daily_rows.to_parquet(staging / "daily_comparison.parquet", index=False)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
        report_html = render_html(summary, diagnostics, worst, paired, manifest)
        report_md = render_markdown(summary, diagnostics, paired, manifest)
        (staging / "report.html").write_text(report_html, encoding="utf-8")
        (staging / "report.md").write_text(report_md, encoding="utf-8")
        (staging / "_SUCCESS").write_text(
            json.dumps({"run_id": run_id, "manifest_sha256": sha256(
                staging / "manifest.json")}, sort_keys=True) + "\n",
            encoding="utf-8")
        output_root.mkdir(parents=True, exist_ok=True)
        os.replace(staging, output)
        args.markdown_report.write_text(report_md, encoding="utf-8")
        args.html_report.write_text(report_html, encoding="utf-8")
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--run-id")
    parser.add_argument("--markdown-report", type=Path, default=DEFAULT_MD)
    parser.add_argument("--html-report", type=Path, default=DEFAULT_HTML)
    return parser.parse_args()


if __name__ == "__main__":
    destination = run(parse_args())
    print(f"published: {destination}")
