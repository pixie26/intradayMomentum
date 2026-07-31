"""Publish a point-in-time financing-rate release for evaluation spec v2."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd


SCRIPT_PATH = Path(__file__).resolve()
ECB_SERIES = "RTD.M.S0.N.C_USL3M.U"
OFR_SERIES = "FNYR-SOFR-A"
ECB_URL = (
    "https://data-api.ecb.europa.eu/service/data/RTD/"
    "M.S0.N.C_USL3M.U?startPeriod=2007-12&endPeriod=2023-06"
    "&format=csvdata"
)
OFR_URL = (
    "https://data.financialresearch.gov/v1/series/full"
    "?mnemonic=FNYR-SOFR-A&start_date=2018-04-01&end_date=2026-07-09"
    "&remove_nulls=true"
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(8 << 20):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "intradayMomentum-research/1.0"})
    with urllib.request.urlopen(request, timeout=90) as response:
        if response.status != 200:
            raise RuntimeError(f"download failed with HTTP {response.status}: {url}")
        return response.read()


def source_bytes(path: Path | None, url: str) -> bytes:
    return path.read_bytes() if path is not None else fetch(url)


def parse_ecb(raw: bytes) -> pd.Series:
    frame = pd.read_csv(io.BytesIO(raw))
    required = {"KEY", "TIME_PERIOD", "OBS_VALUE", "UNIT", "COLLECTION"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise RuntimeError(f"ECB response missing columns: {missing}")
    if set(frame["KEY"].dropna().unique()) != {ECB_SERIES}:
        raise RuntimeError("ECB response contains an unexpected series")
    if set(frame["UNIT"].dropna().unique()) != {"PCPA"}:
        raise RuntimeError("ECB LIBOR proxy is not percent per annum")
    if set(frame["COLLECTION"].dropna().unique()) != {"A"}:
        raise RuntimeError("ECB LIBOR proxy is not a monthly average")
    period = pd.PeriodIndex(frame["TIME_PERIOD"].astype(str), freq="M")
    values = pd.to_numeric(frame["OBS_VALUE"], errors="raise")
    out = pd.Series(values.to_numpy(dtype=float), index=period).sort_index()
    if out.index.duplicated().any():
        raise RuntimeError("ECB LIBOR proxy contains duplicate months")
    return out


def parse_sofr(raw: bytes) -> tuple[pd.Series, dict]:
    payload = json.loads(raw.decode("utf-8"))
    if set(payload) != {OFR_SERIES}:
        raise RuntimeError("OFR response contains an unexpected series")
    item = payload[OFR_SERIES]
    metadata = item["metadata"]
    if metadata.get("mnemonic") != OFR_SERIES:
        raise RuntimeError("OFR SOFR mnemonic mismatch")
    if metadata.get("unit", {}).get("name") != "Percent":
        raise RuntimeError("OFR SOFR unit is not percent")
    pairs = item["timeseries"]["aggregation"]
    out = pd.Series(
        [float(value) for _, value in pairs],
        index=pd.to_datetime([date for date, _ in pairs]).normalize(),
        dtype=float,
    ).sort_index()
    if out.index.duplicated().any():
        raise RuntimeError("OFR SOFR contains duplicate observation dates")
    return out, metadata


def expected_sessions(start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    calendar = xcals.get_calendar("XNYS")
    return pd.DatetimeIndex(
        calendar.sessions_in_range(start, end)).tz_localize(None).normalize()


def build_curve(
        libor: pd.Series, sofr: pd.Series, start: pd.Timestamp,
        end: pd.Timestamp, libor_end: pd.Timestamp, sofr_start: pd.Timestamp,
        cash_spread_bps: float, funding_spread_bps: float,
        borrow_bps: float) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for session in expected_sessions(start, end):
        if session <= libor_end:
            observation = session.to_period("M") - 1
            if observation not in libor.index:
                raise RuntimeError(
                    f"no prior completed-month LIBOR proxy for {session.date()}")
            benchmark = "USD_3M_LIBOR_PUBLIC_PROXY"
            observation_label = str(observation)
            rate_percent = float(libor.loc[observation])
            availability = "previous_completed_calendar_month"
        elif session >= sofr_start:
            eligible = sofr.index[sofr.index < session]
            if len(eligible) == 0:
                raise RuntimeError(
                    f"no SOFR observation published before {session.date()}")
            observation = eligible[-1]
            benchmark = "SOFR"
            observation_label = str(observation.date())
            rate_percent = float(sofr.loc[observation])
            availability = "latest_observation_strictly_before_session"
        else:
            raise RuntimeError(
                f"transition leaves session {session.date()} uncovered")
        rows.append({
            "session_date": session,
            "benchmark": benchmark,
            "benchmark_observation": observation_label,
            "benchmark_rate_percent": rate_percent,
            "availability_policy": availability,
            "cash_rate_annual": (
                rate_percent / 100.0 + cash_spread_bps / 10_000.0),
            "funding_rate_annual": (
                rate_percent / 100.0 + funding_spread_bps / 10_000.0),
            "borrow_rate_annual": borrow_bps / 10_000.0,
        })
    curve = pd.DataFrame(rows)
    rate_columns = [
        "benchmark_rate_percent", "cash_rate_annual",
        "funding_rate_annual", "borrow_rate_annual",
    ]
    if curve[rate_columns].isna().any(axis=None):
        raise RuntimeError("financing curve contains missing rates")
    if not np.isfinite(curve[rate_columns].to_numpy()).all():
        raise RuntimeError("financing curve contains non-finite rates")
    if not curve["session_date"].is_monotonic_increasing:
        raise RuntimeError("financing curve is not sorted")
    return curve


def publish(args: argparse.Namespace) -> Path:
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite financing release: {output}")
    start = pd.Timestamp(args.expected_start)
    end = pd.Timestamp(args.expected_end)
    libor_end = pd.Timestamp(args.libor_end)
    sofr_start = pd.Timestamp(args.sofr_start)
    if not start <= libor_end < sofr_start <= end:
        raise ValueError("invalid LIBOR/SOFR transition bounds")

    ecb_raw = source_bytes(args.ecb_input, args.ecb_url)
    sofr_raw = source_bytes(args.ofr_input, args.ofr_url)
    libor = parse_ecb(ecb_raw)
    sofr, sofr_metadata = parse_sofr(sofr_raw)
    curve = build_curve(
        libor, sofr, start, end, libor_end, sofr_start,
        args.cash_spread_bps, args.funding_spread_bps, args.borrow_bps)

    staging = output.parent / f".staging-{output.name}-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        ecb_name = "source_ecb_usd_3m_libor_monthly.csv"
        sofr_name = "source_ofr_sofr.json"
        curve_name = "financing_rates_daily.csv"
        (staging / ecb_name).write_bytes(ecb_raw)
        (staging / sofr_name).write_bytes(sofr_raw)
        curve.to_csv(
            staging / curve_name, index=False, date_format="%Y-%m-%d",
            float_format="%.10g")
        counts = curve["benchmark"].value_counts().to_dict()
        manifest = {
            "release_id": args.release_id,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "classification": "point_in_time_financing_rates",
            "calendar": "XNYS",
            "expected_start": str(start.date()),
            "expected_end": str(end.date()),
            "observed_start": str(curve["session_date"].min().date()),
            "observed_end": str(curve["session_date"].max().date()),
            "sessions": int(len(curve)),
            "day_count": "ACT/360",
            "cash_spread_bps": args.cash_spread_bps,
            "funding_spread_bps": args.funding_spread_bps,
            "borrow_bps": args.borrow_bps,
            "transition": {
                "libor_proxy_end": str(libor_end.date()),
                "sofr_start": str(sofr_start.date()),
            },
            "point_in_time_policy": {
                "libor_proxy": (
                    "Use the previous completed calendar month's ECB monthly "
                    "average; the current month's final average is never "
                    "backfilled into that month."),
                "sofr": (
                    "Use the latest SOFR observation whose observation date is "
                    "strictly before the XNYS session date."),
            },
            "sources": {
                "libor_proxy": {
                    "provider": "European Central Bank Data Portal",
                    "series": ECB_SERIES,
                    "frequency": "monthly",
                    "collection": "average_of_observations_through_period",
                    "unit": "percent_per_annum",
                    "url": args.ecb_url,
                    "source_file": ecb_name,
                    "source_sha256": sha256(staging / ecb_name),
                },
                "sofr": {
                    "provider": (
                        "Office of Financial Research API, Federal Reserve "
                        "Bank of New York reference-rate series"),
                    "series": OFR_SERIES,
                    "frequency": "daily",
                    "unit": "percent",
                    "url": args.ofr_url,
                    "source_file": sofr_name,
                    "source_sha256": sha256(staging / sofr_name),
                    "metadata": sofr_metadata,
                },
            },
            "benchmark_session_counts": {
                key: int(value) for key, value in counts.items()},
            "script_sha256": sha256(SCRIPT_PATH),
            "files": {
                name: {
                    "bytes": (staging / name).stat().st_size,
                    "sha256": sha256(staging / name),
                }
                for name in (ecb_name, sofr_name, curve_name)
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
    parser.add_argument(
        "--output", type=Path,
        default=Path("data/reference/financing_rates_v1"))
    parser.add_argument("--release-id", default="financing-rates-v1")
    parser.add_argument("--expected-start", default="2008-01-22")
    parser.add_argument("--expected-end", default="2026-07-09")
    parser.add_argument("--libor-end", default="2023-06-30")
    parser.add_argument("--sofr-start", default="2023-07-03")
    parser.add_argument("--cash-spread-bps", type=float, default=-50.0)
    parser.add_argument("--funding-spread-bps", type=float, default=100.0)
    parser.add_argument("--borrow-bps", type=float, default=25.0)
    parser.add_argument("--ecb-url", default=ECB_URL)
    parser.add_argument("--ofr-url", default=OFR_URL)
    parser.add_argument("--ecb-input", type=Path)
    parser.add_argument("--ofr-input", type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    result = publish(parse_args())
    print(f"published: {result}")
