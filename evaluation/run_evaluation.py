"""Run the frozen intraday-momentum evaluation matrix.

The safe first entry point is::

    python evaluation/run_evaluation.py --plan-only

Plan-only validates the spec and immutable data release without loading the
large parquet files or writing results. Formal publication additionally
requires a clean Git worktree, a v2 spec that closes the remaining economic
assumptions, and an independent daily benchmark with complete date coverage.
"""

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
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import im_engine_v4 as engine  # noqa: E402


DIVIDEND_MODES = ("with_dividends", "ignore_dividends")
SUBPERIODS = ("full_sample", "pre_publication", "post_publication")
FORMAL_V2_FIELDS = (
    "evaluation_end",
    "data_release",
    "capital_path",
    "financing",
    "statistics",
)


@dataclass(frozen=True)
class Cell:
    profile: str
    tier: str
    dividend_mode: str
    slippage_per_share: float

    @property
    def cell_id(self) -> str:
        slip = f"{self.slippage_per_share:.4f}".replace(".", "p")
        return (
            f"{self.profile}__{self.tier}__{self.dividend_mode}"
            f"__slip_{slip}"
        )


@dataclass(frozen=True)
class Period:
    name: str
    start: pd.Timestamp
    end: pd.Timestamp


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


def git_state(root: Path = ROOT) -> dict[str, Any]:
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False)
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        capture_output=True, text=True, check=False)
    paths = []
    for line in status.stdout.splitlines():
        if line.strip():
            paths.append(line[3:].strip())
    return {
        "available": commit.returncode == 0 and status.returncode == 0,
        "commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "dirty": bool(paths) if status.returncode == 0 else None,
        "dirty_paths": paths,
    }


def load_spec(path: Path) -> tuple[dict[str, Any], str]:
    raw_bytes = path.read_bytes()
    raw = raw_bytes.decode("utf-8")
    spec = yaml.safe_load(raw)
    if not isinstance(spec, dict):
        raise ValueError("evaluation spec must be a YAML mapping")

    required = {
        "spec_version", "evaluation_start", "subperiods", "profiles", "tiers",
        "dividends", "slippage_per_share", "decomposition", "benchmark",
        "decision_rule", "provenance_required", "naming",
    }
    missing = sorted(required - set(spec))
    if missing:
        raise ValueError(f"evaluation spec is missing fields: {missing}")
    if tuple(spec["profiles"]) != engine.PROFILES:
        raise ValueError(
            f"profiles must be exactly {engine.PROFILES}, got {spec['profiles']}")
    if tuple(spec["subperiods"]) != SUBPERIODS:
        raise ValueError(
            f"subperiods must be exactly {SUBPERIODS}, got {spec['subperiods']}")

    tiers = [spec["tiers"]["headline"], *spec["tiers"]["also_report"]]
    if len(tiers) != 3 or set(tiers) != set(engine._ENUMS["tier"]):
        raise ValueError(
            "tiers must contain paper_ready, halt_aware and exploratory once")
    dividends = [
        spec["dividends"]["headline"], spec["dividends"]["also_report"]]
    if len(dividends) != 2 or set(dividends) != set(DIVIDEND_MODES):
        raise ValueError(
            "dividend modes must contain with_dividends and ignore_dividends")
    slips = [float(x) for x in spec["slippage_per_share"]]
    if len(slips) != 4 or len(set(slips)) != 4 or any(x < 0 for x in slips):
        raise ValueError("slippage_per_share must contain four unique nonnegative values")
    return spec, hashlib.sha256(raw_bytes).hexdigest()


def build_cells(spec: dict[str, Any]) -> list[Cell]:
    tiers = [spec["tiers"]["headline"], *spec["tiers"]["also_report"]]
    dividends = [
        spec["dividends"]["headline"], spec["dividends"]["also_report"]]
    return [
        Cell(profile, tier, dividend_mode, float(slip))
        for profile in spec["profiles"]
        for tier in tiers
        for dividend_mode in dividends
        for slip in spec["slippage_per_share"]
    ]


def build_periods(
        spec: dict[str, Any], data_start: str, data_end: str) -> list[Period]:
    release_start = pd.Timestamp(data_start)
    release_end = pd.Timestamp(spec.get("evaluation_end", data_end))
    if release_end > pd.Timestamp(data_end):
        raise ValueError(
            f"evaluation_end {release_end.date()} exceeds data end {data_end}")
    cut = pd.Timestamp(spec["evaluation_start"])
    if not release_start < cut <= release_end:
        raise ValueError("evaluation_start must lie inside the data release")
    return [
        Period("full_sample", release_start, release_end),
        Period("pre_publication", release_start, cut - pd.Timedelta(days=1)),
        Period("post_publication", cut, release_end),
    ]


def inspect_release(release_dir: Path) -> dict[str, Any]:
    if not (release_dir / "_SUCCESS").exists():
        raise RuntimeError(f"{release_dir} has no _SUCCESS marker")
    manifest, bundle_meta, paths = engine._load_bundle_manifest(release_dir)
    release_manifest_path = release_dir / "data_manifest.json"
    release_manifest = json.loads(
        release_manifest_path.read_text(encoding="utf-8"))

    required_paths = {
        "clean", "vmin", "vsess", "dividends",
    }
    missing = sorted(
        name for name in required_paths
        if paths.get(name) is None or not Path(paths[name]).exists())
    if missing:
        raise RuntimeError(f"immutable release is missing inputs: {missing}")

    expected_start = release_manifest.get("expected_start")
    expected_end = release_manifest.get("expected_end")
    if manifest.get("observed_start") != expected_start:
        raise RuntimeError("release expected_start does not match observed_start")
    if manifest.get("observed_end") != expected_end:
        raise RuntimeError("release expected_end does not match observed_end")

    return {
        "release_id": release_manifest.get("release_id"),
        "release_manifest_sha256": sha256(release_manifest_path),
        "source_run_id": manifest.get("run_id"),
        "source_sha256": manifest.get("source_sha256"),
        "data_script_sha256": manifest.get("script_sha256"),
        "dividend_sha256": manifest["dividends"]["output_sha256"],
        "dividend_source_sha256": manifest["dividends"]["source_sha256"],
        "expected_start": expected_start,
        "expected_end": expected_end,
        "observed_start": manifest.get("observed_start"),
        "observed_end": manifest.get("observed_end"),
        "bundle": bundle_meta,
    }


