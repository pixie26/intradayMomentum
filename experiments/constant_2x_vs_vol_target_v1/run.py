"""Compare constant 2x sizing with paper-style volatility targeting."""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
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
from experiments.no_leverage_v1 import run as common  # noqa: E402

SCRIPT_PATH = Path(__file__).resolve()
DEFAULT_CONFIG = SCRIPT_PATH.with_name("config.yml")


def load_config(path: Path) -> dict[str, Any]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("experiment config must be a mapping")
    variants = doc.get("variants", [])
    expected = ["cap_4x", "constant_2x", "constant_2x_independent"]
    if [v.get("id") for v in variants] != expected:
        raise ValueError(f"variants must be {expected}")
    if variants[0].get("sizing") != "vol_target":
        raise ValueError("cap_4x must use vol_target sizing")
    if any(v.get("sizing") != "flat" for v in variants[1:]):
        raise ValueError("both constant_2x variants must use flat sizing")
    return doc


def daily_comparison(
        results: dict[str, pd.DataFrame], target: pd.Series) -> pd.DataFrame:
    frames = []
    baseline = results["cap_4x"]
    for variant_id, result in results.items():
        frame = result.copy()
        frame.attrs = {}
        frame["paper_target_leverage_uncapped"] = target.reindex(frame.index)
        frame["sizing_leverage"] = (
            frame["paper_target_leverage_uncapped"].clip(upper=4.0)
            if variant_id == "cap_4x" else 2.0)
        frame["held_gross_leverage"] = (
            (frame["long_notional_minute_dollars"]
             + frame["short_notional_minute_dollars"])
            / (frame["prev_aum"] * frame["holding_minutes"])
        ).where(frame["holding_minutes"] > 0)
        frame["variant_id"] = variant_id
        frame["delta_ret_vs_paper"] = frame["ret"] - baseline["ret"]
        frames.append(frame.reset_index())
    return pd.concat(frames, ignore_index=True)


def annual_comparison(results: dict[str, pd.DataFrame]) -> pd.DataFrame:
    returns = pd.DataFrame({
        variant_id: result.loc[
            result["is_evaluation"].astype(bool), "ret"].fillna(0.0)
        for variant_id, result in results.items()
    })
    annual = (1.0 + returns).groupby(returns.index.year).prod() - 1.0
    annual.index.name = "year"
    annual["delta_constant_2x_minus_paper"] = (
        annual["constant_2x"] - annual["cap_4x"])
    annual["delta_independent_minus_paper"] = (
        annual["constant_2x_independent"] - annual["cap_4x"])
    return annual.reset_index()


def start_year_sensitivity(results: dict[str, pd.DataFrame]) -> pd.DataFrame:
    returns = pd.DataFrame({
        variant_id: result.loc[
            result["is_evaluation"].astype(bool), "ret"].fillna(0.0)
        for variant_id, result in results.items()
    })
    rows = []
    final_year = int(returns.index.max().year)
    for start_year in range(int(returns.index.min().year), final_year - 1):
        start = pd.Timestamp(f"{start_year}-01-01")
        sliced = returns.loc[returns.index >= start]
        years = max(
            (sliced.index.max() - sliced.index.min()).days / 365.2425,
            1 / 365.2425)
        cagrs = (1.0 + sliced).prod() ** (1.0 / years) - 1.0
        rows.append({
            "start_year": start_year,
            "first_session": str(sliced.index.min().date()),
            "last_session": str(sliced.index.max().date()),
            "paper_cagr": float(cagrs["cap_4x"]),
            "constant_2x_cagr": float(cagrs["constant_2x"]),
            "constant_2x_independent_cagr": float(
                cagrs["constant_2x_independent"]),
            "delta_constant_2x_minus_paper": float(
                cagrs["constant_2x"] - cagrs["cap_4x"]),
        })
    return pd.DataFrame(rows)


