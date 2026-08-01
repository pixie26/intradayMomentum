"""Build an audited interactive attribution report for a formal headline run.

The report is a derived artifact: it reads a completed formal evaluation run,
never changes its files, and publishes to a separate output directory.  The
daily accounting identity is decomposed into long gross, short gross,
commission, slippage, cash interest, leveraged-cash funding and SPY borrow.

Run:
    python evaluation/make_attribution_report.py \
      --results-dir evaluation/results/<formal_run> \
      --output-dir evaluation/results/<formal_run>_attribution \
      --publish-html docs/POST_PUBLICATION_EVALUATION_V2_HALT0025_ATTRIBUTION.html
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation import run_evaluation as runner


ROOT = Path(__file__).resolve().parents[1]
FINANCING_DENOMINATOR = 360.0 * 24.0 * 60.0
COMPONENTS = (
    "long", "short", "commission", "slippage", "cash_interest",
    "funding", "borrow",
)
EVENT_LABELS = {
    "2008Q3": "雷曼倒闭与金融危机",
    "2008Q4": "全球金融危机",
    "2009Q1": "金融危机触底阶段",
    "2010Q2": "欧洲主权债务危机",
    "2011Q3": "美国债务上限与欧债危机",
    "2015Q3": "中国增长担忧引发抛售",
    "2018Q4": "美联储紧缩与贸易摩擦",
    "2020Q1": "新冠疫情冲击",
    "2022Q2": "通胀与加息",
    "2022Q3": "持续加息",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--benchmark-daily", type=Path,
        default=ROOT / "benchmark_release_v1" / "spy_daily_raw_close.csv")
    parser.add_argument(
        "--dividends", type=Path,
        default=ROOT / "data" / "reference"
        / "spy_dividends_state_street_20260730.csv")
    parser.add_argument(
        "--publish-html", type=Path,
        help="optional readable report copy outside the derived output")
    return parser.parse_args()


def headline_cell_id(spec: dict[str, Any]) -> str:
    return runner.headline_cell_id(spec)


def headline_label_zh(spec: dict[str, Any]) -> str:
    headline = spec["headline"]
    profiles = {
        "corrected_execution": "修正执行",
        "paper_spec": "论文规则",
        "official_sample_compatible": "作者样例兼容",
    }
    tiers = {
        "halt_aware": "停牌感知层级",
        "paper_ready": "论文可比层级",
        "exploratory": "探索层级",
    }
    dividends = {
        "with_dividends": "含分红信号锚点",
        "ignore_dividends": "忽略分红信号锚点",
    }
    return " × ".join([
        profiles.get(headline["profile"], headline["profile"]),
        tiers.get(headline["tier"], headline["tier"]),
        dividends.get(headline["dividend_mode"], headline["dividend_mode"]),
        f"每股滑点 ${float(headline['slippage_per_share']):.4f}",
    ])


def resolve_spec(manifest: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    """Resolve the exact spec by manifest hash, tolerating expired temp paths."""
    expected_hash = manifest.get("spec_sha256")
    candidates = [Path(str(manifest.get("spec_path", "")))]
    candidates.extend(sorted((ROOT / "config").glob("evaluation_spec*.yml")))
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        spec, actual_hash = runner.load_spec(resolved)
        if actual_hash == expected_hash:
            return resolved, spec
    raise RuntimeError(
        f"cannot resolve a local spec matching formal hash {expected_hash}")


def load_dividends(path: Path) -> pd.Series:
    frame = pd.read_csv(path)
    normalized = {column.lower(): column for column in frame.columns}
    date_col = normalized.get("date") or normalized.get("ex_date")
    amount_col = normalized.get("dividend") or normalized.get("cash_amount")
    if date_col is None or amount_col is None:
        raise ValueError("dividend file requires date/ex_date and dividend/cash_amount")
    out = pd.Series(
        pd.to_numeric(frame[amount_col], errors="coerce").to_numpy(),
        index=pd.to_datetime(frame[date_col]).dt.normalize(), name="dividend")
    if out.index.duplicated().any() or out.isna().any():
        raise ValueError("dividend file contains duplicate dates or invalid amounts")
    return out.sort_index()


def benchmark_returns(close: pd.Series, dividends: pd.Series,
                      sessions: pd.DatetimeIndex) -> pd.Series:
    div = dividends.reindex(close.index).fillna(0.0)
    total = (close + div) / close.shift(1) - 1.0
    aligned = total.reindex(sessions)
    if aligned.isna().any():
        missing = [str(x.date()) for x in aligned.index[aligned.isna()][:5]]
        raise RuntimeError(f"benchmark misses headline sessions: {missing}")
    return aligned.rename("benchmark_total_return")


def prepare_daily(results_dir: Path, spec: dict[str, Any],
                  benchmark_path: Path, dividend_path: Path
                  ) -> tuple[pd.DataFrame, dict[str, float]]:
    cid = headline_cell_id(spec)
    daily = pd.read_parquet(results_dir / "daily_results.parquet")
    daily = daily[daily["cell_id"].eq(cid)].copy()
    daily["session_date"] = pd.to_datetime(daily["session_date"]).dt.normalize()
    daily = daily.sort_values("session_date").set_index("session_date")
    daily = daily[daily["is_evaluation"].astype(bool)].copy()
    if daily.empty or daily["ret"].isna().any():
        raise RuntimeError("headline has no complete evaluation return path")

    daily["funding_cost"] = (
        daily["borrowed_cash_minute_dollars"]
        * daily["funding_rate_annual_used"] / FINANCING_DENOMINATOR)
    daily["borrow_cost"] = (
        daily["short_notional_minute_dollars"]
        * daily["borrow_rate_annual_used"] / FINANCING_DENOMINATOR)

    split_residual = (
        daily["financing"] + daily["funding_cost"] + daily["borrow_cost"])
    gross_residual = daily["gross"] - daily["long_gross"] - daily["short_gross"]
    net_from_components = (
        daily["long_gross"] + daily["short_gross"]
        - daily["commission"] - daily["slippage"]
        + daily["cash_interest"] - daily["funding_cost"]
        - daily["borrow_cost"])
    net_residual = daily["net"] - net_from_components
    aum_residual = daily["aum"] - daily["prev_aum"] - daily["net"]
    checks = {
        "max_abs_financing_split_residual": float(split_residual.abs().max()),
        "max_abs_gross_identity_residual": float(gross_residual.abs().max()),
        "max_abs_net_identity_residual": float(net_residual.abs().max()),
        "max_abs_aum_identity_residual": float(aum_residual.abs().max()),
    }
    scale = max(float(daily["prev_aum"].abs().max()), 1.0)
    if max(checks.values()) > scale * 1e-11:
        raise RuntimeError(f"headline accounting identity failed: {checks}")

    close = runner.load_daily_benchmark(benchmark_path)
    dividends = load_dividends(dividend_path)
    daily["benchmark_ret"] = benchmark_returns(
        close, dividends, pd.DatetimeIndex(daily.index))

    pnl_columns = {
        "long": daily["long_gross"],
        "short": daily["short_gross"],
        "commission": -daily["commission"],
        "slippage": -daily["slippage"],
        "cash_interest": daily["cash_interest"],
        "funding": -daily["funding_cost"],
        "borrow": -daily["borrow_cost"],
    }
    for name, values in pnl_columns.items():
        daily[f"pnl_{name}"] = values
        daily[f"ret_{name}"] = values / daily["prev_aum"]
    return_residual = daily["ret"] - daily[
        [f"ret_{name}" for name in COMPONENTS]].sum(axis=1)
    checks["max_abs_return_identity_residual"] = float(return_residual.abs().max())
    if checks["max_abs_return_identity_residual"] > 1e-12:
        raise RuntimeError(f"return attribution identity failed: {checks}")

    daily["active"] = daily["status"].eq("active")
    daily["scheduled_minutes"] = (
        daily["intraday_daycount_fraction"] * FINANCING_DENOMINATOR)
    daily["gross_notional_minute_dollars"] = (
        daily["long_notional_minute_dollars"]
        + daily["short_notional_minute_dollars"])
    daily["trading_only_pnl"] = daily["net"] - daily["cash_interest"]
    daily["trading_only_ret"] = daily["ret"] - daily["ret_cash_interest"]
    daily["year"] = daily.index.year
    daily["month"] = daily.index.to_period("M").astype(str)
    daily["quarter"] = daily.index.to_period("Q").astype(str)
    daily["week"] = daily.index.to_period("W-FRI").astype(str)
    return daily, checks


def load_headline_ledgers(
        results_dir: Path, spec: dict[str, Any], daily: pd.DataFrame
        ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load compact headline ledgers and attach auditable daily counts."""
    cid = headline_cell_id(spec)
    trips = pd.read_parquet(
        results_dir / "round_trips.parquet",
        filters=[("cell_id", "==", cid)]).copy()
    trips["session_date"] = pd.to_datetime(
        trips["session_date"]).dt.normalize()
    trips = trips.sort_values(["session_date", "entry_minute"]).reset_index(
        drop=True)

    signals = pd.read_parquet(
        results_dir / "signals.parquet",
        filters=[("cell_id", "==", cid)]).copy()
    signals["session_date"] = pd.to_datetime(
        signals["session_date"]).dt.normalize()
    signals["nonflat"] = pd.to_numeric(
        signals["signal"], errors="coerce").fillna(0.0).ne(0.0)
    signal_daily = signals.groupby("session_date").agg(
        signal_observations=("signal", "size"),
        nonflat_signal_observations=("nonflat", "sum"),
    )
    trip_daily = trips.groupby("session_date").size().rename("entries")
    for name, values in (
            ("signal_observations", signal_daily["signal_observations"]),
            ("nonflat_signal_observations",
             signal_daily["nonflat_signal_observations"]),
            ("entries", trip_daily)):
        daily[name] = values.reindex(daily.index).fillna(0).astype(int)
    signal_check = (
        daily["signal_observations"].astype(float) - daily["signal_count"])
    if signal_check.abs().max() > 0:
        raise RuntimeError("signals ledger does not match daily signal_count")
    return daily, trips


