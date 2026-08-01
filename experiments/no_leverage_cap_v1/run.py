"""Run the isolated no-leverage-cap sizing sensitivity."""

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
    if [v.get("id") for v in variants] != ["cap_4x", "uncapped"]:
        raise ValueError("variants must be cap_4x followed by uncapped")
    if float(variants[0].get("max_leverage")) != 4.0:
        raise ValueError("baseline max_leverage must be 4.0")
    if variants[1].get("max_leverage") is not None:
        raise ValueError("uncapped max_leverage must be null")
    return doc


def period_masks(index: pd.DatetimeIndex, base: dict[str, Any]) -> dict[str, Any]:
    cut = pd.Timestamp(base["evaluation_start"])
    end = pd.Timestamp(base["evaluation_end"])
    return {
        "full_sample": index <= end,
        "pre_publication": (index < cut) & (index <= end),
        "post_publication": (index >= cut) & (index <= end),
    }


def compounded_cagr(returns: pd.Series) -> float:
    returns = returns.dropna()
    if returns.empty:
        return np.nan
    wealth = float((1.0 + returns).prod())
    years = max(
        (returns.index.max() - returns.index.min()).days / 365.2425,
        1 / 365.2425)
    if wealth <= 0:
        return np.nan
    return wealth ** (1.0 / years) - 1.0


def drawdown(returns: pd.Series) -> float:
    wealth = (1.0 + returns.fillna(0.0)).cumprod()
    return float((wealth / wealth.cummax() - 1.0).min())