def regime_attribution(
        results: dict[str, pd.DataFrame], target: pd.Series,
        base: dict[str, Any]) -> pd.DataFrame:
    paper = results["cap_4x"]
    constant = results["constant_2x"]
    paper_sizing = target.clip(upper=4.0)
    regimes = {
        "paper_below_2x": paper_sizing.lt(2.0),
        "paper_2x_to_below_4x": paper_sizing.ge(2.0) & paper_sizing.lt(4.0),
        "paper_at_4x_cap": paper_sizing.ge(4.0),
    }
    rows = []
    for period, period_mask in common.period_masks(paper.index, base).items():
        common_eval = (
            period_mask & paper["is_evaluation"].astype(bool)
            & constant["is_evaluation"].astype(bool))
        paper_trade = (
            paper["ret"].fillna(0.0)
            - paper["cash_interest"] / paper["prev_aum"])
        constant_trade = (
            constant["ret"].fillna(0.0)
            - constant["cash_interest"] / constant["prev_aum"])
        for regime, regime_mask in regimes.items():
            mask = common_eval & regime_mask.reindex(paper.index).fillna(False)
            p = paper.loc[mask, "ret"].fillna(0.0)
            c = constant.loc[mask, "ret"].fillna(0.0)
            log_relative = np.log1p(c) - np.log1p(p)
            log_relative_trade = (
                np.log1p(constant_trade.loc[mask])
                - np.log1p(paper_trade.loc[mask]))
            rows.append({
                "period": period, "paper_sizing_regime": regime,
                "sessions": int(mask.sum()),
                "active_sessions": int(
                    (paper.loc[mask, "holding_minutes"] > 0).sum()),
                "paper_sizing_leverage_median": float(
                    paper_sizing.loc[mask].median()),
                "paper_simple_return_sum": float(p.sum()),
                "constant_2x_simple_return_sum": float(c.sum()),
                "constant_minus_paper_simple_return_sum": float(c.sum() - p.sum()),
                "log_relative_wealth_contribution": float(log_relative.sum()),
                "relative_wealth_constant_vs_paper": float(
                    np.expm1(log_relative.sum())),
                "trading_only_log_relative_contribution": float(
                    log_relative_trade.sum()),
            })
    return pd.DataFrame(rows)