def load_headline_decomposition(
        results_dir: Path, spec: dict[str, Any]) -> pd.DataFrame:
    frame = pd.read_csv(results_dir / "decomposition.csv")
    frame = frame[frame["cell_id"].eq(headline_cell_id(spec))].copy()
    wanted = frame["component"].isin(
        ["entry_time_bucket", "volatility_regime_quintile"])
    return frame[wanted].reset_index(drop=True)


def linked_contributions(frame: pd.DataFrame) -> dict[str, float]:
    future_wealth = 1.0
    result = {name: 0.0 for name in COMPONENTS}
    for row in frame.iloc[::-1].itertuples():
        for name in COMPONENTS:
            result[name] += getattr(row, f"ret_{name}") * future_wealth
        future_wealth *= 1.0 + row.ret
    total = future_wealth - 1.0
    if abs(sum(result.values()) - total) > 1e-10:
        raise RuntimeError("linked component contributions do not sum to total return")
    return result


def reporting_metrics(frame: pd.DataFrame) -> dict[str, float]:
    """Metrics that keep portfolio carry separate from trading economics."""
    if len(frame) < 2:
        raise ValueError("reporting metrics require at least two sessions")
    days = (frame.index[-1] - frame.index[0]).days
    years = max(days / 365.2425, 1.0 / 365.2425)
    portfolio_total = float((1.0 + frame["ret"]).prod() - 1.0)
    trading_total = float(
        (1.0 + frame["trading_only_ret"]).prod() - 1.0)
    simple_total = float(frame["ret"].sum())
    simple_cash = float(frame["ret_cash_interest"].sum())
    return {
        "portfolio_total_return": portfolio_total,
        "trading_only_total_return": trading_total,
        "portfolio_cagr": (1.0 + portfolio_total) ** (1.0 / years) - 1.0,
        "trading_only_cagr": (
            (1.0 + trading_total) ** (1.0 / years) - 1.0),
        "cash_interest_annualized": float(
            frame["ret_cash_interest"].mean() * 252.0),
        "cash_share_simple_sum_return": (
            simple_cash / simple_total if simple_total else np.nan),
    }


def max_drawdown(returns: pd.Series) -> float:
    wealth = (1.0 + returns).cumprod()
    with_anchor = pd.concat([pd.Series([1.0]), wealth.reset_index(drop=True)])
    return float((with_anchor / with_anchor.cummax() - 1.0).min())


def aggregate_period(frame: pd.DataFrame, label: str,
                     complete: bool = True,
                     benchmark_return: float | None = None) -> dict[str, Any]:
    linked = linked_contributions(frame)
    return {
        "period": label,
        "start": str(frame.index.min().date()),
        "end": str(frame.index.max().date()),
        "complete": bool(complete),
        "event": EVENT_LABELS.get(label, ""),
        "strategy": float((1.0 + frame["ret"]).prod() - 1.0),
        "trading_only": float(
            (1.0 + frame["trading_only_ret"]).prod() - 1.0),
        "benchmark": (
            float(benchmark_return) if benchmark_return is not None else
            float((1.0 + frame["benchmark_ret"]).prod() - 1.0)),
        "mdd": max_drawdown(frame["ret"]),
        "active_sessions": int(frame["active"].sum()),
        "sessions": int(len(frame)),
        **{f"contrib_{name}": value for name, value in linked.items()},
    }