def summarize(
        variant: dict[str, Any], result: pd.DataFrame,
        base: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for period, mask in period_masks(result.index, base).items():
        sliced = result.loc[mask & result["is_evaluation"].astype(bool)].copy()
        ret = sliced["ret"].fillna(0.0)
        years = max(
            (sliced.index.max() - sliced.index.min()).days / 365.2425,
            1 / 365.2425)
        std = float(ret.std())
        hurdle = sliced["cash_hurdle_ret"].fillna(0.0)
        trading_only_ret = ret - sliced["cash_interest"] / sliced["prev_aum"]
        gross_integral = (
            sliced["long_notional_minute_dollars"]
            + sliced["short_notional_minute_dollars"])
        held_denominator = sliced["prev_aum"] * sliced["holding_minutes"]
        scheduled_minutes = sliced["intraday_daycount_fraction"] * 24.0 * 60.0
        scheduled_denominator = sliced["prev_aum"] * scheduled_minutes
        rows.append({
            "variant_id": variant["id"],
            "label": variant["label"],
            "max_leverage": variant.get("max_leverage"),
            "period": period,
            "first": str(sliced.index.min().date()),
            "last": str(sliced.index.max().date()),
            "evaluation_sessions": int(len(sliced)),
            "active_sessions": int(sliced["status"].eq("active").sum()),
            "total_return": float((1.0 + ret).prod() - 1.0),
            "cagr": compounded_cagr(ret),
            "trading_only_cagr": compounded_cagr(trading_only_ret),
            "annual_volatility": std * np.sqrt(252.0),
            "sharpe_calendar": (
                float((ret - hurdle).mean() / std * np.sqrt(252.0))
                if std > 0 else np.nan),
            "max_drawdown": drawdown(ret),
            "worst_day": float(ret.min()),
            "gross_pnl": float(sliced["gross"].sum()),
            "execution_cost": float(sliced["cost"].sum()),
            "cash_interest": float(sliced["cash_interest"].sum()),
            "funding_and_borrow_cost": float(-sliced["financing"].sum()),
            "net_pnl": float(sliced["net"].sum()),
            "shares_traded": float(sliced["shares_traded"].sum()),
            "traded_notional": float(sliced["traded_notional"].sum()),
            "held_minute_weighted_gross_leverage": (
                float(gross_integral.sum() / held_denominator.sum())
                if held_denominator.sum() > 0 else np.nan),
            "scheduled_minute_weighted_gross_exposure": (
                float(gross_integral.sum() / scheduled_denominator.sum())
                if scheduled_denominator.sum() > 0 else np.nan),
            "calendar_years": years,
        })
    return rows


def add_deltas(summary: pd.DataFrame) -> pd.DataFrame:
    baseline = summary.loc[summary["variant_id"].eq("cap_4x")].set_index("period")
    delta_columns = (
        "total_return", "cagr", "trading_only_cagr", "annual_volatility",
        "sharpe_calendar", "max_drawdown", "worst_day", "net_pnl",
        "held_minute_weighted_gross_leverage",
        "scheduled_minute_weighted_gross_exposure",
    )
    for column in delta_columns:
        summary[f"delta_{column}"] = summary.apply(
            lambda row: row[column] - baseline.loc[row["period"], column], axis=1)
    return summary


def accounting_audit(results: dict[str, pd.DataFrame]) -> dict[str, Any]:
    audit: dict[str, Any] = {}
    for variant_id, result in results.items():
        net_residual = (
            result["net"] - result["gross"] + result["cost"]
            - result["cash_interest"] - result["financing"]
        ).abs().max()
        aum_residual = (
            result["aum"] - result["prev_aum"] - result["net"]
        ).abs().max()
        finite = np.isfinite(result[["prev_aum", "aum", "ret"]].dropna()).all().all()
        positive_aum = bool((result["aum"] > 0).all())
        if net_residual > 1e-8 or aum_residual > 1e-8 or not finite:
            raise RuntimeError(
                f"{variant_id} audit failed: net={net_residual}, "
                f"aum={aum_residual}, finite={finite}")
        audit[variant_id] = {
            "max_net_identity_residual": float(net_residual),
            "max_aum_identity_residual": float(aum_residual),
            "all_reported_values_finite": bool(finite),
            "aum_stayed_positive": positive_aum,
            "minimum_aum": float(result["aum"].min()),
        }
    return audit


def formal_baseline_audit(
        baseline: pd.DataFrame, base: dict[str, Any]) -> dict[str, Any]:
    formal_dir = (ROOT / base["formal_baseline_run"]).resolve()
    formal = pd.read_parquet(formal_dir / "daily_results.parquet")
    formal = formal.loc[
        formal["cell_id"].eq(base["formal_baseline_cell"])].copy()
    formal["session_date"] = pd.to_datetime(formal["session_date"]).dt.normalize()
    formal = formal.set_index("session_date").sort_index()
    common = [
        "gross", "commission", "slippage", "cost", "cash_interest",
        "financing", "net", "shares_traded", "traded_notional", "prev_aum",
        "aum", "ret",
    ]
    if not baseline.index.equals(formal.index):
        raise RuntimeError("baseline date index does not match formal run")
    differences = (baseline[common] - formal[common]).abs()
    max_difference = float(differences.max().max())
    if max_difference > 1e-8:
        raise RuntimeError(
            f"4x baseline differs from formal run by {max_difference}")
    return {
        "formal_run": str(formal_dir),
        "formal_cell": base["formal_baseline_cell"],
        "compared_rows": int(len(formal)),
        "compared_columns": common,
        "max_absolute_numeric_difference": max_difference,
    }


def build_daily_comparison(
        results: dict[str, pd.DataFrame], target_leverage: pd.Series) -> pd.DataFrame:
    frames = []
    baseline = results["cap_4x"]
    for variant_id, result in results.items():
        out = result.copy()
        # Ledgers and daily features are published through dedicated outputs.
        # DataFrame-valued attrs cannot be compared as scalars during concat.
        out.attrs = {}
        out["target_leverage_uncapped"] = target_leverage.reindex(out.index)
        out["sizing_leverage"] = (
            out["target_leverage_uncapped"].clip(upper=4.0)
            if variant_id == "cap_4x" else out["target_leverage_uncapped"])
        out["held_gross_leverage"] = (
            (out["long_notional_minute_dollars"]
             + out["short_notional_minute_dollars"])
            / (out["prev_aum"] * out["holding_minutes"])
        ).where(out["holding_minutes"] > 0)
        out["variant_id"] = variant_id
        out["delta_ret_vs_4x"] = out["ret"] - baseline["ret"]
        out["delta_aum_vs_4x"] = out["aum"] - baseline["aum"]
        frames.append(out.reset_index())
    return pd.concat(frames, ignore_index=True)


def leverage_diagnostics(
        results: dict[str, pd.DataFrame], target_leverage: pd.Series,
        base: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for period, mask in period_masks(target_leverage.index, base).items():
        for variant_id, result in results.items():
            eligible = mask & result["is_evaluation"].astype(bool)
            active = eligible & result["holding_minutes"].gt(0)
            target_all = target_leverage.loc[eligible].dropna()
            target_active = target_leverage.loc[active].dropna()
            observed = result.loc[active]
            held = (
                (observed["long_notional_minute_dollars"]
                 + observed["short_notional_minute_dollars"])
                / (observed["prev_aum"] * observed["holding_minutes"])
            )
            rows.append({
                "variant_id": variant_id,
                "period": period,
                "eligible_sessions": int(eligible.sum()),
                "active_position_sessions": int(active.sum()),
                "target_leverage_median": float(target_all.median()),
                "target_leverage_p95": float(target_all.quantile(0.95)),
                "target_leverage_p99": float(target_all.quantile(0.99)),
                "target_leverage_max": float(target_all.max()),
                "target_leverage_max_date": str(target_all.idxmax().date()),
                "cap_binding_rate_all": float(target_all.gt(4.0).mean()),
                "cap_binding_rate_active": float(target_active.gt(4.0).mean()),
                "held_gross_leverage_median": float(held.median()),
                "held_gross_leverage_p95": float(held.quantile(0.95)),
                "held_gross_leverage_max": float(held.max()),
                "held_gross_leverage_max_date": str(held.idxmax().date()),
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
    annual["delta_uncapped_minus_4x"] = annual["uncapped"] - annual["cap_4x"]
    return annual.reset_index()


def pct(value: Any, digits: int = 2) -> str:
    return "—" if pd.isna(value) else f"{100.0 * float(value):.{digits}f}%"


def num(value: Any, digits: int = 2) -> str:
    return "—" if pd.isna(value) else f"{float(value):,.{digits}f}"


def render_report(
        summary: pd.DataFrame, leverage: pd.DataFrame,
        annual: pd.DataFrame, top_days: pd.DataFrame,
        manifest: dict[str, Any]) -> str:
    def at(variant: str, period: str) -> pd.Series:
        return summary.loc[
            summary["variant_id"].eq(variant)
            & summary["period"].eq(period)].iloc[0]

    rows = []
    for period in ("full_sample", "pre_publication", "post_publication"):
        capped = at("cap_4x", period)
        uncapped = at("uncapped", period)
        rows.append(
            f"| {period} | {pct(capped['cagr'])} | {pct(uncapped['cagr'])} | "
            f"{100 * uncapped['delta_cagr']:+.2f}pp | "
            f"{pct(capped['trading_only_cagr'])} | "
            f"{pct(uncapped['trading_only_cagr'])} | "
            f"{num(capped['sharpe_calendar'])} | "
            f"{num(uncapped['sharpe_calendar'])} | "
            f"{pct(capped['max_drawdown'])} | "
            f"{pct(uncapped['max_drawdown'])} |")
    post_4x = at("cap_4x", "post_publication")
    post_u = at("uncapped", "post_publication")
    full_lev = leverage.loc[
        leverage["variant_id"].eq("uncapped")
        & leverage["period"].eq("full_sample")].iloc[0]
    post_lev = leverage.loc[
        leverage["variant_id"].eq("uncapped")
        & leverage["period"].eq("post_publication")].iloc[0]
    best_year = annual.loc[annual["delta_uncapped_minus_4x"].idxmax()]
    worst_year = annual.loc[annual["delta_uncapped_minus_4x"].idxmin()]
    annual_show = annual.copy()
    for column in ("cap_4x", "uncapped", "delta_uncapped_minus_4x"):
        annual_show[column] = annual_show[column].map(pct)
    provenance = html.escape(
        json.dumps(manifest, indent=2, default=str), quote=False)
    top_table = top_days.to_html(index=False, border=0, classes="data")
    md = f"""# 取消 4 倍杠杆上限：敏感性实验 v1

本实验固定当前主要经济展示的所有其他条件，只把仓位公式从
`min(2% / 前14个合格交易日波动率, 4)` 改为 `2% / 波动率`。这是看过结果后的
仓位敏感性测试，不修改 frozen v2，也不构成新的预注册或可交易建议。

## 主要结果

| 时段 | 4x Portfolio CAGR | 无上限 Portfolio CAGR | CAGR 变化 | 4x Trading-only CAGR | 无上限 Trading-only CAGR | 4x Sharpe | 无上限 Sharpe | 4x MDD | 无上限 MDD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

Post-publication 的 portfolio CAGR 从 {pct(post_4x['cagr'])} 变为
{pct(post_u['cagr'])}，但其中仍包含现金利息；same-path trading-only CAGR 从
{pct(post_4x['trading_only_cagr'])} 变为 {pct(post_u['trading_only_cagr'])}。
年化波动从 {pct(post_4x['annual_volatility'])} 升至
{pct(post_u['annual_volatility'])}，最差单日从 {pct(post_4x['worst_day'])}
变为 {pct(post_u['worst_day'])}。

## 杠杆与风险

- Full sample 有 {pct(full_lev['cap_binding_rate_all'], 1)} 的合格 session 的目标杠杆超过 4x；有持仓日比例为 {pct(full_lev['cap_binding_rate_active'], 1)}。
- Post-publication 对应比例为 {pct(post_lev['cap_binding_rate_all'], 1)} / {pct(post_lev['cap_binding_rate_active'], 1)}。
- 无上限目标杠杆 full-sample P95 / P99 / 最大值为 {num(full_lev['target_leverage_p95'])}x / {num(full_lev['target_leverage_p99'])}x / {num(full_lev['target_leverage_max'])}x；最大值日期为 {full_lev['target_leverage_max_date']}。
- 无上限实际有持仓时的日内 gross leverage，full-sample P95 / 最大值为 {num(full_lev['held_gross_leverage_p95'])}x / {num(full_lev['held_gross_leverage_max'])}x。
- 年度效果不稳定：最大改善是 {int(best_year['year'])} 年 {100 * best_year['delta_uncapped_minus_4x']:+.2f}pp，最大恶化是 {int(worst_year['year'])} 年 {100 * worst_year['delta_uncapped_minus_4x']:+.2f}pp。

## 边界

- 没有加入券商保证金、强平、participation、market impact、queue 或 partial-fill 模型。
- 固定每股佣金/滑点在高杠杆、高成交量下尤其乐观，结果主要用于机械压力测试。
- 各变体按自己的 AUM 连续复利，后续股数会随之前盈亏变化，并非事后线性缩放 4x P&L。
- 只报告点估计；未运行 HAC 或 block bootstrap。

## 审计

- run：`{manifest['run_id']}`
- 数据：2008-01-22 至 {manifest['sample']['end']}
- baseline 对正式 72-cell run 的最大数值差：{manifest['formal_baseline_audit']['max_absolute_numeric_difference']:.3g}
- 会计恒等式最大残差：{max(max(v['max_net_identity_residual'], v['max_aum_identity_residual']) for v in manifest['accounting_audit'].values()):.3g}
- Git commit：`{manifest['git']['commit']}`；运行时 dirty：`{str(manifest['git']['dirty']).lower()}`
"""
    html_report = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>取消 4 倍杠杆上限</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;background:#f5f7fb;color:#172033;margin:0}}main{{max-width:1180px;margin:auto;padding:28px}}section{{background:white;border:1px solid #dfe5ef;border-radius:12px;padding:20px;margin:16px 0}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid #e8edf5;text-align:right}}th:first-child,td:first-child{{text-align:left}}pre{{white-space:pre-wrap;background:#111827;color:#dbeafe;padding:14px;border-radius:8px}}.warn{{color:#745500;background:#fff3cd;padding:7px 11px;border-radius:99px;display:inline-block}}.scroll{{overflow:auto}}</style></head>
<body><main><span class="warn">POST-RESULT SIZING SENSITIVITY</span><h1>取消 4 倍杠杆上限</h1>
<section><h2>结论与口径</h2><pre>{html.escape(md)}</pre></section>
<section><h2>年度收益稳定性</h2><div class="scroll">{annual_show.to_html(index=False, border=0, classes="data")}</div></section>
<section><h2>最高目标杠杆日期</h2><div class="scroll">{top_table}</div></section>
<section><h2>Provenance</h2><pre>{provenance}</pre></section></main></body></html>"""
    return md, html_report


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
    summary_rows: list[dict[str, Any]] = []
    target_leverage: pd.Series | None = None
    for variant in doc["variants"]:
        max_lev = (
            float(variant["max_leverage"])
            if variant["max_leverage"] is not None else float("inf"))
        cfg = engine.profile_cfg(
            base["profile"], tier=base["tier"],
            ignore_dividends=base["dividend_mode"] == "ignore_dividends",
            slip_per_share=float(base["slippage_per_share"]),
            target_vol=float(base["target_daily_volatility"]), max_lev=max_lev)
        result = engine.backtest(
            data, cfg, collect_ledger=True, financing_rates=financing)
        if target_leverage is None:
            dvol = result.attrs["daily_features"]["dvol"]
            if (~np.isfinite(dvol.dropna()) | (dvol.dropna() <= 0)).any():
                raise RuntimeError("uncapped sizing requires finite positive dvol")
            target_leverage = float(base["target_daily_volatility"]) / dvol
        results[variant["id"]] = result
        summary_rows.extend(summarize(variant, result, base))
        print(f"completed {variant['id']}", flush=True)
    assert target_leverage is not None

    summary = add_deltas(pd.DataFrame(summary_rows))
    daily = build_daily_comparison(results, target_leverage)
    leverage = leverage_diagnostics(results, target_leverage, base)
    annual = annual_comparison(results)
    top_days = daily.loc[daily["variant_id"].eq("uncapped")].nlargest(
        20, "target_leverage_uncapped")[[
            "session_date", "target_leverage_uncapped", "sizing_leverage",
            "held_gross_leverage", "ret", "delta_ret_vs_4x", "aum",
        ]]
    audit = accounting_audit(results)
    formal_audit = formal_baseline_audit(results["cap_4x"], base)

    run_id = args.run_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_no_leverage_cap_v1")
    output_root = ROOT / "experiments" / "no_leverage_cap_v1" / "results"
    output = output_root / run_id
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    staging = output_root / f".staging-{run_id}-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        manifest = {
            "run_id": run_id,
            "classification": doc["classification"],
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "experiment_id": doc["experiment_id"],
            "sample": {"start": "2008-01-22", "end": base["evaluation_end"],
                       "post_publication_start": base["evaluation_start"]},
            "base_config": base,
            "variants": doc["variants"],
            "git": git_state(),
            "hashes": {
                "config_sha256": sha256(config_path),
                "runner_sha256": sha256(SCRIPT_PATH),
                "engine_sha256": sha256(ROOT / "im_engine_v4.py"),
                "data_release_manifest_sha256": sha256(
                    data_release / "data_manifest.json"),
                "financing_sha256": sha256(financing_path),
            },
            "coverage": {"summary_rows": int(len(summary)),
                         "daily_rows": int(len(daily))},
            "formal_baseline_audit": formal_audit,
            "accounting_audit": audit,
            "limitations": [
                "no market impact or participation model",
                "no broker margin or forced-liquidation model",
                "point estimates only; HAC and block bootstrap not run",
            ],
        }
        md, report_html = render_report(
            summary, leverage, annual, top_days, manifest)
        summary.to_csv(staging / "summary.csv", index=False)
        leverage.to_csv(staging / "leverage_diagnostics.csv", index=False)
        annual.to_csv(staging / "annual_returns.csv", index=False)
        top_days.to_csv(staging / "top_uncapped_leverage_days.csv", index=False)
        daily.to_parquet(staging / "daily_comparison.parquet", index=False)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
        (staging / "report.md").write_text(md, encoding="utf-8")
        (staging / "report.html").write_text(report_html, encoding="utf-8")
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
