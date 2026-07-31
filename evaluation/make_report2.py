"""Add-on interactive report (report2) for a finished evaluation matrix run.

report2.html is a focused companion to the runner's report.html: one
interactive section where the reader picks any matrix cell
(profile x tier x dividend mode x slippage) plus a start and an end date, and
both the daily cumulative-NAV chart (log scale) and the metrics table are
recomputed live in the browser for the chosen window:

- chart series: selected cell strategy NAV and the independent SPY
  total-return NAV (raw close + State Street cash dividends), both rebased to
  1.0 at the window start;
- metrics table columns: 指标 | 策略 | SPY 全收益, recomputed from the daily
  series with the same daily methodology as evaluation/run_evaluation.py
  (performance_metrics / benchmark_metrics): calendar-year CAGR, sample-std
  vol x sqrt(252), strategy Sharpe vs the daily cash hurdle, benchmark Sharpe
  at rf=0, MDD on daily NAV, beta with np.cov/np.var ddof asymmetry preserved;
- per-cell daily series are embedded as Float32 base64 (relative error ~1e-7
  per day). Windowed numbers can therefore differ from summary.csv in the
  last displayed digit; the frozen per-subperiod numbers in report.html and
  summary.csv remain the official ones. This companion never modifies the
  run's frozen artifacts.

The strategy daily series is sliced on the cell's own is_evaluation mask, so
windows are always measured on the same session set the runner used.

Run:
    python evaluation/make_report2.py \
      --results-dir evaluation/results/<run_id>
"""

from __future__ import annotations

import argparse
import base64
import html
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]

DIMENSIONS = {
    "profile": [
        "official_sample_compatible", "paper_spec", "corrected_execution"],
    "tier": ["paper_ready", "halt_aware", "exploratory"],
    "dividend_mode": ["with_dividends", "ignore_dividends"],
    "slippage_per_share": [0.001, 0.0025, 0.005, 0.010],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results-dir", type=Path, required=True,
                        help="finished evaluation run directory")
    parser.add_argument("--benchmark-daily", type=Path,
                        default=REPO_ROOT / "benchmark_release_v1"
                        / "spy_daily_raw_close.csv")
    parser.add_argument("--dividends", type=Path,
                        default=REPO_ROOT / "data" / "reference"
                        / "spy_dividends_state_street_20260730.csv")
    parser.add_argument("--output", type=Path, default=None,
                        help="defaults to <results-dir>/report2.html")
    return parser.parse_args()


def b64_f32(values: np.ndarray) -> str:
    return base64.b64encode(
        np.ascontiguousarray(values, dtype="<f4").tobytes()).decode("ascii")


def b64_bits(mask: np.ndarray) -> str:
    return base64.b64encode(
        np.packbits(np.asarray(mask, dtype=bool)).tobytes()).decode("ascii")


def cell_id(profile: str, tier: str, dividend_mode: str, slip: float) -> str:
    return (f"{profile}__{tier}__{dividend_mode}"
            f"__slip_{f'{slip:.4f}'.replace('.', 'p')}")


def load_cell_arrays(results_dir: Path) -> dict[str, dict[str, str]]:
    daily = pd.read_parquet(
        results_dir / "daily_results.parquet",
        columns=["cell_id", "session_date", "status", "ret",
                 "cash_hurdle_ret", "is_evaluation"])
    out: dict[str, dict[str, str]] = {}
    for cid, frame in daily.groupby("cell_id", sort=False):
        frame = frame.sort_values("session_date")
        out[cid] = {
            "ret": b64_f32(frame["ret"].to_numpy(dtype="float64")),
            "hurdle": b64_f32(
                frame["cash_hurdle_ret"].to_numpy(dtype="float64")),
            "eval": b64_bits(frame["is_evaluation"].to_numpy()),
            "active": b64_bits(frame["status"].eq("active").to_numpy()),
        }
    return out


def load_sessions(results_dir: Path) -> list[str]:
    daily = pd.read_parquet(
        results_dir / "daily_results.parquet", columns=["session_date"])
    sessions = pd.to_datetime(daily["session_date"]).drop_duplicates()
    sessions = sessions.sort_values()
    return [str(d.date()) for d in sessions]


