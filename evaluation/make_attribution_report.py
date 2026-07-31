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
    "2008Q3": "Lehman collapse / financial crisis",
    "2008Q4": "Global financial crisis",
    "2009Q1": "Financial-crisis bottom",
    "2010Q2": "European sovereign-debt crisis",
    "2011Q3": "US debt ceiling / euro-area crisis",
    "2015Q3": "China slowdown selloff",
    "2018Q4": "Fed tightening / trade tensions",
    "2020Q1": "COVID-19 shock",
    "2022Q2": "Inflation and rate hikes",
    "2022Q3": "Continued rate hikes",
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
    daily["year"] = daily.index.year
    daily["month"] = daily.index.to_period("M").astype(str)
    daily["quarter"] = daily.index.to_period("Q").astype(str)
    daily["week"] = daily.index.to_period("W-FRI").astype(str)
    return daily, checks


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
        "shares_traded", *[f"pnl_{name}" for name in COMPONENTS],
        *[f"ret_{name}" for name in COMPONENTS],
    ]
    out = daily[fields].copy()
    out.insert(0, "date", [str(x.date()) for x in out.index])
    out.insert(1, "week", daily["week"].to_numpy())
    out.insert(2, "month", daily["month"].to_numpy())
    out.insert(3, "quarter", daily["quarter"].to_numpy())
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
"""


JS = r"""
const DAILY=__DAILY__, BENCHMARK=__BENCHMARK__, QUARTERS=__QUARTERS__, ANNUAL=__ANNUAL__;
const COMPONENTS=["long","short","commission","slippage","cash_interest","funding","borrow"];
const LABELS={long:"Long gross",short:"Short gross",commission:"Commission",slippage:"Slippage",
cash_interest:"Cash interest",funding:"Leveraged funding",borrow:"SPY borrow"};
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
function metrics(rs){const s=rs.map(r=>r.ret),b=rs.map(r=>r.benchmark_ret),bm=bmRows(rs),h=rs.map(r=>r.cash_hurdle_ret),days=(Date.parse(rs.at(-1).date)-Date.parse(rs[0].date))/864e5,years=Math.max(days/365.2425,1/365.2425),tot=compound(s),bt=compound(bm.map(x=>x.ret)),sd=std(s),[beta,alpha]=betaAlpha(rs);return{total:tot,cagr:(1+tot)**(1/years)-1,bench:bt,benchCagr:(1+bt)**(1/years)-1,vol:sd*Math.sqrt(252),sharpe:mean(s.map((x,i)=>x-h[i]))/sd*Math.sqrt(252),mdd:mdd(s),pearson:corr(s,b),spearman:corr(ranks(s),ranks(b)),beta,alpha,active:rs.filter(r=>r.active).length,n:rs.length}}
function path(rs,key){let w=1;return rs.map(r=>{w*=1+r[key];return w})}
function benchmarkPath(rs){const bm=bmRows(rs),byDate=new Map();let w=1;for(const r of bm){w*=1+r.ret;byDate.set(r.date,w)}let last=1;return rs.map(r=>{if(byDate.has(r.date))last=byDate.get(r.date);return last})}
function lineChart(id,rs,series,log=false,yBounds=null){const svg=$(id),W=1100,H=390,L=62,R=18,T=18,B=38,n=rs.length;if(n<2){svg.innerHTML="";return}let vals=series.flatMap(s=>s.v.filter(Number.isFinite));let lo=yBounds?yBounds[0]:Math.min(...vals),hi=yBounds?yBounds[1]:Math.max(...vals);if(log){lo=Math.log(Math.max(lo*.98,1e-8));hi=Math.log(hi*1.02)}if(hi===lo){hi+=1;lo-=1}const x=i=>L+(W-L-R)*i/(n-1),y=v=>T+(H-T-B)*(1-((log?Math.log(v):v)-lo)/(hi-lo));let out="";for(let j=0;j<5;j++){const yy=T+(H-T-B)*j/4;out+=`<line x1="${L}" x2="${W-R}" y1="${yy}" y2="${yy}" stroke="#e7ecf2"/>`}for(const s of series)out+=`<polyline fill="none" stroke="${s.c}" stroke-width="1.7" points="${s.v.map((v,i)=>Number.isFinite(v)?`${x(i)},${y(v)}`:"").join(" ")}"/>`;out+=series.map((s,i)=>`<text x="${L+10+i*180}" y="${T+14}" fill="${s.c}" font-size="12">${s.n}</text>`).join("");out+=`<text x="${L}" y="${H-10}" font-size="11" fill="#64748b">${rs[0].date}</text><text x="${W-R}" y="${H-10}" text-anchor="end" font-size="11" fill="#64748b">${rs.at(-1).date}</text>`;svg.innerHTML=out}
function waterfall(rs){const c=linked(rs),arr=COMPONENTS.map(k=>({k,v:c[k]})),svg=$("waterfall"),W=1000,H=360,L=150,R=20,T=20,B=28,max=Math.max(...arr.map(x=>Math.abs(x.v)),.001),x0=L+(W-L-R)/2,scale=(W-L-R)/2/max;let out=`<line x1="${x0}" x2="${x0}" y1="${T}" y2="${H-B}" stroke="#94a3b8"/>`;arr.forEach((a,i)=>{const y=T+i*(H-T-B)/arr.length+5,h=(H-T-B)/arr.length-10,x=a.v>=0?x0:x0+a.v*scale,w=Math.abs(a.v)*scale;out+=`<text x="${L-8}" y="${y+h*.72}" text-anchor="end" font-size="12">${LABELS[a.k]}</text><rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${COLORS[a.k]}"><title>${LABELS[a.k]} ${pct(a.v)}</title></rect><text x="${a.v>=0?x+w+5:x-5}" y="${y+h*.72}" text-anchor="${a.v>=0?'start':'end'}" font-size="11">${pct(a.v)}</text>`});svg.innerHTML=out;return c}
function table(id,head,body){$(id).innerHTML=`<thead><tr>${head.map(x=>`<th>${x}</th>`).join("")}</tr></thead><tbody>${body.map(r=>`<tr>${r.map(x=>`<td>${x}</td>`).join("")}</tr>`).join("")}</tbody>`}
function rolling(rs,w){const out=[];for(let i=w-1;i<rs.length;i++){const g=rs.slice(i-w+1,i+1);out.push({date:rs[i].date,v:corr(g.map(x=>x.ret),g.map(x=>x.benchmark_ret))})}return out}
function correlation(rs){const freqs=[["Daily",rs.map(r=>[r.ret,r.benchmark_ret])],["Weekly",grouped(rs,"week")],["Monthly",grouped(rs,"month")],["Quarterly",grouped(rs,"quarter")]],active=rs.filter(r=>r.active),[bu,au]=betaAlpha(rs.filter(r=>r.benchmark_ret>0)),[bd,ad]=betaAlpha(rs.filter(r=>r.benchmark_ret<0));table("corr",["Frequency","Observations","Pearson","Spearman"],freqs.map(([n,p])=>[n,p.length,num(corr(p.map(x=>x[0]),p.map(x=>x[1])),3),num(corr(ranks(p.map(x=>x[0])),ranks(p.map(x=>x[1]))),3)]));const up=rs.filter(r=>r.benchmark_ret>0),dn=rs.filter(r=>r.benchmark_ret<0);table("conditional",["Measure","Value"],[["Active-day daily correlation",num(corr(active.map(x=>x.ret),active.map(x=>x.benchmark_ret)),3)],["Gross trading daily correlation",num(corr(rs.map(x=>x.ret_long+x.ret_short),rs.map(x=>x.benchmark_ret)),3)],["Long contribution correlation",num(corr(rs.map(x=>x.ret_long),rs.map(x=>x.benchmark_ret)),3)],["Short contribution correlation",num(corr(rs.map(x=>x.ret_short),rs.map(x=>x.benchmark_ret)),3)],["Up-market beta",num(bu,3)],["Down-market beta",num(bd,3)],["Up capture (mean return ratio)",num(mean(up.map(x=>x.ret))/mean(up.map(x=>x.benchmark_ret)),3)],["Down capture (mean return ratio)",num(mean(dn.map(x=>x.ret))/mean(dn.map(x=>x.benchmark_ret)),3)]]);const a=rolling(rs,63),b=rolling(rs,252),dates=a.map(x=>({date:x.date})),bm=new Map(b.map(x=>[x.date,x.v]));lineChart("rolling",dates,[{n:"63-session correlation",c:"#1769aa",v:a.map(x=>x.v)},{n:"252-session correlation",c:"#d97706",v:a.map(x=>bm.get(x.date)??NaN)}],false,[-1,1])}
function scenarios(rs){const mode=$("scenarioMode").value,n=+$('scenarioN').value;let q=QUARTERS.filter(x=>x.complete&&x.start>=rs[0].date&&x.end<=rs.at(-1).date);if(mode==="worst_benchmark")q.sort((a,b)=>a.benchmark-b.benchmark);else if(mode==="best_benchmark")q.sort((a,b)=>b.benchmark-a.benchmark);else if(mode==="worst_strategy")q.sort((a,b)=>a.strategy-b.strategy);else q.sort((a,b)=>a.period.localeCompare(b.period));if(mode!=="all")q=q.slice(0,n);table("quarters",["Quarter","Event (descriptive)","SPY TR","Strategy","Long","Short","Execution","Cash interest","Funding + borrow","Strategy MDD"],q.map(x=>[x.period,x.event||"—",pct(x.benchmark),pct(x.strategy),pct(x.contrib_long),pct(x.contrib_short),pct(x.contrib_commission+x.contrib_slippage),pct(x.contrib_cash_interest),pct(x.contrib_funding+x.contrib_borrow),pct(x.mdd)]))}
function selectedAnnual(rs){const groups=new Map();for(const r of rs){const y=r.date.slice(0,4);if(!groups.has(y))groups.set(y,[]);groups.get(y).push(r)}return [...groups].map(([period,g])=>{const c=linked(g),base=ANNUAL.find(x=>x.period===period),bm=BENCHMARK.filter(x=>x.date>=g[0].date&&x.date<=g.at(-1).date);return{period,complete:base&&g[0].date===base.start&&g.at(-1).date===base.end,benchmark:compound(bm.map(x=>x.ret)),strategy:compound(g.map(x=>x.ret)),mdd:mdd(g.map(x=>x.ret)),...Object.fromEntries(COMPONENTS.map(k=>["contrib_"+k,c[k]]))}})}
function render(){const rs=rows();if(rs.length<2)return;const m=metrics(rs);$("cards").innerHTML=[["Total return",pct(m.total)],["CAGR",pct(m.cagr)],["SPY TR CAGR",pct(m.benchCagr)],["Excess CAGR",pct(m.cagr-m.benchCagr)],["Sharpe vs cash",num(m.sharpe)],["Max drawdown",pct(m.mdd)],["Beta",num(m.beta)],["Alpha annualized",pct(m.alpha)]].map(x=>`<div class="card"><div class="label">${x[0]}</div><div class="value">${x[1]}</div></div>`).join("");lineChart("nav",rs,[{n:"Strategy NAV",c:"#1769aa",v:path(rs,"ret")},{n:"SPY total-return NAV",c:"#d97706",v:benchmarkPath(rs)}],true);const c=waterfall(rs),shares=rs.reduce((s,r)=>s+r.shares_traded,0),cents=x=>Number.isFinite(x)?(100*x).toFixed(3)+"¢":"—";table("pnl",["Component","Dollar P&L","Per traded share","Linked return contribution"],COMPONENTS.map(k=>{const p=rs.reduce((s,r)=>s+r["pnl_"+k],0);return[LABELS[k],usd(p),shares?cents(p/shares):"—",pct(c[k])] }));table("identity",["Diagnostic","Value"],[["Sessions / active sessions",`${m.n} / ${m.active}`],["Linked contributions sum",pct(Object.values(c).reduce((s,x)=>s+x,0))],["Strategy total return",pct(m.total)],["Pure trading contribution (ex cash interest)",pct(m.total-c.cash_interest)],["Cash-interest contribution",pct(c.cash_interest)]]);correlation(rs);scenarios(rs);const years=selectedAnnual(rs);table("annual",["Year","Coverage","SPY TR","Strategy","Long","Short","Execution","Cash interest","Funding + borrow","MDD"],years.map(x=>[x.period,x.complete?"Full":"Partial",pct(x.benchmark),pct(x.strategy),pct(x.contrib_long),pct(x.contrib_short),pct(x.contrib_commission+x.contrib_slippage),pct(x.contrib_cash_interest),pct(x.contrib_funding+x.contrib_borrow),pct(x.mdd)]))}
function preset(s,e){$("start").value=s;$("end").value=e;render()}$("start").onchange=render;$("end").onchange=render;$("scenarioMode").onchange=()=>scenarios(rows());$("scenarioN").onchange=()=>scenarios(rows());render();
"""


def render_html(manifest: dict[str, Any], spec: dict[str, Any],
                daily: pd.DataFrame, quarters: pd.DataFrame,
                annual: pd.DataFrame, benchmark: pd.DataFrame,
                checks: dict[str, float]) -> str:
    records = json_records(daily)
    quarter_records = quarters.replace({np.nan: None}).to_dict(orient="records")
    annual_records = annual.replace({np.nan: None}).to_dict(orient="records")
    benchmark_records = [
        {"date": str(index.date()), "ret": float(row.ret)}
        for index, row in benchmark.iterrows()]
    js = (JS.replace("__DAILY__", json.dumps(records, separators=(",", ":")))
          .replace("__BENCHMARK__", json.dumps(
              benchmark_records, separators=(",", ":")))
          .replace("__QUARTERS__", json.dumps(quarter_records, separators=(",", ":")))
          .replace("__ANNUAL__", json.dumps(annual_records, separators=(",", ":"))))
    first, last = str(daily.index.min().date()), str(daily.index.max().date())
    headline = runner.headline_label(spec)
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
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SPY 日内动量：P&amp;L 归因、相关性与情景分析</title><style>{CSS}</style></head><body><main>
<span class="badge">FORMAL HEADLINE DERIVED ANALYSIS · POINT ESTIMATES ONLY</span>
<h1>SPY 日内动量：P&amp;L 归因、相关性与情景分析</h1>
<p class="meta">Headline: <b>{html.escape(headline)}</b><br>Source run: <code>{html.escape(str(manifest.get('run_id')))}</code> · spec SHA-256: <code>{html.escape(str(manifest.get('spec_sha256')))}</code><br>本报告由冻结正式 run 的逐日账本派生，不改变策略、正式经济评价或原始 v2 报告。事件名称是描述性标签，不构成单一因果判断。</p>
<section class="panel"><h2>交互窗口</h2><div class="controls"><label>开始日期<input id="start" type="date" value="{first}" min="{first}" max="{last}"></label><label>结束日期<input id="end" type="date" value="{last}" min="{first}" max="{last}"></label><button onclick="preset('{first}','{last}')">全样本</button><button onclick="preset('{first}','2024-04-30')">发表前</button><button onclick="preset('2024-05-01','{last}')">发表后</button><button onclick="preset('2025-01-01','2025-12-31')">2025</button></div></section>
<section class="panel"><h2>Performance vs benchmark</h2><div id="cards" class="cards"></div><h3>累计净值（窗口起点=1，对数尺度）</h3><svg id="nav" class="chart" viewBox="0 0 1100 390"></svg></section>
<section class="panel"><h2>P&amp;L 来源与复利链接贡献</h2><p class="note">逐日收益组件精确加总到 net return；linked contribution 将每一天的组件收益按其后的策略财富增长链接，因此各组件精确加总到所选窗口总收益。分红只改变信号锚点，不作为日内现金 P&amp;L。</p><svg id="waterfall" class="chart" viewBox="0 0 1000 360"></svg><div class="grid2"><div class="scroll"><table id="pnl"></table></div><div class="scroll"><table id="identity"></table></div></div></section>
<section class="panel"><h2>与 SPY total return 的相关性和暴露</h2><div class="grid2"><div class="scroll"><table id="corr"></table></div><div class="scroll"><table id="conditional"></table></div></div><h3>Rolling correlation</h3><svg id="rolling" class="chart" viewBox="0 0 1100 390"></svg><p class="note">主 benchmark 是独立日频 SPY raw close 加 State Street 现金分红。active-day、gross、long、short 分开报告，避免空仓现金收益机械压低相关性。Beta/alpha 保持正式 runner 的日频口径；未提供 HAC 或 bootstrap 区间。</p></section>
<section class="panel"><h2>季度情景分析</h2><div class="controls"><label>排序<select id="scenarioMode"><option value="worst_benchmark">SPY 最差季度</option><option value="best_benchmark">SPY 最好季度</option><option value="worst_strategy">策略最差季度</option><option value="all">全部季度（按时间）</option></select></label><label>显示数量<select id="scenarioN"><option>5</option><option selected>10</option><option>20</option></select></label></div><div class="scroll"><table id="quarters"></table></div><p class="note">排序只使用完整季度。2008Q1 因策略 warm-up/样本起点不完整、2026Q3 因截至 2026-07-09，均不进入最差/最好季度排名。Execution = commission + slippage；Funding + borrow 为真实融资现金流。</p></section>
<section class="panel"><h2>年度归因</h2><div class="scroll"><table id="annual"></table></div></section>
<section class="panel"><h2>审计口径与 provenance</h2><div class="grid2"><pre>{checks_text}</pre><pre>{provenance}</pre></div><p class="note">会计恒等式：net = long gross + short gross − commission − slippage + cash interest − leveraged funding − SPY borrow；AUM = previous AUM + net。Cash interest 是未使用现金收益；“pure trading contribution”仅表示在相同已实现仓位路径下移除 cash-interest 组件后的贡献，不是重新回测的自融资组合。Post-publication 是评价期，不是 untouched OOS。</p></section>
<script>{js}</script></main></body></html>"""


