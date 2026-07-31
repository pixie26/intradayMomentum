"""Publish an immutable independent SPY daily raw-close benchmark release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

import exchange_calendars as xcals
import pandas as pd


SCRIPT_PATH = Path(__file__).resolve()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


def parse_yahoo_chart(path: Path) -> pd.DataFrame:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("chart", {}).get("error") is not None:
        raise RuntimeError(f"Yahoo chart error: {payload['chart']['error']}")
    result = payload["chart"]["result"][0]
    timestamps = pd.to_datetime(
        result["timestamp"], unit="s", utc=True
    ).tz_convert("America/New_York")
    quote = result["indicators"]["quote"][0]
    frame = pd.DataFrame({
        "session_date": pd.DatetimeIndex(timestamps.date),
        "open": quote["open"],
        "high": quote["high"],
        "low": quote["low"],
        "close": quote["close"],
        "volume": quote["volume"],
    })
    if frame["session_date"].duplicated(keep=False).any():
        raise RuntimeError("daily source contains duplicate session dates")
    numeric = ["open", "high", "low", "close", "volume"]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    if frame[["open", "high", "low", "close"]].isna().any(axis=None):
        raise RuntimeError("daily source contains missing OHLC")
    if (frame[["open", "high", "low", "close"]] <= 0).any(axis=None):
        raise RuntimeError("daily source contains nonpositive OHLC")
    if (
        (frame["high"] < frame[["open", "close", "low"]].max(axis=1))
        | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
    ).any():
        raise RuntimeError("daily source contains invalid OHLC")
    return frame.sort_values("session_date").reset_index(drop=True)


def expected_sessions(start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    cal = xcals.get_calendar("XNYS")
    sessions = cal.sessions_in_range(start, end)
    return pd.DatetimeIndex(sessions).tz_localize(None).normalize()


def compare_minute_closes(
        daily: pd.DataFrame, data_release: Path) -> dict[str, object]:
    minute = pd.read_parquet(
        data_release / "clean.parquet",
        columns=["session_date", "minute_of_session", "close"])
    validity = pd.read_parquet(
        data_release / "feature_validity_session.parquet",
        columns=["session_date", "close_valid"])
    validity["session_date"] = pd.to_datetime(
        validity["session_date"]).dt.normalize()
    valid_dates = set(validity.loc[
        validity["close_valid"].astype(bool), "session_date"])
    minute["session_date"] = pd.to_datetime(
        minute["session_date"]).dt.normalize()
    last = (
        minute.sort_values(["session_date", "minute_of_session"])
        .groupby("session_date", as_index=False).tail(1)
        [["session_date", "close"]]
        .rename(columns={"close": "minute_close"})
    )
    merged = daily.merge(last, on="session_date", how="left")
    merged = merged[merged["session_date"].isin(valid_dates)].copy()
    merged["absolute_difference"] = (
        merged["close"] - merged["minute_close"]).abs()
    return {
        "valid_minute_closes_compared": int(len(merged)),
        "missing_minute_comparisons": int(merged["minute_close"].isna().sum()),
        "max_absolute_close_difference": float(
            merged["absolute_difference"].max()),
        "p99_absolute_close_difference": float(
            merged["absolute_difference"].quantile(0.99)),
        "count_difference_over_0_01": int(
            (merged["absolute_difference"] > 0.01).sum()),
        "count_difference_over_0_05": int(
            (merged["absolute_difference"] > 0.05).sum()),
    }


def publish(args: argparse.Namespace) -> Path:
    source = args.input.resolve()
    data_release = args.data_release.resolve()
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite benchmark release: {output}")
    if not (data_release / "_SUCCESS").exists():
        raise RuntimeError(f"{data_release} is not a successful data release")

    start = pd.Timestamp(args.expected_start)
    end = pd.Timestamp(args.expected_end)
    frame = parse_yahoo_chart(source)
    frame = frame[
        (frame["session_date"] >= start) & (frame["session_date"] <= end)
    ].copy()
    expected = expected_sessions(start, end)
    observed = pd.DatetimeIndex(frame["session_date"])
    missing = expected.difference(observed)
    extra = observed.difference(expected)
    if len(missing) or len(extra):
        raise RuntimeError(
            f"daily calendar mismatch: missing={list(missing.date[:10])}, "
            f"extra={list(extra.date[:10])}")

    close_comparison = compare_minute_closes(frame, data_release)
    if close_comparison["missing_minute_comparisons"]:
        raise RuntimeError("a valid minute close has no daily comparison")

    staging = output.parent / f".staging-{output.name}-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        raw_name = "source_yahoo_chart.json"
        clean_name = "spy_daily_raw_close.csv"
        shutil.copy2(source, staging / raw_name)
        frame.to_csv(
            staging / clean_name, index=False, date_format="%Y-%m-%d",
            float_format="%.10g")
        manifest = {
            "release_id": args.release_id,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "classification": "independent_daily_raw_close_benchmark",
            "symbol": "SPY",
            "calendar": "XNYS",
            "source_provider": "Yahoo Finance chart endpoint",
            "source_url": args.source_url,
            "source_sha256": sha256(staging / raw_name),
            "script_sha256": sha256(SCRIPT_PATH),
            "data_release_reference": {
                "path": str(data_release),
                "success_sha256": sha256(data_release / "_SUCCESS"),
            },
            "expected_start": str(start.date()),
            "expected_end": str(end.date()),
            "observed_start": str(frame["session_date"].min().date()),
            "observed_end": str(frame["session_date"].max().date()),
            "sessions": int(len(frame)),
            "calendar_missing_sessions": 0,
            "calendar_extra_sessions": 0,
            "minute_close_comparison": close_comparison,
            "files": {
                raw_name: {
                    "bytes": (staging / raw_name).stat().st_size,
                    "sha256": sha256(staging / raw_name),
                },
                clean_name: {
                    "bytes": (staging / clean_name).stat().st_size,
                    "sha256": sha256(staging / clean_name),
                },
            },
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        (staging / "_SUCCESS").write_text(
            json.dumps({
                "release_id": args.release_id,
                "manifest_sha256": sha256(manifest_path),
            }, sort_keys=True) + "\n",
            encoding="utf-8")
        os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--data-release", type=Path, default=Path("data_release_v1"))
    parser.add_argument("--output", type=Path, default=Path("benchmark_release_v1"))
    parser.add_argument("--release-id", default="spy-daily-benchmark-v1")
    parser.add_argument("--expected-start", default="2008-01-02")
    parser.add_argument("--expected-end", default="2026-07-09")
    parser.add_argument("--source-url", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    result = publish(parse_args())
    print(f"published: {result}")