def load_benchmark(close_path: Path, div_path: Path) -> dict[str, object]:
    close = pd.read_csv(close_path)
    normalized = {c.lower(): c for c in close.columns}
    date_col = normalized.get("date") or normalized.get("session_date")
    close = pd.Series(
        pd.to_numeric(close[normalized["close"]], errors="coerce").to_numpy(),
        index=pd.to_datetime(close[date_col]).dt.normalize()).dropna()
    close = close[~close.index.duplicated(keep=False)].sort_index()
    dividends = pd.read_csv(div_path)
    div = pd.Series(
        pd.to_numeric(dividends["Dividend"], errors="coerce").to_numpy(),
        index=pd.to_datetime(dividends["Date"]).dt.normalize())
    sparse = {
        int(close.index.get_loc(d)): float(v)
        for d, v in div.items() if d in close.index and v}
    return {
        "dates": [str(d.date()) for d in close.index],
        "close": b64_f32(close.to_numpy(dtype="float64")),
        "div": sparse,
    }


CSS = """
body { font-family: "Segoe UI", "Microsoft YaHei", sans-serif; margin: 24px;
  color: #1c2733; background: #f4f6f8; }
.panel { background: #fff; border: 1px solid #d7dee6; border-radius: 8px;
  padding: 16px 20px; margin-bottom: 18px; }
h1 { font-size: 20px; margin: 0 0 6px; } h2 { font-size: 16px; }
.meta, .note { color: #617080; font-size: 12px; }
.controls { display: flex; flex-wrap: wrap; gap: 10px 16px; align-items:
  end; margin: 10px 0; }
label { font-size: 12px; color: #37485c; display: flex; flex-direction:
  column; gap: 3px; }
select, input[type=date] { padding: 4px 6px; border: 1px solid #b9c4d0;
  border-radius: 4px; font-size: 13px; }
button { padding: 4px 10px; border: 1px solid #b9c4d0; border-radius: 4px;
  background: #eef2f6; cursor: pointer; font-size: 12px; }
button:hover { background: #dfe7ef; }
svg { width: 100%; height: auto; display: block; background: #fff;
  border: 1px solid #d7dee6; border-radius: 6px; }
table { border-collapse: collapse; font-size: 13px; }
th, td { border: 1px solid #d7dee6; padding: 4px 12px; text-align: right; }
th:first-child, td:first-child { text-align: left; }
thead { background: #eef2f6; }
.legend span { display: inline-block; margin-right: 16px; font-size: 12px; }
.sw { display: inline-block; width: 18px; height: 3px; vertical-align:
  middle; margin-right: 5px; }
"""

