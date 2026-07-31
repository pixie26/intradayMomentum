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


def formal_gaps(spec: dict[str, Any], benchmark_path: Path | None) -> list[str]:
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
        benchmark_path: Path | None = None) -> dict[str, Any]:
    spec, spec_hash = load_spec(spec_path)
    release = inspect_release(release_dir)
    cells = build_cells(spec)
    periods = build_periods(
        spec, release["expected_start"], release["expected_end"])
    git = git_state()
    gaps = formal_gaps(spec, benchmark_path)
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
            float((x.mean() - rf_annual / 252.0) / std * np.sqrt(252))
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
        manifest: dict[str, Any]) -> Path:
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
        preflight = make_plan(spec_path, release_dir, args.benchmark_daily)
        if not preflight["formal_ready"]:
            raise RuntimeError(
                "formal execution blocked: "
                + "; ".join(preflight["formal_gaps"]))
    elif git["dirty"] and not args.allow_dirty_smoke:
        raise RuntimeError("dirty smoke run requires --allow-dirty-smoke")

    financing = spec.get("financing", {})
    data_cache: dict[str, dict[str, Any]] = {}
    benchmark_close = (
        load_daily_benchmark(args.benchmark_daily)
        if args.benchmark_daily is not None else None)
    summary_rows: list[dict[str, Any]] = []
    daily_frames: list[pd.DataFrame] = []
    ledger_frames: dict[str, list[pd.DataFrame]] = {
        "signals": [], "orders": [], "fills": [], "round_trips": []}
    decomposition: list[dict[str, Any]] = []

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
        result = engine.backtest(data, cfg, collect_ledger=True)
        tagged = result.reset_index()
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
        ],
    }
    return publish(args.output_root.resolve(), run_name, frames, manifest)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spec", type=Path, default=ROOT / "config" / "evaluation_spec_v1.yml")
    parser.add_argument(
        "--data-release", type=Path, default=ROOT / "data_release_v1")
    parser.add_argument(
        "--output-root", type=Path, default=ROOT / "evaluation" / "results")
    parser.add_argument("--benchmark-daily", type=Path)
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
            args.benchmark_daily.resolve() if args.benchmark_daily else None)
        print(json.dumps(plan, indent=2, sort_keys=True, default=str))
        return 0
    output = execute(args)
    print(f"published: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
