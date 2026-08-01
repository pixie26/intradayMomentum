"use strict";

const fs = require("fs");
const path = require("path");

const reportPath = path.join(
  __dirname, "docs", "POST_PUBLICATION_EVALUATION_V2_HALT0025_ATTRIBUTION.html");
const html = fs.readFileSync(reportPath, "utf8");
const matches = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
if (!matches.length) throw new Error("report has no inline script");
const script = matches[matches.length - 1][1];
if (script.includes("__DAILY__") || html.includes("__PLACEHOLDER__")) {
  throw new Error("unresolved report placeholder");
}
new Function(script);
for (const token of [
  "FORMAL HEADLINE DERIVED ANALYSIS", "Performance vs benchmark",
  "Post headline portfolio CAGR", "Gross P&L", "Win rate",
  "Cash interest annualized", "Event (descriptive)",
]) {
  if (html.includes(token)) throw new Error(`reader-facing English remains: ${token}`);
}
for (const token of ["正式主口径派生分析", "表现与基准对比", "发表后组合 CAGR", "毛损益"]) {
  if (!html.includes(token)) throw new Error(`Chinese report label missing: ${token}`);
}

const ids = [
  "start", "end", "scenarioMode", "scenarioN", "cards", "nav",
  "cashWarning", "cashCompare", "waterfall", "pnl", "identity", "capital",
  "longShort", "entryBuckets", "volatility", "signalsAnnual",
  "concentration", "concentrationStats", "extremeDays", "corr", "conditional",
  "rolling", "quarters", "annual",
];
const elements = Object.fromEntries(ids.map((id) => [id, {
  id, value: "", innerHTML: "", onchange: null,
}]));
elements.start.value = "2008-02-08";
elements.end.value = "2026-07-09";
elements.scenarioMode.value = "worst_benchmark";
elements.scenarioN.value = "10";
const documentStub = {getElementById: (id) => {
  if (!elements[id]) elements[id] = {id, value: "", innerHTML: "", onchange: null};
  return elements[id];
}};

new Function("document", script)(documentStub);
if (!elements.cashWarning.innerHTML.includes("7.52%") ||
    !elements.cashWarning.innerHTML.includes("3.27%") ||
    !elements.cashWarning.innerHTML.includes("48.6%")) {
  throw new Error("cash/headline warning does not expose post carry economics");
}
if ((elements.nav.innerHTML.match(/<polyline/g) || []).length !== 3) {
  throw new Error("NAV chart does not contain portfolio/trading-only/benchmark");
}

elements.start.value = "2024-05-01";
elements.end.value = "2026-07-09";
elements.start.onchange();
for (const token of ["7.52%", "3.27%", "4.06%", "48.6%"]){
  if (!elements.cards.innerHTML.includes(token)) {
    throw new Error(`post headline card missing ${token}`);
  }
}
if (!elements.capital.innerHTML.includes("68.70%") ||
    !elements.capital.innerHTML.includes("2.71×")) {
  throw new Error("post capital diagnostics did not render");
}
if (!elements.entryBuckets.innerHTML.includes("254,317") ||
    !elements.volatility.innerHTML.includes("213,137")) {
  throw new Error("entry-time or volatility diagnostics did not render");
}
if (!elements.concentration.innerHTML.includes("7.5%") ||
    !elements.concentration.innerHTML.includes("47.0%")) {
  throw new Error("profit concentration diagnostics did not render");
}
elements.start.value = "2008-02-08";
elements.end.value = "2026-07-09";
elements.start.onchange();
const quarterBody = elements.quarters.innerHTML.match(/<tbody>([\s\S]*)<\/tbody>/);
const quarterRows = quarterBody ? (quarterBody[1].match(/<tr>/g) || []).length : 0;
if (quarterRows !== 10) throw new Error(`expected 10 scenario rows, got ${quarterRows}`);

console.log("ATTRIBUTION REPORT DOM TEST PASSED");