JS = r"""
const DATES = __SESSIONS__;
const BM = __BENCHMARK__;
const CELLS = __CELLS__;
const DIMS = __DIMENSIONS__;
const HEADLINE = __HEADLINE__;
const PRESETS = __PRESETS__;

const byId = id => document.getElementById(id);
function f32(b64) {
  const bin = atob(b64), n = bin.length, buf = new Uint8Array(n);
  for (let i = 0; i < n; i++) buf[i] = bin.charCodeAt(i);
  return new Float32Array(buf.buffer);
}
function bits(b64, n) {
  const bin = atob(b64), buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  const out = new Uint8Array(n);
  for (let i = 0; i < n; i++) out[i] = (buf[i >> 3] >> (7 - (i & 7))) & 1;
  return out;
}
const cellCache = {};
function getCell() {
  const key = ["profile", "tier", "dividend", "slippage"]
    .map(id => byId(id).value).join("|");
  if (!cellCache[key]) {
    const raw = CELLS[key];
    cellCache[key] = {
      ret: f32(raw.ret), hurdle: f32(raw.hurdle),
      eval: bits(raw.eval, DATES.length),
      active: bits(raw.active, DATES.length),
    };
  }
  return cellCache[key];
}

function fillSelect(id, values, selected) {
  const el = byId(id);
  el.innerHTML = values.map(v =>
    `<option value="${v}"${String(v) === String(selected) ?
      " selected" : ""}>${v}</option>`).join("");
  el.onchange = render;
}

const mean = a => a.reduce((s, x) => s + x, 0) / a.length;
function std1(a) {  // sample std, ddof=1 (pandas .std())
  const m = mean(a);
  return Math.sqrt(a.reduce((s, x) => s + (x - m) ** 2, 0) / (a.length - 1));
}
function windowIndex(startStr, endStr) {
  // strategy evaluation-session indices inside [start, end]
  const cell = getCell(), idx = [];
  for (let i = 0; i < DATES.length; i++) {
    if (cell.eval[i] && DATES[i] >= startStr && DATES[i] <= endStr) idx.push(i);
  }
  return idx;
}
function benchmarkWindow(startStr, endStr) {
  // anchor = last benchmark session strictly before start (runner rule)
  let anchor = -1;
  for (let i = 0; i < BM.dates.length; i++) {
    if (BM.dates[i] < startStr) anchor = i; else break;
  }
  if (anchor < 0) return null;
  const px = f32(BM.close), dates = [], tr = [];
  let prev = px[anchor];
  for (let i = anchor + 1; i < BM.dates.length && BM.dates[i] <= endStr; i++) {
    const d = BM.div[i] || 0;
    dates.push(BM.dates[i]);
    tr.push((px[i] + d) / prev - 1);
    prev = px[i];
  }
  return { dates, tr };
}
function compute(startStr, endStr) {
  const cell = getCell(), idx = windowIndex(startStr, endStr);
  if (idx.length < 2) return null;
  const rets = idx.map(i => {
    const r = cell.ret[i]; return Number.isNaN(r) ? 0 : r;
  });
  const hurdles = idx.map(i => {
    const h = cell.hurdle[i]; return Number.isNaN(h) ? 0 : h;
  });
  const dates = idx.map(i => DATES[i]);
  const years = Math.max(
    (Date.parse(dates[dates.length - 1]) - Date.parse(dates[0])) / 864e5
    / 365.2425, 1 / 365.2425);
  const nav = [1];
  for (const r of rets) nav.push(nav[nav.length - 1] * (1 + r));
  nav.shift();  // nav[i] now aligns with dates[i] (post-close wealth)
  const total = nav[nav.length - 1];
  let peak = 0, mdd = 0;
  for (const w of [1, ...nav]) { peak = Math.max(peak, w); mdd = Math.min(mdd, w / peak - 1); }
  const sd = std1(rets);
  const nz = rets.filter(r => r !== 0);
  const strat = {
    total: total - 1,
    cagr: Math.pow(total, 1 / years) - 1,
    vol: sd * Math.sqrt(252),
    sharpe: sd > 0 ?
      mean(rets.map((r, i) => r - hurdles[i])) / sd * Math.sqrt(252) : null,
    mdd, worst: Math.min(...rets),
    hit: nz.length ? nz.filter(r => r > 0).length / nz.length : null,
    active: idx.reduce((s, i) => s + cell.active[i], 0),
    sessions: idx.length,
    nav,
  };
  // runner rule: benchmark stats use ALL benchmark sessions in the window
  // (anchor = last benchmark session before the window), years come from the
  // strategy index; beta/alpha/IR use the strategy-aligned intersection.
  const bm = benchmarkWindow(dates[0], dates[dates.length - 1]);
  let bench = null;
  if (bm && bm.tr.length > 1) {
    const bmap = new Map(bm.dates.map((d, i) => [d, bm.tr[i]]));
    const navMapV = new Map();
    let acc = 1;
    for (let i = 0; i < bm.dates.length; i++) {
      acc *= 1 + bm.tr[i]; navMapV.set(bm.dates[i], acc);
    }
    const chartNav = dates.map(d => navMapV.get(d)).filter(v => v !== undefined);
    const btotal = acc;
    const bsd = std1(bm.tr);
    const mb = mean(bm.tr);
    const aligned = dates.map((d, i) => [rets[i], bmap.get(d)])
      .filter(p => p[1] !== undefined && !Number.isNaN(p[1]));
    const btr = aligned.map(p => p[1]), srt = aligned.map(p => p[0]);
    const ms = mean(srt), mba = mean(btr);
    let cov = 0, varB = 0; const exD = [];
    for (let i = 0; i < btr.length; i++) {
      cov += (srt[i] - ms) * (btr[i] - mba);
      varB += (btr[i] - mba) ** 2;
      exD.push(srt[i] - btr[i]);
    }
    cov /= (btr.length - 1);              // np.cov ddof=1
    varB /= btr.length;                   // np.var ddof=0
    const beta = varB > 0 ? cov / varB : null;
    const exSd = std1(exD);
    bench = {
      cagr: Math.pow(btotal, 1 / years) - 1,
      vol: bsd * Math.sqrt(252),
      sharpe: bsd > 0 ? mb / bsd * Math.sqrt(252) : null,
      total: btotal - 1,
      beta,
      alpha: beta === null ? null : (ms - beta * mba) * 252,
      ir: exSd > 0 ? mean(exD) / exSd * Math.sqrt(252) : null,
      nav: chartNav,
    };
  }
  return { dates, strat, bench, years };
}

const PCT = (x, d = 2) => x === null || x === undefined || Number.isNaN(x) ?
  "—" : (x * 100).toFixed(d) + "%";
const NUM = (x, d = 2) => x === null || x === undefined || Number.isNaN(x) ?
  "—" : x.toFixed(d);

function metricsTable(w) {
  const rows = [
    ["区间累计收益", PCT(w.strat.total), PCT(w.bench && w.bench.total)],
    ["CAGR(日历年)", PCT(w.strat.cagr), PCT(w.bench && w.bench.cagr)],
    ["年化波动率", PCT(w.strat.vol), PCT(w.bench && w.bench.vol)],
    ["Sharpe(策略 vs 现金 / SPY rf=0)", NUM(w.strat.sharpe),
      NUM(w.bench && w.bench.sharpe)],
    ["最大回撤", PCT(w.strat.mdd), "—"],
    ["最差单日", PCT(w.strat.worst), "—"],
    ["非零日胜率", PCT(w.strat.hit, 1), "—"],
    ["Beta / 年化Alpha / IR", "—",
      w.bench ? `${NUM(w.bench.beta)} / ${PCT(w.bench.alpha)} / ${NUM(w.bench.ir)}` : "—"],
    ["评估会话 / 活跃会话", `${w.strat.sessions} / ${w.strat.active}`, "—"],
  ];
  return "<thead><tr><th>指标</th><th>策略</th><th>SPY 全收益</th></tr></thead><tbody>" +
    rows.map(r => `<tr><td>${r[0]}</td><td>${r[1]}</td><td>${r[2]}</td></tr>`)
    .join("") + "</tbody>";
}

function draw(w) {
  const svg = byId("chart");
  const W = 980, H = 430, L = 64, R = 16, T = 16, B = 44;
  const dates = w.dates, n = dates.length;
  const series = [
    { nav: w.strat.nav, color: "#0b62c4", name: "策略 NAV" },
    w.bench ? { nav: w.bench.nav, color: "#c4550b", name: "SPY 全收益 NAV" } : null,
  ].filter(Boolean);
  let lo = Infinity, hi = -Infinity;
  for (const s of series) for (const v of s.nav) {
    if (v > 0) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  }
  if (!Number.isFinite(lo)) {
    svg.innerHTML = '<text x="490" y="215" text-anchor="middle" fill="#617080">区间内无可画数据</text>';
    return;
  }
  const llo = Math.log(lo * 0.98), lhi = Math.log(hi * 1.02);
  const x = i => L + (W - L - R) * (n <= 1 ? 0 : i / (n - 1));
  const y = v => T + (H - T - B) * (1 - (Math.log(v) - llo) / (lhi - llo));
  let out = "";
  // gridlines at powers of 2-ish NAV marks
  const marks = [0.25, 0.5, 1, 2, 4, 8, 16, 32];
  out += marks.filter(m => m > lo * 0.98 && m < hi * 1.02).map(m =>
    `<line x1="${L}" x2="${W - R}" y1="${y(m)}" y2="${y(m)}" stroke="#e4e9ef"/>` +
    `<text x="${L - 6}" y="${y(m) + 4}" text-anchor="end" font-size="11" fill="#617080">${m}</text>`).join("");
  const yearTicks = [];
  for (let i = 0; i < n; i++) {
    if (i === 0 || dates[i].slice(0, 4) !== dates[i - 1].slice(0, 4))
      yearTicks.push(i);
  }
  out += yearTicks.map(i =>
    `<text x="${x(i)}" y="${H - B + 18}" text-anchor="middle" font-size="11" fill="#617080">${dates[i].slice(0, 4)}</text>`).join("");
  for (const s of series) {
    out += `<polyline fill="none" stroke="${s.color}" stroke-width="1.6" points="` +
      s.nav.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ") +
      '"/>';
  }
  out += `<line id="xhair" x1="-10" y1="${T}" x2="-10" y2="${H - B}" stroke="#98a6b5" stroke-dasharray="3 3"/>`;
  out += `<text id="tip" x="${L + 8}" y="${T + 14}" font-size="12" fill="#1c2733"></text>`;
  out += `<rect id="capture" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/>`;
  svg.innerHTML = out;
  const cap = svg.querySelector("#capture");
  cap.addEventListener("mousemove", e => {
    const rect = svg.getBoundingClientRect();
    const fx = (e.clientX - rect.left) / rect.width * W;
    let i = Math.round((fx - L) / (W - L - R) * (n - 1));
    i = Math.max(0, Math.min(n - 1, i));
    const xh = svg.querySelector("#xhair");
    xh.setAttribute("x1", x(i)); xh.setAttribute("x2", x(i));
    const parts = series.map(s =>
      `${s.name} ${s.nav[Math.min(i, s.nav.length - 1)].toFixed(3)}`);
    svg.querySelector("#tip").textContent = `${dates[i]}  |  ${parts.join("  |  ")}`;
  });
  cap.addEventListener("mouseleave", () => {
    const xh = svg.querySelector("#xhair");
    xh.setAttribute("x1", -10); xh.setAttribute("x2", -10);
    svg.querySelector("#tip").textContent = "";
  });
}

function render() {
  const s = byId("start").value, e = byId("end").value;
  if (!s || !e || s >= e) {
    byId("metrics").innerHTML = "<tbody><tr><td>请选择有效的起止日期</td></tr></tbody>";
    byId("chart").innerHTML = "";
    return;
  }
  const w = compute(s, e);
  if (!w) {
    byId("metrics").innerHTML = "<tbody><tr><td>区间内评估会话不足</td></tr></tbody>";
    byId("chart").innerHTML = "";
    return;
  }
  byId("metrics").innerHTML = metricsTable(w);
  draw(w);
}

function preset(key) {
  const p = PRESETS[key];
  byId("start").value = p[0]; byId("end").value = p[1];
  render();
}

fillSelect("profile", DIMS.profile, HEADLINE.profile);
fillSelect("tier", DIMS.tier, HEADLINE.tier);
fillSelect("dividend", DIMS.dividend_mode, HEADLINE.dividend_mode);
fillSelect("slippage", DIMS.slippage_per_share.map(String),
  String(HEADLINE.slippage_per_share));
byId("start").value = PRESETS.post[0];
byId("end").value = PRESETS.post[1];
byId("start").onchange = render; byId("end").onchange = render;
render();
"""


