#!/usr/bin/env python3
"""Prepare SPY 1-minute data for a reproducible intraday-momentum backtest.

v5.  Principles unchanged: raw OHLCV is never adjusted or forward-filled;
ambiguity fails loudly; missing bars are diagnosed, not fabricated.

Changes in v5
-------------
The data-release contract now supports explicit expected boundaries, separates
OHLCV duplicate conflicts from optional metadata conflicts, classifies true
one-minute / ordinary-gap / halt-reopen returns independently, and verifies an
exact dependency lock before publishing.

Changes in v4
-------------
Component-level feature validity replaces the session tier as the engine's gate,
because filtering bars to a tier and computing features afterwards silently
redefines the strategy (previous-close reaches across the removed session; a
14-session window stretches over more calendar days).

IMPORTANT -- `signal_valid_default_config` is named that way on purpose. It is
baked at the --trade-freq / --sigma-window given on this command line and it
assumes the VWAP filter and volatility targeting are both on. A general engine
must NOT consume it; it must reassemble the mask from the parameter-free
primitives (bar_present, open_valid, close_valid, move_open_obs_valid,
vwap_valid, is_halt_minute, is_executable_minute, daily_ret_valid). The field
exists only for default-configuration parity checks.

Earlier changes, driven by review findings reproduced on real data
------------------------------------------------------------------
1. Tiered outputs.  `is_tradable` is an exploration filter, not a backtest gate.
   The formal replication set now contains only sessions whose *every scheduled
   minute* is present.  Four files are emitted: clean / paper_ready /
   halt_aware / exploratory.

2. Halt validation by minute set, not bar count.  v2 compared counts, so a
   vendor that emitted phantom bars *during* a halt (or dropped 14 real minutes
   elsewhere while padding the halt window) passed silently.  v3 computes
   scheduled / halt / observed minute sets and reports missing_required,
   present_during_halt and unexpected_minutes.  Overlapping halt windows are
   unioned before counting.

3. Session-minute eligibility.  A 13:00 early close does not have a 15:30 bar
   because the exchange never scheduled one.  v2 scored those minutes as absent
   and depressed the trailing per-minute counts for the next `sigma_window`
   sessions.  v3 rolls each minute bucket over *eligible* sessions only.

4. Band warmup requires `sigma_window` full observations, not `window - 1`.

5. Timestamp parsing handles numeric-strings, explicit --timestamp-format and
   --epoch-unit, and refuses any interpretation that lands outside a plausible
   calendar range (which is what silently turned YYYYMMDDHHMMSS into year 2611).

6. Dividends: symbol aliases, duplicate/conflict policy instead of blind
   summation, and a residual-based (not sign-based) raw-vs-adjusted test whose
   output is named `evidence_supports_raw_prices`.

7. Format-regime detection is labelled as a heuristic hint
   (`candidate_format_regime`), never as vendor identification.  A real source
   column, or an explicit --source-split, always wins.

8. Atomic publication: everything is written to a temp dir, moved to
   runs/<run_id>/ on success, marked with _SUCCESS, and pointed to by
   latest.json.  A failed run can no longer leave a half-updated directory.

9. Manifest records every threshold, the full CLI, and SHA-256 of the script,
   inputs, outputs and reports.

Usage
-----
python prepare_spy_data.py --self-test

python prepare_spy_data.py \
  --input data/raw/SPY_1min_2008_202607_merged.parquet \
  --dividends data/raw/spy_dividends_full.csv \
  --output-dir data/processed \
  --input-timezone America/New_York \
  --bar-label start
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import yaml
from pandas.api.types import is_datetime64_any_dtype, is_numeric_dtype

UTC_NS = "datetime64[ns, UTC]"
SCRIPT_PATH = Path(__file__).resolve()
DATA_SELF_TEST_COUNT = 28

# Plausible calendar range for any interpretation of an input timestamp.
# Anything outside this is a parsing error, not data.
MIN_PLAUSIBLE_YEAR = 1980
MAX_PLAUSIBLE_YEAR = 2100

CANONICAL_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp": ("timestamp", "caldt", "datetime", "date_time", "window_start",
                  "start_time", "bar_time", "time", "t"),
    "symbol": ("symbol", "ticker", "sym", "s"),
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c"),
    "volume": ("volume", "vol", "v"),
    "vendor_bar_vwap": ("vendor_bar_vwap", "vwap", "vw", "weighted_average_price"),
    "transactions": ("transactions", "trades", "trade_count", "n"),
}

# Columns that, if present in the raw file, identify the data source directly.
SOURCE_COLUMN_ALIASES = ("source", "vendor", "provider", "feed", "data_source")

DIVIDEND_ALIASES: dict[str, tuple[str, ...]] = {
    "ex_date": ("ex_date", "ex_dividend_date", "exdate", "date", "timestamp"),
    "cash_amount": ("cash_amount", "dividend", "amount", "cash_dividend", "value"),
    "symbol": ("symbol", "ticker", "sym", "s"),
    "dividend_type": ("dividend_type", "type", "kind", "frequency"),
}

PRICE_COLUMNS = ["open", "high", "low", "close"]
OPTIONAL_NUMERIC = ["vendor_bar_vwap", "transactions"]

# Market-wide circuit-breaker halts. Exchange calendars model holidays and early
# closes but not intraday halts, so without this table `expected_bars` is wrong
# on exactly the four largest COVID sessions. Local exchange time, inclusive.
KNOWN_HALTS: dict[str, list[tuple[str, str]]] = {
    "2020-03-09": [("09:35", "09:48")],
    "2020-03-12": [("09:36", "09:49")],
    "2020-03-16": [("09:31", "09:44")],
    "2020-03-18": [("12:57", "13:10")],
}

QUALITY_ORDER = ["complete", "halt_adjusted", "interior_gap", "truncated",
                 "halt_anomaly", "sparse", "absent"]


class DataValidationError(RuntimeError):
    """Raised when data cannot be cleaned without an arbitrary repair."""


@dataclass(frozen=True)
class AuditCounts:
    input_rows: int
    symbol_filtered_rows: int
    bad_timestamp_rows: int
    duplicate_rows_removed: int
    conflicting_duplicate_timestamps: int
    conflicting_ohlcv_timestamps: int
    conflicting_optional_metadata_timestamps: int
    outside_rth_rows: int
    off_grid_rows: int
    invalid_ohlc_rows_dropped: int
    clean_rth_rows: int

    def check_conservation(self) -> None:
        accounted = (self.symbol_filtered_rows + self.bad_timestamp_rows
                     + self.duplicate_rows_removed + self.outside_rth_rows
                     + self.invalid_ohlc_rows_dropped + self.clean_rth_rows)
        if accounted != self.input_rows:
            raise DataValidationError(
                f"Row conservation violated: {accounted} accounted for vs "
                f"{self.input_rows} input rows (delta {self.input_rows - accounted})."
            )


# --------------------------------------------------------------------------- #
# args
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", type=Path)
    p.add_argument("--dividends", type=Path, default=None)
    p.add_argument("--output-dir", type=Path, default=Path("data/processed"))
    p.add_argument("--symbol", default="SPY")
    p.add_argument("--calendar", default="XNYS")

    # timestamp interpretation
    p.add_argument("--timestamp-column", default=None,
                   help="Force a timestamp column. Otherwise the alias candidate "
                        "with the fewest nulls wins; the choice is recorded.")
    p.add_argument("--input-timezone", default=None,
                   help="REQUIRED when timestamps are timezone-naive. No default: "
                        "guessing wrong shifts every bar out of session.")
    p.add_argument("--timestamp-format", default=None,
                   help="Explicit strptime format, e.g. '%%Y%%m%%d%%H%%M%%S'. "
                        "Use this for calendar-shaped integers that would "
                        "otherwise be mistaken for an epoch.")
    p.add_argument("--epoch-unit", choices=("auto", "s", "ms", "us", "ns"),
                   default="auto")
    p.add_argument("--bar-label", choices=("start", "end"), default="start")
    p.add_argument("--expected-start", default=None,
                   help="Expected first calendar date of the release, YYYY-MM-DD. "
                        "Must be supplied together with --expected-end.")
    p.add_argument("--expected-end", default=None,
                   help="Expected last calendar date of the release, YYYY-MM-DD. "
                        "Must be supplied together with --expected-start.")
    p.add_argument("--max-boundary-sessions-missing", type=int, default=0,
                   help="Maximum entirely absent exchange sessions before the "
                        "first or after the last observed session inside explicit "
                        "expected boundaries. Formal releases should keep 0.")

    # policies
    p.add_argument("--duplicate-policy",
                   choices=("error", "source_precedence", "last"), default="error",
                   help="Resolution for OHLCV-conflicting duplicate timestamps. "
                        "'error' is required for headline releases. "
                        "'source_precedence' requires --source-precedence; legacy "
                        "'last' also requires --confirm-file-order-precedence.")
    p.add_argument("--source-precedence", default=None,
                   help="Comma-separated source values from highest to lowest "
                        "quality. Required to resolve transactions conflicts or "
                        "when --duplicate-policy source_precedence is selected.")
    p.add_argument("--confirm-file-order-precedence", action="store_true",
                   help="Explicitly permit legacy --duplicate-policy last. File "
                        "order is not a quality signal; never use for headline runs.")
    p.add_argument("--strategy-vwap-source",
                   choices=("hlc3", "ohlc4", "vendor_bar_vwap"), default="hlc3",
                   help="VWAP source intended downstream. Conflicting vendor VWAP "
                        "metadata is fatal only for vendor_bar_vwap.")
    p.add_argument("--invalid-row-policy", choices=("error", "drop"), default="error")
    p.add_argument("--confirm-dividend-sum", action="store_true",
                   help="Required alongside --dividend-duplicate-policy sum "
                        "when no dividend_type evidence is available. Summing "
                        "is only correct for a genuine regular+special pair; "
                        "without confirmation a re-scraped event is doubled.")
    p.add_argument("--dividend-duplicate-policy",
                   choices=("error", "sum", "first"), default="error",
                   help="Same ex-date appearing more than once with different "
                        "amounts. 'sum' is only correct for genuine "
                        "regular+special pairs.")

    # halts
    p.add_argument("--halts", type=Path, default=None,
                   help="CSV: session_date,start_local,end_local (inclusive).")
    p.add_argument("--no-builtin-halts", action="store_true")
    p.add_argument("--halt-bar-policy", choices=("absent", "allow_present"),
                   default="absent",
                   help="Whether the vendor is expected to omit halt minutes "
                        "('absent') or may carry bars through them.")

    # strategy-relevant coverage
    p.add_argument("--trade-freq", type=int, default=30)
    p.add_argument("--sigma-window", type=int, default=14)

    # source segments
    p.add_argument("--source-split", default=None,
                   help="Comma-separated known splice dates, e.g. '2016-01-04'. "
                        "Always more reliable than the heuristic.")
    p.add_argument("--requirements-lock", type=Path,
                   default=SCRIPT_PATH.with_name("requirements.lock"),
                   help="Exact environment lock. Installed versions are checked "
                        "before a run is published.")
    p.add_argument("--release-config", type=Path, default=None,
                   help="Frozen YAML release contract. When supplied, every "
                        "declared pipeline setting must match the CLI.")

    # thresholds
    p.add_argument("--max-bad-timestamp-frac", type=float, default=0.005)
    p.add_argument("--max-invalid-frac", type=float, default=0.005)
    p.add_argument("--min-session-coverage", type=float, default=0.98)
    p.add_argument("--max-absent-sessions", type=int, default=20)
    p.add_argument("--max-consecutive-absent-sessions", type=int, default=1)
    p.add_argument("--min-open-alignment", type=float, default=0.95)
    p.add_argument("--extreme-1min-return", type=float, default=0.01)

    p.add_argument("--keep-runs", type=int, default=10,
                   help="How many historical run directories to retain.")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--preflight-self-test", action="store_true",
                   help="Run and require the complete synthetic data test suite "
                        "before processing market data; required by data-v1.0.")
    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# io
# --------------------------------------------------------------------------- #

def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    d = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while block := fh.read(chunk):
            d.update(block)
    return d.hexdigest()


def validate_environment_lock(path: Path) -> dict:
    """Require the running environment to match an exact requirements lock."""
    if not path.exists():
        raise DataValidationError(
            f"Dependency lock not found: {path}. A publishable data run must use "
            "an exact, versioned environment.")
    locked: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line or line.startswith(("-", "--")):
            raise DataValidationError(
                f"Unsupported requirements lock entry {line!r}; expected name==version.")
        name, version = (x.strip() for x in line.split("==", 1))
        locked[name] = version

    mismatches = []
    installed = {}
    for name, expected in locked.items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = None
        installed[name] = actual
        if actual != expected:
            mismatches.append(
                {"package": name, "expected": expected, "installed": actual})
    if mismatches:
        raise DataValidationError(
            f"Environment does not match {path}: {mismatches}. "
            f"Install with `python -m pip install -r {path}`.")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "packages": installed,
    }


def validate_release_contract(
        path: Path | None, args: argparse.Namespace) -> dict | None:
    if path is None:
        return None
    if not path.exists():
        raise DataValidationError(f"Release config not found: {path}.")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise DataValidationError(f"Release config must be a mapping: {path}.")

    metadata_keys = {"release_id", "status"}
    path_keys = {"input", "dividends", "requirements_lock"}
    date_string_keys = {"source_split", "expected_start", "expected_end"}
    mismatches = []
    for key, expected in loaded.items():
        if key in metadata_keys:
            continue
        if not hasattr(args, key):
            raise DataValidationError(
                f"Release config declares unsupported setting {key!r}.")
        actual = getattr(args, key)
        if key in path_keys:
            expected_value = (
                None if expected is None
                else str((SCRIPT_PATH.parent / str(expected)).resolve()))
            actual_value = (
                None if actual is None else str(Path(actual).resolve()))
        elif key in date_string_keys:
            expected_value = None if expected is None else str(expected)
            actual_value = actual
        else:
            expected_value = expected
            actual_value = actual
        if actual_value != expected_value:
            mismatches.append({
                "setting": key, "config": expected_value, "cli": actual_value})
    if mismatches:
        raise DataValidationError(
            f"CLI does not match release config {path}: {mismatches}.")
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "release_id": loaded.get("release_id"),
        "status": loaded.get("status"),
    }


def git_provenance() -> dict:
    """Tie a data run to both the worktree bytes and the current Git commit."""
    repo = SCRIPT_PATH.parent

    def call(*argv: str, text: bool = True):
        return subprocess.run(
            ["git", *argv], cwd=repo, check=True, capture_output=True,
            text=text)

    try:
        commit = call("rev-parse", "HEAD").stdout.strip()
        status = call("status", "--porcelain").stdout
        head_script = call(
            "show", f"{commit}:{SCRIPT_PATH.name}", text=False).stdout
        head_script_sha256 = hashlib.sha256(head_script).hexdigest()
        script_sha256 = sha256_file(SCRIPT_PATH)
        script_diff = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--", SCRIPT_PATH.name],
            cwd=repo, check=False)
        return {
            "available": True,
            "commit": commit,
            "dirty": bool(status.strip()),
            "script_sha256": script_sha256,
            "head_script_sha256": head_script_sha256,
            # Git applies checkout/clean filters (notably CRLF<->LF) when
            # deciding whether a tracked file matches HEAD. Preserve both exact
            # byte hashes above, but use Git semantics for the acceptance gate.
            "script_matches_head": script_diff.returncode == 0,
        }
    except (OSError, subprocess.CalledProcessError) as exc:
        return {
            "available": False,
            "error": str(exc),
            "script_sha256": sha256_file(SCRIPT_PATH),
        }


def read_table(path: Path) -> pd.DataFrame:
    suffix = Path(path).suffix.lower()
    if suffix in {".parquet", ".pq"}:
        try:
            return pd.read_parquet(path)
        except ImportError as exc:
            raise RuntimeError("Reading parquet requires pyarrow.") from exc
    if suffix in {".csv", ".txt"}:
        return pd.read_csv(path, low_memory=False)
    raise ValueError(f"Unsupported input format: {path}")


def write_parquet(df: pd.DataFrame, path: Path) -> None:
    try:
        df.to_parquet(path, index=False, engine="pyarrow", compression="zstd")
    except ImportError as exc:
        raise RuntimeError("Writing parquet requires pyarrow.") from exc


def normalize_names(columns: Iterable[object]) -> dict[str, str]:
    return {str(c): str(c).strip().lower().replace("-", "_").replace(" ", "_")
            for c in columns}


def resolve_columns(df: pd.DataFrame, aliases: dict[str, tuple[str, ...]],
                    required: Iterable[str]) -> pd.DataFrame:
    n2o = {n: o for o, n in normalize_names(df.columns).items()}
    rename: dict[str, str] = {}
    claimed: set[str] = set()
    for canonical, candidates in aliases.items():
        for cand in candidates:
            src = n2o.get(cand)
            if src is not None and src not in claimed:
                rename[src] = canonical
                claimed.add(src)
                break
    out = df.rename(columns=rename).copy()
    if out.columns.duplicated().any():
        raise DataValidationError(
            f"Alias resolution produced duplicate columns "
            f"{out.columns[out.columns.duplicated()].tolist()}; "
            f"original columns {list(df.columns)}")
    missing = [c for c in required if c not in out.columns]
    if missing:
        raise DataValidationError(
            f"Missing required columns {missing}. Available: {list(df.columns)}")
    return out


def pick_timestamp_column(df: pd.DataFrame, forced: str | None) -> tuple[str, dict]:
    """Choose among competing timestamp aliases by null share, not alias order.

    Taking the first alias match silently discarded 43% of a spliced file that
    carried both `caldt` and a half-null `timestamp`."""
    n2o = {n: o for o, n in normalize_names(df.columns).items()}
    if forced is not None:
        key = forced.strip().lower().replace("-", "_").replace(" ", "_")
        if key not in n2o:
            raise DataValidationError(
                f"--timestamp-column {forced!r} not found. Columns: {list(df.columns)}")
        return n2o[key], {"selection": "forced", "chosen": n2o[key]}
    cands = [n2o[c] for c in CANONICAL_ALIASES["timestamp"] if c in n2o]
    if not cands:
        raise DataValidationError(
            f"No timestamp-like column found. Columns: {list(df.columns)}")
    shares = {c: float(df[c].notna().mean()) for c in cands}
    best = max(shares, key=shares.get)
    return best, {"selection": "auto_min_nulls", "chosen": best,
                  "candidates": {k: round(v, 6) for k, v in shares.items()}}


# --------------------------------------------------------------------------- #
# timestamps
# --------------------------------------------------------------------------- #

def infer_epoch_unit(values: pd.Series) -> str:
    finite = pd.to_numeric(values, errors="coerce").dropna().abs()
    if finite.empty:
        raise DataValidationError("Timestamp column contains no usable values.")
    m = float(finite.median())
    if m >= 1e17:
        return "ns"
    if m >= 1e14:
        return "us"
    if m >= 1e11:
        return "ms"
    return "s"


def _assert_plausible(parsed: pd.Series, how: str) -> pd.Series:
    """Reject any interpretation landing outside a plausible calendar range.

    This is the guard that catches YYYYMMDDHHMMSS being read as milliseconds
    (which silently produced year 2611)."""
    valid = parsed.dropna()
    if valid.empty:
        raise DataValidationError(
            f"Timestamp parsing via {how} produced no valid values.")
    years = valid.dt.year
    lo, hi = int(years.min()), int(years.max())
    if lo < MIN_PLAUSIBLE_YEAR or hi > MAX_PLAUSIBLE_YEAR:
        raise DataValidationError(
            f"Timestamp parsing via {how} produced years {lo}-{hi}, outside "
            f"[{MIN_PLAUSIBLE_YEAR}, {MAX_PLAUSIBLE_YEAR}]. The input is probably "
            "not an epoch in that unit -- e.g. YYYYMMDDHHMMSS integers read as "
            "milliseconds. Pass --timestamp-format (and/or --epoch-unit) "
            "explicitly.")
    return parsed


def _localize(parsed: pd.Series, tz: str | None, what: str) -> pd.Series:
    if tz is None:
        raise DataValidationError(
            f"{what} are timezone-naive and --input-timezone was not supplied. "
            "Refusing to guess: a wrong assumption shifts every bar out of the "
            "trading session while still producing a plausible-looking run. "
            "Pass e.g. --input-timezone America/New_York.")
    return parsed.dt.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")


def _from_epoch(numeric: pd.Series, epoch_unit: str) -> pd.Series:
    unit = infer_epoch_unit(numeric) if epoch_unit == "auto" else epoch_unit
    parsed = pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")
    return _assert_plausible(parsed, f"epoch unit '{unit}'").astype(UTC_NS)


def parse_timestamp(series: pd.Series, input_timezone: str | None,
                    timestamp_format: str | None = None,
                    epoch_unit: str = "auto") -> pd.Series:
    """Return a tz-aware UTC ns series. Datetimes never route through to_numeric."""
    # 0. explicit format always wins
    if timestamp_format is not None:
        parsed = pd.to_datetime(series.astype(str), format=timestamp_format,
                                errors="coerce")
        parsed = _assert_plausible(parsed, f"format {timestamp_format!r}")
        if isinstance(parsed.dtype, pd.DatetimeTZDtype):
            return parsed.dt.tz_convert("UTC").astype(UTC_NS)
        return _localize(parsed, input_timezone, "Parsed timestamps") \
            .dt.tz_convert("UTC").astype(UTC_NS)

    # 1. already datetime, tz-aware
    if isinstance(series.dtype, pd.DatetimeTZDtype):
        return series.dt.tz_convert("UTC").astype(UTC_NS)

    # 2. already datetime, tz-naive  (the `caldt` case)
    if is_datetime64_any_dtype(series):
        return _localize(series, input_timezone, "Input timestamps") \
            .dt.tz_convert("UTC").astype(UTC_NS)

    # 3. true numeric epoch
    if is_numeric_dtype(series):
        return _from_epoch(series, epoch_unit)

    # 4. object/string. Numeric-looking strings are epochs too -- v2 sent these
    #    into the date parser and got NaT for every row.
    as_num = pd.to_numeric(series, errors="coerce")
    if float(as_num.notna().mean()) > 0.99:
        return _from_epoch(as_num, epoch_unit)

    try:
        parsed = pd.to_datetime(series, errors="coerce", format="mixed")
    except (ValueError, TypeError):
        parsed = pd.to_datetime(series, errors="coerce", format="mixed", utc=True)
        return _assert_plausible(parsed, "mixed-offset string parse").astype(UTC_NS)

    if isinstance(parsed.dtype, pd.DatetimeTZDtype):
        return _assert_plausible(parsed, "string parse") \
            .dt.tz_convert("UTC").astype(UTC_NS)
    if not is_datetime64_any_dtype(parsed):
        parsed = pd.to_datetime(series, errors="coerce", format="mixed", utc=True)
        return _assert_plausible(parsed, "string parse").astype(UTC_NS)
    parsed = _assert_plausible(parsed, "string parse")
    return _localize(parsed, input_timezone, "Input timestamps") \
        .dt.tz_convert("UTC").astype(UTC_NS)


def numeric_cast(df: pd.DataFrame) -> pd.DataFrame:
    for c in PRICE_COLUMNS + ["volume"] + OPTIONAL_NUMERIC:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def conflicting_timestamps(df: pd.DataFrame, value_columns: list[str]) -> pd.Index:
    dup = df[df.duplicated("timestamp", keep=False)]
    if dup.empty:
        return pd.Index([], dtype=UTC_NS)
    key = dup[value_columns].astype(object).where(dup[value_columns].notna(), "<NA>")
    # `.values` strips timezone information from a tz-aware Series.
    key = key.assign(timestamp=dup["timestamp"].array)
    counts = key.drop_duplicates().groupby("timestamp", sort=False).size()
    return pd.Index(counts[counts > 1].index, dtype=UTC_NS)


def _parse_source_precedence(value: str | None) -> list[str]:
    if not value:
        return []
    out = [x.strip().lower() for x in value.split(",") if x.strip()]
    if len(out) != len(set(out)):
        raise DataValidationError(
            f"--source-precedence contains duplicates: {value!r}.")
    return out


def resolve_duplicate_rows(
        df: pd.DataFrame, reports: Path, policy: str,
        strategy_vwap_source: str, source_precedence: str | None,
        confirm_file_order: bool, source_column: str | None) -> tuple[pd.DataFrame, dict]:
    """Classify duplicate conflicts and resolve only under explicit semantics."""
    duplicate_rows_removed = int(df.duplicated("timestamp", keep="first").sum())
    dup = df[df.duplicated("timestamp", keep=False)].copy()
    empty = pd.Index([], dtype=UTC_NS)
    ohlcv_path = reports / "conflicting_ohlcv.csv"
    optional_path = reports / "conflicting_optional_metadata.csv"
    if dup.empty:
        df.iloc[0:0].to_csv(ohlcv_path, index=False)
        optional_report = df.iloc[0:0].copy()
        for col in OPTIONAL_NUMERIC:
            optional_report[f"conflict_{col}"] = pd.Series(dtype=bool)
        optional_report.to_csv(optional_path, index=False)
        return df, {
            "duplicate_rows_removed": 0,
            "conflicting_duplicate_timestamps": 0,
            "conflicting_ohlcv_timestamps": 0,
            "conflicting_optional_metadata_timestamps": 0,
            "vendor_bar_vwap_conflict_timestamps": 0,
            "transactions_conflict_timestamps": 0,
            "resolution": "none",
            "source_precedence": [],
        }

    ohlcv_cols = [*PRICE_COLUMNS, "volume"]
    ohlcv_conflicts = conflicting_timestamps(df, ohlcv_cols)
    optional_conflicts = {
        c: conflicting_timestamps(df, [c]) if c in df.columns else empty
        for c in OPTIONAL_NUMERIC
    }
    optional_union = empty
    for idx in optional_conflicts.values():
        optional_union = optional_union.union(idx)
    all_conflicts = ohlcv_conflicts.union(optional_union)

    df[df["timestamp"].isin(ohlcv_conflicts)].to_csv(
        ohlcv_path, index=False)
    optional_report = df[df["timestamp"].isin(optional_union)].copy()
    for col, timestamps in optional_conflicts.items():
        optional_report[f"conflict_{col}"] = \
            optional_report["timestamp"].isin(timestamps)
    optional_report.to_csv(optional_path, index=False)

    precedence = _parse_source_precedence(source_precedence)
    if len(ohlcv_conflicts):
        if policy == "error":
            raise DataValidationError(
                f"{len(ohlcv_conflicts)} duplicate timestamps conflict on OHLCV; "
                f"headline runs must fail. See {reports / 'conflicting_ohlcv.csv'}.")
        if policy == "source_precedence" and not precedence:
            raise DataValidationError(
                "--duplicate-policy source_precedence requires "
                "--source-precedence.")
        if policy == "last" and not confirm_file_order:
            raise DataValidationError(
                "--duplicate-policy last uses arbitrary file order and requires "
                "--confirm-file-order-precedence. Never use it for headline runs.")

    vendor_conflicts = optional_conflicts["vendor_bar_vwap"]
    transaction_conflicts = optional_conflicts["transactions"]
    if len(vendor_conflicts) and strategy_vwap_source == "vendor_bar_vwap":
        raise DataValidationError(
            f"{len(vendor_conflicts)} duplicate timestamps conflict on "
            "vendor_bar_vwap, which is the selected strategy VWAP source; see "
            f"{reports / 'conflicting_optional_metadata.csv'}.")
    if len(transaction_conflicts) and not precedence:
        raise DataValidationError(
            f"{len(transaction_conflicts)} duplicate timestamps conflict on "
            "transactions. Supply an explicit --source-precedence; file order "
            "is not a quality rule.")

    needs_precedence = (
        (len(ohlcv_conflicts) and policy == "source_precedence")
        or len(transaction_conflicts)
    )
    work = df.copy()
    if needs_precedence:
        if source_column is None or source_column not in work.columns:
            raise DataValidationError(
                "Source precedence was requested but the input has no source/"
                "vendor/provider column.")
        rank = {name: i for i, name in enumerate(precedence)}
        affected_sources = (
            work.loc[work["timestamp"].isin(all_conflicts), source_column]
            .astype(str).str.strip().str.lower())
        unknown = sorted(set(affected_sources) - set(rank))
        if unknown:
            raise DataValidationError(
                "Conflicting duplicates contain sources absent from "
                f"--source-precedence: {unknown}.")
        work["_source_rank"] = (
            work[source_column].astype(str).str.strip().str.lower()
            .map(rank).fillna(len(rank)).astype("int64"))
        work = work.sort_values(
            ["timestamp", "_source_rank"], kind="mergesort")
        work = work.drop_duplicates("timestamp", keep="first") \
            .drop(columns="_source_rank")
        resolution = "source_precedence"
    elif policy == "last" and len(ohlcv_conflicts):
        work = work.drop_duplicates("timestamp", keep="last")
        resolution = "confirmed_file_order_last"
    else:
        # Optional vendor metadata that is not used by the strategy must still
        # not acquire an arbitrary value from file order.
        if len(vendor_conflicts):
            work.loc[work["timestamp"].isin(vendor_conflicts),
                     "vendor_bar_vwap"] = np.nan
        work = work.drop_duplicates("timestamp", keep="first")
        resolution = ("unused_vendor_vwap_conflicts_nulled"
                      if len(vendor_conflicts) else "identical_rows_collapsed")

    return work.reset_index(drop=True), {
        "duplicate_rows_removed": duplicate_rows_removed,
        "conflicting_duplicate_timestamps": int(len(all_conflicts)),
        "conflicting_ohlcv_timestamps": int(len(ohlcv_conflicts)),
        "conflicting_optional_metadata_timestamps": int(len(optional_union)),
        "vendor_bar_vwap_conflict_timestamps": int(len(vendor_conflicts)),
        "transactions_conflict_timestamps": int(len(transaction_conflicts)),
        "resolution": resolution,
        "source_precedence": precedence,
    }


# --------------------------------------------------------------------------- #
# calendar / sessions
# --------------------------------------------------------------------------- #

def build_schedule(calendar_name: str, first_date, last_date) -> pd.DataFrame:
    cal = xcals.get_calendar(calendar_name)
    sessions = cal.sessions_in_range(pd.Timestamp(first_date), pd.Timestamp(last_date))
    opens = pd.to_datetime(pd.Series(cal.opens.reindex(sessions).values), utc=True)
    closes = pd.to_datetime(pd.Series(cal.closes.reindex(sessions).values), utc=True)
    sch = pd.DataFrame({
        "session_date": pd.to_datetime(sessions.date),
        "market_open": opens.astype(UTC_NS),
        "market_close": closes.astype(UTC_NS),
    })
    sch["calendar_bars"] = (
        (sch["market_close"] - sch["market_open"]) / pd.Timedelta(minutes=1)
    ).round().astype("int64")
    return sch


def load_halts(args: argparse.Namespace) -> tuple[dict[str, list[tuple[str, str]]], dict]:
    halts: dict[str, list[tuple[str, str]]] = {}
    if not args.no_builtin_halts:
        halts = {k: list(v) for k, v in KNOWN_HALTS.items()}
    meta = {"builtin_enabled": not args.no_builtin_halts,
            "builtin_sessions": 0 if args.no_builtin_halts else len(KNOWN_HALTS),
            "halt_file": None, "halt_file_sha256": None, "extra_sessions": 0}
    if args.halts is not None:
        extra = read_table(args.halts)
        extra.columns = [str(c).strip().lower() for c in extra.columns]
        need = {"session_date", "start_local", "end_local"}
        if not need.issubset(extra.columns):
            raise DataValidationError(f"--halts must contain {sorted(need)}")
        for _, row in extra.iterrows():
            key = str(pd.Timestamp(row["session_date"]).date())
            halts.setdefault(key, []).append(
                (str(row["start_local"]).strip(), str(row["end_local"]).strip()))
        meta.update(halt_file=str(args.halts),
                    halt_file_sha256=sha256_file(args.halts),
                    extra_sessions=int(extra["session_date"].nunique()))
    meta["total_sessions_with_halts"] = len(halts)
    return halts, meta


def halt_minutes_frame(schedule: pd.DataFrame, halts: dict, tz: str,
                       bar_label: str) -> pd.DataFrame:
    """Explode halt windows into (session_date, minute_of_session).

    Windows are unioned per session before counting -- v2 summed overlapping
    windows and double-counted the minutes."""
    open_local = (schedule.set_index("session_date")["market_open"]
                  .dt.tz_convert(tz))
    bars = schedule.set_index("session_date")["calendar_bars"]
    offset = 1 if bar_label == "start" else 0
    rows = []
    for day, windows in halts.items():
        ts = pd.Timestamp(day)
        if ts not in open_local.index:
            continue
        mo = open_local.loc[ts]
        minutes: set[int] = set()
        for start, end in windows:
            s = pd.Timestamp(f"{day} {start}", tz=tz)
            e = pd.Timestamp(f"{day} {end}", tz=tz)
            if e < s:
                raise DataValidationError(f"Halt window end before start on {day}.")
            m0 = int((s - mo) / pd.Timedelta(minutes=1)) + offset
            m1 = int((e - mo) / pd.Timedelta(minutes=1)) + offset
            minutes.update(range(m0, m1 + 1))
        limit = int(bars.loc[ts])
        minutes = {m for m in minutes if 1 <= m <= limit}
        rows.extend((ts, m) for m in sorted(minutes))
    return pd.DataFrame(rows, columns=["session_date", "minute_of_session"]) \
        if rows else pd.DataFrame({"session_date": pd.Series(dtype="datetime64[ns]"),
                                   "minute_of_session": pd.Series(dtype="int64")})


def scheduled_minutes_frame(schedule: pd.DataFrame) -> pd.DataFrame:
    """One row per (session, scheduled minute). ~1.8M rows for 18y of SPY."""
    n = schedule["calendar_bars"].to_numpy()
    sess = np.repeat(schedule["session_date"].to_numpy(), n)
    minute = np.concatenate([np.arange(1, k + 1, dtype="int64") for k in n])
    return pd.DataFrame({"session_date": sess, "minute_of_session": minute})


def attach_sessions(df: pd.DataFrame, schedule: pd.DataFrame,
                    bar_label: str) -> pd.DataFrame:
    att = pd.merge_asof(df.sort_values("timestamp"),
                        schedule.sort_values("market_open"),
                        left_on="timestamp", right_on="market_open",
                        direction="backward", allow_exact_matches=True)
    if bar_label == "start":
        inside = att["timestamp"].ge(att["market_open"]) & \
                 att["timestamp"].lt(att["market_close"])
        offset = 1
    else:
        inside = att["timestamp"].gt(att["market_open"]) & \
                 att["timestamp"].le(att["market_close"])
        offset = 0
    att["minute_of_session"] = (
        (att["timestamp"] - att["market_open"]) / pd.Timedelta(minutes=1)
    ).round().astype("Int64") + offset
    att["is_rth"] = inside.fillna(False)
    return att


def ohlc_invalid_mask(df: pd.DataFrame) -> pd.Series:
    px = df[PRICE_COLUMNS]
    ok = (np.isfinite(px).all(axis=1)
          & px.gt(0).all(axis=1)
          & df["high"].ge(df[["open", "close", "low"]].max(axis=1))
          & df["low"].le(df[["open", "close", "high"]].min(axis=1))
          & np.isfinite(df["volume"]) & df["volume"].ge(0))
    return ~ok


# --------------------------------------------------------------------------- #
# session grading by minute set
# --------------------------------------------------------------------------- #

def session_diagnostics(bars: pd.DataFrame, schedule: pd.DataFrame,
                        halt_min: pd.DataFrame, invalid_per_session: pd.Series,
                        bar_label: str, halt_bar_policy: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Grade sessions by comparing minute *sets*, not bar counts.

    v2 compared counts, so 390 observed bars against 376 expected produced
    missing=clip(376-390,0)=0 and graded the session pristine -- a vendor that
    padded the halt window while dropping 14 real minutes elsewhere passed."""
    sched = scheduled_minutes_frame(schedule)
    if len(halt_min):
        halt_min = halt_min.assign(is_halt=True)
        sched = sched.merge(halt_min, on=["session_date", "minute_of_session"],
                            how="left")
        sched["is_halt"] = sched["is_halt"].fillna(False).astype(bool)
    else:
        sched["is_halt"] = False

    obs = bars[["session_date", "minute_of_session"]].copy()
    obs["minute_of_session"] = obs["minute_of_session"].astype("int64")
    obs = obs.drop_duplicates()
    obs["observed"] = True

    grid = sched.merge(obs, on=["session_date", "minute_of_session"], how="outer",
                       indicator=True)
    grid["observed"] = grid["observed"].fillna(False).astype(bool)
    grid["is_halt"] = grid["is_halt"].fillna(False).astype(bool)
    grid["is_scheduled"] = grid["_merge"].ne("right_only").to_numpy()
    grid["is_required"] = grid["is_scheduled"] & ~grid["is_halt"]

    flags = pd.DataFrame({
        "session_date": grid["session_date"],
        "scheduled_minutes": grid["is_scheduled"],
        "halt_minutes": grid["is_scheduled"] & grid["is_halt"],
        "required_minutes": grid["is_required"],
        "observed_minutes": grid["observed"],
        "missing_required": grid["is_required"] & ~grid["observed"],
        "present_during_halt": grid["is_halt"] & grid["observed"],
        "unexpected_minutes": ~grid["is_scheduled"] & grid["observed"],
    })
    agg = flags.groupby("session_date", observed=True).sum().astype("int64")

    seen = grid.loc[grid["observed"]]
    first_obs = seen.groupby("session_date", observed=True)["minute_of_session"].min()
    last_obs = seen.groupby("session_date", observed=True)["minute_of_session"].max()

    missing = grid.loc[grid["is_required"] & ~grid["observed"],
                       ["session_date", "minute_of_session"]].copy()
    if len(missing):
        missing["f"] = missing["session_date"].map(first_obs)
        missing["l"] = missing["session_date"].map(last_obs)
        missing["pos"] = np.where(
            missing["f"].isna() | (missing["minute_of_session"] < missing["f"]),
            "leading",
            np.where(missing["minute_of_session"] > missing["l"].fillna(-1),
                     "trailing", "interior"))
        pos_counts = (missing.groupby(["session_date", "pos"], observed=True)
                      .size().unstack(fill_value=0))
    else:
        pos_counts = pd.DataFrame(index=pd.DatetimeIndex([], name="session_date"))
    pos_counts = pos_counts.reindex(columns=["leading", "interior", "trailing"],
                                    fill_value=0)

    d = schedule.set_index("session_date").join(agg, how="left")
    for c in ["scheduled_minutes", "halt_minutes", "required_minutes",
              "observed_minutes", "missing_required", "present_during_halt",
              "unexpected_minutes"]:
        d[c] = d[c].fillna(0).astype("int64")
    d = d.join(pos_counts.rename(columns={"leading": "leading_missing",
                                          "interior": "interior_missing",
                                          "trailing": "trailing_missing"}))
    for c in ["leading_missing", "interior_missing", "trailing_missing"]:
        d[c] = d[c].fillna(0).astype("int64")
    d["invalid_ohlc_rows"] = (invalid_per_session.reindex(d.index).fillna(0)
                              .astype("int64"))
    d["total_volume"] = (bars.groupby("session_date", observed=True)["volume"].sum()
                         .reindex(d.index).fillna(0.0))

    # A leading- or trailing-truncated session can still contribute *some*
    # components. 2009-07-27 opens at 11:15: its move_open anchor is worthless
    # but its close is a perfectly good previous-close for the next session.
    d["open_valid"] = d["leading_missing"].eq(0) & d["observed_minutes"].gt(0)
    d["close_valid"] = d["trailing_missing"].eq(0) & d["observed_minutes"].gt(0)

    halt_ok = (d["present_during_halt"].eq(0) if halt_bar_policy == "absent"
               else pd.Series(True, index=d.index))

    def grade(r) -> str:
        if r["observed_minutes"] == 0:
            return "absent"
        if r["unexpected_minutes"] > 0:
            return "halt_anomaly"
        if r["halt_minutes"] > 0 and r["present_during_halt"] > 0 \
                and halt_bar_policy == "absent":
            return "halt_anomaly"
        if r["missing_required"] == 0 and r["invalid_ohlc_rows"] == 0:
            return "halt_adjusted" if r["halt_minutes"] > 0 else "complete"
        if r["observed_minutes"] < 0.5 * r["required_minutes"]:
            return "sparse"
        if r["leading_missing"] + r["trailing_missing"] >= r["interior_missing"]:
            return "truncated"
        return "interior_gap"

    d["quality"] = d.apply(grade, axis=1)

    # --- tiers -----------------------------------------------------------
    # Every scheduled minute present, nothing dropped: the only set that
    # supports a paper-faithful replication. `move_open` anchors on the first
    # bar of the session and the band uses a per-minute 14-day rolling mean, so
    # a truncated open corrupts both the day itself and the next `sigma_window`
    # sessions at those minutes.
    d["is_paper_ready"] = d["quality"].eq("complete") & d["invalid_ohlc_rows"].eq(0)
    # Sessions that traded fully except for an officially halted window. VWAP and
    # the band are correct here; only PnL accrual across the halt needs care.
    d["is_halt_usable"] = (d["quality"].isin(["complete", "halt_adjusted"])
                           & halt_ok & d["invalid_ohlc_rows"].eq(0))
    # Exploration only. Never the source of a headline number.
    d["is_exploratory"] = ~d["quality"].isin(["absent", "sparse", "halt_anomaly"])
    return d.reset_index(), grid