def load_financing_rates(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {
        "session_date", "benchmark", "benchmark_observation",
        "benchmark_rate_percent", "cash_rate_annual",
        "funding_rate_annual", "borrow_rate_annual",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"financing rate CSV missing columns: {missing}")
    frame["session_date"] = pd.to_datetime(
        frame["session_date"]).dt.normalize()
    if frame["session_date"].duplicated().any():
        raise ValueError("financing rate CSV has duplicate session dates")
    rate_columns = [
        "benchmark_rate_percent", "cash_rate_annual",
        "funding_rate_annual", "borrow_rate_annual",
    ]
    frame[rate_columns] = frame[rate_columns].apply(
        pd.to_numeric, errors="coerce")
    if frame[rate_columns].isna().any(axis=None):
        raise ValueError("financing rate CSV contains missing or invalid rates")
    return frame.sort_values("session_date").reset_index(drop=True)


def inspect_financing_release(
        release_dir: Path, spec: dict[str, Any]) -> dict[str, Any]:
    release_dir = release_dir.resolve()
    success_path = release_dir / "_SUCCESS"
    manifest_path = release_dir / "manifest.json"
    if not success_path.exists() or not manifest_path.exists():
        raise RuntimeError("financing release requires _SUCCESS and manifest.json")
    success = json.loads(success_path.read_text(encoding="utf-8"))
    manifest_hash = sha256(manifest_path)
    if success.get("manifest_sha256") != manifest_hash:
        raise RuntimeError("financing release manifest hash does not match _SUCCESS")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    curve_name = "financing_rates_daily.csv"
    curve_path = release_dir / curve_name
    declared_file = manifest.get("files", {}).get(curve_name, {})
    if not curve_path.exists():
        raise RuntimeError("financing release is missing its daily rate curve")
    curve_hash = sha256(curve_path)
    if declared_file.get("sha256") != curve_hash:
        raise RuntimeError("financing daily curve hash does not match manifest")
    frame = load_financing_rates(curve_path)
    financing_spec = spec.get("financing", {})
    declared_release = financing_spec.get("rate_release", {})
    checks = {
        "release_id": manifest.get("release_id"),
        "manifest_sha256": manifest_hash,
        "daily_rates_sha256": curve_hash,
    }
    for key, observed in checks.items():
        declared = declared_release.get(key)
        if declared and declared != observed:
            raise RuntimeError(
                f"financing release {key} does not match spec")
    expected_start = str(frame["session_date"].min().date())
    expected_end = str(frame["session_date"].max().date())
    if manifest.get("expected_start") != expected_start:
        raise RuntimeError("financing release start does not match its curve")
    if manifest.get("expected_end") != expected_end:
        raise RuntimeError("financing release end does not match its curve")
    return {
        "path": str(release_dir),
        "release_id": manifest.get("release_id"),
        "manifest_sha256": manifest_hash,
        "daily_rates_path": str(curve_path),
        "daily_rates_sha256": curve_hash,
        "rows": int(len(frame)),
        "first": expected_start,
        "last": expected_end,
        "day_count": manifest.get("day_count"),
        "source_hashes": {
            name: details.get("source_sha256")
            for name, details in manifest.get("sources", {}).items()
        },
    }


def formal_gaps(
        spec: dict[str, Any], benchmark_path: Path | None,
        financing_release: Path | None = None) -> list[str]:
    gaps = [
        f"spec missing formal v2 field: {field}"
        for field in FORMAL_V2_FIELDS if field not in spec
    ]
    benchmark = spec.get("benchmark", {})
    if benchmark.get("external_file_required", False) and benchmark_path is None:
        gaps.append("independent daily benchmark path is required")
    if benchmark_path is not None:
        if not benchmark_path.exists():
            gaps.append("independent daily benchmark path does not exist")
        elif (
            benchmark.get("daily_close_sha256")
            and sha256(benchmark_path) != benchmark["daily_close_sha256"]
        ):
            gaps.append("independent daily benchmark hash does not match spec")
    if int(spec.get("spec_version", 0)) < 2:
        gaps.append("spec_version must be at least 2 for formal publication")
    if spec.get("status") != "frozen":
        gaps.append("spec status must be frozen for formal publication")
    if spec.get("financing", {}).get("status") != "frozen":
        gaps.append("financing and borrow assumptions are not frozen")
    if financing_release is None:
        gaps.append("point-in-time financing rate release is required")
    else:
        try:
            rate_info = inspect_financing_release(financing_release, spec)
            data_release = spec.get("data_release", {})
            if rate_info["first"] != data_release.get("expected_start"):
                gaps.append("financing rate release starts after data release")
            if rate_info["last"] != spec.get("evaluation_end"):
                gaps.append("financing rate release does not end at evaluation_end")
            if rate_info["day_count"] != "ACT/360":
                gaps.append("financing rate release must use ACT/360")
        except Exception as exc:
            gaps.append(f"financing rate release cannot be verified: {exc}")
    if spec.get("statistics", {}).get("status") != "frozen":
        gaps.append("statistical inference assumptions are not frozen")
    if benchmark_path is not None and benchmark_path.exists():
        try:
            daily = load_daily_benchmark(benchmark_path)
            if daily.index.min() > pd.Timestamp(spec.get("evaluation_start")):
                gaps.append("daily benchmark has no pre-evaluation history")
            if daily.index.max() < pd.Timestamp(
                    spec.get("evaluation_end", spec.get("evaluation_start"))):
                gaps.append("daily benchmark ends before evaluation_end")
        except Exception as exc:
            gaps.append(f"daily benchmark cannot be parsed: {exc}")
    return gaps


def make_plan(
        spec_path: Path, release_dir: Path,
        benchmark_path: Path | None = None,
        financing_release: Path | None = None) -> dict[str, Any]:
    spec, spec_hash = load_spec(spec_path)
    release = inspect_release(release_dir)
    cells = build_cells(spec)
    periods = build_periods(
        spec, release["expected_start"], release["expected_end"])
    git = git_state()
    gaps = formal_gaps(spec, benchmark_path, financing_release)
    declared_release = spec.get("data_release", {})
    if (
        declared_release.get("release_id")
        and declared_release["release_id"] != release["release_id"]
    ):
        gaps.append("data release ID does not match spec")
    if (
        declared_release.get("manifest_sha256")
        and declared_release["manifest_sha256"]
        != release["release_manifest_sha256"]
    ):
        gaps.append("data release manifest hash does not match spec")
    if git["dirty"]:
        gaps.append("Git worktree is dirty")
    return {
        "classification": "preflight_plan_only",
        "spec": {
            "path": str(spec_path.resolve()),
            "version": spec["spec_version"],
            "sha256": spec_hash,
        },
        "data_release": release,
        "engine_script_sha256": sha256(ROOT / "im_engine_v4.py"),
        "data_script_sha256_current": sha256(ROOT / "prepare_spy_data.py"),
        "git": git,
        "matrix": {
            "profiles": len(spec["profiles"]),
            "tiers": 3,
            "dividend_modes": 2,
            "slippage_values": 4,
            "cells": len(cells),
            "subperiods": len(periods),
            "expected_summary_rows": len(cells) * len(periods),
        },
        "periods": [
            {"name": p.name, "start": str(p.start.date()), "end": str(p.end.date())}
            for p in periods
        ],
        "benchmark": (
            {
                "path": str(benchmark_path.resolve()),
                "sha256": sha256(benchmark_path),
                "rows": int(len(load_daily_benchmark(benchmark_path))),
                "first": str(load_daily_benchmark(benchmark_path).index.min().date()),
                "last": str(load_daily_benchmark(benchmark_path).index.max().date()),
            }
            if benchmark_path is not None and benchmark_path.exists() else None
        ),
        "financing_rates": (
            inspect_financing_release(financing_release, spec)
            if financing_release is not None and financing_release.exists()
            else None
        ),
        "formal_ready": not gaps,
        "formal_gaps": gaps,
    }


def select_cells(cells: list[Cell], selectors: Iterable[str]) -> list[Cell]:
    wanted = set(selectors)
    if not wanted:
        return cells
    selected = [cell for cell in cells if cell.cell_id in wanted]
    missing = sorted(wanted - {cell.cell_id for cell in selected})
    if missing:
        raise ValueError(f"unknown --smoke-cell values: {missing}")
    return selected


def slice_result(r: pd.DataFrame, period: Period) -> pd.DataFrame:
    mask = (
        (r.index >= period.start) & (r.index <= period.end)
        & r["is_evaluation"].astype(bool)
    )
    return r.loc[mask].copy()


def performance_metrics(r: pd.DataFrame, rf_annual: float) -> dict[str, Any]:
    if r.empty:
        return {}
    x = r["ret"].fillna(0.0)
    first, last = r.index.min(), r.index.max()
    years = max((last - first).days / 365.2425, 1 / 365.2425)
    wealth = (1.0 + x).cumprod()
    total = float(wealth.iloc[-1])
    dd = wealth / wealth.cummax() - 1.0
    std = float(x.std())
    shares = float(r["shares_traded"].sum())
    avg_aum = float(r["prev_aum"].mean())
    nonzero = x[x != 0]
    hurdle = (
        r["cash_hurdle_ret"].fillna(0.0)
        if "cash_hurdle_ret" in r else rf_annual / 252.0)
    return {
        "first": str(first.date()),
        "last": str(last.date()),
        "calendar_years": years,
        "evaluation_sessions": int(len(r)),
        "active_sessions": int(r["status"].eq("active").sum()),
        "total_return": total - 1.0,
        "cagr": total ** (1.0 / years) - 1.0,
        "annual_volatility": std * np.sqrt(252),
        "sharpe_calendar": (
            float((x - hurdle).mean() / std * np.sqrt(252))
            if std > 0 else None),
        "max_drawdown": float(dd.min()),
        "worst_day": float(x.min()),
        "hit_rate_nonzero": (
            float((nonzero > 0).mean()) if len(nonzero) else None),
        "gross_pnl": float(r["gross"].sum()),
        "commission": float(r["commission"].sum()),
        "slippage": float(r["slippage"].sum()),
        "execution_cost": float(r["cost"].sum()),
        "cash_interest": float(r["cash_interest"].sum()),
        "funding_and_borrow_cost": float(-r["financing"].sum()),
        "net_pnl": float(r["net"].sum()),
        "signal_count": int(r["signal_count"].sum()),
        "fill_events": int(r["fill_events"].sum()),
        "trade_units": float(r["trade_units"].sum()),
        "shares_traded": shares,
        "traded_notional": float(r["traded_notional"].sum()),
        "turnover_on_average_aum": (
            float(r["traded_notional"].sum()) / avg_aum if avg_aum > 0 else None),
        "average_holding_minutes_per_active_session": (
            float(r["holding_minutes"].sum())
            / max(int(r["status"].eq("active").sum()), 1)),
        "gross_edge_per_traded_share": (
            float(r["gross"].sum()) / shares if shares > 0 else None),
        "execution_cost_per_traded_share": (
            float(r["cost"].sum()) / shares if shares > 0 else None),
        "financing_cost_per_traded_share": (
            float(-r["financing"].sum()) / shares if shares > 0 else None),
        "trading_edge_after_costs_per_traded_share": (
            float(
                r["gross"].sum() - r["cost"].sum()
                + r["financing"].sum()
            ) / shares if shares > 0 else None),
        "net_edge_per_traded_share": (
            float(r["net"].sum()) / shares if shares > 0 else None),
        "long_gross_pnl": float(r["long_gross"].sum()),
        "short_gross_pnl": float(r["short_gross"].sum()),
        "unknown_exit_sessions": int(r["status"].eq("unknown_exit").sum()),
    }


def load_daily_benchmark(path: Path) -> pd.Series:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = payload["chart"]["result"][0]
        timestamps = pd.to_datetime(
            result["timestamp"], unit="s", utc=True
        ).tz_convert("America/New_York")
        close = result["indicators"]["quote"][0]["close"]
        out = pd.Series(
            close, index=pd.DatetimeIndex(timestamps.date), name="close")
    else:
        frame = pd.read_csv(path)
        normalized = {c.lower(): c for c in frame.columns}
        date_col = normalized.get("date") or normalized.get("session_date")
        close_col = normalized.get("close")
        if date_col is None or close_col is None:
            raise ValueError("benchmark CSV requires date/session_date and close")
        out = pd.Series(
            pd.to_numeric(frame[close_col], errors="coerce").to_numpy(),
            index=pd.to_datetime(frame[date_col]).dt.normalize(),
            name="close")
    out.index = pd.DatetimeIndex(out.index).tz_localize(None).normalize()
    out = out[~out.index.duplicated(keep=False)].dropna().sort_index()
    return out


def benchmark_metrics(
        strategy: pd.Series, close: pd.Series, dividends: pd.Series,
        period: Period) -> dict[str, Any]:
    start, end = period.start, period.end
    eligible = close.loc[:end]
    prior = eligible.index[eligible.index < start]
    if len(prior) == 0:
        raise RuntimeError(f"daily benchmark has no anchor before {start.date()}")
    anchor = prior[-1]
    px = close.loc[anchor:end]
    div = dividends.reindex(px.index).fillna(0.0)
    price_ret = px.pct_change(fill_method=None).loc[start:end]
    total_ret = ((px + div) / px.shift(1) - 1.0).loc[start:end]
    aligned = pd.DataFrame({
        "strategy": strategy.reindex(total_ret.index),
        "benchmark": total_ret,
    }).dropna()
    expected = strategy.index
    missing = expected.difference(total_ret.dropna().index)
    if len(missing):
        sample = ", ".join(str(x.date()) for x in missing[:5])
        raise RuntimeError(
            f"daily benchmark misses {len(missing)} evaluation sessions "
            f"(first: {sample})")
    years = max((expected.max() - expected.min()).days / 365.2425,
                1 / 365.2425)

    def annualized(x: pd.Series) -> float:
        return float((1.0 + x).prod() ** (1.0 / years) - 1.0)

    beta = float(
        np.cov(aligned["strategy"], aligned["benchmark"])[0, 1]
        / np.var(aligned["benchmark"]))
    alpha_daily = float(
        aligned["strategy"].mean() - beta * aligned["benchmark"].mean())
    excess = aligned["strategy"] - aligned["benchmark"]
    return {
        "benchmark_price_cagr": annualized(price_ret.dropna()),
        "benchmark_total_cagr": annualized(total_ret.dropna()),
        "benchmark_total_volatility":
            float(total_ret.std() * np.sqrt(252)),
        "benchmark_total_sharpe":
            float(total_ret.mean() / total_ret.std() * np.sqrt(252)),
        "excess_cagr": annualized(strategy) - annualized(total_ret.dropna()),
        "beta_vs_benchmark_total": beta,
        "alpha_annualized": alpha_daily * 252,
        "information_ratio":
            float(excess.mean() / excess.std() * np.sqrt(252)),
        "benchmark_sessions": int(len(total_ret)),
        "benchmark_aligned_sessions": int(len(aligned)),
    }


def headline_cell_id(spec: dict[str, Any]) -> str:
    headline = spec["headline"]
    return Cell(
        headline["profile"], headline["tier"], headline["dividend_mode"],
        float(headline["slippage_per_share"])).cell_id


def headline_label(spec: dict[str, Any]) -> str:
    """Return the human-readable headline selector recorded in the spec."""
    headline = spec["headline"]
    dividend_mode = str(headline["dividend_mode"]).replace("_", "-")
    return (
        f"{headline['profile']} × {headline['tier']} × {dividend_mode} × "
        f"${float(headline['slippage_per_share']):.4f}/share slippage"
    )


def headline_calendar_tables(
        result: pd.DataFrame, close: pd.Series,
        dividends: pd.Series) -> tuple[pd.DataFrame, pd.DataFrame]:
    strategy = result.loc[
        result["is_evaluation"].astype(bool), "ret"].dropna()
    if strategy.empty:
        raise RuntimeError("headline result has no evaluation returns")
    prior = close.index[close.index < strategy.index.min()]
    if len(prior) == 0:
        raise RuntimeError("benchmark has no anchor for headline calendar table")
    px = close.loc[prior[-1]:strategy.index.max()]
    div = dividends.reindex(px.index).fillna(0.0)
    benchmark = ((px + div) / px.shift(1) - 1.0).reindex(strategy.index)
    if benchmark.isna().any():
        missing = benchmark.index[benchmark.isna()]
        raise RuntimeError(
            f"headline benchmark misses {len(missing)} sessions")
    daily = pd.DataFrame({
        "strategy": strategy,
        "benchmark_total_return": benchmark,
    })

    yearly = (
        daily.groupby(daily.index.year)
        .agg(lambda values: (1.0 + values).prod() - 1.0)
        .rename_axis("year").reset_index()
    )
    yearly["excess"] = (
        yearly["strategy"] - yearly["benchmark_total_return"])

    month_names = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]
    monthly_returns = daily.groupby(
        [daily.index.year, daily.index.month]
    ).agg(lambda values: (1.0 + values).prod() - 1.0)
    monthly_rows: list[dict[str, Any]] = []
    for series_name in daily.columns:
        for year, group in monthly_returns[series_name].groupby(level=0):
            by_month = group.droplevel(0)
            row: dict[str, Any] = {"series": series_name, "year": int(year)}
            for month, label in enumerate(month_names, start=1):
                row[label] = (
                    float(by_month.loc[month]) if month in by_month.index
                    else None)
            year_values = daily.loc[
                daily.index.year == year, series_name]
            row["Yearly"] = float((1.0 + year_values).prod() - 1.0)
            monthly_rows.append(row)
    return pd.DataFrame(monthly_rows), yearly