def render_html(manifest: dict, sessions: list[str], benchmark: dict,
                cells: dict[str, dict[str, str]]) -> str:
    spec_path = manifest.get("spec_path", "")
    headline = manifest_headline(manifest)
    cell_map = {}
    for profile in DIMENSIONS["profile"]:
        for tier in DIMENSIONS["tier"]:
            for div in DIMENSIONS["dividend_mode"]:
                for slip in DIMENSIONS["slippage_per_share"]:
                    cid = cell_id(profile, tier, div, slip)
                    if cid in cells:
                        cell_map[f"{profile}|{tier}|{div}|{slip:.4f}"] = cells[cid]
    presets = {
        "full": [sessions[0], sessions[-1]],
        "pre": [sessions[0], "2024-04-30"],
        "post": ["2024-05-01", sessions[-1]],
        "y2025": ["2025-01-01", "2025-12-31"],
        "y2026": ["2026-01-01", sessions[-1]],
        "l12m": [
            str((pd.Timestamp(sessions[-1]) - pd.DateOffset(years=1)).date()),
            sessions[-1]],
    }
    js = (JS
          .replace("__SESSIONS__", json.dumps(sessions))
          .replace("__BENCHMARK__", json.dumps(benchmark))
          .replace("__CELLS__", json.dumps(cell_map))
          .replace("__DIMENSIONS__", json.dumps({
              k: [f"{x:.4f}" if k == "slippage_per_share" else x
                  for x in v] for k, v in DIMENSIONS.items()}))
          .replace("__HEADLINE__", json.dumps(headline))
          .replace("__PRESETS__", json.dumps(presets)))
    title = "Interactive report2 — evaluation matrix companion"
    buttons = "".join(
        f'<button onclick="preset(\'{k}\')">{label}</button>'
        for k, label in [
            ("full", "全样本"), ("pre", "发表前"), ("post", "发表后"),
            ("y2025", "2025"), ("y2026", "2026 YTD"), ("l12m", "近12个月")])
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>{title}</title><style>{CSS}</style></head><body>
<div class="panel">
<h1>{title}</h1>
<div class="meta">
run: <b>{html.escape(manifest.get('run_id', ''))}</b> ·
classification: {html.escape(manifest.get('classification', ''))} ·
spec sha256: <code>{html.escape(manifest.get('spec_sha256', ''))[:16]}…</code>
({html.escape(Path(spec_path).name)}) ·
headline cell: <b>{html.escape(headline['cell_id'])}</b><br>
generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ·
companion to report.html; frozen per-subperiod numbers live in
summary.csv / report.html, this page recomputes arbitrary windows client-side.
</div>
</div>
<div class="panel">
<h2>自选矩阵格与日期窗口</h2>
<div class="controls">
<label>profile<select id="profile"></select></label>
<label>tier<select id="tier"></select></label>
<label>dividend<select id="dividend"></select></label>
<label>slippage/share<select id="slippage"></select></label>
<label>开始日期<input type="date" id="start"></label>
<label>结束日期<input type="date" id="end"></label>
<span>{buttons}</span>
</div>
<div class="legend">
<span><span class="sw" style="background:#0b62c4"></span>策略 NAV(日频复利,窗口起点=1,对数轴)</span>
<span><span class="sw" style="background:#c4550b"></span>SPY 全收益 NAV(独立日频基准 + State Street 现金股息)</span>
</div>
<svg id="chart" viewBox="0 0 980 430" role="img"
 aria-label="所选矩阵格与日期窗口内的策略与 SPY 全收益累计净值曲线(对数刻度)"></svg>
