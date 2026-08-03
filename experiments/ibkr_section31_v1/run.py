"""Run the isolated historical SEC Section 31 cost sensitivity."""

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
    expected = ["legacy", "legacy_plus_section31"]
    if [v.get("explicit_cost_model") for v in variants] != expected:
        raise ValueError(f"variants must be ordered as {expected}")
    return doc


def cagr(returns: pd.Series) -> float:
    returns = returns.dropna()
    wealth = float((1.0 + returns).prod())
    years = max(
        (returns.index.max() - returns.index.min()).days / 365.2425,
        1 / 365.2425)
    return wealth ** (1.0 / years) - 1.0 if wealth > 0 else np.nan


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
    rows = []
    for period, mask in period_masks(result.index, base).items():
        x = result.loc[mask & result["is_evaluation"].astype(bool)].copy()
        ret = x["ret"].fillna(0.0)
        trading_only = ret - x["cash_interest"] / x["prev_aum"]
        wealth = (1.0 + ret).cumprod()
        std = float(ret.std())
        shares = float(x["shares_traded"].sum())
        rows.append({
            "variant_id": variant["id"],
            "label": variant["label"],
            "explicit_cost_model": variant["explicit_cost_model"],
            "period": period,
            "first": str(x.index.min().date()),
            "last": str(x.index.max().date()),
            "evaluation_sessions": int(len(x)),
            "total_return": float(wealth.iloc[-1] - 1.0),
            "cagr": cagr(ret),
            "trading_only_cagr": cagr(trading_only),
            "annual_volatility": std * np.sqrt(252.0),
            "sharpe_calendar": (
                float((ret - x["cash_hurdle_ret"].fillna(0.0)).mean()
                      / std * np.sqrt(252.0)) if std > 0 else np.nan),
            "max_drawdown": float((wealth / wealth.cummax() - 1.0).min()),
            "gross_pnl": float(x["gross"].sum()),
            "ibkr_commission": float(x["ibkr_commission"].sum()),
            "section31_fee": float(x["section31_fee"].sum()),
            "total_explicit_cost": float(x["total_explicit_cost"].sum()),
            "slippage": float(x["slippage"].sum()),
            "total_execution_cost": float(x["total_execution_cost"].sum()),
            "cash_interest": float(x["cash_interest"].sum()),
            "funding_and_borrow_cost": float(-x["financing"].sum()),
            "net_pnl": float(x["net"].sum()),
            "shares_traded": shares,
            "section31_per_traded_share": (
                float(x["section31_fee"].sum()) / shares if shares else np.nan),
            "execution_cost_per_traded_share": (
                float(x["cost"].sum()) / shares if shares else np.nan),
        })
    return rows


def add_deltas(summary: pd.DataFrame) -> pd.DataFrame:
    baseline = summary.loc[summary["variant_id"].eq("legacy")].set_index("period")
    for column in (
        "total_return", "cagr", "trading_only_cagr", "annual_volatility",
        "sharpe_calendar", "max_drawdown", "net_pnl",
        "section31_fee", "total_execution_cost",
    ):
        summary[f"delta_{column}"] = summary.apply(
            lambda row: row[column] - baseline.loc[row["period"], column], axis=1)
    return summary