def sizing_diagnostics(
        results: dict[str, pd.DataFrame], target: pd.Series,
        base: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for period, period_mask in common.period_masks(target.index, base).items():
        for variant_id, result in results.items():
            mask = period_mask & result["is_evaluation"].astype(bool)
            active = mask & result["holding_minutes"].gt(0)
            held_rows = result.loc[active]
            held = (
                (held_rows["long_notional_minute_dollars"]
                 + held_rows["short_notional_minute_dollars"])
                / (held_rows["prev_aum"] * held_rows["holding_minutes"])
            )
            scheduled_minutes = (
                result.loc[mask, "intraday_daycount_fraction"]
                * 360.0 * 24.0 * 60.0)
            scheduled_denominator = (
                result.loc[mask, "prev_aum"] * scheduled_minutes).sum()
            rows.append({
                "variant_id": variant_id, "period": period,
                "eligible_sessions": int(mask.sum()),
                "active_position_sessions": int(active.sum()),
                "held_gross_leverage_median": float(held.median()),
                "held_gross_leverage_p95": float(held.quantile(0.95)),
                "held_gross_leverage_max": float(held.max()),
                "scheduled_minute_weighted_gross_exposure": float(
                    (result.loc[mask, "long_notional_minute_dollars"]
                     + result.loc[mask, "short_notional_minute_dollars"]).sum()
                    / scheduled_denominator),
                "scheduled_minute_weighted_borrowed_cash": float(
                    result.loc[mask, "borrowed_cash_minute_dollars"].sum()
                    / scheduled_denominator),
            })
    return pd.DataFrame(rows)


def eligibility_audit(results: dict[str, pd.DataFrame]) -> dict[str, Any]:
    paper = results["cap_4x"]
    constant = results["constant_2x"]
    independent = results["constant_2x_independent"]
    mismatch = paper["is_evaluation"].astype(bool) != constant["is_evaluation"].astype(bool)
    status_mismatch = paper["status"].astype(str) != constant["status"].astype(str)
    holding_mismatch = paper["holding_minutes"].gt(0) != constant["holding_minutes"].gt(0)
    independent_status_mismatch = (
        paper["status"].astype(str) != independent["status"].astype(str))
    independent_extra_trades = (
        independent["holding_minutes"].gt(0) & paper["holding_minutes"].eq(0))
    if mismatch.any():
        dates = [str(x.date()) for x in paper.index[mismatch][:10]]
        raise RuntimeError(
            f"evaluation eligibility differs on {int(mismatch.sum())} sessions: {dates}")
    if holding_mismatch.any():
        dates = [str(x.date()) for x in paper.index[holding_mismatch][:10]]
        raise RuntimeError(
            f"same-eligibility 2x trade participation differs on "
            f"{int(holding_mismatch.sum())} sessions: {dates}")
    return {
        "evaluation_mask_mismatch_sessions": int(mismatch.sum()),
        "status_mismatch_sessions": int(status_mismatch.sum()),
        "status_mismatch_dates_first_10": [
            str(x.date()) for x in paper.index[status_mismatch][:10]],
        "holding_presence_mismatch_sessions": int(holding_mismatch.sum()),
        "independent_status_mismatch_sessions": int(
            independent_status_mismatch.sum()),
        "independent_extra_trade_sessions": int(independent_extra_trades.sum()),
        "independent_extra_trade_dates": [
            str(x.date()) for x in paper.index[independent_extra_trades]],
    }


def pct(value: Any) -> str:
    return "—" if pd.isna(value) else f"{100 * float(value):.2f}%"


def render_report(
        summary: pd.DataFrame, annual: pd.DataFrame,
        regimes: pd.DataFrame, sizing: pd.DataFrame,
        start_years: pd.DataFrame, daily: pd.DataFrame,
        manifest: dict[str, Any]) -> tuple[str, str]:
    def row(variant: str, period: str) -> pd.Series:
        return summary.loc[
            summary["variant_id"].eq(variant)
            & summary["period"].eq(period)].iloc[0]

    lines = []
    for period in ("full_sample", "pre_publication", "post_publication"):
        paper, constant = row("cap_4x", period), row("constant_2x", period)
        lines.append(
            f"| {period} | {pct(paper['cagr'])} | {pct(constant['cagr'])} | "
            f"{100 * constant['delta_cagr']:+.2f}pp | "
            f"{pct(paper['trading_only_cagr'])} | "
            f"{pct(constant['trading_only_cagr'])} | "
            f"{paper['sharpe_calendar']:.2f} | {constant['sharpe_calendar']:.2f} | "
            f"{pct(paper['max_drawdown'])} | {pct(constant['max_drawdown'])} |")
    post_p, post_c = row("cap_4x", "post_publication"), row("constant_2x", "post_publication")
    post_i = row("constant_2x_independent", "post_publication")
    full_size = sizing.loc[sizing["period"].eq("full_sample")].set_index("variant_id")
    post_regime = regimes.loc[regimes["period"].eq("post_publication")].set_index(
        "paper_sizing_regime")
    best = annual.loc[annual["delta_constant_2x_minus_paper"].idxmax()]
    worst = annual.loc[annual["delta_constant_2x_minus_paper"].idxmin()]
    start_2009 = start_years.loc[start_years["start_year"].eq(2009)].iloc[0]
    constant_start_wins = int(
        start_years["delta_constant_2x_minus_paper"].gt(0).sum())
    strict_daily = daily.loc[daily["variant_id"].isin(
        ["cap_4x", "constant_2x"])]
    worst_days = strict_daily.loc[
        strict_daily.groupby("variant_id")["ret"].idxmin()
    ].set_index("variant_id")
    md = f"""# 恒定 2x vs Paper 波动率动态定仓

唯一改变是仓位规则：Paper 使用 `min(2% / 前14日波动率, 4x)`，对照组长期使用
恒定 2x。信号、执行、成本、融资、分红、tier 和连续 AUM 路径保持一致。

| 时段 | Paper CAGR | 恒定2x CAGR | CAGR变化 | Paper Trading-only | 恒定2x Trading-only | Paper Sharpe | 恒定2x Sharpe | Paper MDD | 恒定2x MDD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(lines)}

Post-publication：portfolio CAGR 为 {pct(post_p['cagr'])} vs
{pct(post_c['cagr'])}，trading-only CAGR 为 {pct(post_p['trading_only_cagr'])}
vs {pct(post_c['trading_only_cagr'])}；年化波动为
{pct(post_p['annual_volatility'])} vs {pct(post_c['annual_volatility'])}。

Full sample 有持仓时实际 gross leverage 中位数：Paper
{full_size.loc['cap_4x','held_gross_leverage_median']:.2f}x，恒定 2x
{full_size.loc['constant_2x','held_gross_leverage_median']:.2f}x。

恒定 2x 的 full-sample 最差单日为
{worst_days.loc['constant_2x','session_date'].date()} 的
{pct(worst_days.loc['constant_2x','ret'])}；Paper 的最差单日为
{worst_days.loc['cap_4x','session_date'].date()} 的
{pct(worst_days.loc['cap_4x','ret'])}。恒定 2x 在高波动时不会自动降仓，
因此虽然最大回撤较浅，单日尾部风险反而更大。

Post relative-wealth 分解（正数表示恒定 2x 优于 Paper）：

- Paper 目标低于 2x：{pct(post_regime.loc['paper_below_2x','relative_wealth_constant_vs_paper'])}
- Paper 目标 2x–<4x：{pct(post_regime.loc['paper_2x_to_below_4x','relative_wealth_constant_vs_paper'])}
- Paper 撞到 4x 上限：{pct(post_regime.loc['paper_at_4x_cap','relative_wealth_constant_vs_paper'])}

年度差异最大改善为 {int(best['year'])} 年
{100 * best['delta_constant_2x_minus_paper']:+.2f}pp，最大恶化为
{int(worst['year'])} 年 {100 * worst['delta_constant_2x_minus_paper']:+.2f}pp。

逐年移动回测起点后，恒定 2x 仅在 {constant_start_wins}/{len(start_years)} 个起点胜出，
即包含 2008 危机期的完整起点。从 2009 开始，Paper / 恒定 2x CAGR 为
{pct(start_2009['paper_cagr'])} / {pct(start_2009['constant_2x_cagr'])}。
因此 full-sample 的恒定 2x 小幅领先高度依赖 2008，不能视为稳定长期优势。

不要求 dvol 的 operational 恒定 2x 版本，post CAGR 为 {pct(post_i['cagr'])}，
与同资格恒定 2x 的差异来自
{manifest['eligibility_audit']['independent_extra_trade_sessions']} 个额外交易日；
它只作为数据可用性敏感性，不用于判断波动率择时本身。

这是 post-result sizing-rule sensitivity，不修改 frozen v2；HAC/block bootstrap、
market impact、queue 和 partial fill 未建模。

## 审计

- run：`{manifest['run_id']}`
- 4x baseline 对正式 run 最大数值差：{manifest['audit']['formal_baseline']['max_absolute_numeric_difference']:.3g}
- 两种仓位规则 evaluation mask 差异：{manifest['eligibility_audit']['evaluation_mask_mismatch_sessions']} sessions
- Git commit：`{manifest['git']['commit']}`；运行时 dirty：`{str(manifest['git']['dirty']).lower()}`
"""
    annual_show = annual.copy()
    for column in (
        "cap_4x", "constant_2x", "constant_2x_independent",
        "delta_constant_2x_minus_paper", "delta_independent_minus_paper",
    ):
        annual_show[column] = annual_show[column].map(pct)
    regime_show = regimes.copy()
    for column in (
        "paper_simple_return_sum", "constant_2x_simple_return_sum",
        "constant_minus_paper_simple_return_sum",
        "relative_wealth_constant_vs_paper",
    ):
        regime_show[column] = regime_show[column].map(pct)
    start_show = start_years.copy()
    for column in (
        "paper_cagr", "constant_2x_cagr", "constant_2x_independent_cagr",
        "delta_constant_2x_minus_paper",
    ):
        start_show[column] = start_show[column].map(pct)
    provenance = html.escape(json.dumps(manifest, indent=2), quote=False)
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>恒定2x vs Paper动态定仓</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;background:#f5f7fb;color:#172033;margin:0}}main{{max-width:1200px;margin:auto;padding:28px}}section{{background:#fff;border:1px solid #dfe5ef;border-radius:12px;padding:20px;margin:16px 0}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid #e8edf5;text-align:right}}th:first-child,td:first-child{{text-align:left}}pre{{white-space:pre-wrap;background:#111827;color:#dbeafe;padding:14px;border-radius:8px}}.scroll{{overflow:auto}}</style></head><body><main>
<h1>恒定 2x vs Paper 波动率动态定仓</h1><section><pre>{html.escape(md)}</pre></section>
<section><h2>年度收益</h2><div class="scroll">{annual_show.to_html(index=False, border=0)}</div></section>
<section><h2>逐年移动起点敏感性</h2><div class="scroll">{start_show.to_html(index=False, border=0)}</div></section>
<section><h2>Paper 杠杆状态归因</h2><div class="scroll">{regime_show.to_html(index=False, border=0)}</div></section>
<section><h2>Provenance</h2><pre>{provenance}</pre></section></main></body></html>"""
    return md, page


def run(args: argparse.Namespace) -> Path:
    config_path = args.config.resolve()
    doc = load_config(config_path)
    base = doc["base"]
    baseline_cfg = engine.profile_cfg(
        base["profile"], tier=base["tier"],
        ignore_dividends=base["dividend_mode"] == "ignore_dividends",
        slip_per_share=float(base["slippage_per_share"]),
        target_vol=float(base["target_daily_volatility"]), max_lev=4.0)
    data_release = (ROOT / base["data_release"]).resolve()
    financing_path = (ROOT / base["financing_rates"]).resolve()
    data = engine.load_run(data_release, baseline_cfg)
    financing = pd.read_csv(financing_path, parse_dates=["session_date"])

    configs = {
        "cap_4x": baseline_cfg,
        "constant_2x": engine.profile_cfg(
            base["profile"], tier=base["tier"],
            ignore_dividends=base["dividend_mode"] == "ignore_dividends",
            slip_per_share=float(base["slippage_per_share"]),
            sizing="flat", flat_lev=2.0),
        "constant_2x_independent": engine.profile_cfg(
            base["profile"], tier=base["tier"],
            ignore_dividends=base["dividend_mode"] == "ignore_dividends",
            slip_per_share=float(base["slippage_per_share"]),
            sizing="flat", flat_lev=2.0),
    }
    results: dict[str, pd.DataFrame] = {}
    summary_rows = []
    target: pd.Series | None = None
    for variant in doc["variants"]:
        if variant["id"] == "constant_2x":
            if "cap_4x" not in results:
                raise RuntimeError("paper baseline must run before constant 2x")
            invalid_days = results["cap_4x"].index[
                results["cap_4x"]["status"].eq("invalid_feature_dvol")]
            tier_column = "is_halt_usable"
            bars = data["bars"]
            invalid_rows = bars["session_date"].isin(invalid_days)
            original_tier = bars.loc[invalid_rows, tier_column].copy()
            bars.loc[invalid_rows, tier_column] = False
            try:
                result = engine.backtest(
                    data, configs[variant["id"]], collect_ledger=True,
                    financing_rates=financing)
            finally:
                bars.loc[invalid_rows, tier_column] = original_tier
        else:
            result = engine.backtest(
                data, configs[variant["id"]], collect_ledger=True,
                financing_rates=financing)
        if target is None:
            target = (
                float(base["target_daily_volatility"])
                / result.attrs["daily_features"]["dvol"])
        results[variant["id"]] = result
        summary_rows.extend(common.summarize(variant, result, base))
        print(f"completed {variant['id']}", flush=True)
    assert target is not None

    eligibility = eligibility_audit(results)
    summary = common.add_deltas(pd.DataFrame(summary_rows))
    daily = daily_comparison(results, target)
    annual = annual_comparison(results)
    start_years = start_year_sensitivity(results)
    regimes = regime_attribution(results, target, base)
    sizing = sizing_diagnostics(results, target, base)
    audit_result = common.audit(results, base)

    run_id = args.run_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_constant_2x_vs_vol_target_v1")
    output_root = (
        ROOT / "experiments" / "constant_2x_vs_vol_target_v1" / "results")
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
            "git": common.git_state(), "audit": audit_result,
            "eligibility_audit": eligibility,
            "hashes": {
                "config_sha256": common.sha256(config_path),
                "runner_sha256": common.sha256(SCRIPT_PATH),
                "engine_sha256": common.sha256(ROOT / "im_engine_v4.py"),
                "data_release_manifest_sha256": common.sha256(
                    data_release / "data_manifest.json"),
                "financing_sha256": common.sha256(financing_path),
            },
            "coverage": {"summary_rows": len(summary), "daily_rows": len(daily),
                         "annual_rows": len(annual),
                         "start_year_rows": len(start_years),
                         "regime_rows": len(regimes)},
        }
        md, page = render_report(
            summary, annual, regimes, sizing, start_years, daily, manifest)
        summary.to_csv(staging / "summary.csv", index=False)
        annual.to_csv(staging / "annual_returns.csv", index=False)
        start_years.to_csv(staging / "start_year_sensitivity.csv", index=False)
        regimes.to_csv(staging / "paper_leverage_regime_attribution.csv", index=False)
        sizing.to_csv(staging / "sizing_diagnostics.csv", index=False)
        daily.to_parquet(staging / "daily_comparison.parquet", index=False)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        (staging / "report.md").write_text(md, encoding="utf-8")
        (staging / "report.html").write_text(page, encoding="utf-8")
        (staging / "_SUCCESS").write_text(json.dumps({
            "run_id": run_id,
            "manifest_sha256": common.sha256(staging / "manifest.json"),
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