def render_html_report(
        summary: pd.DataFrame, monthly: pd.DataFrame, yearly: pd.DataFrame,
        spec: dict[str, Any], manifest: dict[str, Any]) -> str:
    headline_id = headline_cell_id(spec)
    headline = summary[summary["cell_id"].eq(headline_id)].copy()
    post = headline[headline["subperiod"].eq("post_publication")]
    if len(post) != 1:
        raise RuntimeError("formal report requires one post-publication headline")
    post_row = post.iloc[0]

    def percent(value: Any, digits: int = 2) -> str:
        if value is None or pd.isna(value):
            return "—"
        return f"{float(value) * 100:.{digits}f}%"

    def cents(value: Any, digits: int = 3) -> str:
        if value is None or pd.isna(value):
            return "—"
        return f"{float(value) * 100:.{digits}f}¢"

    gross_edge = float(post_row["gross_edge_per_traded_share"])
    after_costs = float(
        post_row["trading_edge_after_costs_per_traded_share"])
    if gross_edge <= 0:
        verdict = "Post-publication gross edge is non-positive: the signal is gone."
        verdict_class = "bad"
    elif after_costs <= 0:
        verdict = (
            "Post-publication gross edge is positive, but execution, funding "
            "and borrow costs consume it.")
        verdict_class = "warn"
    else:
        verdict = (
            "Post-publication edge remains positive after execution, funding "
            "and borrow costs.")
        verdict_class = "good"

    headline_rows = headline[[
        "subperiod", "cagr", "benchmark_total_cagr", "excess_cagr",
        "sharpe_calendar", "max_drawdown", "beta_vs_benchmark_total",
        "alpha_annualized", "information_ratio",
        "gross_edge_per_traded_share",
        "execution_cost_per_traded_share",
        "financing_cost_per_traded_share",
        "trading_edge_after_costs_per_traded_share",
    ]].copy()
    for column in (
        "cagr", "benchmark_total_cagr", "excess_cagr", "max_drawdown",
        "alpha_annualized",
    ):
        headline_rows[column] = headline_rows[column].map(percent)
    for column in (
        "gross_edge_per_traded_share",
        "execution_cost_per_traded_share",
        "financing_cost_per_traded_share",
        "trading_edge_after_costs_per_traded_share",
    ):
        headline_rows[column] = headline_rows[column].map(cents)

    yearly_display = yearly.copy()
    for column in ("strategy", "benchmark_total_return", "excess"):
        yearly_display[column] = yearly_display[column].map(percent)
    monthly_display = monthly.copy()
    for column in [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Yearly",
    ]:
        monthly_display[column] = monthly_display[column].map(percent)

    matrix_columns = [
        "profile", "tier", "dividend_mode", "slippage_per_share",
        "subperiod", "cagr", "benchmark_total_cagr", "excess_cagr",
        "sharpe_calendar", "max_drawdown", "gross_edge_per_traded_share",
        "execution_cost_per_traded_share",
        "financing_cost_per_traded_share",
        "trading_edge_after_costs_per_traded_share",
        "beta_vs_benchmark_total", "alpha_annualized", "information_ratio",
    ]
    matrix_records = summary[matrix_columns].replace(
        {np.nan: None}).to_dict(orient="records")
    matrix_json = json.dumps(matrix_records, separators=(",", ":"))
    spec_json = html.escape(json.dumps(
        spec, indent=2, sort_keys=True), quote=False)
    provenance_json = html.escape(json.dumps(
        manifest, indent=2, sort_keys=True, default=str), quote=False)
    headline_table = headline_rows.to_html(
        index=False, classes="data-table", border=0, escape=True)
    yearly_table = yearly_display.to_html(
        index=False, classes="data-table", border=0, escape=True)
    monthly_table = monthly_display.to_html(
        index=False, classes="data-table compact", border=0, escape=True)
    is_formal = manifest.get("classification") == (
        "formal_post_publication_evaluation")
    title = (
        "SPY Intraday Momentum — Frozen Post-Publication Evaluation"
        if is_formal else
        "SPY Intraday Momentum — Non-Formal Engineering Smoke")
    classification_badge = (
        "FORMAL · POINT ESTIMATES ONLY"
        if is_formal else "NON-FORMAL SMOKE · POINT ESTIMATES ONLY")
    matrix_title = (
        "Full 216-row parameter comparison"
        if len(summary) == 216 else
        f"Selected {len(summary)}-row smoke comparison")
    matrix_description = (
        "3 profiles × 3 tiers × 2 dividend modes × 4 slippage levels"
        if len(summary) == 216 else
        f"{summary['cell_id'].nunique()} selected smoke cell")
    missing_metric_rows = int(summary["cagr"].isna().sum())
    missing_metric_note = (
        f"<p class=\"note\"><strong>Coverage note:</strong> "
        f"{missing_metric_rows} rows have no performance estimate. They are "
        "exploratory post-publication cells whose capital path had already "
        "terminated at an unknown exit; the runner reports them as unavailable "
        "instead of fabricating or restarting returns.</p>"
        if missing_metric_rows else "")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root{{--ink:#172033;--muted:#667085;--line:#dbe2ea;--paper:#fff;
--wash:#f4f7fb;--blue:#175cd3;--good:#067647;--warn:#b54708;--bad:#b42318}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--wash);color:var(--ink);
font:14px/1.5 Inter,Segoe UI,Arial,sans-serif}} main{{max-width:1500px;margin:auto;
padding:30px}} h1{{font-size:30px;margin:0 0 6px}} h2{{margin-top:32px}}
.subtitle,.note{{color:var(--muted)}} .panel{{background:var(--paper);
border:1px solid var(--line);border-radius:12px;padding:20px;margin:18px 0;
box-shadow:0 1px 2px #1018280d}} .cards{{display:grid;
grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}
.card{{border:1px solid var(--line);border-radius:10px;padding:14px}}
.label{{color:var(--muted);font-size:12px;text-transform:uppercase}}
.value{{font-size:24px;font-weight:700;margin-top:4px}} .verdict{{font-size:17px;
font-weight:650;border-left:5px solid;padding:12px 14px;background:#f8fafc}}
.verdict.good{{border-color:var(--good)}} .verdict.warn{{border-color:var(--warn)}}
.verdict.bad{{border-color:var(--bad)}} .scroll{{overflow:auto}}
.data-table{{border-collapse:collapse;width:100%;white-space:nowrap}}
.data-table th,.data-table td{{border-bottom:1px solid var(--line);
padding:8px 10px;text-align:right}} .data-table th{{background:#f8fafc;
position:sticky;top:0;z-index:1}} .data-table td:first-child,
.data-table th:first-child{{text-align:left}} .compact{{font-size:12px}}
.filters{{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0}}
select{{padding:7px;border:1px solid var(--line);border-radius:7px;background:white}}
pre{{white-space:pre-wrap;word-break:break-word;background:#101828;color:#e6edf7;
padding:14px;border-radius:8px;max-height:440px;overflow:auto}}
.badge{{display:inline-block;background:#eaf2ff;color:var(--blue);
padding:3px 8px;border-radius:99px;font-weight:650}}
</style>
</head>
<body><main>
<span class="badge">{classification_badge}</span>
<h1>{title}</h1>
<div class="subtitle">Data: 2008-01-22–2026-07-09 · publication cutoff:
2024-05-01 · {matrix_description}</div>

<section class="panel">
<h2>Economic headline</h2>
<p class="verdict {verdict_class}">{html.escape(verdict)}</p>
<div class="cards">
<div class="card"><div class="label">Post CAGR</div><div class="value">
{percent(post_row["cagr"])}</div></div>
<div class="card"><div class="label">SPY total-return CAGR</div><div class="value">
{percent(post_row["benchmark_total_cagr"])}</div></div>
<div class="card"><div class="label">Post excess CAGR</div><div class="value">
{percent(post_row["excess_cagr"])}</div></div>
<div class="card"><div class="label">Post Sharpe vs cash</div><div class="value">
{float(post_row["sharpe_calendar"]):.2f}</div></div>
<div class="card"><div class="label">Gross edge / share</div><div class="value">
{cents(gross_edge)}</div></div>
<div class="card"><div class="label">After-cost edge / share</div><div class="value">
{cents(after_costs)}</div></div>
</div>
<p class="note">Headline: {html.escape(headline_label(spec))}. Cash earns
LIBOR-proxy/SOFR −50bp, borrowed cash costs
benchmark +100bp, and SPY borrow is 25bp p.a. HAC and block bootstrap are
deferred; this report does not provide confidence intervals.</p>
<div class="scroll">{headline_table}</div>
</section>

<section class="panel"><h2>Annual performance vs SPY total return</h2>
<div class="scroll">{yearly_table}</div></section>

<section class="panel"><h2>Monthly performance (paper-style calendar table)</h2>
<p class="note">Both the strategy headline and the independent SPY total-return
benchmark are shown. Q24 paper matching remains a separate replication
experiment and did not tune this economic headline.</p>
<div class="scroll">{monthly_table}</div></section>

<section class="panel"><h2>{matrix_title}</h2>
{missing_metric_note}
<div class="filters">
<select id="period"></select><select id="profile"></select>
<select id="tier"></select><select id="dividend"></select>
<select id="slippage"></select>
</div><div class="scroll"><table class="data-table" id="matrix"></table></div>
</section>

<section class="panel"><h2>Frozen specification and provenance</h2>
<details><summary>evaluation spec v2</summary><pre>{spec_json}</pre></details>
<details><summary>run provenance</summary><pre>{provenance_json}</pre></details>
</section>
</main>
<script>
const rows={matrix_json};
const fields={json.dumps(matrix_columns)};
const filters=[
  ["period","subperiod"],["profile","profile"],["tier","tier"],
  ["dividend","dividend_mode"],["slippage","slippage_per_share"]];
const labels={{
  cagr:"Strategy CAGR",benchmark_total_cagr:"SPY total CAGR",
  excess_cagr:"Excess CAGR",sharpe_calendar:"Sharpe vs cash",
  max_drawdown:"Max drawdown",gross_edge_per_traded_share:"Gross edge/share",
  execution_cost_per_traded_share:"Execution/share",
  financing_cost_per_traded_share:"Funding+borrow/share",
  trading_edge_after_costs_per_traded_share:"After-cost edge/share",
  beta_vs_benchmark_total:"Beta",alpha_annualized:"Annual alpha",
  information_ratio:"Information ratio",slippage_per_share:"Slippage/share"}};
for(const [id,key] of filters){{
  const el=document.getElementById(id);
  const vals=[...new Set(rows.map(r=>String(r[key])))];
  el.innerHTML=`<option value="">All ${{labels[key]||key}}</option>`+
    vals.map(v=>`<option>${{v}}</option>`).join("");
  el.onchange=render;
}}
function fmt(key,value){{
  if(value===null||value===undefined) return "—";
  if(["cagr","benchmark_total_cagr","excess_cagr","max_drawdown",
      "alpha_annualized"].includes(key)) return (value*100).toFixed(2)+"%";
  if(key.includes("per_traded_share")) return (value*100).toFixed(3)+"¢";
  if(key==="slippage_per_share") return "$"+Number(value).toFixed(4);
  if(typeof value==="number") return value.toFixed(3);
  return value;
}}
function render(){{
  const selected=Object.fromEntries(filters.map(([id,key])=>
    [key,document.getElementById(id).value]));
  const shown=rows.filter(r=>Object.entries(selected).every(
    ([k,v])=>!v||String(r[k])===v));
  const head="<thead><tr>"+fields.map(f=>`<th>${{labels[f]||f}}</th>`).join("")+
    "</tr></thead>";
  const body="<tbody>"+shown.map(r=>"<tr>"+fields.map(f=>
    `<td>${{fmt(f,r[f])}}</td>`).join("")+"</tr>").join("")+"</tbody>";
  document.getElementById("matrix").innerHTML=head+body;
}}
render();
</script></body></html>"""


def decomposition_rows(
        cell: Cell, period: Period, result: pd.DataFrame,
        ledger: dict[str, pd.DataFrame]) -> list[dict[str, Any]]:
    sliced = slice_result(result, period)
    rows: list[dict[str, Any]] = []

    def add(component: str, bucket: str, value: float | int | None,
            unit: str, observations: int | None = None) -> None:
        rows.append({
            "cell_id": cell.cell_id,
            **asdict(cell),
            "subperiod": period.name,
            "component": component,
            "bucket": bucket,
            "value": value,
            "unit": unit,
            "observations": observations,
        })

    add("long_vs_short", "long_gross_pnl",
        float(sliced["long_gross"].sum()), "USD")
    add("long_vs_short", "short_gross_pnl",
        float(sliced["short_gross"].sum()), "USD")
    add("signal_count", "all", int(sliced["signal_count"].sum()), "signals")
    active_count = int(sliced["status"].eq("active").sum())
    add(
        "average_holding_minutes", "active_session",
        float(sliced["holding_minutes"].sum()) / max(active_count, 1),
        "minutes", active_count)
    avg_aum = float(sliced["prev_aum"].mean()) if len(sliced) else np.nan
    add(
        "turnover", "traded_notional_over_average_aum",
        (float(sliced["traded_notional"].sum()) / avg_aum
         if np.isfinite(avg_aum) and avg_aum > 0 else None),
        "ratio")
    shares = float(sliced["shares_traded"].sum())
    add(
        "gross_edge_per_traded_share", "all",
        float(sliced["gross"].sum()) / shares if shares > 0 else None,
        "USD_per_share")
    add(
        "cost_per_traded_share", "execution",
        float(sliced["cost"].sum()) / shares if shares > 0 else None,
        "USD_per_share")
    add(
        "cost_per_traded_share", "financing_and_borrow",
        float(-sliced["financing"].sum()) / shares if shares > 0 else None,
        "USD_per_share")

    trips = ledger.get("round_trips", pd.DataFrame())
    if not trips.empty:
        trips = trips.copy()
        trips["session_date"] = pd.to_datetime(trips["session_date"])
        trips = trips[
            (trips["session_date"] >= period.start)
            & (trips["session_date"] <= period.end)
        ]
        if not trips.empty:
            entry = pd.to_numeric(trips["entry_minute"], errors="coerce")
            bucket = pd.cut(
                entry, bins=[0, 120, 270, np.inf],
                labels=["open_1_120", "midday_121_270", "close_271_plus"])
            grouped = trips.assign(entry_bucket=bucket).groupby(
                "entry_bucket", observed=False)
            for name, group in grouped:
                add(
                    "entry_time_bucket", str(name),
                    float(group["gross"].sum()), "gross_USD", len(group))

            daily_features = result.attrs.get(
                "daily_features", pd.DataFrame()).copy()
            if not daily_features.empty:
                dvol = daily_features["dvol"].dropna()
                pre = dvol[dvol.index < pd.Timestamp("2024-05-01")]
                if len(pre) >= 5:
                    thresholds = pre.quantile([0.2, 0.4, 0.6, 0.8]).to_numpy()
                    trips["dvol"] = trips["session_date"].map(dvol)
                    trips["volatility_quintile"] = pd.cut(
                        trips["dvol"],
                        bins=[-np.inf, *thresholds, np.inf],
                        labels=["Q1", "Q2", "Q3", "Q4", "Q5"])
                    grouped = trips.groupby(
                        "volatility_quintile", observed=False)
                    for name, group in grouped:
                        add(
                            "volatility_regime_quintile", str(name),
                            float(group["gross"].sum()), "gross_USD", len(group))
    return rows


def write_frame(frame: pd.DataFrame, path: Path) -> None:
    frame = frame.copy()
    # In-memory backtest attrs contain DataFrames (ledgers/features). They are
    # intentionally published as separate files, not embedded in parquet
    # metadata where pyarrow cannot JSON-serialize them.
    frame.attrs = {}
    if path.suffix == ".csv":
        frame.to_csv(path, index=False, float_format="%.12g")
    else:
        frame.to_parquet(path, index=False)


def publish(
        output_root: Path, run_name: str, frames: dict[str, pd.DataFrame],
        manifest: dict[str, Any],
        text_files: dict[str, str] | None = None) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    final = output_root / run_name
    if final.exists():
        raise FileExistsError(f"refusing to overwrite existing result: {final}")
    staging = output_root / f".staging-{run_name}-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        files: dict[str, dict[str, Any]] = {}
        for name, frame in frames.items():
            path = staging / name
            write_frame(frame, path)
            files[name] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
        for name, content in (text_files or {}).items():
            path = staging / name
            path.write_text(content, encoding="utf-8")
            files[name] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
        manifest = {**manifest, "files": files}
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8")
        success = {
            "run_name": run_name,
            "manifest_sha256": sha256(manifest_path),
            "classification": manifest["classification"],
        }
        (staging / "_SUCCESS").write_text(
            json.dumps(success, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(staging, final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return final


def execute(args: argparse.Namespace) -> Path:
    spec_path = args.spec.resolve()
    release_dir = args.data_release.resolve()
    spec, spec_hash = load_spec(spec_path)
    release = inspect_release(release_dir)
    cells = select_cells(build_cells(spec), args.smoke_cell)
    periods = build_periods(
        spec, release["expected_start"], release["expected_end"])
    git = git_state()
    is_smoke = bool(args.smoke_cell)
    classification = (
        "non_formal_smoke" if is_smoke else "formal_post_publication_evaluation")

    if not is_smoke:
        preflight = make_plan(
            spec_path, release_dir, args.benchmark_daily,
            args.financing_rates)
        if not preflight["formal_ready"]:
            raise RuntimeError(
                "formal execution blocked: "
                + "; ".join(preflight["formal_gaps"]))
    elif git["dirty"] and not args.allow_dirty_smoke:
        raise RuntimeError("dirty smoke run requires --allow-dirty-smoke")

    financing = spec.get("financing", {})
    financing_info = (
        inspect_financing_release(args.financing_rates, spec)
        if args.financing_rates is not None else None)
    financing_rates = (
        load_financing_rates(Path(financing_info["daily_rates_path"]))
        if financing_info is not None else None)
    data_cache: dict[str, dict[str, Any]] = {}
    benchmark_close = (
        load_daily_benchmark(args.benchmark_daily)
        if args.benchmark_daily is not None else None)
    summary_rows: list[dict[str, Any]] = []
    daily_frames: list[pd.DataFrame] = []
    ledger_frames: dict[str, list[pd.DataFrame]] = {
        "signals": [], "orders": [], "fills": [], "round_trips": []}
    decomposition: list[dict[str, Any]] = []
    headline_result: pd.DataFrame | None = None
    headline_dividends = pd.Series(dtype=float)
    target_headline = headline_cell_id(spec) if "headline" in spec else None

    for i, cell in enumerate(cells, start=1):
        print(f"[{i}/{len(cells)}] {cell.cell_id}", flush=True)
        cfg = engine.profile_cfg(
            cell.profile,
            tier=cell.tier,
            ignore_dividends=cell.dividend_mode == "ignore_dividends",
            slip_per_share=cell.slippage_per_share,
            cash_rate_annual=float(financing.get("cash_rate_annual", 0.0)),
            funding_rate_annual=float(financing.get("funding_rate_annual", 0.0)),
            borrow_rate_annual=float(financing.get("borrow_rate_annual", 0.0)),
        )
        cache_key = cell.dividend_mode
        if cache_key not in data_cache:
            data_cache[cache_key] = engine.load_run(release_dir, cfg)
        data = data_cache[cache_key]
        result = engine.backtest(
            data, cfg, collect_ledger=True,
            financing_rates=financing_rates)
        if cell.cell_id == target_headline:
            headline_result = result.copy()
            headline_dividends = (
                data["dividends"].copy()
                if data["dividends"] is not None
                else pd.Series(dtype=float))
        tagged = result.reset_index()
        # Backtest attrs contain DataFrame-valued ledgers/features. Pandas
        # compares attrs during concat, where DataFrame equality has no scalar
        # truth value. Those attrs are published separately below.
        tagged.attrs = {}
        tagged.insert(0, "cell_id", cell.cell_id)
        daily_frames.append(tagged)
        ledger = result.attrs.get("ledger", {})
        for name in ledger_frames:
            frame = ledger.get(name, pd.DataFrame()).copy()
            if not frame.empty:
                frame.insert(0, "cell_id", cell.cell_id)
                ledger_frames[name].append(frame)

        for period in periods:
            sliced = slice_result(result, period)
            row = {
                "cell_id": cell.cell_id,
                **asdict(cell),
                "subperiod": period.name,
                **performance_metrics(sliced, cfg.rf_annual),
            }
            if benchmark_close is not None and not sliced.empty:
                row.update(benchmark_metrics(
                    sliced["ret"].fillna(0.0), benchmark_close,
                    data["dividends"] if data["dividends"] is not None
                    else pd.Series(dtype=float),
                    period))
            summary_rows.append(row)
            decomposition.extend(
                decomposition_rows(cell, period, result, ledger))

    summary = pd.DataFrame(summary_rows)
    expected_rows = len(cells) * len(periods)
    key = ["cell_id", "subperiod"]
    if len(summary) != expected_rows or summary.duplicated(key).any():
        raise RuntimeError(
            f"completeness failure: expected {expected_rows} unique rows, "
            f"got {len(summary)}")

    frames: dict[str, pd.DataFrame] = {
        "summary.csv": summary,
        "decomposition.csv": pd.DataFrame(decomposition),
        "daily_results.parquet": pd.concat(daily_frames, ignore_index=True),
    }
    monthly = yearly = None
    if benchmark_close is not None and headline_result is not None:
        monthly, yearly = headline_calendar_tables(
            headline_result, benchmark_close, headline_dividends)
        frames["headline_monthly.csv"] = monthly
        frames["headline_yearly.csv"] = yearly
    for name, parts in ledger_frames.items():
        frames[f"{name}.parquet"] = (
            pd.concat(parts, ignore_index=True) if parts else pd.DataFrame())

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_name = (
        f"{timestamp}_{'smoke' if is_smoke else 'formal'}_"
        f"spec{spec['spec_version']}_{spec_hash[:12]}"
    )
    manifest = {
        "classification": classification,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "spec_path": str(spec_path),
        "spec_version": spec["spec_version"],
        "spec_sha256": spec_hash,
        "engine_script_sha256": sha256(ROOT / "im_engine_v4.py"),
        "data_script_sha256_current": sha256(ROOT / "prepare_spy_data.py"),
        "data_release": release,
        "benchmark": (
            {"path": str(args.benchmark_daily.resolve()),
             "sha256": sha256(args.benchmark_daily)}
            if args.benchmark_daily is not None else None),
        "financing_rates": financing_info,
        "git": git,
        "matrix_cells": len(cells),
        "subperiods": [asdict(period) for period in periods],
        "expected_summary_rows": expected_rows,
        "actual_summary_rows": len(summary),
        "capital_path": spec.get(
            "capital_path", "continuous_full_run_then_slice_returns"),
        "notes": [
            "post-publication is an evaluation period, not untouched OOS",
            "tier is applied after full-calendar feature construction",
            "statistics are point estimates; HAC and bootstrap are deferred",
        ],
    }
    text_files = {}
    if monthly is not None and yearly is not None:
        text_files["report.html"] = render_html_report(
            summary, monthly, yearly, spec, manifest)
    return publish(
        args.output_root.resolve(), run_name, frames, manifest,
        text_files=text_files)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spec", type=Path, default=ROOT / "config" / "evaluation_spec_v1.yml")
    parser.add_argument(
        "--data-release", type=Path, default=ROOT / "data_release_v1")
    parser.add_argument(
        "--output-root", type=Path, default=ROOT / "evaluation" / "results")
    parser.add_argument("--benchmark-daily", type=Path)
    parser.add_argument(
        "--financing-rates", type=Path,
        help="immutable financing-rate release directory")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument(
        "--smoke-cell", action="append", default=[],
        help="run only the exact deterministic cell_id; repeatable")
    parser.add_argument("--allow-dirty-smoke", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.plan_only:
        plan = make_plan(
            args.spec.resolve(), args.data_release.resolve(),
            args.benchmark_daily.resolve() if args.benchmark_daily else None,
            args.financing_rates.resolve() if args.financing_rates else None)
        print(json.dumps(plan, indent=2, sort_keys=True, default=str))
        return 0
    output = execute(args)
    print(f"published: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
