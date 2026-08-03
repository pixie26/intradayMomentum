"""Run the isolated 4x-cap versus unlevered 1x-cap sensitivity."""

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


def load_config(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("experiment config must be a mapping")
    variants = doc.get("variants", [])
    if [v.get("id") for v in variants] != ["cap_4x", "cap_1x"]:
        raise ValueError("variants must be cap_4x followed by cap_1x")
    if [float(v["max_leverage"]) for v in variants] != [4.0, 1.0]:
        raise ValueError("max_leverage values must be 4.0 and 1.0")
    return doc


def period_masks(index: pd.DatetimeIndex, base: dict[str, Any]) -> dict[str, Any]:
    cut = pd.Timestamp(base["evaluation_start"])
    end = pd.Timestamp(base["evaluation_end"])
    return {
        "full_sample": index <= end,
        "pre_publication": (index < cut) & (index <= end),
        "post_publication": (index >= cut) & (index <= end),
    }


def cagr(returns: pd.Series) -> float:
    returns = returns.dropna()
    wealth = float((1.0 + returns).prod())
    years = max(
        (returns.index.max() - returns.index.min()).days / 365.2425,
        1 / 365.2425)
    return wealth ** (1.0 / years) - 1.0 if wealth > 0 else np.nan


def summarize(
        variant: dict[str, Any], result: pd.DataFrame,
        base: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for period, mask in period_masks(result.index, base).items():
        x = result.loc[mask & result["is_evaluation"].astype(bool)].copy()
        ret = x["ret"].fillna(0.0)
        trading_only = ret - x["cash_interest"] / x["prev_aum"]
        wealth = (1.0 + ret).cumprod()
        std = float(ret.std())
        hurdle = x["cash_hurdle_ret"].fillna(0.0)
        gross_integral = (
            x["long_notional_minute_dollars"]
            + x["short_notional_minute_dollars"])
        held_denominator = x["prev_aum"] * x["holding_minutes"]
        scheduled_minutes = (
            x["intraday_daycount_fraction"] * 360.0 * 24.0 * 60.0)
        rows.append({
            "variant_id": variant["id"], "label": variant["label"],
            "max_leverage": float(variant["max_leverage"]), "period": period,
            "first": str(x.index.min().date()), "last": str(x.index.max().date()),
            "evaluation_sessions": int(len(x)),
            "active_sessions": int(x["status"].eq("active").sum()),
            "total_return": float(wealth.iloc[-1] - 1.0),
            "cagr": cagr(ret), "trading_only_cagr": cagr(trading_only),
            "annual_volatility": std * np.sqrt(252.0),
            "sharpe_calendar": (
                float((ret - hurdle).mean() / std * np.sqrt(252.0))
                if std > 0 else np.nan),
            "max_drawdown": float((wealth / wealth.cummax() - 1.0).min()),
            "worst_day": float(ret.min()),
            "gross_pnl": float(x["gross"].sum()),
            "execution_cost": float(x["cost"].sum()),
            "cash_interest": float(x["cash_interest"].sum()),
            "funding_and_borrow_cost": float(-x["financing"].sum()),
            "net_pnl": float(x["net"].sum()),
            "shares_traded": float(x["shares_traded"].sum()),
            "traded_notional": float(x["traded_notional"].sum()),
            "held_minute_weighted_gross_leverage": (
                float(gross_integral.sum() / held_denominator.sum())
                if held_denominator.sum() > 0 else np.nan),
            "scheduled_minute_weighted_gross_exposure": float(
                gross_integral.sum()
                / (x["prev_aum"] * scheduled_minutes).sum()),
        })
    return rows


def add_deltas(summary: pd.DataFrame) -> pd.DataFrame:
    baseline = summary.loc[summary["variant_id"].eq("cap_4x")].set_index("period")
    for column in (
        "total_return", "cagr", "trading_only_cagr", "annual_volatility",
        "sharpe_calendar", "max_drawdown", "worst_day", "net_pnl",
        "held_minute_weighted_gross_leverage",
        "scheduled_minute_weighted_gross_exposure",
    ):
        summary[f"delta_{column}"] = summary.apply(
            lambda row: row[column] - baseline.loc[row["period"], column], axis=1)
    return summary


def audit(results: dict[str, pd.DataFrame], base: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for variant_id, result in results.items():
        net_residual = (
            result["net"] - result["gross"] + result["cost"]
            - result["cash_interest"] - result["financing"]
        ).abs().max()
        aum_residual = (
            result["aum"] - result["prev_aum"] - result["net"]
        ).abs().max()
        if max(net_residual, aum_residual) > 1e-8:
            raise RuntimeError(f"{variant_id} accounting identity failed")
        out[variant_id] = {
            "max_net_identity_residual": float(net_residual),
            "max_aum_identity_residual": float(aum_residual),
            "aum_stayed_positive": bool((result["aum"] > 0).all()),
            "minimum_aum": float(result["aum"].min()),
        }

    formal_dir = (ROOT / base["formal_baseline_run"]).resolve()
    formal = pd.read_parquet(formal_dir / "daily_results.parquet")
    formal = formal.loc[formal["cell_id"].eq(base["formal_baseline_cell"])]
    formal["session_date"] = pd.to_datetime(formal["session_date"]).dt.normalize()
    formal = formal.set_index("session_date").sort_index()
    baseline = results["cap_4x"]
    columns = [
        "gross", "cost", "cash_interest", "financing", "net",
        "shares_traded", "traded_notional", "prev_aum", "aum", "ret",
    ]
    if not baseline.index.equals(formal.index):
        raise RuntimeError("4x baseline index differs from formal run")
    max_difference = float((baseline[columns] - formal[columns]).abs().max().max())
    if max_difference > 1e-8:
        raise RuntimeError("4x baseline differs from formal run")
    out["formal_baseline"] = {
        "run": str(formal_dir), "cell": base["formal_baseline_cell"],
        "max_absolute_numeric_difference": max_difference,
    }
    return out


def daily_comparison(
        results: dict[str, pd.DataFrame], target_leverage: pd.Series) -> pd.DataFrame:
    frames = []
    baseline = results["cap_4x"]
    for variant_id, result in results.items():
        frame = result.copy()
        frame.attrs = {}
        cap = 4.0 if variant_id == "cap_4x" else 1.0
        frame["target_leverage_uncapped"] = target_leverage.reindex(frame.index)
        frame["sizing_leverage"] = frame["target_leverage_uncapped"].clip(upper=cap)
        frame["held_gross_leverage"] = (
            (frame["long_notional_minute_dollars"]
             + frame["short_notional_minute_dollars"])
            / (frame["prev_aum"] * frame["holding_minutes"])
        ).where(frame["holding_minutes"] > 0)
        frame["variant_id"] = variant_id
        frame["delta_ret_vs_4x"] = frame["ret"] - baseline["ret"]
        frames.append(frame.reset_index())
    return pd.concat(frames, ignore_index=True)


def leverage_diagnostics(
        results: dict[str, pd.DataFrame], target: pd.Series,
        base: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for period, mask in period_masks(target.index, base).items():
        for variant_id, result in results.items():
            eligible = mask & result["is_evaluation"].astype(bool)
            active = eligible & result["holding_minutes"].gt(0)
            held_rows = result.loc[active]
            held = (
                (held_rows["long_notional_minute_dollars"]
                 + held_rows["short_notional_minute_dollars"])
                / (held_rows["prev_aum"] * held_rows["holding_minutes"])
            )
            target_x = target.loc[eligible].dropna()
            scheduled_minutes = (
                result.loc[eligible, "intraday_daycount_fraction"]
                * 360.0 * 24.0 * 60.0)
            scheduled_denominator = (
                result.loc[eligible, "prev_aum"] * scheduled_minutes).sum()
            rows.append({
                "variant_id": variant_id, "period": period,
                "eligible_sessions": int(eligible.sum()),
                "active_position_sessions": int(active.sum()),
                "target_leverage_median": float(target_x.median()),
                "share_of_sessions_target_above_1x": float(target_x.gt(1.0).mean()),
                "share_of_active_days_target_above_1x": float(
                    target.loc[active].gt(1.0).mean()),
                "held_gross_leverage_median": float(held.median()),
                "held_gross_leverage_p95": float(held.quantile(0.95)),
                "held_gross_leverage_max": float(held.max()),
                "held_gross_leverage_max_date": str(held.idxmax().date()),
                "borrowed_cash_minute_dollars": float(
                    result.loc[eligible, "borrowed_cash_minute_dollars"].sum()),
                "scheduled_minute_weighted_borrowed_cash": float(
                    result.loc[eligible, "borrowed_cash_minute_dollars"].sum()
                    / scheduled_denominator),
            })
    return pd.DataFrame(rows)


def annual_comparison(results: dict[str, pd.DataFrame]) -> pd.DataFrame:
    returns = pd.DataFrame({
        variant_id: result.loc[
            result["is_evaluation"].astype(bool), "ret"].fillna(0.0)
        for variant_id, result in results.items()
    })
    annual = (1.0 + returns).groupby(returns.index.year).prod() - 1.0
    annual.index.name = "year"
    annual["delta_1x_minus_4x"] = annual["cap_1x"] - annual["cap_4x"]
    return annual.reset_index()


def pct(value: Any) -> str:
    return "—" if pd.isna(value) else f"{100 * float(value):.2f}%"


def render_report(
        summary: pd.DataFrame, leverage: pd.DataFrame,
        annual: pd.DataFrame, manifest: dict[str, Any]) -> tuple[str, str]:
    def row(variant: str, period: str) -> pd.Series:
        return summary.loc[
            summary["variant_id"].eq(variant)
            & summary["period"].eq(period)].iloc[0]

    table_rows = []
    for period in ("full_sample", "pre_publication", "post_publication"):
        four, one = row("cap_4x", period), row("cap_1x", period)
        table_rows.append(
            f"| {period} | {pct(four['cagr'])} | {pct(one['cagr'])} | "
            f"{100 * one['delta_cagr']:+.2f}pp | {pct(four['trading_only_cagr'])} | "
            f"{pct(one['trading_only_cagr'])} | {four['sharpe_calendar']:.2f} | "
            f"{one['sharpe_calendar']:.2f} | {pct(four['max_drawdown'])} | "
            f"{pct(one['max_drawdown'])} |")
    post4, post1 = row("cap_4x", "post_publication"), row("cap_1x", "post_publication")
    lev1 = leverage.loc[
        leverage["variant_id"].eq("cap_1x")
        & leverage["period"].eq("full_sample")].iloc[0]
    best = annual.loc[annual["delta_1x_minus_4x"].idxmax()]
    worst = annual.loc[annual["delta_1x_minus_4x"].idxmin()]
    md = f"""# 不使用杠杆（1x 上限）：敏感性实验 v1

本实验将仓位公式从 `min(2% / 前14日波动率, 4)` 改为
`min(2% / 前14日波动率, 1)`。任何 sizing-time gross notional 均不超过 AUM；
高波动日仍可低于 1x。其他条件保持当前主要经济展示口径不变。

| 时段 | 4x Portfolio CAGR | 1x Portfolio CAGR | CAGR 变化 | 4x Trading-only CAGR | 1x Trading-only CAGR | 4x Sharpe | 1x Sharpe | 4x MDD | 1x MDD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(table_rows)}

Post-publication portfolio CAGR 从 {pct(post4['cagr'])} 变为 {pct(post1['cagr'])}；
扣除现金利息后的 same-path trading-only CAGR 从
{pct(post4['trading_only_cagr'])} 变为 {pct(post1['trading_only_cagr'])}。
年化波动从 {pct(post4['annual_volatility'])} 变为
{pct(post1['annual_volatility'])}，最差单日从 {pct(post4['worst_day'])}
变为 {pct(post1['worst_day'])}。

Full sample 中有 {pct(lev1['share_of_sessions_target_above_1x'])} 的合格 session
原始波动目标要求超过 1x，因此 1x 上限会频繁生效。1x 路径有持仓时的日内
gross leverage 中位数 / P95 / 最大值为
{lev1['held_gross_leverage_median']:.2f}x / {lev1['held_gross_leverage_p95']:.2f}x /
{lev1['held_gross_leverage_max']:.2f}x。1x 是按开盘价计算股数；next-open 成交价和
持仓后的价格变化会令实际 gross exposure 短暂略高于 1，full-sample 全时段加权的
临时负现金仅为 AUM 的 {pct(lev1['scheduled_minute_weighted_borrowed_cash'])}。
因此这不是“每一分钟强制零负现金”的动态去杠杆模型。

年度影响最大改善为 {int(best['year'])} 年
{100 * best['delta_1x_minus_4x']:+.2f}pp，最大恶化为 {int(worst['year'])} 年
{100 * worst['delta_1x_minus_4x']:+.2f}pp。

这是 post-result sizing sensitivity，不修改 frozen v2。未运行 HAC/block bootstrap；
market impact、queue 和 partial fill 仍未建模。

## 审计

- run：`{manifest['run_id']}`
- 数据：2008-01-22 至 {manifest['sample']['end']}
- 4x baseline 对正式 run 最大数值差：{manifest['audit']['formal_baseline']['max_absolute_numeric_difference']:.3g}
- Git commit：`{manifest['git']['commit']}`；运行时 dirty：`{str(manifest['git']['dirty']).lower()}`
"""
    annual_show = annual.copy()
    for column in ("cap_4x", "cap_1x", "delta_1x_minus_4x"):
        annual_show[column] = annual_show[column].map(pct)
    provenance = html.escape(json.dumps(manifest, indent=2), quote=False)
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>1x 无杠杆敏感性</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;background:#f5f7fb;color:#172033;margin:0}}main{{max-width:1150px;margin:auto;padding:28px}}section{{background:#fff;border:1px solid #dfe5ef;border-radius:12px;padding:20px;margin:16px 0}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid #e8edf5;text-align:right}}th:first-child,td:first-child{{text-align:left}}pre{{white-space:pre-wrap;background:#111827;color:#dbeafe;padding:14px;border-radius:8px}}.scroll{{overflow:auto}}</style></head><body><main>
<h1>不使用杠杆（1x 上限）</h1><section><pre>{html.escape(md)}</pre></section>
<section><h2>年度收益</h2><div class="scroll">{annual_show.to_html(index=False, border=0)}</div></section>
<section><h2>Provenance</h2><pre>{provenance}</pre></section></main></body></html>"""
    return md, page


def run(args: argparse.Namespace) -> Path:
    config_path = args.config.resolve()
    doc = load_config(config_path)
    base = doc["base"]
    base_cfg = engine.profile_cfg(
        base["profile"], tier=base["tier"],
        ignore_dividends=base["dividend_mode"] == "ignore_dividends",
        slip_per_share=float(base["slippage_per_share"]),
        target_vol=float(base["target_daily_volatility"]), max_lev=4.0)
    data_release = (ROOT / base["data_release"]).resolve()
    financing_path = (ROOT / base["financing_rates"]).resolve()
    data = engine.load_run(data_release, base_cfg)
    financing = pd.read_csv(financing_path, parse_dates=["session_date"])

    results: dict[str, pd.DataFrame] = {}
    summary_rows = []
    target_leverage: pd.Series | None = None
    for variant in doc["variants"]:
        cfg = engine.profile_cfg(
            base["profile"], tier=base["tier"],
            ignore_dividends=base["dividend_mode"] == "ignore_dividends",
            slip_per_share=float(base["slippage_per_share"]),
            target_vol=float(base["target_daily_volatility"]),
            max_lev=float(variant["max_leverage"]))
        result = engine.backtest(
            data, cfg, collect_ledger=True, financing_rates=financing)
        if target_leverage is None:
            dvol = result.attrs["daily_features"]["dvol"]
            target_leverage = float(base["target_daily_volatility"]) / dvol
        results[variant["id"]] = result
        summary_rows.extend(summarize(variant, result, base))
        print(f"completed {variant['id']}", flush=True)
    assert target_leverage is not None

    summary = add_deltas(pd.DataFrame(summary_rows))
    daily = daily_comparison(results, target_leverage)
    leverage = leverage_diagnostics(results, target_leverage, base)
    annual = annual_comparison(results)
    audit_result = audit(results, base)

    run_id = args.run_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_no_leverage_v1")
    output_root = ROOT / "experiments" / "no_leverage_v1" / "results"
    output = output_root / run_id
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    staging = output_root / f".staging-{run_id}-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        manifest = {
            "run_id": run_id, "experiment_id": doc["experiment_id"],
            "classification": doc["classification"],
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "sample": {"start": "2008-01-22", "end": base["evaluation_end"],
                       "post_publication_start": base["evaluation_start"]},
            "base_config": base, "variants": doc["variants"],
            "git": git_state(), "audit": audit_result,
            "hashes": {
                "config_sha256": sha256(config_path),
                "runner_sha256": sha256(SCRIPT_PATH),
                "engine_sha256": sha256(ROOT / "im_engine_v4.py"),
                "data_release_manifest_sha256": sha256(
                    data_release / "data_manifest.json"),
                "financing_sha256": sha256(financing_path),
            },
            "coverage": {"summary_rows": len(summary), "daily_rows": len(daily),
                         "annual_rows": len(annual)},
        }
        md, page = render_report(summary, leverage, annual, manifest)
        summary.to_csv(staging / "summary.csv", index=False)
        leverage.to_csv(staging / "leverage_diagnostics.csv", index=False)
        annual.to_csv(staging / "annual_returns.csv", index=False)
        daily.to_parquet(staging / "daily_comparison.parquet", index=False)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        (staging / "report.md").write_text(md, encoding="utf-8")
        (staging / "report.html").write_text(page, encoding="utf-8")
        (staging / "_SUCCESS").write_text(json.dumps({
            "run_id": run_id,
            "manifest_sha256": sha256(staging / "manifest.json"),
        }, sort_keys=True) + "\n", encoding="utf-8")
        output_root.mkdir(parents=True, exist_ok=True)
        os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--run-id")
    return parser.parse_args()


if __name__ == "__main__":
    destination = run(parse_args())
    print(f"published: {destination}")