# --------------------------------------------------------------------------- #
# component-level feature validity
# --------------------------------------------------------------------------- #

def build_feature_validity(bars: pd.DataFrame, diagnostics: pd.DataFrame,
                           halt_min: pd.DataFrame, schedule: pd.DataFrame,
                           trade_freq: int, sigma_window: int
                           ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-component validity on the FULL exchange calendar.

    Session tiers are too coarse to be a feature gate. Filtering the bar file
    first and computing features afterwards silently redefines the strategy:
    `close.shift(1)` then reaches across the removed session (2009-07-28 would
    take its previous close from 07-24 instead of 07-27), and a "previous 14
    sessions" window becomes "previous 14 *retained* sessions", stretching over
    more calendar days.

    The engine must therefore compute features on the full calendar and apply a
    trading mask last. These frames are that mask.

    Components have different dependency structures, which is why a single flag
    will not do:
      - move_open at minute m depends on the session open and the bar at m.
        An interior gap elsewhere does NOT invalidate it.
      - cumulative VWAP at minute m depends on every required minute from the
        open through m. An interior gap invalidates everything after it.
      - previous-close depends only on the prior session's close.

    `sigma_history_valid` / `daily_vol_history_valid` are derived at the
    trade_freq / sigma_window passed on the command line. They are a
    convenience: if the backtest sweeps those parameters it must recompute them
    from the primitives, which are parameter-free.
    """
    d = diagnostics.set_index("session_date")

    # ---- session level ---------------------------------------------------
    sess = pd.DataFrame(index=d.index)
    sess["calendar_bars"] = d["calendar_bars"]
    sess["quality"] = d["quality"]
    sess["open_valid"] = d["open_valid"].to_numpy()
    sess["close_valid"] = d["close_valid"].to_numpy()
    sess["session_close"] = (bars.groupby("session_date", observed=True)["close"]
                             .last().reindex(d.index))
    sess["session_open"] = (bars.groupby("session_date", observed=True)["open"]
                            .first().reindex(d.index))
    sess["prev_close_valid"] = sess["close_valid"].shift(1).fillna(False).astype(bool)
    # a close-to-close return needs both ends
    sess["daily_ret_valid"] = sess["close_valid"] & sess["prev_close_valid"]
    sess["daily_vol_history_valid"] = (
        sess["daily_ret_valid"].shift(1).rolling(sigma_window, min_periods=1)
        .sum().fillna(0).ge(sigma_window))
    for c in ["open_valid", "close_valid", "prev_close_valid", "daily_ret_valid",
              "daily_vol_history_valid"]:
        sess[c] = sess[c].fillna(False).astype(bool)

    # ---- minute level ----------------------------------------------------
    grid = scheduled_minutes_frame(schedule)
    if len(halt_min):
        grid = grid.merge(halt_min.assign(is_halt_minute=True),
                          on=["session_date", "minute_of_session"], how="left")
        grid["is_halt_minute"] = grid["is_halt_minute"].fillna(False).astype(bool)
    else:
        grid["is_halt_minute"] = False

    obs = bars[["session_date", "minute_of_session"]].drop_duplicates()
    obs["bar_present"] = True
    grid = grid.merge(obs, on=["session_date", "minute_of_session"], how="left")
    grid["bar_present"] = grid["bar_present"].fillna(False).astype(bool)

    grid["is_required"] = ~grid["is_halt_minute"]
    grid["is_executable_minute"] = grid["bar_present"] & ~grid["is_halt_minute"]
    grid["is_scheduled_decision_minute"] = grid["minute_of_session"].mod(trade_freq).eq(0)

    grid = grid.sort_values(["session_date", "minute_of_session"])
    grid["open_valid"] = grid["session_date"].map(sess["open_valid"]).to_numpy()

    # move_open uses the session open and this executable observation only.
    # Under allow_present a vendor may retain a phantom bar during a declared
    # halt; it is neither a price observation nor sigma_open history.
    grid["move_open_obs_valid"] = (
        grid["open_valid"] & grid["bar_present"] & ~grid["is_halt_minute"])

    # cumulative VWAP: every required minute from the open through m must exist.
    # Halt minutes are exempt -- no volume traded, so nothing is missing.
    miss = (grid["is_required"] & ~grid["bar_present"]).astype("int64")
    cum_miss = miss.groupby(grid["session_date"], sort=False).cumsum()
    grid["vwap_valid"] = grid["open_valid"] & grid["bar_present"] & cum_miss.eq(0).to_numpy()

    # per-minute band history, over sessions where the exchange scheduled this
    # minute AND the move_open observation is actually usable
    parts = []
    for m, g in grid.groupby("minute_of_session", sort=True):
        g = g.sort_values("session_date")
        prior = (g["move_open_obs_valid"].astype("int64").shift(1)
                 .rolling(sigma_window, min_periods=1).sum().fillna(0))
        parts.append(pd.Series(prior.to_numpy() >= sigma_window, index=g.index))
    grid["sigma_history_valid"] = pd.concat(parts).reindex(grid.index).fillna(False)

    grid["daily_vol_history_valid"] = grid["session_date"].map(
        sess["daily_vol_history_valid"]).to_numpy()

    # a decision is only usable if the band, the VWAP filter and the sizing
    # inputs are all valid at that minute
    grid["signal_valid_default_config"] = (grid["is_scheduled_decision_minute"]
                            & grid["move_open_obs_valid"] & grid["vwap_valid"]
                            & grid["sigma_history_valid"]
                            & grid["daily_vol_history_valid"]
                            & grid["session_date"].map(sess["prev_close_valid"]).to_numpy())

    cols = ["session_date", "minute_of_session", "is_halt_minute", "bar_present",
            "is_executable_minute", "is_scheduled_decision_minute", "open_valid",
            "move_open_obs_valid", "vwap_valid", "sigma_history_valid",
            "daily_vol_history_valid", "signal_valid_default_config"]
    return sess.reset_index(), grid[cols].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# strategy-relevant minute coverage
# --------------------------------------------------------------------------- #

def minute_coverage(validity_minute: pd.DataFrame, trade_freq: int,
                    window: int) -> pd.DataFrame:
    """Per (session, trade bucket): usable move_open observation, and how many
    of the previous `window` *eligible* sessions also had one.

    Two corrections over v3. Eligibility respects early closes -- a 13:00
    session never had a 15:30 bar, so scoring it as absent depressed the
    trailing count for every afternoon bucket over the next `window` sessions
    (39 early closes in the SPY sample). And the count is now over *usable*
    observations, not bar presence: a session opening at 11:15 has a 15:59 bar,
    but its move_open anchors on the 11:15 price, so the observation is not
    usable and must not enter the band history."""
    v = validity_minute.loc[validity_minute["minute_of_session"].mod(trade_freq).eq(0),
                            ["session_date", "minute_of_session",
                             "move_open_obs_valid", "bar_present",
                             "sigma_history_valid"]].copy()
    out = v.rename(columns={"move_open_obs_valid": "usable_observation"})
    prior = []
    for _, g in out.groupby("minute_of_session", sort=True):
        g = g.sort_values("session_date")
        prior.append(pd.Series(
            g["usable_observation"].astype("int64").shift(1)
            .rolling(window, min_periods=1).sum().fillna(0).to_numpy().astype("int64"),
            index=g.index))
    out[f"prior_{window}_eligible_usable"] = pd.concat(prior).reindex(out.index)
    out["band_warm"] = out["sigma_history_valid"]
    return out.sort_values(["session_date", "minute_of_session"]).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# profiling
# --------------------------------------------------------------------------- #

def decimal_places(s: pd.Series, cap: int = 8) -> pd.Series:
    txt = s.round(cap).map(lambda x: f"{x:.{cap}f}".rstrip("0"))
    return txt.str.split(".").str[1].fillna("").str.len()


def assign_regime(df: pd.DataFrame, explicit: str | None,
                  source_column: str | None) -> tuple[pd.Series, dict]:
    """Label rows by data regime.

    Priority: a real source column > explicit --source-split > heuristic.
    The heuristic is a *hint about formatting*, not vendor identification:
    float storage does not preserve the vendor's decimal formatting, a vendor
    can switch mid-year, and a precision change need not be a vendor change."""
    if source_column is not None and source_column in df.columns:
        codes, uniques = pd.factorize(df[source_column].astype(str))
        return pd.Series(codes, index=df.index), {
            "method": "source_column", "column": source_column,
            "levels": list(map(str, uniques))}
    if explicit:
        cuts = sorted(pd.Timestamp(x) for x in explicit.split(","))
        labels = pd.Series(0, index=df.index, dtype="int64")
        for i, cut in enumerate(cuts, start=1):
            labels[df["session_date"] >= cut] = i
        return labels, {"method": "explicit_source_split",
                        "cuts": [str(c.date()) for c in cuts]}

    sub_penny = decimal_places(df["close"]).gt(2)
    by_year = sub_penny.groupby(df["session_date"].dt.year).mean()
    jumps = by_year.diff().abs()
    if by_year.empty or pd.isna(jumps.max()) or float(jumps.max()) < 0.05:
        return (pd.Series(0, index=df.index, dtype="int64"),
                {"method": "candidate_format_regime", "detected": False,
                 "note": "no year-over-year step in decimal granularity"})
    cut_year = int(jumps.idxmax())
    return ((df["session_date"].dt.year >= cut_year).astype("int64"),
            {"method": "candidate_format_regime", "detected": True,
             "cut_year": cut_year,
             "max_yoy_step_in_sub_penny_share": round(float(jumps.max()), 4),
             "warning": "heuristic hint only; not vendor identification. "
                        "Prefer a source column or --source-split."})


def profile_regimes(df: pd.DataFrame, extreme: float) -> list[dict]:
    out = []
    for seg, g in df.groupby("regime", observed=True):
        prev_minute = g["minute_of_session"].groupby(
            g["session_date"], observed=True).shift(1)
        prev_executable = g["is_executable_minute"].groupby(
            g["session_date"], observed=True).shift(1).fillna(False)
        continuous = (
            g["is_executable_minute"] & prev_executable
            & g["minute_of_session"].sub(prev_minute).eq(1)
        )
        ret = g["close"].groupby(
            g["session_date"], observed=True).pct_change().where(continuous)
        stale = (g["open"].eq(g["high"]) & g["high"].eq(g["low"])
                 & g["low"].eq(g["close"]))
        out.append({
            "regime": int(seg),
            "first_session": str(g["session_date"].min().date()),
            "last_session": str(g["session_date"].max().date()),
            "bars": int(len(g)), "sessions": int(g["session_date"].nunique()),
            "sub_penny_close_pct": round(float(decimal_places(g["close"]).gt(2).mean() * 100), 3),
            "sub_penny_open_pct": round(float(decimal_places(g["open"]).gt(2).mean() * 100), 3),
            "zero_volume_bar_pct": round(float(g["volume"].eq(0).mean() * 100), 4),
            "stale_bar_pct": round(float(stale.mean() * 100), 4),
            f"abs_1min_ret_gt_{extreme:.3%}": int((ret.abs() > extreme).sum()),
            "max_abs_1min_ret_pct": round(float(ret.abs().max() * 100), 3),
            "median_session_volume": float(
                g.groupby("session_date", observed=True)["volume"].sum().median()),
        })
    return out


def classified_intraday_returns(
        df: pd.DataFrame, halt_min: pd.DataFrame) -> pd.DataFrame:
    """Classify returns by elapsed executable minutes, never by row adjacency."""
    work = df.sort_values(
        ["session_date", "minute_of_session"], kind="mergesort").copy()
    executable = work["is_executable_minute"].astype(bool)
    group = work["session_date"]

    exec_close = work["close"].where(executable)
    exec_minute = work["minute_of_session"].where(executable)
    prev_close = exec_close.groupby(group, observed=True).ffill() \
        .groupby(group, observed=True).shift(1)
    prev_minute = exec_minute.groupby(group, observed=True).ffill() \
        .groupby(group, observed=True).shift(1)
    elapsed = work["minute_of_session"].sub(prev_minute)
    returns = work["close"].div(prev_close).sub(1).where(executable)

    reopen_keys = pd.MultiIndex.from_arrays(
        [pd.DatetimeIndex([], dtype="datetime64[ns]"),
         pd.Index([], dtype="int64")],
        names=["session_date", "minute_of_session"])
    if len(halt_min):
        hm = halt_min.sort_values(
            ["session_date", "minute_of_session"]).copy()
        brk = (
            hm["session_date"].ne(hm["session_date"].shift())
            | hm["minute_of_session"].ne(hm["minute_of_session"].shift() + 1)
        )
        ends = (hm.assign(_block=brk.cumsum())
                .groupby("_block", observed=True)
                .agg(session_date=("session_date", "first"),
                     minute_of_session=("minute_of_session", "max")))
        ends["minute_of_session"] += 1
        reopen_keys = pd.MultiIndex.from_frame(
            ends[["session_date", "minute_of_session"]])
    row_keys = pd.MultiIndex.from_arrays(
        [work["session_date"], work["minute_of_session"]])
    halt_reopen = pd.Series(
        row_keys.isin(reopen_keys), index=work.index) & executable & prev_close.notna()
    continuous = executable & prev_close.notna() & elapsed.eq(1) & ~halt_reopen
    gap = executable & prev_close.notna() & ~continuous & ~halt_reopen

    work["prior_executable_minute"] = prev_minute
    work["minutes_since_prior_executable"] = elapsed
    work["continuous_1min_return"] = returns.where(continuous)
    work["gap_return"] = returns.where(gap)
    work["halt_reopen_return"] = returns.where(halt_reopen)
    return work


def anomaly_details(df: pd.DataFrame, extreme: float, out_dir: Path,
                    halt_min: pd.DataFrame) -> dict:
    """A single bad print can manufacture a band breakout and a large PnL, so
    emit the rows, not just a count."""
    written = {}
    returns = classified_intraday_returns(df, halt_min)
    context = [
        "timestamp_local", "session_date", "minute_of_session",
        "prior_executable_minute", "minutes_since_prior_executable", "regime",
        "open", "high", "low", "close", "volume",
    ]

    continuous = returns.loc[
        returns["continuous_1min_return"].abs() > extreme]
    if len(continuous):
        continuous[[*context, "continuous_1min_return"]] \
            .sort_values("continuous_1min_return", key=abs, ascending=False) \
            .to_csv(out_dir / "extreme_continuous_1min_returns.csv", index=False)
    written["extreme_continuous_1min_return_rows"] = int(len(continuous))

    gaps = returns.loc[returns["gap_return"].notna()]
    if len(gaps):
        gaps[[*context, "gap_return"]] \
            .sort_values("gap_return", key=abs, ascending=False) \
            .to_csv(out_dir / "gap_returns.csv", index=False)
    written["gap_return_rows"] = int(len(gaps))
    written["extreme_gap_return_rows"] = int(
        (gaps["gap_return"].abs() > extreme).sum())

    reopens = returns.loc[returns["halt_reopen_return"].notna()]
    if len(reopens):
        reopens[[*context, "halt_reopen_return"]] \
            .sort_values("halt_reopen_return", key=abs, ascending=False) \
            .to_csv(out_dir / "halt_reopen_returns.csv", index=False)
    written["halt_reopen_return_rows"] = int(len(reopens))
    written["extreme_halt_reopen_return_rows"] = int(
        (reopens["halt_reopen_return"].abs() > extreme).sum())

    def runs(mask: pd.Series, name: str) -> None:
        if not mask.any():
            return
        # A run must break on a session change or a minute discontinuity,
        # otherwise the last minute of one day and the first of the next are
        # reported as a single stretch.
        brk = (mask != mask.shift()) \
            | (df["session_date"] != df["session_date"].shift()) \
            | (df["minute_of_session"] != df["minute_of_session"].shift() + 1)
        grp = brk.cumsum()[mask]
        r = (df.loc[mask].assign(_g=grp)
             .groupby("_g", observed=True)
             .agg(session_date=("session_date", "first"),
                  start_minute=("minute_of_session", "min"),
                  end_minute=("minute_of_session", "max"),
                  length=("minute_of_session", "size"))
             .sort_values("length", ascending=False))
        r.head(500).to_csv(out_dir / f"longest_{name}_runs.csv", index=False)
        written[f"{name}_runs"] = int(len(r))
        written[f"{name}_longest_run"] = int(r["length"].max())

    runs(df["volume"].eq(0), "zero_volume")
    runs(df["open"].eq(df["high"]) & df["high"].eq(df["low"])
         & df["low"].eq(df["close"]), "stale_bar")
    return written


def regime_seams(df: pd.DataFrame) -> list[dict]:
    daily = (df.groupby("session_date", observed=True)
             .agg(open=("open", "first"), close=("close", "last"),
                  regime=("regime", "last")).sort_index())
    out = []
    for pos in np.where(daily["regime"].diff().fillna(0).to_numpy() != 0)[0]:
        if pos == 0:
            continue
        pc, no = float(daily["close"].iloc[pos - 1]), float(daily["open"].iloc[pos])
        out.append({"seam_session": str(pd.Timestamp(daily.index[pos]).date()),
                    "prev_session": str(pd.Timestamp(daily.index[pos - 1]).date()),
                    "prev_close": pc, "next_open": no,
                    "gap_pct": round((no / pc - 1) * 100, 4)})
    return out


def raw_price_evidence(df: pd.DataFrame, dividends: pd.DataFrame | None) -> dict | None:
    """Residual test for unadjusted prices.

    A sign comparison (`mean gap on ex-dates < mean gap overall`) is confounded
    by market direction. Instead: if adding the dividend back to the ex-date
    open removes the anomaly, the series is unadjusted."""
    if dividends is None or dividends.empty:
        return None
    daily = (df.groupby("session_date", observed=True)
             .agg(open=("open", "first"), close=("close", "last")).sort_index())
    prev_close = daily["close"].shift()
    raw_gap = daily["open"] / prev_close - 1
    div = (dividends.set_index("ex_date")["cash_amount"]
           .reindex(daily.index).fillna(0.0))
    div_added_gap = (daily["open"] + div) / prev_close - 1

    ex = div.gt(0) & prev_close.notna()
    n = int(ex.sum())
    if n == 0:
        return {"ex_dates_matched": 0, "evidence_supports_raw_prices": None,
                "note": "no ex-date overlaps a trading session"}
    baseline = float(raw_gap[~ex].mean())
    raw_ex = float(raw_gap[ex].mean())
    adj_ex = float(div_added_gap[ex].mean())
    # unadjusted <=> the dividend-added gap is closer to the non-ex-date baseline
    supports_raw = abs(adj_ex - baseline) < abs(raw_ex - baseline)
    return {
        "ex_dates_matched": n,
        "mean_gap_non_ex_dates_pct": round(baseline * 100, 4),
        "mean_gap_on_ex_dates_pct": round(raw_ex * 100, 4),
        "mean_gap_on_ex_dates_with_dividend_added_pct": round(adj_ex * 100, 4),
        "residual_raw_pct": round((raw_ex - baseline) * 100, 4),
        "residual_dividend_added_pct": round((adj_ex - baseline) * 100, 4),
        "evidence_supports_raw_prices": bool(supports_raw),
        "note": "evidence, not proof; a single test cannot rule out partial "
                "adjustment or vendor-specific handling",
    }


# --------------------------------------------------------------------------- #
# dividends
# --------------------------------------------------------------------------- #

def clean_dividends(path: Path, out_dir: Path, symbol: str,
                    sessions: pd.DatetimeIndex, policy: str,
                    confirm_sum: bool = False) -> tuple[pd.DataFrame, dict]:
    raw = read_table(path)
    norm = resolve_columns(raw, DIVIDEND_ALIASES, required=("ex_date", "cash_amount"))
    norm["ex_date"] = pd.to_datetime(norm["ex_date"], errors="coerce").dt.normalize()
    norm["cash_amount"] = pd.to_numeric(norm["cash_amount"], errors="coerce")

    bad = (norm["ex_date"].isna() | ~np.isfinite(norm["cash_amount"])
           | norm["cash_amount"].lt(0))
    bad_count = int(bad.sum())
    norm = norm.loc[~bad].copy()

    # symbol filter -- v2 only recognised a column literally named `symbol`, so a
    # `ticker`/`sym` column was ignored and every symbol's cash was summed.
    symbol_info: dict[str, object] = {"symbol_column_present": "symbol" in norm.columns}
    if "symbol" in norm.columns:
        upper = norm["symbol"].astype(str).str.upper()
        distinct = sorted(upper.unique().tolist())
        symbol_info["distinct_symbols"] = distinct
        mask = upper.eq(symbol.upper())
        if not mask.any():
            raise DataValidationError(
                f"Dividend file contains symbols {distinct} but none match "
                f"--symbol {symbol}.")
        symbol_info["rows_other_symbols_dropped"] = int((~mask).sum())
        norm = norm.loc[mask].copy()

    exact_dupes = int(norm.duplicated().sum())
    norm = norm.drop_duplicates().copy()

    grouped = norm.groupby("ex_date", observed=True)["cash_amount"]
    multi = grouped.size()
    conflict_dates = multi[multi > 1].index
    conflicts = []
    if len(conflict_dates):
        for d0 in conflict_dates:
            amts = sorted(norm.loc[norm["ex_date"].eq(d0), "cash_amount"].tolist())
            conflicts.append({"ex_date": str(pd.Timestamp(d0).date()), "amounts": amts})
        if policy == "error":
            raise DataValidationError(
                f"{len(conflict_dates)} ex-dates carry more than one amount after "
                f"exact-duplicate removal: {conflicts[:5]}. Blind summation would "
                "double-count a re-scraped event. Rerun with "
                "--dividend-duplicate-policy sum (only if these are genuine "
                "regular+special pairs) or first.")

    if policy == "sum" and len(conflict_dates):
        # v3 promised in the help text that summation was only for genuine
        # regular+special pairs, then summed anything. Enforce that evidence
        # independently on every ex-date: a valid pair on one date must not
        # bless a duplicated scrape on another.
        if "dividend_type" in norm.columns:
            conflict_rows = norm.loc[norm["ex_date"].isin(conflict_dates)]
            for ex_date, group in conflict_rows.groupby("ex_date", observed=True):
                types = (group["dividend_type"].astype(str)
                         .str.lower().str.strip())
                valid_pair = (
                    len(types) == 2
                    and types.value_counts().to_dict()
                    == {"regular": 1, "special": 1}
                )
                if not valid_pair:
                    raise DataValidationError(
                        "--dividend-duplicate-policy sum requested but "
                        f"{pd.Timestamp(ex_date).date()} has dividend_type="
                        f"{sorted(types.tolist())}. Each conflicting ex-date "
                        "must contain exactly one regular and one special "
                        "dividend; otherwise it may be a duplicated scrape.")
        elif not confirm_sum:
            raise DataValidationError(
                "--dividend-duplicate-policy sum requires either a "
                "`dividend_type` column showing exactly one regular and one "
                "special row on every conflicting ex-date, or an explicit "
                "--confirm-dividend-sum.")
    cleaned = (grouped.sum() if policy == "sum" else grouped.first()) \
        .reset_index().sort_values("ex_date")
    cleaned.insert(0, "symbol", symbol.upper())

    off_session = int((~cleaned["ex_date"].isin(sessions)).sum())
    px = cleaned["cash_amount"]
    out_path = out_dir / "spy_dividends_clean.csv"
    cleaned.assign(ex_date=cleaned["ex_date"].dt.date).to_csv(out_path, index=False)
    manifest = {
        "source": str(path), "source_sha256": sha256_file(path),
        "input_rows": int(len(raw)), "bad_rows_removed": bad_count,
        "exact_duplicate_rows_removed": exact_dupes,
        "conflicting_ex_dates": conflicts,
        "duplicate_policy": policy, "sum_confirmed": bool(confirm_sum),
        "dividend_type_column_present": "dividend_type" in norm.columns,
        "output_rows": int(len(cleaned)),
        "ex_dates_not_on_a_trading_session": off_session,
        "first_ex_date": str(cleaned["ex_date"].min().date()) if len(cleaned) else None,
        "last_ex_date": str(cleaned["ex_date"].max().date()) if len(cleaned) else None,
        "amount_min": float(px.min()) if len(px) else None,
        "amount_max": float(px.max()) if len(px) else None,
        "output": out_path.name, "output_sha256": sha256_file(out_path),
        **symbol_info,
    }
    return cleaned, manifest


def render_audit_summary(manifest: dict, reports: Path) -> str:
    """Render release gates from actual run outputs, not source promises."""
    quality = pd.read_csv(reports / "session_quality.csv")
    non_paper = quality.loc[~quality["is_paper_ready"].astype(bool)].copy()
    known_quality = non_paper["quality"].notna() & \
        non_paper["quality"].isin(QUALITY_ORDER)
    halt = manifest["halt_validation"]
    counts = manifest["counts"]
    boundary = manifest["boundary_sessions_missing"]
    duplicate = manifest["duplicate_audit"]
    dividends = manifest.get("dividends")
    tests = manifest["data_self_tests"]
    git = manifest["git"]

    gates = [
        ("Unexplained OHLCV conflicts",
         duplicate["conflicting_ohlcv_timestamps"] == 0,
         str(duplicate["conflicting_ohlcv_timestamps"])),
        ("Off-grid rows", counts["off_grid_rows"] == 0,
         str(counts["off_grid_rows"])),
        ("Unapproved invalid-row deletion",
         counts["invalid_ohlc_rows_dropped"] == 0,
         str(counts["invalid_ohlc_rows_dropped"])),
        ("Expected boundaries complete",
         boundary["known"] and boundary["total"] == 0,
         f"known={boundary['known']}, missing={boundary['total']}"),
        ("Every non-paper-ready session classified",
         bool(known_quality.all()),
         f"{len(non_paper)} non-paper-ready sessions"),
        ("Halt minutes explained",
         halt["sessions_with_unexpected_minutes"] == 0
         and (halt["halt_bar_policy"] == "allow_present"
              or halt["sessions_with_bars_present_during_halt"] == 0),
         f"unexpected={halt['sessions_with_unexpected_minutes']}, "
         f"present_in_halt={halt['sessions_with_bars_present_during_halt']}"),
        ("Dividend file validated", dividends is not None,
         "present" if dividends is not None else "missing"),
        ("Data self-tests passed",
         tests["passed"] and tests["checks"] == DATA_SELF_TEST_COUNT,
         f"{tests['checks']}/{DATA_SELF_TEST_COUNT}"),
        ("Script matches clean Git HEAD",
         bool(git.get("available") and not git.get("dirty")
              and git.get("script_matches_head")),
         f"commit={git.get('commit')}, dirty={git.get('dirty')}, "
         f"matches_head={git.get('script_matches_head')}"),
    ]

    lines = [
        "# Data release audit summary",
        "",
        f"- Run ID: `{manifest.get('run_id', 'pending')}`",
        f"- Source SHA-256: `{manifest['source_sha256']}`",
        f"- Script SHA-256: `{manifest['script_sha256']}`",
        f"- Expected range: `{manifest['expected_start']}` to "
        f"`{manifest['expected_end']}`",
        f"- Observed range: `{manifest['observed_start']}` to "
        f"`{manifest['observed_end']}`",
        "",
        "## Acceptance gates",
        "",
        "| Gate | Status | Evidence |",
        "|---|---:|---|",
    ]
    for label, passed, evidence in gates:
        lines.append(
            f"| {label} | {'PASS' if passed else 'FAIL'} | {evidence} |")
    lines += [
        "| Deterministic rerun hashes | PENDING | compare a second run |",
        "",
        "## Non-paper-ready sessions",
        "",
    ]
    if non_paper.empty:
        lines.append("None.")
    else:
        cols = [
            "session_date", "quality", "leading_missing", "interior_missing",
            "trailing_missing", "halt_minutes", "present_during_halt",
            "invalid_ohlc_rows",
        ]
        lines += [
            "| " + " | ".join(cols) + " |",
            "|" + "|".join(["---"] * len(cols)) + "|",
        ]
        for row in non_paper[cols].itertuples(index=False, name=None):
            lines.append("| " + " | ".join(map(str, row)) + " |")
    lines += [
        "",
        "## Return anomaly classification",
        "",
    ]
    for key, value in manifest["anomaly_reports"].items():
        lines.append(f"- `{key}`: {value}")
    lines += [
        "",
        "This file is generated from this run's manifest and CSV reports. "
        "It is not an acceptance signature until every gate, including the "
        "deterministic rerun comparison, passes.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def run(args: argparse.Namespace) -> dict:
    t0 = time.time()
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    final_dir = args.output_dir / "runs" / run_id
    if final_dir.exists():
        run_id += f"_{os.getpid()}"
        final_dir = args.output_dir / "runs" / run_id
    args.output_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".tmp_{run_id}_", dir=args.output_dir))
    reports = staging / "reports"
    reports.mkdir(parents=True, exist_ok=True)

    try:
        manifest = _run_into(args, staging, reports, run_id)
    except BaseException as exc:
        # Failed data never enters runs/ and never updates latest.json, but audit
        # CSVs must survive; otherwise a fatal duplicate report points to a
        # staging path that is deleted before the caller can inspect it.
        try:
            report_files = sorted(p for p in reports.iterdir() if p.is_file())
            if report_files:
                failed_dir = args.output_dir / "failed_audits" / run_id
                if failed_dir.exists():
                    failed_dir = failed_dir.with_name(f"{run_id}_{os.getpid()}")
                failed_dir.mkdir(parents=True)
                for report in report_files:
                    shutil.copy2(report, failed_dir / report.name)
                (failed_dir / "failure.json").write_text(
                    json.dumps({
                        "run_id": run_id,
                        "exception_type": type(exc).__name__,
                        "message": str(exc),
                        "reports": [p.name for p in report_files],
                    }, indent=2), encoding="utf-8")
                if isinstance(exc, DataValidationError):
                    exc.args = (
                        f"{exc}. Audit files preserved at {failed_dir}.",
                    )
        except Exception as preserve_exc:
            if isinstance(exc, DataValidationError):
                exc.args = (
                    f"{exc}. Additionally failed to preserve audit files: "
                    f"{preserve_exc}",
                )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        raise

    manifest["run_id"] = run_id
    manifest["runtime_seconds"] = round(time.time() - t0, 2)
    audit_summary = staging / "audit_summary.md"
    audit_summary.write_text(
        render_audit_summary(manifest, reports), encoding="utf-8")
    manifest["outputs"]["audit_summary"] = {
        "path": "audit_summary.md",
        "sha256": sha256_file(audit_summary),
        "note": "run-specific acceptance gates rendered from actual reports",
    }
    (reports / "data_manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (staging / "_SUCCESS").write_text(run_id, encoding="utf-8")

    final_dir.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, final_dir)

    pointer = {
        "run_id": run_id,
        "run_dir": str(final_dir),
        "created_utc": run_id,
        "manifest": str(final_dir / "reports" / "data_manifest.json"),
        "script_sha256": manifest["script_sha256"],
        "source_sha256": manifest["source_sha256"],
        # paths inside the manifest are relative; resolve against run_dir
        "outputs": {k: str(final_dir / v["path"])
                    for k, v in manifest["outputs"].items()
                    if isinstance(v, dict) and "path" in v},
    }
    tmp_ptr = args.output_dir / f".latest.{os.getpid()}.json"
    tmp_ptr.write_text(json.dumps(pointer, indent=2), encoding="utf-8")
    os.replace(tmp_ptr, args.output_dir / "latest.json")

    runs_dir = args.output_dir / "runs"
    olds = sorted([p for p in runs_dir.iterdir() if p.is_dir()], reverse=True)
    for old in olds[max(args.keep_runs, 1):]:
        shutil.rmtree(old, ignore_errors=True)

    manifest["run_dir"] = str(final_dir)
    return manifest


def _run_into(args: argparse.Namespace, out_dir: Path, reports: Path,
              run_id: str) -> dict:
    release_contract = validate_release_contract(args.release_config, args)
    environment_lock = validate_environment_lock(args.requirements_lock)
    git = git_provenance()
    raw = read_table(args.input)
    input_rows = len(raw)

    ts_col, ts_choice = pick_timestamp_column(raw, args.timestamp_column)
    n2o = {n: o for o, n in normalize_names(raw.columns).items()}
    source_col = next((n2o[c] for c in SOURCE_COLUMN_ALIASES if c in n2o), None)

    prepared = raw.copy()
    for cand in CANONICAL_ALIASES["timestamp"]:
        src = n2o.get(cand)
        if src is not None and src != ts_col and src in prepared.columns:
            prepared = prepared.drop(columns=[src])
    prepared = prepared.rename(columns={ts_col: "timestamp"})

    keep_source = None
    if source_col and source_col in prepared.columns:
        keep_source = "_raw_source"
        prepared[keep_source] = prepared[source_col].astype(str)

    df = resolve_columns(prepared, CANONICAL_ALIASES,
                         required=("timestamp", "open", "high", "low", "close", "volume"))

    # --- symbol -----------------------------------------------------------
    symbol_filtered_rows = 0
    if "symbol" in df.columns:
        upper = df["symbol"].astype(str).str.upper()
        distinct = int(upper.nunique())
        mask = upper.eq(args.symbol.upper())
        if not mask.any():
            raise DataValidationError(
                f"File carries symbols {sorted(upper.unique())[:10]} but none "
                f"match --symbol {args.symbol}.")
        symbol_filtered_rows = int((~mask).sum())
        df = df.loc[mask].copy()
    df["symbol"] = args.symbol.upper()

    # --- timestamps -------------------------------------------------------
    df["timestamp"] = parse_timestamp(df["timestamp"], args.input_timezone,
                                      args.timestamp_format, args.epoch_unit)
    bad_ts = df["timestamp"].isna()
    bad_timestamp_rows = int(bad_ts.sum())
    if bad_timestamp_rows:
        df.loc[bad_ts].head(100_000).to_csv(reports / "bad_timestamp_rows.csv", index=False)
        frac = bad_timestamp_rows / max(input_rows, 1)
        if frac > args.max_bad_timestamp_frac:
            raise DataValidationError(
                f"{bad_timestamp_rows} rows ({frac:.1%}) have unparseable "
                f"timestamps, above --max-bad-timestamp-frac="
                f"{args.max_bad_timestamp_frac:.1%}. A large null block usually "
                f"means the wrong column was chosen (used {ts_col!r}; candidates "
                f"{ts_choice.get('candidates')}).")
        df = df.loc[~bad_ts].copy()

    df = numeric_cast(df)
    df = df.sort_values("timestamp", kind="mergesort").reset_index(drop=True)

    # --- duplicates -------------------------------------------------------
    df, duplicate_audit = resolve_duplicate_rows(
        df, reports, args.duplicate_policy, args.strategy_vwap_source,
        args.source_precedence, args.confirm_file_order_precedence, keep_source)
    duplicate_rows_removed = duplicate_audit["duplicate_rows_removed"]

    # --- calendar ---------------------------------------------------------
    cal = xcals.get_calendar(args.calendar)
    tz = str(cal.tz)
    local_dates = df["timestamp"].dt.tz_convert(tz).dt.date
    if (args.expected_start is None) != (args.expected_end is None):
        raise DataValidationError(
            "--expected-start and --expected-end must be supplied together.")
    observed_local_start = pd.Timestamp(local_dates.min()).normalize()
    observed_local_end = pd.Timestamp(local_dates.max()).normalize()
    expected_start = expected_end = None
    if args.expected_start is not None:
        try:
            expected_start = pd.Timestamp(args.expected_start).normalize()
            expected_end = pd.Timestamp(args.expected_end).normalize()
        except (TypeError, ValueError) as exc:
            raise DataValidationError(
                "--expected-start/--expected-end must be valid YYYY-MM-DD dates.") from exc
        if expected_start > expected_end:
            raise DataValidationError(
                f"Expected range is inverted: {expected_start.date()} > "
                f"{expected_end.date()}.")
        if observed_local_start < expected_start or observed_local_end > expected_end:
            raise DataValidationError(
                f"Observed dates {observed_local_start.date()}.."
                f"{observed_local_end.date()} extend outside expected range "
                f"{expected_start.date()}..{expected_end.date()}.")
    schedule_start = expected_start if expected_start is not None else observed_local_start
    schedule_end = expected_end if expected_end is not None else observed_local_end
    schedule = build_schedule(args.calendar, schedule_start, schedule_end)
    df = attach_sessions(df, schedule, args.bar_label)

    outside = df.loc[~df["is_rth"]]
    outside_rth_rows = int(len(outside))
    outside.head(200_000).to_csv(
        reports / "outside_rth_rows.csv", index=False)
    df = df.loc[df["is_rth"]].copy()
    if df.empty:
        raise DataValidationError(
            "No bars fall inside a regular trading session -- almost always a wrong "
            f"--input-timezone (given: {args.input_timezone}) or --bar-label.")

    # --- grid -------------------------------------------------------------
    off_grid = (df["timestamp"].dt.second.ne(0) | df["timestamp"].dt.microsecond.ne(0)
                | df["timestamp"].dt.nanosecond.ne(0))
    off_grid_rows = int(off_grid.sum())
    df.loc[off_grid].head(100_000).to_csv(
        reports / "off_grid_rows.csv", index=False)
    if off_grid_rows:
        raise DataValidationError(
            f"{off_grid_rows} timestamps are not on minute boundaries; see "
            f"{reports / 'off_grid_rows.csv'}.")

    # --- invalid OHLC -----------------------------------------------------
    invalid = ohlc_invalid_mask(df)
    invalid_count = int(invalid.sum())
    invalid_per_session = (df.loc[invalid].groupby("session_date", observed=True).size()
                           if invalid_count else pd.Series(dtype="int64"))
    invalid_dropped = 0
    df.loc[invalid].to_csv(reports / "invalid_ohlc_rows.csv", index=False)
    if invalid_count:
        frac = invalid_count / max(len(df), 1)
        if args.invalid_row_policy == "error":
            raise DataValidationError(
                f"{invalid_count} invalid OHLCV rows ({frac:.3%}); see "
                f"{reports / 'invalid_ohlc_rows.csv'}. Fix at source or use "
                "--invalid-row-policy drop.")
        if frac > args.max_invalid_frac:
            raise DataValidationError(
                f"{invalid_count} invalid rows ({frac:.3%}) exceeds "
                f"--max-invalid-frac={args.max_invalid_frac:.3%}.")
        df = df.loc[~invalid].copy()
        invalid_dropped = invalid_count

    # --- plausibility guards ---------------------------------------------
    first_minute = df.groupby("session_date", observed=True)["minute_of_session"].min()
    alignment = float(first_minute.eq(1).mean())
    if alignment < args.min_open_alignment:
        raise DataValidationError(
            f"Only {alignment:.1%} of sessions start at the exchange open "
            f"(--min-open-alignment={args.min_open_alignment:.0%}). Check "
            f"--input-timezone ({args.input_timezone}) and --bar-label "
            f"({args.bar_label}). First-minute distribution:\n"
            f"{first_minute.value_counts().head(5).to_string()}")

    observed_sessions = int(df["session_date"].nunique())
    coverage = observed_sessions / max(len(schedule), 1)
    present = schedule["session_date"].isin(set(df["session_date"]))
    absent_n = int((~present).sum())
    observed_start = df["session_date"].min()
    observed_end = df["session_date"].max()
    leading_boundary = schedule.loc[
        ~present & schedule["session_date"].lt(observed_start), "session_date"]
    trailing_boundary = schedule.loc[
        ~present & schedule["session_date"].gt(observed_end), "session_date"]
    boundary_dates = pd.concat([leading_boundary, trailing_boundary])
    boundary_missing_n = int(len(boundary_dates))
    if (expected_start is not None
            and boundary_missing_n > args.max_boundary_sessions_missing):
        raise DataValidationError(
            f"{boundary_missing_n} expected boundary sessions are absent "
            f"({len(leading_boundary)} leading, {len(trailing_boundary)} trailing), "
            f"above --max-boundary-sessions-missing="
            f"{args.max_boundary_sessions_missing}. Missing dates: "
            f"{[str(x.date()) for x in boundary_dates.head(20)]}")
    gaps = (~present).astype(int)
    max_consec = int((gaps * (gaps.groupby((gaps != gaps.shift()).cumsum()).cumcount() + 1)).max()) \
        if absent_n else 0
    if coverage < args.min_session_coverage:
        raise DataValidationError(
            f"Observed {observed_sessions}/{len(schedule)} calendar sessions "
            f"({coverage:.1%}) < --min-session-coverage={args.min_session_coverage:.0%}.")
    if absent_n > args.max_absent_sessions:
        raise DataValidationError(
            f"{absent_n} calendar sessions are entirely absent "
            f"(--max-absent-sessions={args.max_absent_sessions}).")
    if max_consec > args.max_consecutive_absent_sessions:
        raise DataValidationError(
            f"{max_consec} consecutive calendar sessions are absent "
            f"(--max-consecutive-absent-sessions="
            f"{args.max_consecutive_absent_sessions}). A run of missing days is a "
            "vendor outage, not sampling noise.")

    # --- halts + session grading -----------------------------------------
    halts, halt_meta = load_halts(args)
    halt_min = halt_minutes_frame(schedule, halts, tz, args.bar_label)
    diagnostics, _grid = session_diagnostics(df, schedule, halt_min,
                                             invalid_per_session, args.bar_label,
                                             args.halt_bar_policy)
    diagnostics.to_csv(reports / "session_quality.csv", index=False)
    halt_min.to_csv(reports / "halt_minutes.csv", index=False)

    dd = diagnostics.set_index("session_date")
    df["session_quality"] = df["session_date"].map(dd["quality"])
    for flag in ["is_paper_ready", "is_halt_usable", "is_exploratory"]:
        df[flag] = df["session_date"].map(dd[flag]).fillna(False).astype(bool)

    # --- regimes / profiling ---------------------------------------------
    df["regime"], regime_meta = assign_regime(df, args.source_split, keep_source)
    seams = regime_seams(df)

    validity_session, validity_minute = build_feature_validity(
        df, diagnostics, halt_min, schedule, args.trade_freq, args.sigma_window)
    write_parquet(validity_session, out_dir / "feature_validity_session.parquet")
    write_parquet(validity_minute, out_dir / "feature_validity_minute.parquet")

    cov = minute_coverage(validity_minute, args.trade_freq, args.sigma_window)
    cov.to_csv(reports / "minute_coverage.csv", index=False)

    # halt / executability flags must travel with the bars: an engine reading
    # only the parquet cannot otherwise tell which minutes may carry an order.
    vm = validity_minute.set_index(["session_date", "minute_of_session"])
    key = pd.MultiIndex.from_arrays([df["session_date"], df["minute_of_session"]])
    for col in ["is_halt_minute", "is_executable_minute",
                "is_scheduled_decision_minute", "vwap_valid",
                "move_open_obs_valid", "signal_valid_default_config"]:
        df[col] = vm[col].reindex(key).fillna(False).to_numpy()

    df["timestamp_local"] = df["timestamp"].dt.tz_convert(tz)
    regime_profile = profile_regimes(df, args.extreme_1min_return)
    anomalies = anomaly_details(
        df, args.extreme_1min_return, reports, halt_min)

    # --- dividends --------------------------------------------------------
    dividends_df, dividend_manifest = None, None
    if args.dividends is not None:
        dividends_df, dividend_manifest = clean_dividends(
            args.dividends, out_dir, args.symbol,
            pd.DatetimeIndex(diagnostics["session_date"]),
            args.dividend_duplicate_policy, args.confirm_dividend_sum)
    raw_evidence = raw_price_evidence(df, dividends_df)

    # --- outputs ----------------------------------------------------------
    cols = ["timestamp", "timestamp_local", "session_date", "minute_of_session",
            "symbol", "open", "high", "low", "close", "volume"]
    cols += [c for c in OPTIONAL_NUMERIC if c in df.columns]
    cols += ["market_open", "market_close", "calendar_bars", "regime",
             "session_quality", "is_halt_minute", "is_executable_minute",
             "is_scheduled_decision_minute", "move_open_obs_valid", "vwap_valid",
             "signal_valid_default_config", "is_paper_ready", "is_halt_usable", "is_exploratory"]
    df = df[cols].sort_values("timestamp").reset_index(drop=True)

    tiers = {
        "clean": (df, "All row-level-valid RTH bars. Diagnostics only."),
        "paper_ready": (df.loc[df["is_paper_ready"]],
                        "Every scheduled minute present. Use for the formal "
                        "paper replication and all headline return figures."),
        "halt_aware": (df.loc[df["is_halt_usable"]],
                       "paper_ready plus sessions whose only absent minutes are "
                       "officially halted. Once the engine handles halts this "
                       "is the primary economic tier -- report its FULL metric "
                       "set, do not take only risk numbers from it."),
        "exploratory": (df.loc[df["is_exploratory"]],
                        "Includes small gaps and truncated sessions. Never a "
                        "headline number."),
    }
    outputs: dict[str, object] = {}
    for name, (frame, note) in tiers.items():
        fname = f"spy_1min_{name}.parquet"
        path = out_dir / fname
        write_parquet(frame.reset_index(drop=True), path)
        # Relative to the run directory. v3 stored the staging path, which no
        # longer exists once the directory is moved into place.
        outputs[name] = {"path": fname, "rows": int(len(frame)),
                         "sessions": int(frame["session_date"].nunique()),
                         "sha256": sha256_file(path), "note": note}
    for name in ["feature_validity_session", "feature_validity_minute"]:
        path = out_dir / f"{name}.parquet"
        outputs[name] = {"path": f"{name}.parquet",
                         "rows": int(len(pd.read_parquet(path))),
                         "sha256": sha256_file(path),
                         "note": "component-level validity on the full exchange "
                                 "calendar; the engine's trading mask"}

    counts = AuditCounts(
        input_rows=int(input_rows), symbol_filtered_rows=symbol_filtered_rows,
        bad_timestamp_rows=bad_timestamp_rows,
        duplicate_rows_removed=duplicate_rows_removed,
        conflicting_duplicate_timestamps=duplicate_audit[
            "conflicting_duplicate_timestamps"],
        conflicting_ohlcv_timestamps=duplicate_audit[
            "conflicting_ohlcv_timestamps"],
        conflicting_optional_metadata_timestamps=duplicate_audit[
            "conflicting_optional_metadata_timestamps"],
        outside_rth_rows=outside_rth_rows, off_grid_rows=off_grid_rows,
        invalid_ohlc_rows_dropped=invalid_dropped, clean_rth_rows=int(len(df)))
    counts.check_conservation()
    if outputs["paper_ready"]["rows"] == 0:
        raise DataValidationError("paper_ready tier is empty; refusing to publish.")

    report_hashes = {p.name: sha256_file(p) for p in sorted(reports.glob("*.csv"))}

    return {
        "pipeline_version": 5,
        "manifest_schema_version": 3,
        "feature_validity_schema_version": 1,
        "default_config": {
            "note": "signal_valid_default_config was computed with these values "
                    "and assumes use_vwap=True and volatility targeting. Any "
                    "engine running a different configuration must rebuild the "
                    "mask from the primitives.",
            "trade_freq": args.trade_freq,
            "sigma_window": args.sigma_window,
            "assumes_use_vwap": True,
            "assumes_vol_target_sizing": True,
        },
        "script_sha256": sha256_file(SCRIPT_PATH),
        "cli": sys.argv[1:],
        "config": {k: (str(v) if isinstance(v, Path) else v)
                   for k, v in vars(args).items()},
        "symbol": args.symbol.upper(),
        "source": str(args.input), "source_sha256": sha256_file(args.input),
        "timestamp_column": ts_col, "timestamp_column_selection": ts_choice,
        "raw_source_column": source_col,
        "input_timezone_for_naive_input": args.input_timezone,
        "timestamp_format": args.timestamp_format, "epoch_unit": args.epoch_unit,
        "output_timezone": "UTC", "exchange_calendar": args.calendar,
        "exchange_tz": tz, "bar_label": args.bar_label,
        "expected_start": (
            str(expected_start.date()) if expected_start is not None else None),
        "expected_end": (
            str(expected_end.date()) if expected_end is not None else None),
        "observed_start": str(observed_start.date()),
        "observed_end": str(observed_end.date()),
        "boundary_sessions_missing": {
            "known": expected_start is not None,
            "leading": int(len(leading_boundary)),
            "trailing": int(len(trailing_boundary)),
            "total": boundary_missing_n,
            "dates": [str(x.date()) for x in boundary_dates],
        },
        "price_adjustment": "none_raw_ohlc",
        "missing_bar_policy": "diagnose_do_not_impute",
        "first_timestamp": df["timestamp"].min().isoformat(),
        "last_timestamp": df["timestamp"].max().isoformat(),
        "first_session": str(df["session_date"].min().date()),
        "last_session": str(df["session_date"].max().date()),
        "sessions_calendar": int(len(schedule)),
        "sessions_observed": observed_sessions,
        "session_coverage": round(coverage, 5),
        "sessions_absent": absent_n,
        "max_consecutive_absent_sessions": max_consec,
        "session_open_alignment": round(alignment, 5),
        "session_quality_counts": {
            k: int(v) for k, v in diagnostics["quality"].value_counts()
            .reindex(QUALITY_ORDER).fillna(0).items()},
        "halt_table": halt_meta,
        "halt_validation": {
            "method": "minute_set_difference",
            "halt_bar_policy": args.halt_bar_policy,
            "sessions_with_bars_present_during_halt":
                int(diagnostics["present_during_halt"].gt(0).sum()),
            "sessions_with_unexpected_minutes":
                int(diagnostics["unexpected_minutes"].gt(0).sum()),
            "total_halt_minutes": int(diagnostics["halt_minutes"].sum()),
        },
        "duplicate_audit": duplicate_audit,
        "counts": asdict(counts),
        "regime_detection": regime_meta,
        "regimes": regime_profile,
        "regime_seams": seams,
        "anomaly_reports": anomalies,
        "dividend_adjustment_evidence": raw_evidence,
        "band_warmup": {
            "trade_freq": args.trade_freq, "sigma_window": args.sigma_window,
            "rule": f"prior_{args.sigma_window}_eligible_USABLE_observations >= {args.sigma_window} "
                    f"(usable = open_valid & bar_present, not merely bar_present)",
            "bucket_observations": int(len(cov)),
            "band_warm_pct": round(float(cov["band_warm"].mean() * 100), 3),
            "note": "eligibility respects early closes: a 13:00 session is not "
                    "scored as missing its 15:30 bar",
        },
        "usage_guidance": {
            "feature_computation": "Compute every feature on spy_1min_clean "
                                   "over the full exchange calendar, then apply "
                                   "feature_validity_minute as a trading mask. "
                                   "Filtering bars to a tier first and computing "
                                   "features afterwards silently changes the "
                                   "strategy: previous-close reaches across the "
                                   "removed session and a 14-session window "
                                   "stretches over more calendar days.",
            "reporting": "Report the FULL metric set (return, Sharpe, MDD, worst "
                         "day, skew) separately for each tier. Never take CAGR "
                         "from one tier and MDD from another -- they are "
                         "different equity curves and the resulting Calmar is "
                         "not a defined quantity.",
            "tier_roles": {
                "paper_ready": "official-sample compatibility and data-integrity "
                               "baseline",
                "halt_aware": "primary economic result once the engine handles "
                              "halts (freeze the position, trade nothing inside "
                              "the halt, and book the reopening gap in full)",
                "exploratory": "sensitivity analysis only",
            },
            "disclosure": "Tightening the session set moved results in the "
                          "flattering direction on this sample; always print the "
                          "tier alongside every number.",
        },
        "outputs": outputs,
        "reports": {"dir": "reports", "sha256": report_hashes},
        "dividends": dividend_manifest,
        "release_contract": release_contract,
        "environment_lock": environment_lock,
        "data_self_tests": {
            "preflight_requested": bool(args.preflight_self_test),
            "passed": bool(getattr(args, "_preflight_self_test_passed", False)),
            "checks": (
                DATA_SELF_TEST_COUNT
                if getattr(args, "_preflight_self_test_passed", False) else 0),
        },
        "git": git,
        "python": sys.version.split()[0], "pandas": pd.__version__,
        "numpy": np.__version__,
        "exchange_calendars": getattr(xcals, "__version__", "unknown"),
    }


# --------------------------------------------------------------------------- #
# self-test
# --------------------------------------------------------------------------- #

def _synth(tmp: Path, name: str = "synth.parquet",
           first: str = "2023-10-16", last: str = "2023-12-15",
           random_walk: bool = False, seed: int = 0
           ) -> tuple[Path, pd.DataFrame]:
    """~40 real XNYS sessions incl. the 2023-11-24 early close."""
    cal = xcals.get_calendar("XNYS")
    sessions = cal.sessions_in_range(pd.Timestamp(first), pd.Timestamp(last))
    opens = pd.to_datetime(pd.Series(cal.opens.reindex(sessions).values), utc=True)
    closes = pd.to_datetime(pd.Series(cal.closes.reindex(sessions).values), utc=True)
    frames = []
    for k, (o, c) in enumerate(zip(opens, closes)):
        n = int((c - o) / pd.Timedelta(minutes=1))
        idx = pd.date_range(o, periods=n, freq="1min", tz="UTC") \
            .tz_convert("America/New_York").tz_localize(None)
        if random_walk:
            rng = np.random.default_rng(seed + k)
            px = 400.0 * np.exp(np.cumsum(rng.normal(0, 3e-4, n)))
        else:
            px = 400 + k + np.arange(n) * 0.01
        frames.append(pd.DataFrame({"caldt": idx, "open": px, "high": px + 0.05,
                                    "low": px - 0.05, "close": px + 0.01,
                                    "volume": 1000.0}))
    df = pd.concat(frames, ignore_index=True)
    path = tmp / name
    df.to_parquet(path, index=False)
    return path, df


def _base(src: Path, out: Path, *extra: str) -> list[str]:
    return ["--input", str(src), "--output-dir", str(out),
            "--input-timezone", "America/New_York",
            "--min-session-coverage", "0.0", "--max-absent-sessions", "9999",
            "--max-consecutive-absent-sessions", "9999", *extra]


def _cov(m: dict) -> pd.DataFrame:
    return pd.read_csv(Path(m["run_dir"]) / "reports" / "minute_coverage.csv",
                       parse_dates=["session_date"])


def self_test(verbose: bool = True) -> int:
    fails: list[str] = []
    n_checks = 0

    def expect_raise(argv: list[str], label: str) -> None:
        nonlocal n_checks
        n_checks += 1
        try:
            run(parse_args(argv))
            fails.append(f"{label}: expected DataValidationError, none raised")
        except DataValidationError:
            pass

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src, d = _synth(tmp)
        n_sessions = d["caldt"].dt.normalize().nunique()

        # 1 naive input without --input-timezone
        expect_raise(["--input", str(src), "--output-dir", str(tmp / "a")], "naive tz")

        # 2 wrong timezone caught by open alignment
        expect_raise(["--input", str(src), "--output-dir", str(tmp / "b"),
                      "--input-timezone", "UTC", "--min-session-coverage", "0.0",
                      "--max-absent-sessions", "9999",
                      "--max-consecutive-absent-sessions", "9999"], "wrong tz")

        # 3 clean run
        n_checks += 1
        m = run(parse_args(_base(src, tmp / "c")))
        if m["session_quality_counts"].get("complete", 0) != n_sessions:
            fails.append(f"clean run not all complete: {m['session_quality_counts']}")
        if m["outputs"]["paper_ready"]["sessions"] != n_sessions:
            fails.append("paper_ready lost sessions on clean input")
        if not (Path(m["run_dir"]) / "_SUCCESS").exists():
            fails.append("_SUCCESS marker missing")

        # 4 manifest paths must be RELATIVE and resolve inside the run dir
        n_checks += 1
        man = json.loads((Path(m["run_dir"]) / "reports" / "data_manifest.json")
                         .read_text())
        rd = Path(m["run_dir"])
        for k, v in man["outputs"].items():
            if Path(v["path"]).is_absolute():
                fails.append(f"manifest path for {k} is absolute: {v['path']}")
            elif not (rd / v["path"]).exists():
                fails.append(f"manifest path for {k} does not resolve: {v['path']}")
        if Path(man["reports"]["dir"]).is_absolute():
            fails.append("manifest reports.dir is absolute")
        if not (man["expected_start"] is None
                and man["observed_start"] == str(d["caldt"].dt.date.min())
                and man["boundary_sessions_missing"]["known"] is False):
            fails.append("inferred-boundary manifest fields are wrong: "
                         f"{man.get('expected_start')}/"
                         f"{man.get('observed_start')}/"
                         f"{man.get('boundary_sessions_missing')}")
        if man["environment_lock"]["sha256"] != sha256_file(
                SCRIPT_PATH.with_name("requirements.lock")):
            fails.append("requirements lock hash missing or wrong in manifest")
        required_zero_row_reports = {
            "conflicting_ohlcv.csv",
            "conflicting_optional_metadata.csv",
            "off_grid_rows.csv",
            "invalid_ohlc_rows.csv",
        }
        missing_zero_row_reports = sorted(
            name for name in required_zero_row_reports
            if not (rd / "reports" / name).exists())
        if missing_zero_row_reports:
            fails.append(
                "zero-row audit reports were not published with headers: "
                f"{missing_zero_row_reports}")

        # 5 explicit expected boundaries expose a wholly missing leading session
        expected_end = str(d["caldt"].dt.date.max())
        expect_raise(_base(
            src, tmp / "boundary",
            "--expected-start", "2023-10-13",
            "--expected-end", expected_end), "missing expected boundary")

        # 6 duplicate conflicts are classified by semantic field group
        dupts = pd.to_datetime(
            ["2023-10-16 13:30:00+00:00"] * 2, utc=True)
        dup_base = pd.DataFrame({
            "timestamp": dupts, "open": [100.0, 100.0],
            "high": [101.0, 101.0], "low": [99.0, 99.0],
            "close": [100.5, 100.5], "volume": [1000.0, 1000.0],
            "vendor_bar_vwap": [100.1, 100.2],
            "transactions": [10.0, 10.0],
            "_raw_source": ["preferred", "secondary"],
        })
        n_checks += 1
        dup_reports = tmp / "dup_reports"
        dup_reports.mkdir()
        optional_resolved, optional_audit = resolve_duplicate_rows(
            dup_base, dup_reports, "error", "hlc3", None, False, "_raw_source")
        if (len(optional_resolved) != 1
                or not pd.isna(optional_resolved["vendor_bar_vwap"].iloc[0])
                or optional_audit["conflicting_ohlcv_timestamps"] != 0
                or optional_audit["vendor_bar_vwap_conflict_timestamps"] != 1
                or not (dup_reports / "conflicting_optional_metadata.csv").exists()):
            fails.append("optional-only duplicate conflict was not reported/"
                         f"nulled independently: {optional_audit}")

        # 7 vendor VWAP conflicts are fatal when that metadata drives strategy VWAP
        n_checks += 1
        try:
            resolve_duplicate_rows(
                dup_base, dup_reports, "error", "vendor_bar_vwap",
                None, False, "_raw_source")
            fails.append("vendor VWAP conflict passed while vendor_bar_vwap "
                         "was selected")
        except DataValidationError:
            pass

        # 8 OHLCV conflicts fail a headline-policy run
        n_checks += 1
        ohlcv_bad = dup_base.copy()
        ohlcv_bad.loc[1, "close"] = 100.6
        try:
            resolve_duplicate_rows(
                ohlcv_bad, dup_reports, "error", "hlc3",
                None, False, "_raw_source")
            fails.append("OHLCV-conflicting duplicate passed error policy")
        except DataValidationError:
            pass

        # 9 transactions conflicts resolve only through explicit source precedence
        n_checks += 1
        tx_bad = dup_base.copy()
        tx_bad["vendor_bar_vwap"] = 100.1
        tx_bad["transactions"] = [10.0, 20.0]
        tx_resolved, tx_audit = resolve_duplicate_rows(
            tx_bad, dup_reports, "error", "hlc3",
            "preferred,secondary", False, "_raw_source")
        if (len(tx_resolved) != 1
                or tx_resolved["transactions"].iloc[0] != 10.0
                or tx_audit["resolution"] != "source_precedence"):
            fails.append("transactions conflict ignored source precedence: "
                         f"{tx_audit}")

        # 10 return anomalies distinguish true 1-minute, gap and halt reopen
        n_checks += 1
        rday = pd.Timestamp("2023-10-16")
        rminute = pd.Series([1, 2, 5, 6, 8], dtype="int64")
        rclose = pd.Series([100.0, 102.0, 110.0, 111.0, 120.0])
        rframe = pd.DataFrame({
            "timestamp_local": pd.date_range(
                "2023-10-16 09:30", periods=5, freq="1min",
                tz="America/New_York"),
            "session_date": rday,
            "minute_of_session": rminute,
            "regime": 0,
            "open": rclose, "high": rclose, "low": rclose,
            "close": rclose, "volume": 1000.0,
            "is_executable_minute": True,
        })
        rhalt = pd.DataFrame({
            "session_date": [rday],
            "minute_of_session": [7],
        })
        ret_reports = tmp / "return_reports"
        ret_reports.mkdir()
        ret_audit = anomaly_details(rframe, 0.01, ret_reports, rhalt)
        if (ret_audit["extreme_continuous_1min_return_rows"] != 1
                or ret_audit["gap_return_rows"] != 1
                or ret_audit["halt_reopen_return_rows"] != 1
                or not (ret_reports / "gap_returns.csv").exists()
                or not (ret_reports / "halt_reopen_returns.csv").exists()):
            fails.append(f"intraday return classification failed: {ret_audit}")

        # 11 a mismatched dependency lock fails before a publishable run
        n_checks += 1
        bad_lock = tmp / "requirements.bad.lock"
        bad_lock.write_text("numpy==0.0.0\\n", encoding="utf-8")
        try:
            validate_environment_lock(bad_lock)
            fails.append("mismatched requirements lock was accepted")
        except DataValidationError:
            pass

        # band_warm boundary: session 14 (prior=13) False, session 15 (prior=14) True
        n_checks += 1
        cov = _cov(m)
        w = cov[cov["minute_of_session"] == 30].sort_values("session_date") \
            .reset_index(drop=True)
        col = "prior_14_eligible_usable"
        if len(w) < 16:
            fails.append("synthetic sample too short for the warmup boundary test")
        else:
            if not (w[col].iloc[13] == 13 and not bool(w["band_warm"].iloc[13])):
                fails.append(f"session 14 should be prior=13/False, got "
                             f"{w[col].iloc[13]}/{w['band_warm'].iloc[13]}")
            if not (w[col].iloc[14] == 14 and bool(w["band_warm"].iloc[14])):
                fails.append(f"session 15 should be prior=14/True, got "
                             f"{w[col].iloc[14]}/{w['band_warm'].iloc[14]}")

        # 6 early close: afternoon buckets neither scored nor allowed to
        #   depress the trailing count of later sessions
        n_checks += 1
        q = pd.read_csv(Path(m["run_dir"]) / "reports" / "session_quality.csv",
                        parse_dates=["session_date"])
        half = q.loc[q["calendar_bars"] < 390, "session_date"]
        if half.empty:
            fails.append("synthetic sample contains no early close")
        else:
            h = half.iloc[0]
            if ((cov["session_date"] == h) & (cov["minute_of_session"] > 210)).any():
                fails.append("early close scored on unscheduled minutes")
            after = cov[(cov["minute_of_session"] == 390)
                        & (cov["session_date"] > h)].sort_values("session_date")
            if len(after) >= 3 and not (after[col].head(3) == 14).all():
                fails.append("early close depressed later afternoon counts: "
                             f"{after[col].head(3).tolist()}")

        # 7 interior gap -> out of paper_ready, in exploratory; vwap_valid
        #   collapses after the gap but move_open stays usable
        n_checks += 1
        day0 = pd.Timestamp(d["caldt"].dt.normalize().unique()[5])
        gap = d[~d["caldt"].between(day0 + pd.Timedelta(hours=11),
                                    day0 + pd.Timedelta(hours=11, minutes=4))]
        p2 = tmp / "gap.parquet"; gap.to_parquet(p2, index=False)
        m2 = run(parse_args(_base(p2, tmp / "d")))
        if m2["session_quality_counts"].get("interior_gap", 0) != 1:
            fails.append(f"interior gap misgraded: {m2['session_quality_counts']}")
        if m2["outputs"]["paper_ready"]["sessions"] != n_sessions - 1:
            fails.append("interior-gap session leaked into paper_ready")
        if m2["outputs"]["exploratory"]["sessions"] != n_sessions:
            fails.append("interior-gap session missing from exploratory")
        vm = pd.read_parquet(Path(m2["run_dir"]) / "feature_validity_minute.parquet")
        gday = vm[vm["session_date"] == day0]
        pre = gday[gday["minute_of_session"] == 60]
        post = gday[gday["minute_of_session"] == 120]
        if not bool(pre["vwap_valid"].iloc[0]):
            fails.append("vwap_valid false before the gap")
        if bool(post["vwap_valid"].iloc[0]):
            fails.append("vwap_valid still true after an interior gap")
        if not bool(post["move_open_obs_valid"].iloc[0]):
            fails.append("interior gap wrongly invalidated a later move_open "
                         "observation (it depends on the open and that bar only)")

        # 8 truncated open: every minute unusable for move_open, but the close
        #   is still a valid previous-close for the next session
        n_checks += 1
        day1 = pd.Timestamp(d["caldt"].dt.normalize().unique()[20])
        trunc = d[~((d["caldt"] >= day1) & (d["caldt"] < day1 + pd.Timedelta(hours=11)))]
        p3 = tmp / "trunc.parquet"; trunc.to_parquet(p3, index=False)
        m3 = run(parse_args(_base(p3, tmp / "e", "--min-open-alignment", "0.5")))
        if m3["session_quality_counts"].get("truncated", 0) != 1:
            fails.append(f"truncated misgraded: {m3['session_quality_counts']}")
        if m3["outputs"]["paper_ready"]["sessions"] != n_sessions - 1:
            fails.append("truncated session leaked into paper_ready")
        vs = pd.read_parquet(Path(m3["run_dir"]) / "feature_validity_session.parquet")
        row = vs[vs["session_date"] == day1].iloc[0]
        if bool(row["open_valid"]) or not bool(row["close_valid"]):
            fails.append("truncated-open session should be open_valid=False, "
                         f"close_valid=True; got {row['open_valid']}/{row['close_valid']}")
        vm3 = pd.read_parquet(Path(m3["run_dir"]) / "feature_validity_minute.parquet")
        aft = vm3[(vm3["session_date"] == day1) & (vm3["minute_of_session"] == 390)]
        if bool(aft["move_open_obs_valid"].iloc[0]):
            fails.append("REGRESSION: afternoon bar of a truncated-open session "
                         "counted as a usable move_open observation")
        cov3 = _cov(m3)
        nxt = cov3[(cov3["minute_of_session"] == 390)
                   & (cov3["session_date"] > day1)].sort_values("session_date")
        if len(nxt) and int(nxt[col].iloc[0]) != 13:
            fails.append("truncated session did not reduce the following "
                         f"session's band history: {nxt[col].iloc[0]}")

        # 9 halt absent -> halt_adjusted, in halt_aware, out of paper_ready
        n_checks += 1
        day2 = pd.Timestamp(d["caldt"].dt.normalize().unique()[10])
        hp = tmp / "halts.csv"
        pd.DataFrame({"session_date": [str(day2.date())],
                      "start_local": ["11:00"], "end_local": ["11:13"]}) \
            .to_csv(hp, index=False)
        halted = d[~((d["caldt"] >= day2 + pd.Timedelta(hours=11))
                     & (d["caldt"] <= day2 + pd.Timedelta(hours=11, minutes=13)))]
        p4 = tmp / "halt_ok.parquet"; halted.to_parquet(p4, index=False)
        m5 = run(parse_args(_base(p4, tmp / "g", "--halts", str(hp))))
        if m5["session_quality_counts"].get("halt_adjusted", 0) != 1:
            fails.append(f"halt session misgraded: {m5['session_quality_counts']}")
        if m5["outputs"]["paper_ready"]["sessions"] != n_sessions - 1:
            fails.append("halt session must not be in paper_ready")
        if m5["outputs"]["halt_aware"]["sessions"] != n_sessions:
            fails.append("halt session must be in halt_aware")
        vm5 = pd.read_parquet(Path(m5["run_dir"]) / "feature_validity_minute.parquet")
        hm = vm5[(vm5["session_date"] == day2)
                 & vm5["minute_of_session"].between(91, 104)]
        if not bool(hm["is_halt_minute"].all()) or bool(hm["is_executable_minute"].any()):
            fails.append("halt minutes not flagged / marked executable")
        after_halt = vm5[(vm5["session_date"] == day2)
                         & (vm5["minute_of_session"] == 150)]
        if not bool(after_halt["vwap_valid"].iloc[0]):
            fails.append("halt wrongly invalidated VWAP (no volume trades in a halt)")
        bars5 = pd.read_parquet(Path(m5["run_dir"]) / "spy_1min_halt_aware.parquet")
        if "is_halt_minute" not in bars5.columns or "is_executable_minute" not in bars5.columns:
            fails.append("halt flags missing from the bar-level output")

        # 10 bars present during a declared halt must not pass (v2 count check
        #    graded this pristine)
        n_checks += 1
        m6 = run(parse_args(_base(src, tmp / "h", "--halts", str(hp))))
        if m6["session_quality_counts"].get("halt_anomaly", 0) != 1:
            fails.append(f"bars during halt not flagged: {m6['session_quality_counts']}")
        if m6["halt_validation"]["sessions_with_bars_present_during_halt"] != 1:
            fails.append("present_during_halt not reported")

        # 11 allow_present phantom halt bars are non-observations and must not
        #    increase the next eligible session's same-minute sigma count
        n_checks += 1
        m6a = run(parse_args(_base(
            src, tmp / "h_allow", "--halts", str(hp),
            "--halt-bar-policy", "allow_present", "--trade-freq", "100")))
        if m6a["session_quality_counts"].get("halt_adjusted", 0) != 1:
            fails.append("allow_present halt session not graded halt_adjusted: "
                         f"{m6a['session_quality_counts']}")
        vm6a = pd.read_parquet(
            Path(m6a["run_dir"]) / "feature_validity_minute.parquet")
        phantom = vm6a[(vm6a["session_date"] == day2)
                       & (vm6a["minute_of_session"] == 100)]
        if len(phantom) != 1 or bool(phantom["move_open_obs_valid"].iloc[0]):
            fails.append("REGRESSION: allow_present phantom halt bar counted as "
                         "a usable move_open observation")
        cov6a = _cov(m6a)
        bucket = cov6a[cov6a["minute_of_session"] == 100] \
            .sort_values("session_date").reset_index(drop=True)
        halt_bucket = bucket[bucket["session_date"] == day2]
        next_bucket = bucket[bucket["session_date"] > day2].head(1)
        if len(halt_bucket) != 1 or len(next_bucket) != 1:
            fails.append("allow_present sigma-count test could not find halt/"
                         "next eligible session")
        elif (bool(halt_bucket["usable_observation"].iloc[0])
              or int(next_bucket[col].iloc[0]) != int(halt_bucket[col].iloc[0])):
            fails.append("allow_present phantom halt bar increased next "
                         "session's sigma observation count: "
                         f"{halt_bucket[col].iloc[0]} -> "
                         f"{next_bucket[col].iloc[0]}")

        # 12 overlapping halt windows unioned, not summed
        n_checks += 1
        ovp = tmp / "ov.csv"
        pd.DataFrame({"session_date": [str(day2.date())] * 2,
                      "start_local": ["11:00", "11:05"],
                      "end_local": ["11:13", "11:20"]}).to_csv(ovp, index=False)
        ov_src = d[~((d["caldt"] >= day2 + pd.Timedelta(hours=11))
                     & (d["caldt"] <= day2 + pd.Timedelta(hours=11, minutes=20)))]
        p5 = tmp / "ov.parquet"; ov_src.to_parquet(p5, index=False)
        m7 = run(parse_args(_base(p5, tmp / "i", "--halts", str(ovp))))
        if m7["halt_validation"]["total_halt_minutes"] != 21:
            fails.append("overlapping halts not unioned: "
                         f"{m7['halt_validation']['total_halt_minutes']} != 21")

        # 13 numeric-string epoch
        n_checks += 1
        epoch_ms = (d["caldt"].dt.tz_localize("America/New_York")
                    .dt.tz_convert("UTC").astype("int64") // 10**6)
        p6 = tmp / "epoch.parquet"
        d.assign(caldt=epoch_ms.astype(str)).to_parquet(p6, index=False)
        m8 = run(parse_args(["--input", str(p6), "--output-dir", str(tmp / "j"),
                             "--min-session-coverage", "0.0",
                             "--max-absent-sessions", "9999",
                             "--max-consecutive-absent-sessions", "9999"]))
        if m8["counts"]["clean_rth_rows"] != len(d):
            fails.append(f"numeric-string epoch lost rows: {m8['counts']}")

        # 14 YYYYMMDDHHMMSS must not be silently read as an epoch
        p7 = tmp / "calint.parquet"
        d.assign(caldt=d["caldt"].dt.strftime("%Y%m%d%H%M%S").astype("int64")) \
            .to_parquet(p7, index=False)
        expect_raise(["--input", str(p7), "--output-dir", str(tmp / "k"),
                      "--min-session-coverage", "0.0"], "calendar-shaped int")
        n_checks += 1
        m9 = run(parse_args(["--input", str(p7), "--output-dir", str(tmp / "l"),
                             "--timestamp-format", "%Y%m%d%H%M%S",
                             "--input-timezone", "America/New_York",
                             "--min-session-coverage", "0.0",
                             "--max-absent-sessions", "9999",
                             "--max-consecutive-absent-sessions", "9999"]))
        if m9["counts"]["clean_rth_rows"] != len(d):
            fails.append("--timestamp-format path lost rows")

        # 15 dividends: symbol alias, conflict policy, sum gating
        dvp = tmp / "div.csv"
        pd.DataFrame({"ticker": ["SPY", "SPY", "QQQ"],
                      "date": [str(day2.date())] * 3,
                      "dividend": [1.50, 1.61, 0.70]}).to_csv(dvp, index=False)
        expect_raise(_base(src, tmp / "m", "--dividends", str(dvp)), "div conflict")
        expect_raise(_base(src, tmp / "m2", "--dividends", str(dvp),
                           "--dividend-duplicate-policy", "sum"),
                     "unconfirmed dividend sum")
        n_checks += 1
        m10 = run(parse_args(_base(src, tmp / "n", "--dividends", str(dvp),
                                   "--dividend-duplicate-policy", "first")))
        dm = m10["dividends"]
        if dm["output_rows"] != 1 or dm.get("rows_other_symbols_dropped") != 1:
            fails.append(f"dividend symbol filter via `ticker` failed: {dm}")

        # 16 dividend sum type evidence is validated independently per ex-date
        typed_bad = tmp / "div_typed_bad.csv"
        day3 = pd.Timestamp(d["caldt"].dt.normalize().unique()[11])
        pd.DataFrame({
            "ticker": ["SPY"] * 4,
            "date": [str(day2.date()), str(day2.date()),
                     str(day3.date()), str(day3.date())],
            "dividend": [1.50, 1.51, 1.60, 0.10],
            "dividend_type": ["regular", "regular", "regular", "special"],
        }).to_csv(typed_bad, index=False)
        n_checks += 1
        typed_bad_out = tmp / "div_typed_bad_out"
        typed_bad_out.mkdir()
        try:
            clean_dividends(
                typed_bad, typed_bad_out, "SPY",
                pd.DatetimeIndex([day2, day3]), "sum")
            fails.append("per-date dividend validation accepted regular+regular "
                         "because another ex-date had regular+special")
        except DataValidationError:
            pass

        # 17 a case/whitespace-normalised regular+special pair may be summed
        typed_good = tmp / "div_typed_good.csv"
        pd.DataFrame({
            "ticker": ["SPY", "SPY"],
            "date": [str(day2.date())] * 2,
            "dividend": [1.50, 0.11],
            "dividend_type": [" Regular ", "SPECIAL"],
        }).to_csv(typed_good, index=False)
        n_checks += 1
        typed_good_out = tmp / "div_typed_good_out"
        typed_good_out.mkdir()
        typed_clean, _ = clean_dividends(
            typed_good, typed_good_out, "SPY",
            pd.DatetimeIndex([day2]), "sum")
        if len(typed_clean) != 1 or not np.isclose(
                typed_clean["cash_amount"].iloc[0], 1.61):
            fails.append("valid regular+special dividend pair was not summed")

        # 18 failure AFTER reports are written leaves no run dir and no pointer
        n_checks += 1
        bad = d.copy()
        bad.loc[bad.index[:3], "high"] = bad.loc[bad.index[:3], "low"] - 1.0
        p8 = tmp / "bad.parquet"; bad.to_parquet(p8, index=False)
        outp = tmp / "o"
        try:
            run(parse_args(_base(p8, tmp / "o")))
            fails.append("invalid OHLC did not raise")
        except DataValidationError:
            pass
        leftovers = sorted(x.name for x in outp.iterdir()) if outp.exists() else []
        if any(n.startswith(".tmp_") for n in leftovers):
            fails.append(f"failed run left a staging dir: {leftovers}")
        if "latest.json" in leftovers or (outp / "runs").exists():
            fails.append(f"failed run published a run: {leftovers}")
        failed_reports = list((outp / "failed_audits").glob(
            "*/invalid_ohlc_rows.csv"))
        if len(failed_reports) != 1:
            fails.append("failed validation did not preserve its audit CSV")

    if n_checks != DATA_SELF_TEST_COUNT:
        fails.append(
            f"self-test count changed: {n_checks} != {DATA_SELF_TEST_COUNT}; "
            "update the audited count deliberately")
    if fails:
        print("SELF-TEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print(f"SELF-TEST PASSED ({n_checks} checks)")
    return 0


def main() -> int:
    args = parse_args()
    if args.self_test:
        return self_test()
    if args.preflight_self_test:
        if self_test() != 0:
            raise DataValidationError(
                "Preflight data self-test failed; refusing market-data run.")
        args._preflight_self_test_passed = True
    if args.input is None:
        raise DataValidationError("--input is required (or use --self-test).")
    m = run(args)
    print(json.dumps(m, indent=2, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DataValidationError as exc:
        print(f"DATA VALIDATION FAILED: {exc}", file=sys.stderr)
        raise SystemExit(2)