def audit(
        results: dict[str, pd.DataFrame], base: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for variant_id, result in results.items():
        ledger = result.attrs["ledger"]["fills"]
        daily_component = (
            result["commission"] - result["ibkr_commission"]
            - result["section31_fee"]).abs().max()
        daily_cost = (
            result["cost"] - result["total_explicit_cost"]
            - result["slippage"]).abs().max()
        net = (
            result["net"] - result["gross"] + result["cost"]
            - result["cash_interest"] - result["financing"]).abs().max()
        aum = (
            result["aum"] - result["prev_aum"] - result["net"]).abs().max()
        fill_component = (
            ledger["total_explicit_cost"] - ledger["ibkr_commission"]
            - ledger["section31_fee"]).abs().max()
        fill_sec = (
            ledger["section31_fee"]
            - ledger["sell_notional"] * ledger["section31_rate_decimal"]
        ).abs().max()
        maximum = max(daily_component, daily_cost, net, aum,
                      fill_component, fill_sec)
        if maximum > 1e-8:
            raise RuntimeError(f"{variant_id} accounting identity failed")
        zero_window = ledger[
            ledger["session_date"].between("2025-05-14", "2026-04-03")]
        zero_fee = float(zero_window["section31_fee"].abs().max())
        if zero_fee > 1e-12:
            raise RuntimeError(f"{variant_id} charged Section 31 in zero window")
        out[variant_id] = {
            "max_accounting_residual": float(maximum),
            "zero_rate_window_max_fee": zero_fee,
            "fills": int(len(ledger)),
            "sell_fills": int(ledger["sell_shares"].gt(0).sum()),
            "buy_only_fills": int(ledger["sell_shares"].eq(0).sum()),
        }

    formal_dir = (ROOT / base["formal_baseline_run"]).resolve()
    formal = pd.read_parquet(formal_dir / "daily_results.parquet")
    formal = formal.loc[formal["cell_id"].eq(base["formal_baseline_cell"])]
    formal["session_date"] = pd.to_datetime(formal["session_date"]).dt.normalize()
    formal = formal.set_index("session_date").sort_index()
    legacy = results["legacy"]
    columns = [
        "gross", "commission", "slippage", "cost", "cash_interest",
        "financing", "net", "shares_traded", "traded_notional",
        "prev_aum", "aum", "ret",
    ]
    if not legacy.index.equals(formal.index):
        raise RuntimeError("legacy baseline index differs from formal run")
    max_difference = float((legacy[columns] - formal[columns]).abs().max().max())
    if max_difference > 1e-8:
        raise RuntimeError("legacy baseline differs from formal run")
    out["formal_baseline"] = {
        "run": str(formal_dir),
        "cell": base["formal_baseline_cell"],
        "max_absolute_numeric_difference": max_difference,
    }
    return out


def pct(value: Any) -> str:
    return "—" if pd.isna(value) else f"{100 * float(value):.2f}%"


def cents(value: Any) -> str:
    return "—" if pd.isna(value) else f"{100 * float(value):.4f}¢"


def render_report(
        summary: pd.DataFrame, manifest: dict[str, Any]) -> tuple[str, str]:
    def row(variant: str, period: str) -> pd.Series:
        return summary.loc[
            summary["variant_id"].eq(variant)
            & summary["period"].eq(period)].iloc[0]

    table = []
    for period in ("full_sample", "pre_publication", "post_publication"):
        old, new = row("legacy", period), row("legacy_plus_section31", period)
        table.append(
            f"| {period} | {pct(old['cagr'])} | {pct(new['cagr'])} | "
            f"{100 * new['delta_cagr']:+.3f}pp | "
            f"{pct(old['trading_only_cagr'])} | "
            f"{pct(new['trading_only_cagr'])} | "
            f"{100 * new['delta_trading_only_cagr']:+.3f}pp | "
            f"{old['sharpe_calendar']:.3f} | {new['sharpe_calendar']:.3f} | "
            f"${new['section31_fee']:,.2f} | "
            f"{cents(new['section31_per_traded_share'])} |")
    post = row("legacy_plus_section31", "post_publication")
    md = f"""# IBKR / SEC Section 31 历史成本敏感性 v1

这是 post-result transaction-cost sensitivity，不修改 frozen v2 或
`halt_aware × $0.0025/share` reporting amendment。唯一变化是在原有基础佣金
之外，按每笔卖出成交的名义金额乘以交易日有效的 SEC Section 31 费率。

| 时段 | Legacy Portfolio CAGR | + Section 31 Portfolio CAGR | Portfolio 变化 | Legacy Trading-only CAGR | + Section 31 Trading-only CAGR | Trading-only 变化 | Legacy Sharpe | + Section 31 Sharpe | Section 31 总额 | 每成交股 Section 31 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(table)}

Post-publication 加入 Section 31 后，portfolio CAGR 为 {pct(post['cagr'])}，
相对原口径变化 {100 * post['delta_cagr']:+.3f}pp；same-path trading-only CAGR
为 {pct(post['trading_only_cagr'])}，变化
{100 * post['delta_trading_only_cagr']:+.3f}pp；每成交股增加
{cents(post['section31_per_traded_share'])}。这只量化 Section 31，不能称为
all-in IBKR 成本。

尚未纳入：TAF、CAT、clearing、pass-through、venue fee/rebate、账单舍入、
月度佣金分档与 market impact。它们没有被悄悄塞进 slippage；在取得历史序列
或冻结情景前保持为“未建模”。

## 审计

- run：`{manifest['run_id']}`
- Section 31 schedule SHA-256：`{manifest['hashes']['section31_schedule_sha256']}`
- 费率覆盖：2008-01-22 至 2026-07-09；2025-05-14 至 2026-04-03 为零
- Legacy 对正式 run 最大数值差：{manifest['audit']['formal_baseline']['max_absolute_numeric_difference']:.3g}
- 最大 accounting residual：{max(manifest['audit']['legacy_plus_section31']['max_accounting_residual'], manifest['audit']['legacy']['max_accounting_residual']):.3g}
- Git commit：`{manifest['git']['commit']}`；运行时 dirty：`{str(manifest['git']['dirty']).lower()}`
"""
    display = summary.copy()
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Section 31 历史成本敏感性</title><style>
body{{font-family:Segoe UI,Arial,sans-serif;background:#f5f7fb;color:#172033;margin:0}}
main{{max-width:1180px;margin:auto;padding:28px}}section{{background:#fff;border:1px solid #dfe5ef;border-radius:12px;padding:20px;margin:16px 0}}
table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{padding:8px;border-bottom:1px solid #e8edf5;text-align:right}}th:first-child,td:first-child{{text-align:left}}
pre{{white-space:pre-wrap;background:#111827;color:#dbeafe;padding:14px;border-radius:8px}}.scroll{{overflow:auto}}</style></head><body><main>
<h1>IBKR / SEC Section 31 历史成本敏感性</h1><section><pre>{html.escape(md)}</pre></section>
<section><h2>完整摘要</h2><div class="scroll">{display.to_html(index=False, border=0)}</div></section>
<section><h2>Provenance</h2><pre>{html.escape(json.dumps(manifest, indent=2), quote=False)}</pre></section>
</main></body></html>"""
    return md, page


def run(args: argparse.Namespace) -> Path:
    config_path = args.config.resolve()
    doc = load_config(config_path)
    base = doc["base"]
    cfg0 = engine.profile_cfg(
        base["profile"], tier=base["tier"],
        ignore_dividends=base["dividend_mode"] == "ignore_dividends",
        slip_per_share=float(base["slippage_per_share"]))
    data_release = (ROOT / base["data_release"]).resolve()
    financing_path = (ROOT / base["financing_rates"]).resolve()
    section31_path = (ROOT / base["section31_rates"]).resolve()
    data = engine.load_run(data_release, cfg0)
    financing = pd.read_csv(financing_path, parse_dates=["session_date"])
    section31 = engine.load_section31_rates(section31_path)

    results: dict[str, pd.DataFrame] = {}
    summary_rows: list[dict[str, Any]] = []
    for variant in doc["variants"]:
        cfg = engine.profile_cfg(
            base["profile"], tier=base["tier"],
            ignore_dividends=base["dividend_mode"] == "ignore_dividends",
            slip_per_share=float(base["slippage_per_share"]),
            explicit_cost_model=variant["explicit_cost_model"])
        result = engine.backtest(
            data, cfg, collect_ledger=True, financing_rates=financing,
            section31_rates=(
                section31 if variant["explicit_cost_model"]
                == "legacy_plus_section31" else None))
        results[variant["id"]] = result
        summary_rows.extend(summarize(variant, result, base))
        print(f"completed {variant['id']}", flush=True)

    summary = add_deltas(pd.DataFrame(summary_rows))
    audit_result = audit(results, base)
    run_id = args.run_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_ibkr_section31_v1")
    output_root = ROOT / "experiments" / "ibkr_section31_v1" / "results"
    output = output_root / run_id
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    staging = output_root / f".staging-{run_id}-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        manifest = {
            "run_id": run_id,
            "experiment_id": doc["experiment_id"],
            "classification": doc["classification"],
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "sample": {"start": "2008-01-22", "end": base["evaluation_end"],
                       "post_publication_start": base["evaluation_start"]},
            "base_config": base,
            "variants": doc["variants"],
            "git": git_state(),
            "audit": audit_result,
            "hashes": {
                "config_sha256": sha256(config_path),
                "runner_sha256": sha256(SCRIPT_PATH),
                "engine_sha256": sha256(ROOT / "im_engine_v4.py"),
                "data_release_manifest_sha256": sha256(
                    data_release / "data_manifest.json"),
                "financing_sha256": sha256(financing_path),
                "section31_schedule_sha256": sha256(section31_path),
            },
            "coverage": {"summary_rows": len(summary)},
            "limitations": [
                "not an all-in IBKR model",
                "TAF CAT clearing pass-through venue fees and rebates unmodelled",
                "customer statement rounding and monthly volume tiers unmodelled",
                "market impact queue position and partial fills unmodelled",
            ],
        }
        md, page = render_report(summary, manifest)
        summary.to_csv(staging / "summary.csv", index=False, float_format="%.12g")
        for variant_id, result in results.items():
            daily = result.reset_index()
            daily.attrs = {}
            daily.to_parquet(staging / f"daily_{variant_id}.parquet", index=False)
            fills = result.attrs["ledger"]["fills"].copy()
            fills.to_parquet(staging / f"fills_{variant_id}.parquet", index=False)
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--run-id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    output = run(parse_args(argv))
    print(f"published: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