def publish(output_dir: Path, publish_html: Path | None,
            report: str, daily: pd.DataFrame, quarters: pd.DataFrame,
            annual: pd.DataFrame, source_manifest: dict[str, Any],
            checks: dict[str, float]) -> Path:
    if output_dir.exists():
        raise FileExistsError(f"derived output already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    daily_path = output_dir / "headline_attribution_daily.csv"
    quarterly_path = output_dir / "headline_attribution_quarterly.csv"
    annual_path = output_dir / "headline_attribution_annual.csv"
    report_path = output_dir / "report.html"
    export_columns = [
        "status", "active", "ret", "benchmark_ret", "prev_aum", "aum",
        "shares_traded", *[f"pnl_{name}" for name in COMPONENTS],
        *[f"ret_{name}" for name in COMPONENTS],
    ]
    daily[export_columns].to_csv(daily_path, index_label="session_date")
    quarters.to_csv(quarterly_path, index=False)
    annual.to_csv(annual_path, index=False)
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
            for path in (daily_path, quarterly_path, annual_path, report_path)
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
    close = runner.load_daily_benchmark(args.benchmark_daily.resolve())
    dividends = load_dividends(args.dividends.resolve())
    benchmark = full_benchmark(
        close, dividends, daily.index.min(), daily.index.max())
    quarters, annual = period_tables(daily, close, dividends)
    report = render_html(
        manifest, spec, daily, quarters, annual, benchmark, checks)
    report_path = publish(
        output_dir, args.publish_html.resolve() if args.publish_html else None,
        report, daily, quarters, annual, manifest, checks)
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
