"""Add-on interactive report (report2) for a finished paper-replication-v1 run.

report2.html is a focused companion to report.html: one interactive section
where the reader picks a start date and an end date, and both the month-end
cumulative-NAV chart (log scale) and the metrics table are recomputed live
for the chosen window:

- chart series: paper Q24 strategy (compounded from the published monthly
  table), local strategy, local SPY benchmark (month-end equity of the
  selected profile x tier x dividend mode);
- metrics table columns: 指标 | 论文策略 | 本地 SPY | 本地策略;
- all three columns are recomputed from month-end series with a
  monthly methodology (compounding, vol = monthly std x sqrt(12), Sharpe with
  rf = 0, MDD on month-end points) — comparable to each other inside this
  table, but NOT to the paper's daily-frequency Table 3 numbers or to the
  engine's daily full-sample metrics in report.html section 3;
- the SPY column uses the local SPY month-end series (same equity file, same
  profile x tier x dividend selection as the local strategy), because the
  paper publishes no monthly benchmark series; the paper's Table 3 SPY
  buy-and-hold values stay as a fixed reference line below the table.

The script reads only existing artifacts (config, paper Q24 table, a finished
run's equity_curves_monthly.csv and manifest.json) and writes only the
--output path. It never modifies report.html, the CSVs, or the manifest.

Run:
    python experiments/paper_replication_v1/make_report2.py \
      --results-dir experiments/paper_replication_v1/results/data-v1.0_q24_detailed_report_20260730_v2
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MONTH_COLUMNS = [
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        default=REPO_ROOT / "experiments/paper_replication_v1/config.yml",
        type=Path,
        help="experiment config (paper reference + primary selection)",
    )
    parser.add_argument(
        "--results-dir",
        default=REPO_ROOT
        / "experiments/paper_replication_v1/results"
        / "data-v1.0_q24_detailed_report_20260730_v2",
        type=Path,
        help="finished run directory holding equity_curves_monthly.csv",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="output html path (default: <results-dir>/report2.html)",
    )
    return parser.parse_args()


def resolve_repo_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (REPO_ROOT / path)


def paper_monthly_records(reference_csv: Path) -> list[dict]:
    """Wide Q24 table -> long records [{date: month-end, ret: pct}]."""
    frame = pd.read_csv(reference_csv)
    records: list[dict] = []
    for _, row in frame.iterrows():
        year = int(row["year"])
        for month, column in enumerate(MONTH_COLUMNS, start=1):
            value = row[column]
            if pd.isna(value):
                continue
            month_end = pd.Timestamp(year, month, 1) + pd.offsets.MonthEnd(0)
            records.append({
                "date": month_end.strftime("%Y-%m-%d"),
                "ret": round(float(value), 6),
            })
    return records


def records_json(records: list[dict] | dict) -> str:
    text = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
    return text.replace("</", "<\\/")


TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Report 2 · 区间互动对照 — SPY Intraday Momentum Q24 复现</title>
<style>
:root { --ink:#17212b; --muted:#617080; --navy:#123a5a; --blue:#1875c1;
  --red:#c7423b; --green:#27835a; --amber:#a96d00; --line:#d9e1e8;
  --card:#fff; }
* { box-sizing:border-box; }
body { margin:0; color:var(--ink); background:#eef2f5;
  font:15px/1.5 Inter,Segoe UI,system-ui,sans-serif; }
header { background:linear-gradient(135deg,#0c2e49,#195d8e); color:#fff;
  padding:30px max(24px,calc((100vw - 1180px)/2)); }
header h1 { margin:0 0 8px; font-size:26px; }
header p { margin:0; color:#dcecf7; max-width:1000px; }
main { max-width:1180px; margin:0 auto; padding:24px; }
h2 { color:var(--navy); margin:26px 0 10px; font-size:20px; }
.notice { background:#fff4cf; border-left:5px solid #d59b00;
  padding:12px 16px; margin-bottom:18px; }
.panel { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:18px; margin:14px 0; }
.controls { display:grid; grid-template-columns:repeat(3,minmax(150px,1fr));
  gap:12px; background:#f7fafc; border:1px solid var(--line);
  border-radius:10px; padding:14px; }
label { color:var(--muted); font-size:12px; font-weight:700; }
select,input[type=date] { display:block; width:100%; margin-top:4px;
  padding:8px 10px; border:1px solid #b9c6d0; border-radius:6px;
  background:#fff; color:var(--ink); font:inherit; }
.presets { grid-column:1 / -1; display:flex; align-items:center; gap:8px;
  flex-wrap:wrap; }
.presets span { color:var(--muted); font-size:12px; font-weight:700; }
.presets button { border:1px solid #b9c6d0; background:#fff; color:var(--navy);
  border-radius:6px; padding:6px 12px; cursor:pointer; font:inherit;
  font-size:13px; }
.presets button:hover { background:#e9f2f9; border-color:var(--blue); }
.hint { margin:10px 2px 0; font-size:13px; color:var(--muted); }
.hint.error { color:var(--red); font-weight:700; }
.charthead { display:flex; justify-content:space-between; align-items:baseline;
  gap:16px; flex-wrap:wrap; }
.legend { display:flex; gap:18px; flex-wrap:wrap; color:var(--muted);
  font-size:13px; }
.swatch { display:inline-block; width:12px; height:3px; vertical-align:middle;
  margin-right:5px; }
.hover { font-size:13px; color:var(--navy); font-weight:600; min-height:20px;
  font-variant-numeric:tabular-nums; }
svg { width:100%; height:auto; display:block; background:#fff;
  border:1px solid var(--line); border-radius:8px; }
.table-wrap { overflow:auto; border:1px solid var(--line); margin-top:6px; }
table { border-collapse:collapse; width:100%; font-size:14px; background:#fff; }
th,td { border-bottom:1px solid var(--line); padding:8px 12px; text-align:right;
  white-space:nowrap; }
th { background:#e9f0f5; color:#264a64; }
th:first-child,td:first-child { text-align:left; }
tr:hover td { background:#f4f8fb; }
td.range { font-size:12.5px; color:var(--muted); }
.static-ref { background:#f2f7fb; border-left:4px solid var(--navy);
  padding:10px 14px; font-size:13px; color:#25475f; }
.footnote { color:var(--muted); font-size:12.5px; margin:8px 0 0; }
details { background:#fff; border:1px solid var(--line); border-radius:8px;
  padding:10px 14px; margin-top:18px; }
pre { overflow:auto; max-height:320px; font-size:11px; }
a { color:var(--blue); }
@media(max-width:760px) { .controls { grid-template-columns:1fr 1fr; }
  main { padding:12px; } header h1 { font-size:21px; } }
</style>
</head>
<body>
<header>
  <h1>Report 2 — 区间互动对照（月末净值，log scale）</h1>
  <p>自由选择开始 / 结束日期，累计净值图与指标表随区间实时重算。
  论文 Q24 复现（版本 __PAPER_REVISION__），数据 data-v1.0；
  本页为 report.html 的补充视图，由 make_report2.py 生成。</p>
</header>
<main>
<div class="notice"><strong>独立复现实验。</strong>
本页所有数字为 replication-only 诊断，不消费、不修改、也不替代冻结的正式经济评价。
论文样本与本地样本区间不同；区间内指标按月频口径计算，与论文 Table 3（日频）
以及 report.html 第 3 节（引擎日频全区间）口径不同，不能直接等同。</div>

<section class="panel">
  <div class="controls">
    <label>开始日期<input type="date" id="start"></label>
    <label>结束日期<input type="date" id="end"></label>
    <label>Profile<select id="profile"></select></label>
    <label>Tier<select id="tier"></select></label>
    <label>Dividend mode<select id="dividend"></select></label>
    <div class="presets"><span>快捷区间</span>
      <button type="button" data-preset="all">全区间</button>
      <button type="button" data-preset="postpub">论文发表后 (2024-05+)</button>
      <button type="button" data-preset="gfc">金融危机 2008–2009</button>
      <button type="button" data-preset="covid">新冠 2020–2021</button>
      <button type="button" data-preset="y5">近 5 年</button>
    </div>
  </div>
  <div class="hint" id="winHint"></div>

  <h2>累计净值（月末，log scale）</h2>
  <div class="charthead">
    <div class="legend">
      <span><i class="swatch" style="background:#123a5a"></i>论文策略（Q24 月度表复利）</span>
      <span><i class="swatch" style="background:#1875c1"></i>本地策略（所选组合）</span>
      <span><i class="swatch" style="background:#c7423b"></i>本地 SPY（同一净值文件）</span>
    </div>
    <div class="hover" id="hover"></div>
  </div>
  <svg id="chart" viewBox="0 0 980 430" role="img"
    aria-label="所选区间内三条月末累计净值曲线（对数刻度）"></svg>

  <h2>指标（随所选区间重算）</h2>
  <div class="table-wrap">
    <table><thead><tr>
      <th>指标</th><th>论文策略</th><th>本地 SPY †</th><th>本地策略</th>
    </tr></thead><tbody id="mBody"></tbody></table>
  </div>
  <p class="static-ref" id="paperRefLine"></p>
  <p class="footnote">† 本地 SPY 列：与图上红色曲线同源（同一净值文件的
  spy_equity 月末序列，与本地策略同一 profile × tier × dividend 组合），随所选
  区间重算；论文未公布 SPY 的逐月序列，其 Table 3 SPY Buy&Hold 全区间值
  （2007 至 2024 初，日频口径）见下方静态参考行，口径不同不能直接等同。</p>
  <p class="footnote">口径：论文策略列 = 论文 Q24 月度收益表（2007-05 至
  __PAPER_LAST__）在所选区间内的复利；本地策略 / 本地 SPY = 所选组合月末净值
  （strategy_equity / spy_equity）推得的月收益。
  波动 = 月收益标准差 × √12；Sharpe = 月均 / 月标准差 × √12（rf=0）；
  最大回撤基于月末净值点（浅于日频回撤）；区间首月收益计入区间
  （净值在区间起点之前归一为 1）。本地净值最后一个点为 __LAST_PARTIAL__，
  按月末标注为 __LAST_PARTIAL_LABEL__。</p>
</section>

<details><summary>数据与 provenance</summary>
<pre>__PROVENANCE__</pre>
</details>
</main>

<script>
const CURVES = __CURVES_JSON__;
const PAPER = __PAPER_JSON__;
const PAPER_REF = __PAPER_REF_JSON__;
const MATRIX = __MATRIX_JSON__;

const byId = id => document.getElementById(id);
const fmt = (v, d=2) => v===null || v===undefined || !Number.isFinite(Number(v))
  ? "—" : Number(v).toFixed(d);
const paperMap = new Map(PAPER.map(r => [r.date, r.ret/100]));
const FIRST_MONTH = PAPER[0].date.slice(0,7) + "-01";
const LAST_DATE = CURVES.reduce((a,r) => r.date>a ? r.date : a,
  PAPER[PAPER.length-1].date);

function fillSelect(id, values, selected) {
  byId(id).innerHTML = values.map(v =>
    '<option value="'+v+'"'+(v===selected?' selected':'')+'>'+v+'</option>').join('');
}
fillSelect("profile", MATRIX.profiles, MATRIX.primary_profile);
fillSelect("tier", MATRIX.tiers, MATRIX.primary_tier);
fillSelect("dividend", MATRIX.dividend_modes, MATRIX.primary_dividend_mode);

const seriesCache = {};
function getLocal() {
  const k = byId("profile").value+"|"+byId("tier").value+"|"+byId("dividend").value;
  if (seriesCache[k]) return seriesCache[k];
  const rows = CURVES.filter(r =>
    r.profile+"|"+r.tier+"|"+r.dividend_mode === k)
    .sort((a,b) => a.date.localeCompare(b.date));
  const toRets = L => L.map((v,i) => i ? v/L[i-1]-1 : v-1);
  const s = { dates: rows.map(r=>r.date),
    strat: toRets(rows.map(r=>r.strategy_equity)),
    spy: toRets(rows.map(r=>r.spy_equity)) };
  seriesCache[k] = s;
  return s;
}

function metrics(rets) {
  const n = rets.length;
  if (!n) return null;
  let nav = 1, peak = 1, mdd = 0, sum = 0, hit = 0;
  const curve = rets.map(r => {
    nav *= 1+r; peak = Math.max(peak, nav);
    mdd = Math.min(mdd, nav/peak-1); sum += r;
    if (r > 0) hit++; return nav;
  });
  const mean = sum/n;
  const sd = n>1 ? Math.sqrt(rets.reduce((a,r)=>a+(r-mean)*(r-mean),0)/(n-1)) : NaN;
  return { n, curve, tot: nav-1, cagr: Math.pow(nav, 12/n)-1,
    vol: sd*Math.sqrt(12), sharpe: sd>0 ? mean/sd*Math.sqrt(12) : NaN,
    mdd, hit: hit/n*100 };
}

let chartState = null;
function draw(months, series) {
  const svg = byId("chart");
  const vals = [];
  series.forEach(s => s.nav.forEach(v => { if (v>0) vals.push(v); }));
  if (months.length < 1 || !vals.length) {
    svg.innerHTML = '<text x="490" y="215" text-anchor="middle" fill="#617080">区间内无可画数据</text>';
    chartState = null; return;
  }
  let lo = Math.min(...vals, 1), hi = Math.max(...vals, 1);
  const pad = (Math.log(hi)-Math.log(lo))*0.07 || 0.15;
  lo = Math.exp(Math.log(lo)-pad); hi = Math.exp(Math.log(hi)+pad);
  const W=980, H=430, L=64, R=16, T=16, B=40;
  const x = months.length>1 ? (i => L + i*(W-L-R)/(months.length-1))
    : (() => { const mid=(L+W-R)/2; return () => mid; })();
  const y = v => T + (Math.log(hi)-Math.log(v))/(Math.log(hi)-Math.log(lo))*(H-T-B);
  let out = "";
  const cands = [0.1,0.15,0.2,0.3,0.5,0.75,1,1.5,2,3,4,5,7,10,15,20,25,40,60,100];
  cands.filter(c => c>lo && c<hi).forEach(c => {
    out += '<line x1="'+L+'" y1="'+y(c)+'" x2="'+(W-R)+'" y2="'+y(c)+
      '" stroke="'+(c===1?"#b8c6d2":"#e2e8ed")+'"'+(c===1?' stroke-dasharray="5 4"':'')+'/>'+
      '<text x="'+(L-8)+'" y="'+(y(c)+4)+'" text-anchor="end" font-size="11" fill="#617080">'+c+'x</text>';
  });
  series.forEach(s => {
    let d = "", started = false;
    months.forEach((m,i) => {
      const v = s.nav.get(m);
      if (v===undefined || !(v>0)) { started = false; return; }
      d += (started?"L":"M")+x(i).toFixed(1)+","+y(v).toFixed(1);
      started = true;
    });
    out += '<path d="'+d+'" fill="none" stroke="'+s.color+'" stroke-width="2.2"/>';
    if (months.length <= 3) {
      months.forEach((m,i) => {
        const v = s.nav.get(m);
        if (v!==undefined && v>0)
          out += '<circle cx="'+x(i)+'" cy="'+y(v)+'" r="3" fill="'+s.color+'"/>';
      });
    }
  });
  const step = Math.max(1, Math.round(months.length/9));
  months.forEach((m,i) => {
    if (i%step===0 || i===months.length-1)
      out += '<text x="'+x(i)+'" y="'+(H-14)+'" text-anchor="middle" font-size="11" fill="#617080">'+m.slice(0,7)+'</text>';
  });
  out += '<line id="xhair" x1="-10" y1="'+T+'" x2="-10" y2="'+(H-B)+'" stroke="#8fa1ae" stroke-dasharray="3 3"/>';
  out += '<rect id="capture" x="'+L+'" y="'+T+'" width="'+(W-L-R)+'" height="'+(H-T-B)+'" fill="transparent"/>';
  svg.innerHTML = out;
  chartState = { months, series, L, R, W };
  const cap = svg.querySelector("#capture");
  cap.addEventListener("mousemove", e => {
    const rect = svg.getBoundingClientRect();
    const sx = (e.clientX-rect.left) * (W/rect.width);
    let i = Math.round((sx-L)/((W-L-R)/Math.max(1,months.length-1)));
    i = Math.max(0, Math.min(months.length-1, i));
    svg.querySelector("#xhair").setAttribute("x1", x(i));
    svg.querySelector("#xhair").setAttribute("x2", x(i));
    const m = months[i];
    const parts = series.map(s => {
      const v = s.nav.get(m);
      return '<span style="color:'+s.color+'">'+s.name+' '+(v===undefined?"—":fmt(v,2)+'x')+'</span>';
    });
    byId("hover").innerHTML = m + " · " + parts.join(" · ");
  });
  cap.addEventListener("mouseleave", () => {
    svg.querySelector("#xhair").setAttribute("x1", -10);
    svg.querySelector("#xhair").setAttribute("x2", -10);
    byId("hover").textContent = "";
  });
}

function navMap(dates, rets, anchor) {
  let nav = 1;
  const m = new Map();
  if (anchor) m.set(anchor, 1.0);
  dates.forEach((d,i) => { nav *= 1+rets[i]; m.set(d, nav); });
  return m;
}
function priorMonthEnd(startStr) {
  const [y, m] = startStr.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, 1));   // 1st of start month
  dt.setUTCDate(0);                             // last day of prior month
  return dt.toISOString().slice(0, 10);
}

function render() {
  const a = byId("start").value, b = byId("end").value;
  const hint = byId("winHint");
  hint.classList.remove("error");
  if (!a || !b) { hint.textContent = "请选择开始与结束日期。"; return; }
  if (a > b) { hint.textContent = "开始日期晚于结束日期，请调整。";
    hint.classList.add("error"); return; }
  const inWin = d => d >= a && d <= b;
  const local = getLocal();
  const lIdx = local.dates.map((d,i)=>inWin(d)?i:-1).filter(i=>i>=0);
  const lDates = lIdx.map(i=>local.dates[i]);
  const lRets = lIdx.map(i=>local.strat[i]);
  const sRets = lIdx.map(i=>local.spy[i]);
  const pDates = PAPER.map(r=>r.date).filter(inWin);
  const pRets = pDates.map(d=>paperMap.get(d));
  const pm = metrics(pRets), lm = metrics(lRets), sm = metrics(sRets);
  const hasData = pDates.length || lDates.length;
  const anchor = hasData ? priorMonthEnd(a) : null;
  const months = hasData ? [anchor, ...new Set([...pDates, ...lDates])].sort() : [];
  const series = [
    { name:"论文策略", color:"#123a5a", nav: navMap(pDates, pRets, anchor) },
    { name:"本地策略", color:"#1875c1", nav: navMap(lDates, lRets, anchor) },
    { name:"本地 SPY", color:"#c7423b", nav: navMap(lDates, sRets, anchor) },
  ];
  draw(months, series);
  const pRange = pm ? pDates[0]+" .. "+pDates[pDates.length-1] : "无数据";
  const lRange = lm ? lDates[0]+" .. "+lDates[lDates.length-1] : "无数据";
  const rows = [
    ["区间（月末落点）", pRange, lRange, lRange, true],
    ["月数", pm&&pm.n, sm&&sm.n, lm&&lm.n],
    ["累计收益 %", pm&&pm.tot*100, sm&&sm.tot*100, lm&&lm.tot*100],
    ["年化收益 %", pm&&pm.cagr*100, sm&&sm.cagr*100, lm&&lm.cagr*100],
    ["年化波动 %（月频）", pm&&pm.vol*100, sm&&sm.vol*100, lm&&lm.vol*100],
    ["Sharpe（月频，rf=0）", pm&&pm.sharpe, sm&&sm.sharpe, lm&&lm.sharpe],
    ["最大回撤 %（月末）", pm&&pm.mdd*100, sm&&sm.mdd*100, lm&&lm.mdd*100],
    ["月胜率 %", pm&&pm.hit, sm&&sm.hit, lm&&lm.hit],
  ];
  const dec = r0 => r0==="月数" ? 0 : 2;
  byId("mBody").innerHTML = rows.map(r =>
    '<tr><td>'+r[0]+'</td>'+
    '<td'+(r[4]?' class="range"':'')+'>'+(typeof r[1]==="number"?fmt(r[1],dec(r[0])):(r[1]??"—"))+'</td>'+
    '<td'+(r[4]?' class="range"':'')+'>'+(typeof r[2]==="number"?fmt(r[2],dec(r[0])):(r[2]??"—"))+'</td>'+
    '<td'+(r[4]?' class="range"':'')+'>'+(typeof r[3]==="number"?fmt(r[3],dec(r[0])):(r[3]??"—"))+'</td></tr>').join('');
  hint.textContent = "当前区间："+a+" 至 "+b+"（以月末落点计：论文 "+
    (pm?pm.n:0)+" 个月 / 本地 "+(lm?lm.n:0)+" 个月；以前一月末为基 = 1.0 起步）。";
  byId("hover").textContent = "";
}

const ref = PAPER_REF;
byId("paperRefLine").textContent =
  "论文 Table 3（PDF p."+ref.page+"，全区间 "+ref.period+"，日频口径）报告值：策略 "+
  ref.strategy+" — 累计 "+ref.total_return_pct+"% / 年化 "+ref.annual_return_pct+
  "% / 波动 "+ref.volatility_pct+"% / Sharpe "+ref.sharpe+" / MDD -"+ref.mdd_pct+
  "% / 胜率 "+ref.hit_ratio_pct+"% / α "+ref.alpha_annualised_pct+"% / β "+ref.beta+
  "；SPY Buy&Hold — 累计 "+ref.benchmark.total_return_pct+"% / 年化 "+
  ref.benchmark.annual_return_pct+"% / 波动 "+ref.benchmark.volatility_pct+
  "% / Sharpe "+ref.benchmark.sharpe+" / MDD -"+ref.benchmark.mdd_pct+
  "% / 胜率 "+ref.benchmark.hit_ratio_pct+"%。";

function preset(key) {
  const end = LAST_DATE;
  const map = {
    all: [FIRST_MONTH, end],
    postpub: ["2024-05-01", end],
    gfc: ["2008-01-01", "2009-12-31"],
    covid: ["2020-01-01", "2021-12-31"],
    y5: null,
  };
  let range = map[key];
  if (key === "y5") {
    const [ey, em] = end.split("-").map(Number);
    const d = new Date(Date.UTC(ey, em - 1 - 59, 1));   // 1st of month, 59 before end
    range = [d.toISOString().slice(0, 10), end];
  }
  byId("start").value = range[0];
  byId("end").value = range[1];
  render();
}
document.querySelectorAll("[data-preset]").forEach(btn =>
  btn.addEventListener("click", () => preset(btn.dataset.preset)));
["start","end"].forEach(id => byId(id).addEventListener("change", render));
["profile","tier","dividend"].forEach(id => byId(id).addEventListener("change", render));
byId("start").min = FIRST_MONTH; byId("start").max = LAST_DATE;
byId("end").min = FIRST_MONTH; byId("end").max = LAST_DATE;
byId("start").value = FIRST_MONTH;
byId("end").value = LAST_DATE;
render();
</script>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    results_dir = resolve_repo_path(args.results_dir)
    output = args.output or (results_dir / "report2.html")
    output = resolve_repo_path(output)

    curves_path = results_dir / "equity_curves_monthly.csv"
    manifest_path = results_dir / "manifest.json"
    reference_path = resolve_repo_path(config["paper"]["reference_table"])
    for path in (curves_path, manifest_path, reference_path):
        if not path.exists():
            raise FileNotFoundError(path)

    curves = pd.read_csv(curves_path)[
        ["profile", "tier", "dividend_mode", "date",
         "strategy_equity", "spy_equity"]
    ]
    curve_records = json.loads(
        curves.to_json(orient="records", double_precision=10))
    paper_records = paper_monthly_records(reference_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    matrix = {
        "profiles": config["matrix"]["profiles"],
        "tiers": config["matrix"]["tiers"],
        "dividend_modes": config["matrix"]["dividend_modes"],
        "primary_profile": config["matrix"]["primary_profile"],
        "primary_tier": config["matrix"]["primary_tier"],
        "primary_dividend_mode": config["matrix"]["primary_dividend_mode"],
    }
    provenance = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "generator": "experiments/paper_replication_v1/make_report2.py",
        "classification": config["experiment_id"]
        + " · replication_only_not_economic_evaluation",
        "inputs": {
            "equity_curves_monthly": str(curves_path),
            "manifest": str(manifest_path),
            "paper_reference_table": str(reference_path),
            "config": str(args.config),
        },
        "run_manifest": {
            "created_utc": manifest.get("created_utc"),
            "git": manifest.get("git"),
            "engine_sha256": manifest.get("engine_sha256"),
            "data_release_id": manifest.get("data_bundle", {}).get("release_id"),
        },
        "curve_rows": len(curve_records),
        "paper_months": len(paper_records),
    }

    primary_last_session = None
    benchmark_csv = results_dir / "performance_benchmark.csv"
    if benchmark_csv.exists():
        bench = pd.read_csv(benchmark_csv)
        hit = bench.loc[
            (bench["profile"] == matrix["primary_profile"])
            & (bench["tier"] == matrix["primary_tier"])
            & (bench["dividend_mode"] == matrix["primary_dividend_mode"]),
            "strategy_Last",
        ]
        if len(hit):
            primary_last_session = str(hit.iloc[0])

    last_curve_date = max(r["date"] for r in curve_records)
    replacements = {
        "__PAPER_REVISION__": html.escape(str(config["paper"]["revision"])),
        "__PAPER_LAST__": paper_records[-1]["date"],
        "__LAST_PARTIAL__": primary_last_session or last_curve_date,
        "__LAST_PARTIAL_LABEL__": last_curve_date,
        "__PROVENANCE__": html.escape(json.dumps(
            provenance, ensure_ascii=False, indent=2)),
        "__CURVES_JSON__": records_json(curve_records),
        "__PAPER_JSON__": records_json(paper_records),
        "__PAPER_REF_JSON__": records_json(
            config["paper"]["performance_reference"]),
        "__MATRIX_JSON__": records_json(matrix),
    }
    text = TEMPLATE
    for token, value in replacements.items():
        if token not in text:
            raise ValueError(f"template token missing: {token}")
        text = text.replace(token, value)
    leftovers = [tok for tok in text.split("__") if tok.isupper()]
    if leftovers:
        raise ValueError(f"unreplaced template tokens: {leftovers[:5]}")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    print(f"wrote {output} ({output.stat().st_size:,} bytes)")
    print(f"curve rows: {len(curve_records)}, "
          f"paper months: {len(paper_records)} "
          f"({paper_records[0]['date']} .. {paper_records[-1]['date']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