</div>
<div class="panel">
<h2>窗口指标(与 run_evaluation.py 相同的日频口径)</h2>
<table id="metrics"></table>
<p class="note">口径:CAGR 按日历年 (last−first)/365.2425 年化;波动率 =
日收益样本 std × √252;策略 Sharpe 用当日 cash hurdle(financing-rates-v1),
SPY Sharpe 用 rf=0(与 runner 一致);MDD 基于日频 NAV;beta 保留 runner 的
np.cov(ddof=1)/np.var(ddof=0) 口径。策略序列按该格的 is_evaluation 掩码切片,
基准锚定窗口开始前一个基准会话。日序列以 Float32 嵌入(单日相对误差 ~1e-7),
窗口指标末位可能与 summary.csv 有浮点差异;正式数字以冻结的 report.html /
summary.csv 为准。发表后窗口是 post-publication evaluation period,不是未触碰 OOS。</p>
</div>
<script>
{js}
</script></body></html>"""


def manifest_headline(manifest: dict) -> dict[str, object]:
    """Recover the headline cell from the spec recorded in the manifest."""
    import yaml
    spec = yaml.safe_load(
        Path(manifest["spec_path"]).read_text(encoding="utf-8"))
    h = spec["headline"]
    slip = float(h["slippage_per_share"])
    return {"profile": h["profile"], "tier": h["tier"],
            "dividend_mode": h["dividend_mode"],
            "slippage_per_share": f"{slip:.4f}",
            "cell_id": cell_id(h["profile"], h["tier"],
                               h["dividend_mode"], slip)}


def main() -> int:
    args = parse_args()
    results_dir = args.results_dir.resolve()
    manifest = json.loads((results_dir / "manifest.json").read_text(
        encoding="utf-8"))
    manifest["run_dir"] = str(results_dir)
    manifest.setdefault("run_id", results_dir.name)
    sessions = load_sessions(results_dir)
    cells = load_cell_arrays(results_dir)
    benchmark = load_benchmark(args.benchmark_daily.resolve(),
                               args.dividends.resolve())
    output = args.output or results_dir / "report2.html"
    output.write_text(
        render_html(manifest, sessions, benchmark, cells), encoding="utf-8")
    print(f"written: {output} ({output.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