def full_benchmark(close: pd.Series, dividends: pd.Series,
                   start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    div = dividends.reindex(close.index).fillna(0.0)
    frame = pd.DataFrame({
        "ret": (close + div) / close.shift(1) - 1.0,
    }).loc[start:end].dropna()
    frame["quarter"] = frame.index.to_period("Q").astype(str)
    frame["year"] = frame.index.year
    return frame


def period_tables(daily: pd.DataFrame, close: pd.Series,
                  dividends: pd.Series
                  ) -> tuple[pd.DataFrame, pd.DataFrame]:
    benchmark = full_benchmark(
        close, dividends, daily.index.min(), daily.index.max())
    quarter_rows = []
    for label, frame in daily.groupby("quarter", sort=True):
        bm_frame = benchmark[benchmark["quarter"].eq(label)]
        bm_sessions = bm_frame.index
        complete = (
            len(bm_sessions) > 0
            and frame.index.min() <= bm_sessions.min()
            and frame.index.max() >= bm_sessions.max())
        quarter_rows.append(aggregate_period(
            frame, label, complete,
            float((1.0 + bm_frame["ret"]).prod() - 1.0)))
    annual_rows = []
    for year, frame in daily.groupby("year", sort=True):
        bm_frame = benchmark[benchmark["year"].eq(year)]
        bm_sessions = bm_frame.index
        complete = (
            len(bm_sessions) > 0
            and frame.index.min() <= bm_sessions.min()
            and frame.index.max() >= bm_sessions.max())
        annual_rows.append(aggregate_period(
            frame, str(year), complete,
            float((1.0 + bm_frame["ret"]).prod() - 1.0)))
    return pd.DataFrame(quarter_rows), pd.DataFrame(annual_rows)


def json_records(daily: pd.DataFrame) -> list[dict[str, Any]]:
    fields = [
        "ret", "benchmark_ret", "cash_hurdle_ret", "active",
        "prev_aum", "net", "cash_interest", "trading_only_pnl",
        "trading_only_ret", "shares_traded", "signal_count",
        "signal_observations", "nonflat_signal_observations", "entries",
        "fill_events", "holding_minutes", "scheduled_minutes",
        "gross_notional_minute_dollars",
        "positive_cash_minute_dollars", "borrowed_cash_minute_dollars",
        *[f"pnl_{name}" for name in COMPONENTS],
        *[f"ret_{name}" for name in COMPONENTS],
    ]
    out = daily[fields].copy()
    out.insert(0, "date", [str(x.date()) for x in out.index])
    out.insert(1, "week", daily["week"].to_numpy())
    out.insert(2, "month", daily["month"].to_numpy())
    out.insert(3, "quarter", daily["quarter"].to_numpy())
    return out.replace({np.nan: None}).to_dict(orient="records")


def trip_records(trips: pd.DataFrame) -> list[dict[str, Any]]:
    fields = [
        "direction", "entry_minute", "gross", "shares", "holding_minutes",
        "exit_minute", "exit_reason",
    ]
    out = trips[fields].copy()
    out.insert(0, "date", [str(x.date()) for x in trips["session_date"]])
    return out.replace({np.nan: None}).to_dict(orient="records")


CSS = r"""
:root{--ink:#142033;--muted:#64748b;--line:#d9e1ea;--paper:#fff;
--wash:#f3f6fa;--blue:#1769aa;--orange:#d97706;--green:#16803c;
--red:#c0392b;--purple:#6d44a0}*{box-sizing:border-box}
body{margin:0;background:var(--wash);color:var(--ink);font:14px/1.5
"Segoe UI","Microsoft YaHei",Arial,sans-serif}main{max-width:1500px;margin:auto;padding:28px}
h1{font-size:28px;margin:6px 0}h2{font-size:19px;margin:0 0 12px}
h3{font-size:15px;margin:20px 0 8px}.panel{background:var(--paper);border:1px
solid var(--line);border-radius:12px;padding:20px;margin:16px 0;box-shadow:0 1px 2px #1018280b}
.meta,.note{color:var(--muted);font-size:12px}.badge{display:inline-block;background:#e8f1fb;
color:#15578f;border-radius:99px;padding:3px 9px;font-weight:650}.controls{display:flex;
flex-wrap:wrap;gap:10px 14px;align-items:end}.controls label{display:flex;flex-direction:column;
gap:3px;color:#415268;font-size:12px}input,select,button{border:1px solid #b9c5d1;
border-radius:6px;background:#fff;padding:6px 9px}button{cursor:pointer}.cards{display:grid;
grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}.card{border:1px solid
var(--line);border-radius:9px;padding:12px}.card .label{color:var(--muted);font-size:11px;
text-transform:uppercase}.card .value{font-size:22px;font-weight:700;margin-top:4px}.chart{width:100%;
height:auto;border:1px solid var(--line);border-radius:8px;background:#fff}.scroll{overflow:auto}
table{border-collapse:collapse;width:100%;white-space:nowrap;font-size:12px}th,td{padding:7px 9px;
border-bottom:1px solid var(--line);text-align:right}th{background:#f7f9fc;position:sticky;top:0}
th:first-child,td:first-child{text-align:left}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.pos{color:var(--green)}.neg{color:var(--red)}code{word-break:break-all}@media(max-width:900px){.grid2{grid-template-columns:1fr}main{padding:12px}}
.callout{border-left:5px solid var(--red);background:#fff5f3;padding:14px 16px;
border-radius:8px;margin:12px 0;color:#6f241b}.card.trading{border-color:#87c99b;
background:#f4fbf6}.card.cash{border-color:#b9a1df;background:#faf7ff}.card.portfolio{
border-color:#8bb9dc;background:#f4f9fd}.card .value{font-variant-numeric:tabular-nums}
"""


JS = r"""
const DAILY=__DAILY__, BENCHMARK=__BENCHMARK__, QUARTERS=__QUARTERS__, ANNUAL=__ANNUAL__, TRIPS=__TRIPS__, DECOMP=__DECOMP__;
const PRE_END="__PRE_END__", POST_START="__POST_START__";
const COMPONENTS=["long","short","commission","slippage","cash_interest","funding","borrow"];
const LABELS={long:"多头毛损益",short:"空头毛损益",commission:"佣金",slippage:"滑点",
cash_interest:"现金利息",funding:"杠杆融资成本",borrow:"SPY 借券成本"};
const COLORS={long:"#16803c",short:"#1769aa",commission:"#c0392b",slippage:"#e06b5f",
cash_interest:"#6d44a0",funding:"#d97706",borrow:"#9a6700"};
const $=id=>document.getElementById(id), pct=(x,d=2)=>Number.isFinite(x)?(100*x).toFixed(d)+"%":"—",
num=(x,d=2)=>Number.isFinite(x)?x.toFixed(d):"—", usd=x=>Number.isFinite(x)?x.toLocaleString("en-US",{style:"currency",currency:"USD",maximumFractionDigits:0}):"—";
function rows(){const s=$("start").value,e=$("end").value;return DAILY.filter(r=>r.date>=s&&r.date<=e)}
function mean(a){return a.reduce((s,x)=>s+x,0)/a.length}function std(a){if(a.length<2)return NaN;const m=mean(a);return Math.sqrt(a.reduce((s,x)=>s+(x-m)**2,0)/(a.length-1))}
function corr(a,b){if(a.length<2)return NaN;const ma=mean(a),mb=mean(b),sa=std(a),sb=std(b);return a.reduce((s,x,i)=>s+(x-ma)*(b[i]-mb),0)/(a.length-1)/sa/sb}
function ranks(a){return a.map((v,i)=>[v,i]).sort((x,y)=>x[0]-y[0]).reduce((o,p,i,arr)=>{let l=i,h=i;while(l&&arr[l-1][0]===p[0])l--;while(h+1<arr.length&&arr[h+1][0]===p[0])h++;o[p[1]]=(l+h)/2+1;return o},Array(a.length))}
function compound(a){return a.reduce((w,r)=>w*(1+r),1)-1}function linked(rs){let f=1,o=Object.fromEntries(COMPONENTS.map(k=>[k,0]));for(let i=rs.length-1;i>=0;i--){for(const k of COMPONENTS)o[k]+=rs[i]["ret_"+k]*f;f*=1+rs[i].ret}return o}
function mdd(a){let w=1,p=1,d=0;for(const r of a){w*=1+r;p=Math.max(p,w);d=Math.min(d,w/p-1)}return d}
function grouped(rs,key){const m=new Map();for(const r of rs){if(!m.has(r[key]))m.set(r[key],[]);m.get(r[key]).push(r)}return [...m.values()].map(g=>[compound(g.map(x=>x.ret)),compound(g.map(x=>x.benchmark_ret))])}
function betaAlpha(rs){const s=rs.map(r=>r.ret),b=rs.map(r=>r.benchmark_ret),ms=mean(s),mb=mean(b);let cov=0,v=0;for(let i=0;i<s.length;i++){cov+=(s[i]-ms)*(b[i]-mb);v+=(b[i]-mb)**2}cov/=Math.max(s.length-1,1);v/=Math.max(s.length,1);const beta=v?cov/v:NaN;return[beta,(ms-beta*mb)*252]}
function bmRows(rs){return BENCHMARK.filter(r=>r.date>=rs[0].date&&r.date<=rs.at(-1).date)}
function metrics(rs){const s=rs.map(r=>r.ret),t=rs.map(r=>r.trading_only_ret),b=rs.map(r=>r.benchmark_ret),bm=bmRows(rs),h=rs.map(r=>r.cash_hurdle_ret),days=(Date.parse(rs.at(-1).date)-Date.parse(rs[0].date))/864e5,years=Math.max(days/365.2425,1/365.2425),tot=compound(s),trading=compound(t),bt=compound(bm.map(x=>x.ret)),sd=std(s),[beta,alpha]=betaAlpha(rs),cashSimple=rs.reduce((z,r)=>z+r.ret_cash_interest,0),retSimple=rs.reduce((z,r)=>z+r.ret,0);return{total:tot,trading,cagr:(1+tot)**(1/years)-1,tradingCagr:(1+trading)**(1/years)-1,bench:bt,benchCagr:(1+bt)**(1/years)-1,cashAnnual:mean(rs.map(r=>r.ret_cash_interest))*252,cashShare:retSimple?cashSimple/retSimple:NaN,vol:sd*Math.sqrt(252),sharpe:mean(s.map((x,i)=>x-h[i]))/sd*Math.sqrt(252),mdd:mdd(s),pearson:corr(s,b),spearman:corr(ranks(s),ranks(b)),beta,alpha,active:rs.filter(r=>r.active).length,n:rs.length}}
function path(rs,key){let w=1;return rs.map(r=>{w*=1+r[key];return w})}
function benchmarkPath(rs){const bm=bmRows(rs),byDate=new Map();let w=1;for(const r of bm){w*=1+r.ret;byDate.set(r.date,w)}let last=1;return rs.map(r=>{if(byDate.has(r.date))last=byDate.get(r.date);return last})}
function lineChart(id,rs,series,log=false,yBounds=null){const svg=$(id),W=1100,H=390,L=62,R=18,T=18,B=38,n=rs.length;if(n<2){svg.innerHTML="";return}let vals=series.flatMap(s=>s.v.filter(Number.isFinite));let lo=yBounds?yBounds[0]:Math.min(...vals),hi=yBounds?yBounds[1]:Math.max(...vals);if(log){lo=Math.log(Math.max(lo*.98,1e-8));hi=Math.log(hi*1.02)}if(hi===lo){hi+=1;lo-=1}const x=i=>L+(W-L-R)*i/(n-1),y=v=>T+(H-T-B)*(1-((log?Math.log(v):v)-lo)/(hi-lo));let out="";for(let j=0;j<5;j++){const yy=T+(H-T-B)*j/4;out+=`<line x1="${L}" x2="${W-R}" y1="${yy}" y2="${yy}" stroke="#e7ecf2"/>`}for(const s of series)out+=`<polyline fill="none" stroke="${s.c}" stroke-width="1.7" points="${s.v.map((v,i)=>Number.isFinite(v)?`${x(i)},${y(v)}`:"").join(" ")}"/>`;out+=series.map((s,i)=>`<text x="${L+10+i*180}" y="${T+14}" fill="${s.c}" font-size="12">${s.n}</text>`).join("");out+=`<text x="${L}" y="${H-10}" font-size="11" fill="#64748b">${rs[0].date}</text><text x="${W-R}" y="${H-10}" text-anchor="end" font-size="11" fill="#64748b">${rs.at(-1).date}</text>`;svg.innerHTML=out}
function waterfall(rs){const c=linked(rs),arr=COMPONENTS.map(k=>({k,v:c[k]})),svg=$("waterfall"),W=1000,H=360,L=150,R=20,T=20,B=28,max=Math.max(...arr.map(x=>Math.abs(x.v)),.001),x0=L+(W-L-R)/2,scale=(W-L-R)/2/max;let out=`<line x1="${x0}" x2="${x0}" y1="${T}" y2="${H-B}" stroke="#94a3b8"/>`;arr.forEach((a,i)=>{const y=T+i*(H-T-B)/arr.length+5,h=(H-T-B)/arr.length-10,x=a.v>=0?x0:x0+a.v*scale,w=Math.abs(a.v)*scale;out+=`<text x="${L-8}" y="${y+h*.72}" text-anchor="end" font-size="12">${LABELS[a.k]}</text><rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${COLORS[a.k]}"><title>${LABELS[a.k]} ${pct(a.v)}</title></rect><text x="${a.v>=0?x+w+5:x-5}" y="${y+h*.72}" text-anchor="${a.v>=0?'start':'end'}" font-size="11">${pct(a.v)}</text>`});svg.innerHTML=out;return c}
function table(id,head,body){$(id).innerHTML=`<thead><tr>${head.map(x=>`<th>${x}</th>`).join("")}</tr></thead><tbody>${body.map(r=>`<tr>${r.map(x=>`<td>${x}</td>`).join("")}</tr>`).join("")}</tbody>`}
function rolling(rs,w){const out=[];for(let i=w-1;i<rs.length;i++){const g=rs.slice(i-w+1,i+1);out.push({date:rs[i].date,v:corr(g.map(x=>x.ret),g.map(x=>x.benchmark_ret))})}return out}
function correlation(rs){const freqs=[["日频",rs.map(r=>[r.ret,r.benchmark_ret])],["周频",grouped(rs,"week")],["月频",grouped(rs,"month")],["季频",grouped(rs,"quarter")]],active=rs.filter(r=>r.active),[bu,au]=betaAlpha(rs.filter(r=>r.benchmark_ret>0)),[bd,ad]=betaAlpha(rs.filter(r=>r.benchmark_ret<0));table("corr",["频率","样本数","皮尔逊相关","斯皮尔曼相关"],freqs.map(([n,p])=>[n,p.length,num(corr(p.map(x=>x[0]),p.map(x=>x[1])),3),num(corr(ranks(p.map(x=>x[0])),ranks(p.map(x=>x[1]))),3)]));const up=rs.filter(r=>r.benchmark_ret>0),dn=rs.filter(r=>r.benchmark_ret<0);table("conditional",["指标","数值"],[["活跃日的日频相关",num(corr(active.map(x=>x.ret),active.map(x=>x.benchmark_ret)),3)],["毛交易收益的日频相关",num(corr(rs.map(x=>x.ret_long+x.ret_short),rs.map(x=>x.benchmark_ret)),3)],["多头贡献相关",num(corr(rs.map(x=>x.ret_long),rs.map(x=>x.benchmark_ret)),3)],["空头贡献相关",num(corr(rs.map(x=>x.ret_short),rs.map(x=>x.benchmark_ret)),3)],["上涨市场 Beta",num(bu,3)],["下跌市场 Beta",num(bd,3)],["上涨捕获率（平均收益比）",num(mean(up.map(x=>x.ret))/mean(up.map(x=>x.benchmark_ret)),3)],["下跌捕获率（平均收益比）",num(mean(dn.map(x=>x.ret))/mean(dn.map(x=>x.benchmark_ret)),3)]]);const a=rolling(rs,63),b=rolling(rs,252),dates=a.map(x=>({date:x.date})),bm=new Map(b.map(x=>[x.date,x.v]));lineChart("rolling",dates,[{n:"63 个交易日滚动相关",c:"#1769aa",v:a.map(x=>x.v)},{n:"252 个交易日滚动相关",c:"#d97706",v:a.map(x=>bm.get(x.date)??NaN)}],false,[-1,1])}
function scenarios(rs){const mode=$("scenarioMode").value,n=+$('scenarioN').value;let q=QUARTERS.filter(x=>x.complete&&x.start>=rs[0].date&&x.end<=rs.at(-1).date);if(mode==="worst_benchmark")q.sort((a,b)=>a.benchmark-b.benchmark);else if(mode==="best_benchmark")q.sort((a,b)=>b.benchmark-a.benchmark);else if(mode==="worst_strategy")q.sort((a,b)=>a.strategy-b.strategy);else q.sort((a,b)=>a.period.localeCompare(b.period));if(mode!=="all")q=q.slice(0,n);table("quarters",["季度","事件（描述性标签）","SPY 总回报","组合收益","纯交易收益","多头","空头","执行成本","现金利息","融资与借券","策略最大回撤"],q.map(x=>[x.period,x.event||"—",pct(x.benchmark),pct(x.strategy),pct(x.trading_only),pct(x.contrib_long),pct(x.contrib_short),pct(x.contrib_commission+x.contrib_slippage),pct(x.contrib_cash_interest),pct(x.contrib_funding+x.contrib_borrow),pct(x.mdd)]))}
function selectedAnnual(rs){const groups=new Map();for(const r of rs){const y=r.date.slice(0,4);if(!groups.has(y))groups.set(y,[]);groups.get(y).push(r)}return [...groups].map(([period,g])=>{const c=linked(g),base=ANNUAL.find(x=>x.period===period),bm=BENCHMARK.filter(x=>x.date>=g[0].date&&x.date<=g.at(-1).date);return{period,complete:base&&g[0].date===base.start&&g.at(-1).date===base.end,benchmark:compound(bm.map(x=>x.ret)),strategy:compound(g.map(x=>x.ret)),trading:compound(g.map(x=>x.trading_only_ret)),cashAnnual:mean(g.map(x=>x.ret_cash_interest))*252,mdd:mdd(g.map(x=>x.ret)),...Object.fromEntries(COMPONENTS.map(k=>["contrib_"+k,c[k]]))}})}
function capital(rs){const aumMinutes=rs.reduce((s,r)=>s+r.prev_aum*r.scheduled_minutes,0),heldAumMinutes=rs.reduce((s,r)=>s+r.prev_aum*r.holding_minutes,0),scheduled=rs.reduce((s,r)=>s+r.scheduled_minutes,0),held=rs.reduce((s,r)=>s+r.holding_minutes,0),gross=rs.reduce((s,r)=>s+r.gross_notional_minute_dollars,0),positive=rs.reduce((s,r)=>s+r.positive_cash_minute_dollars,0),borrowed=rs.reduce((s,r)=>s+r.borrowed_cash_minute_dollars,0);return{util:gross/aumMinutes,time:held/scheduled,lev:gross/heldAumMinutes,positive:positive/aumMinutes,borrowed:borrowed/aumMinutes}}
function renderCashComparison(){const pre=DAILY.filter(r=>r.date<=PRE_END),post=DAILY.filter(r=>r.date>=POST_START),pm=metrics(pre),qm=metrics(post);table("cashCompare",["指标","发表前","发表后"],[["组合 CAGR（含现金利息）",pct(pm.cagr),pct(qm.cagr)],["纯交易 CAGR（相同已实现路径，移除现金利息）",pct(pm.tradingCagr),pct(qm.tradingCagr)],["现金利息年化（日均组件收益 × 252）",pct(pm.cashAnnual),pct(qm.cashAnnual)],["现金利息占简单加总收益",pct(pm.cashShare,1),pct(qm.cashShare,1)],["现金收益带来的 CAGR 提升",pct(pm.cagr-pm.tradingCagr),pct(qm.cagr-qm.tradingCagr)],["相对现金利率的夏普比率",num(pm.sharpe),num(qm.sharpe)]]);$("cashWarning").innerHTML=`发表后组合 CAGR 为 <b>${pct(qm.cagr)}</b>，但相同路径的纯交易 CAGR 只有 <b>${pct(qm.tradingCagr)}</b>。现金利息年化为 <b>${pct(qm.cashAnnual)}</b>，占该窗口简单加总收益的 <b>${pct(qm.cashShare,1)}</b>。现金收益不是策略超额收益。`}
function tripWindow(rs){return TRIPS.filter(t=>t.date>=rs[0].date&&t.date<=rs.at(-1).date)}
function tripStats(rs){const ts=tripWindow(rs),dirs=["long","short"].map(direction=>{const g=ts.filter(t=>t.direction===direction),gross=g.reduce((s,t)=>s+t.gross,0);return[direction==="long"?"多头":"空头",g.length,usd(gross),pct(g.length?g.filter(t=>t.gross>0).length/g.length:NaN,1),usd(g.length?gross/g.length:NaN),num(g.length?mean(g.map(t=>t.holding_minutes)):NaN,1)]});table("longShort",["方向","入场笔数","毛损益","胜率","每笔平均毛损益","平均持仓分钟"],dirs);const buckets=[["开盘时段（分钟 1–120）",t=>t.entry_minute<=120],["午间时段（121–270）",t=>t.entry_minute>120&&t.entry_minute<=270],["收盘前时段（271+）",t=>t.entry_minute>270]];table("entryBuckets",["入场时段","入场笔数","多头 / 空头","毛损益","胜率","每笔平均毛损益","平均持仓分钟"],buckets.map(([name,fn])=>{const g=ts.filter(fn),gross=g.reduce((s,t)=>s+t.gross,0);return[name,g.length,`${g.filter(t=>t.direction==="long").length} / ${g.filter(t=>t.direction==="short").length}`,usd(gross),pct(g.length?g.filter(t=>t.gross>0).length/g.length:NaN,1),usd(g.length?gross/g.length:NaN),num(g.length?mean(g.map(t=>t.holding_minutes)):NaN,1)]}))}
function renderVolatility(){const get=(period,q)=>DECOMP.find(x=>x.subperiod===period&&x.component==="volatility_regime_quintile"&&x.bucket===q);table("volatility",["滞后波动率五分位","发表前入场笔数","发表前毛损益","发表后入场笔数","发表后毛损益","发表后每笔平均毛损益"],["Q1","Q2","Q3","Q4","Q5"].map(q=>{const p=get("pre_publication",q),o=get("post_publication",q);return[q,p?Math.round(p.observations):0,p?usd(p.value):"—",o?Math.round(o.observations):0,o?usd(o.value):"—",o&&o.observations?usd(o.value/o.observations):"—"]}))}
function annualSignals(rs){const groups=new Map();for(const r of rs){const y=r.date.slice(0,4);if(!groups.has(y))groups.set(y,[]);groups.get(y).push(r)}table("signalsAnnual",["年份","交易日数","活跃日比例","信号观察数","非零信号观察数","入场笔数","成交事件数","每交易日信号数"],[...groups].map(([y,g])=>[y,g.length,pct(g.filter(r=>r.active).length/g.length,1),Math.round(g.reduce((s,r)=>s+r.signal_observations,0)),Math.round(g.reduce((s,r)=>s+r.nonflat_signal_observations,0)),Math.round(g.reduce((s,r)=>s+r.entries,0)),Math.round(g.reduce((s,r)=>s+r.fill_events,0)),num(g.reduce((s,r)=>s+r.signal_observations,0)/g.length,1)]))}
function concentration(rs){const sorted=[...rs].sort((a,b)=>b.trading_only_pnl-a.trading_only_pnl),positive=sorted.filter(r=>r.trading_only_pnl>0),posTotal=positive.reduce((s,r)=>s+r.trading_only_pnl,0),hhi=positive.reduce((s,r)=>s+(r.trading_only_pnl/posTotal)**2,0),base=compound(rs.map(r=>r.trading_only_ret));table("concentration",["移除最佳交易日数量","占全部盈利日交易损益","移除后的纯交易累计收益"],[1,5,10,20].map(n=>{const top=positive.slice(0,n),dates=new Set(top.map(r=>r.date)),share=top.reduce((s,r)=>s+r.trading_only_pnl,0)/posTotal,ret=compound(rs.map(r=>dates.has(r.date)?0:r.trading_only_ret));return[n,pct(share,1),pct(ret)]}));table("concentrationStats",["诊断指标","数值"],[["纯交易累计收益",pct(base)],["交易损益为正的天数",positive.length],["盈利日有效数量（1 / HHI）",num(1/hhi,1)],["最佳单日占全部盈利日交易损益",pct(positive[0].trading_only_pnl/posTotal,1)]]);const extreme=[...sorted.slice(0,5),...sorted.slice(-5).reverse()];table("extremeDays",["日期","纯交易损益","纯交易收益率","多头","空头","执行成本","融资与借券"],extreme.map(r=>[r.date,usd(r.trading_only_pnl),pct(r.trading_only_ret),usd(r.pnl_long),usd(r.pnl_short),usd(r.pnl_commission+r.pnl_slippage),usd(r.pnl_funding+r.pnl_borrow)]))}
function render(){const rs=rows();if(rs.length<2)return;const m=metrics(rs),cap=capital(rs);$("cards").innerHTML=[["组合 CAGR（含现金利息）",pct(m.cagr),"portfolio"],["纯交易 CAGR",pct(m.tradingCagr),"trading"],["现金利息年化",pct(m.cashAnnual),"cash"],["现金利息占简单加总收益",pct(m.cashShare,1),"cash"],["SPY 总回报 CAGR",pct(m.benchCagr),"benchmark"],["相对现金利率的夏普比率",num(m.sharpe),"risk"],["最大回撤",pct(m.mdd),"risk"],["活跃日比例",pct(m.active/m.n),"activity"]].map(x=>`<div class="card ${x[2]}"><div class="label">${x[0]}</div><div class="value">${x[1]}</div></div>`).join("");lineChart("nav",rs,[{n:"组合净值（含现金利息）",c:"#1769aa",v:path(rs,"ret")},{n:"相同路径纯交易净值",c:"#16803c",v:path(rs,"trading_only_ret")},{n:"SPY 总回报净值",c:"#d97706",v:benchmarkPath(rs)}],true);const c=waterfall(rs),shares=rs.reduce((s,r)=>s+r.shares_traded,0),cents=x=>Number.isFinite(x)?(100*x).toFixed(3)+"¢":"—";table("pnl",["损益组件","美元损益","每交易股","复利链接收益贡献"],COMPONENTS.map(k=>{const p=rs.reduce((s,r)=>s+r["pnl_"+k],0);return[LABELS[k],usd(p),shares?cents(p/shares):"—",pct(c[k])] }));table("identity",["诊断指标","数值"],[["交易日数 / 活跃交易日数",`${m.n} / ${m.active}`],["组合累计收益",pct(m.total)],["纯交易累计收益（移除现金利息后重新复利）",pct(m.trading)],["现金利息的复利链接贡献",pct(c.cash_interest)],["现金利息占简单加总收益",pct(m.cashShare,1)]]);table("capital",["资本与现金诊断","数值"],[["全交易时段平均毛敞口 / AUM",pct(cap.util)],["持仓分钟 / 计划交易分钟",pct(cap.time)],["持仓期间平均毛杠杆",num(cap.lev,2)+"×"],["平均正现金余额 / AUM",pct(cap.positive)],["平均借款余额 / AUM",pct(cap.borrowed)],["非活跃交易日比例",pct(1-m.active/m.n)],["现金收益带来的 CAGR 提升",pct(m.cagr-m.tradingCagr)]]);tripStats(rs);annualSignals(rs);concentration(rs);correlation(rs);scenarios(rs);const years=selectedAnnual(rs);table("annual",["年份","覆盖情况","SPY 总回报","组合收益","纯交易收益","现金利息年化","多头","空头","执行成本","现金利息链接贡献","融资与借券","最大回撤"],years.map(x=>[x.period,x.complete?"完整":"不完整",pct(x.benchmark),pct(x.strategy),pct(x.trading),pct(x.cashAnnual),pct(x.contrib_long),pct(x.contrib_short),pct(x.contrib_commission+x.contrib_slippage),pct(x.contrib_cash_interest),pct(x.contrib_funding+x.contrib_borrow),pct(x.mdd)]))}
function preset(s,e){$("start").value=s;$("end").value=e;render()}$("start").onchange=render;$("end").onchange=render;$("scenarioMode").onchange=()=>scenarios(rows());$("scenarioN").onchange=()=>scenarios(rows());render();
renderCashComparison();renderVolatility();
"""


def render_html(manifest: dict[str, Any], spec: dict[str, Any],
                daily: pd.DataFrame, trips: pd.DataFrame,
                decomposition: pd.DataFrame, quarters: pd.DataFrame,
                annual: pd.DataFrame, benchmark: pd.DataFrame,
                checks: dict[str, float]) -> str:
    records = json_records(daily)
    quarter_records = quarters.replace({np.nan: None}).to_dict(orient="records")
    annual_records = annual.replace({np.nan: None}).to_dict(orient="records")
    trips_json = trip_records(trips)
    decomposition_records = decomposition.replace(
        {np.nan: None}).to_dict(orient="records")
    benchmark_records = [
        {"date": str(index.date()), "ret": float(row.ret)}
        for index, row in benchmark.iterrows()]
    js = (JS.replace("__DAILY__", json.dumps(records, separators=(",", ":")))
          .replace("__BENCHMARK__", json.dumps(
              benchmark_records, separators=(",", ":")))
          .replace("__QUARTERS__", json.dumps(quarter_records, separators=(",", ":")))
          .replace("__ANNUAL__", json.dumps(annual_records, separators=(",", ":")))
          .replace("__TRIPS__", json.dumps(trips_json, separators=(",", ":")))
          .replace("__DECOMP__", json.dumps(
              decomposition_records, separators=(",", ":")))
          .replace("__PRE_END__", str(
              pd.Timestamp(spec["evaluation_start"]) - pd.Timedelta(days=1)).split()[0])
          .replace("__POST_START__", str(spec["evaluation_start"])))
    first, last = str(daily.index.min().date()), str(daily.index.max().date())
    headline = headline_label_zh(spec)
    checks_text = html.escape(json.dumps(checks, indent=2, sort_keys=True))
    provenance = html.escape(json.dumps({
        "run_id": manifest.get("run_id"),
        "classification": manifest.get("classification"),
        "spec_sha256": manifest.get("spec_sha256"),
        "git": manifest.get("git"),
        "data_release": manifest.get("data_release"),
        "benchmark": manifest.get("benchmark"),
        "financing_rates": manifest.get("financing_rates"),
    }, indent=2, sort_keys=True), quote=False)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SPY 日内动量：损益归因、相关性与情景分析</title><style>{CSS}</style></head><body><main>
<span class="badge">正式主口径派生分析 · 仅报告点估计</span>
<h1>SPY 日内动量：损益归因、相关性与情景分析</h1>
<p class="meta">主要展示口径：<b>{html.escape(headline)}</b><br>来源运行：<code>{html.escape(str(manifest.get('run_id')))}</code> · 规格文件 SHA-256：<code>{html.escape(str(manifest.get('spec_sha256')))}</code><br>本报告由冻结正式运行的逐日账本派生，不改变策略、正式经济评价或原始 v2 报告。事件名称是描述性标签，不构成单一因果判断。</p>
<section class="panel"><h2>交互窗口</h2><div class="controls"><label>开始日期<input id="start" type="date" value="{first}" min="{first}" max="{last}"></label><label>结束日期<input id="end" type="date" value="{last}" min="{first}" max="{last}"></label><button onclick="preset('{first}','{last}')">全样本</button><button onclick="preset('{first}','2024-04-30')">发表前</button><button onclick="preset('2024-05-01','{last}')">发表后</button><button onclick="preset('2025-01-01','2025-12-31')">2025</button></div></section>
<section class="panel"><h2>必须先看的报告口径修正</h2><div id="cashWarning" class="callout"></div><div class="scroll"><table id="cashCompare"></table></div><p class="note">组合 CAGR 包含正现金按 LIBOR 代理利率或 SOFR 减 50 个基点获得的利息。纯交易 CAGR 逐日使用 <code>组合收益 − 现金利息收益</code> 后重新复利；它保留原回测实际 AUM、仓位和融资路径，因此是同一路径的分析口径，不是另一条自融资回测。现金利息占比使用简单加总日收益：Σ现金利息收益 / Σ组合收益。</p></section>
<section class="panel"><h2>表现与基准对比</h2><div id="cards" class="cards"></div><h3>累计净值（窗口起点=1，对数尺度）</h3><svg id="nav" class="chart" viewBox="0 0 1100 390"></svg><p class="note">蓝线是含现金利息的正式组合；绿线是从每日日收益移除现金利息后的相同路径纯交易序列；橙线是 SPY 总回报。主要指标同时展示两种 CAGR，不把现金收益表述成策略超额收益。</p></section>
<section class="panel"><h2>损益来源与复利链接贡献</h2><p class="note">逐日收益组件精确加总到净收益；复利链接贡献将每一天的组件收益按其后的策略财富增长链接，因此各组件精确加总到所选窗口总收益。分红只改变信号锚点，不作为日内现金损益。</p><svg id="waterfall" class="chart" viewBox="0 0 1000 360"></svg><div class="grid2"><div class="scroll"><table id="pnl"></table></div><div class="scroll"><table id="identity"></table></div></div></section>
<section class="panel"><h2>资本投入、杠杆与现金诊断</h2><div class="scroll"><table id="capital"></table></div><p class="note">平均毛敞口使用引擎逐分钟记录的（|多头名义本金| + |空头名义本金|）×分钟，除以 AUM×计划交易分钟；持仓期间平均杠杆只在有仓位分钟计算。正现金可因卖空所得资金超过 100%，不能简单解释成“闲置比例”。现金收益带来的 CAGR 提升是组合 CAGR 与相同路径纯交易 CAGR 的差，不是因果意义上的超额收益。</p></section>
<section class="panel"><h2>多头、空头与入场时段</h2><div class="grid2"><div class="scroll"><table id="longShort"></table></div><div class="scroll"><table id="entryBuckets"></table></div></div><p class="note">这里直接使用主要口径的完整开平仓账本。毛损益尚未分摊佣金、滑点、融资成本或现金利息；入场时段严格沿用正式规格：开盘时段=分钟 1–120，午间时段=121–270，收盘前时段=271+。</p></section>
<section class="panel"><h2>波动率分组（正式冻结定义）</h2><div class="scroll"><table id="volatility"></table></div><p class="note">Q1 为最低波动、Q5 为最高波动。分组变量是只含已知历史信息的滞后 14 个交易日日波动率；五分位边界仅用 2024-04-30 及以前样本确定。表中为冻结正式分解的开平仓毛损益与入场笔数，避免使用当日波动率造成未来信息泄漏。</p></section>
<section class="panel"><h2>每年信号与实际入场</h2><div class="scroll"><table id="signalsAnnual"></table></div><p class="note">信号观察数是每个计划决策时点的状态记录（包含空仓信号）；非零信号观察数是目标仓位非零的记录；入场笔数是完整开平仓交易数；成交事件数还包含退出和反转成交。四者含义不同，因此不合并成一个容易误读的“信号数”。</p></section>
<section class="panel"><h2>利润是否集中在少数极端日</h2><div class="grid2"><div class="scroll"><table id="concentration"></table></div><div class="scroll"><table id="concentrationStats"></table></div></div><h3>最佳 5 日与最差 5 日（纯交易口径）</h3><div class="scroll"><table id="extremeDays"></table></div><p class="note">排序和集中度均排除现金利息，使用每日纯交易损益 = 净损益 − 现金利息。盈利日交易损益占比的分母只含盈利日，避免总净利润接近零时比例失真；“移除”表示将这些日的纯交易收益置零后重算复利，仍是诊断而非可交易反事实。</p></section>
<section class="panel"><h2>与 SPY 总回报的相关性和暴露</h2><div class="grid2"><div class="scroll"><table id="corr"></table></div><div class="scroll"><table id="conditional"></table></div></div><h3>滚动相关性</h3><svg id="rolling" class="chart" viewBox="0 0 1100 390"></svg><p class="note">主要基准是独立日频 SPY 未调整收盘价加 State Street 现金分红。活跃日、毛交易收益、多头和空头分别报告，避免空仓现金收益机械压低相关性。Beta 和 alpha 保持正式评估程序的日频口径；未提供 HAC 或区块自助法区间。</p></section>
<section class="panel"><h2>季度情景分析</h2><div class="controls"><label>排序<select id="scenarioMode"><option value="worst_benchmark">SPY 最差季度</option><option value="best_benchmark">SPY 最好季度</option><option value="worst_strategy">策略最差季度</option><option value="all">全部季度（按时间）</option></select></label><label>显示数量<select id="scenarioN"><option>5</option><option selected>10</option><option>20</option></select></label></div><div class="scroll"><table id="quarters"></table></div><p class="note">排序只使用完整季度。2008Q1 因策略预热和样本起点不完整、2026Q3 因截至 2026-07-09，均不进入最差或最好季度排名。执行成本 = 佣金 + 滑点；融资与借券为真实融资现金流。</p></section>
<section class="panel"><h2>年度归因</h2><div class="scroll"><table id="annual"></table></div><p class="note">组合收益与纯交易收益并列；现金利息年化为当年日均现金利息收益 ×252；现金利息链接贡献是其对当年累计组合收益的复利链接贡献。</p></section>
<section class="panel"><h2>审计口径与来源记录</h2><div class="grid2"><pre>{checks_text}</pre><pre>{provenance}</pre></div><p class="note">会计恒等式：净损益 = 多头毛损益 + 空头毛损益 − 佣金 − 滑点 + 现金利息 − 杠杆融资成本 − SPY 借券成本；AUM = 前一日 AUM + 净损益。纯交易 CAGR 是在相同已实现 AUM、仓位和融资路径上逐日移除现金利息收益后重新复利的分析序列，不是重新回测的自融资组合，也不应与正式组合净值混写。发表后是评价期，不是从未查看过的样本外测试。</p></section>
<script>{js}</script></main></body></html>"""


def publish(output_dir: Path, publish_html: Path | None,
            report: str, daily: pd.DataFrame, trips: pd.DataFrame,
            decomposition: pd.DataFrame, quarters: pd.DataFrame,
            annual: pd.DataFrame, source_manifest: dict[str, Any],
            checks: dict[str, float]) -> Path:
    if output_dir.exists():
        raise FileExistsError(f"derived output already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    daily_path = output_dir / "headline_attribution_daily.csv"
    quarterly_path = output_dir / "headline_attribution_quarterly.csv"
    annual_path = output_dir / "headline_attribution_annual.csv"
    trips_path = output_dir / "headline_round_trips.csv"
    decomposition_path = output_dir / "headline_decomposition.csv"
    report_path = output_dir / "report.html"
    export_columns = [
        "status", "active", "ret", "benchmark_ret", "prev_aum", "aum",
        "net", "cash_interest", "trading_only_pnl", "trading_only_ret",
        "shares_traded", "signal_count", "signal_observations",
        "nonflat_signal_observations", "entries", "fill_events",
        "holding_minutes", "scheduled_minutes",
        "gross_notional_minute_dollars", "positive_cash_minute_dollars",
        "borrowed_cash_minute_dollars",
        *[f"pnl_{name}" for name in COMPONENTS],
        *[f"ret_{name}" for name in COMPONENTS],
    ]
    daily[export_columns].to_csv(daily_path, index_label="session_date")
    quarters.to_csv(quarterly_path, index=False)
    annual.to_csv(annual_path, index=False)
    trips.to_csv(trips_path, index=False)
    decomposition.to_csv(decomposition_path, index=False)
    report_path.write_text(report, encoding="utf-8")
    derived_manifest = {
        "classification": "derived_headline_attribution",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_run_id": source_manifest.get("run_id"),
        "source_spec_sha256": source_manifest.get("spec_sha256"),
        "source_git": source_manifest.get("git"),
        "checks": checks,
        "files": {
            path.name: {"bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in (daily_path, quarterly_path, annual_path, trips_path,
                         decomposition_path, report_path)
        },
    }
    manifest_path = output_dir / "attribution_manifest.json"
    manifest_path.write_text(
        json.dumps(derived_manifest, indent=2, sort_keys=True), encoding="utf-8")
    success = {"manifest_sha256": sha256(manifest_path)}
    (output_dir / "_SUCCESS").write_text(
        json.dumps(success, indent=2, sort_keys=True), encoding="utf-8")
    if publish_html is not None:
        publish_html.parent.mkdir(parents=True, exist_ok=True)
        publish_html.write_text(report, encoding="utf-8")
    return report_path


def main() -> int:
    args = parse_args()
    results_dir = args.results_dir.resolve()
    output_dir = args.output_dir.resolve()
    manifest_path = results_dir / "manifest.json"
    success_path = results_dir / "_SUCCESS"
    if not manifest_path.exists() or not success_path.exists():
        raise RuntimeError("source run is not a completed published evaluation")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.setdefault("run_id", results_dir.name)
    if manifest.get("classification") != "formal_post_publication_evaluation":
        raise RuntimeError("attribution report requires a formal evaluation run")
    if manifest.get("git", {}).get("dirty"):
        raise RuntimeError("formal source run unexpectedly records dirty Git state")
    spec_path, spec = resolve_spec(manifest)
    manifest["resolved_spec_path"] = str(spec_path)
    daily, checks = prepare_daily(
        results_dir, spec, args.benchmark_daily.resolve(),
        args.dividends.resolve())
    daily, trips = load_headline_ledgers(results_dir, spec, daily)
    decomposition = load_headline_decomposition(results_dir, spec)
    evaluation_start = pd.Timestamp(spec["evaluation_start"])
    pre_metrics = reporting_metrics(daily[daily.index < evaluation_start])
    post_metrics = reporting_metrics(daily[daily.index >= evaluation_start])
    checks.update({
        f"pre_{key}": value for key, value in pre_metrics.items()
    })
    checks.update({
        f"post_{key}": value for key, value in post_metrics.items()
    })
    close = runner.load_daily_benchmark(args.benchmark_daily.resolve())
    dividends = load_dividends(args.dividends.resolve())
    benchmark = full_benchmark(
        close, dividends, daily.index.min(), daily.index.max())
    quarters, annual = period_tables(daily, close, dividends)
    report = render_html(
        manifest, spec, daily, trips, decomposition, quarters, annual,
        benchmark, checks)
    report_path = publish(
        output_dir, args.publish_html.resolve() if args.publish_html else None,
        report, daily, trips, decomposition, quarters, annual, manifest,
        checks)
    print(f"written: {report_path}")
    print(json.dumps({
        "headline_cell": headline_cell_id(spec),
        "sessions": len(daily),
        "complete_quarters": int(quarters["complete"].sum()),
        "checks": checks,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
