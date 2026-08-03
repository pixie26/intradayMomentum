"""Intraday-momentum engine v4.

Pipeline: primitives -> config-specific validity -> signal -> pending order ->
fill -> filled position -> mark-to-market -> costs -> calendarised report.

Three profiles, because "the paper" and "the authors' Colab" are not the same
artefact and conflating them makes every discrepancy unattributable:

  official_sample_compatible  reproduces the notebook's signal, volatility
                              window and shifted-close execution conventions on
                              valid complete sessions: min_periods=13, the
                              d-15..d-2 window, round() share sizing, the
                              NaN-vol -> 4x fallback, no slippage, row-based
                              rolling and raw (unvalidated) daily returns.
                              This is convention parity, NOT line-by-line
                              parity -- the calendar, session tiering and AUM
                              handling are this engine's. A true parity test
                              must reconcile shares, signal, exposure, trade
                              units, commission, gross, net and AUM day by day;
                              matching final returns is not sufficient.
  paper_spec                  the paper as written: 14-session windows,
                              $0.0035/share commission AND $0.001/share
                              slippage, dividend-adjusted previous close.
  corrected_execution         paper_spec plus honest execution: fills at the
                              next executable open, a real order/fill state
                              machine, halt-aware marking, separated cost
                              components, calendarised statistics.

Two things this file refuses to do
----------------------------------
1. Consume `signal_valid_default_config`. That field is baked at the data
   layer's --trade-freq / --sigma-window and assumes VWAP + vol targeting, so a
   sweep silently runs the wrong mask (Cfg(trade_freq=15) would still decide
   only on 30-minute buckets). The mask is rebuilt here from primitives.
2. Shift exposure by data rows. That hands a reopening gap to an order that
   never filled: a signal at 09:34 whose intended fill minute lies inside a
   halt is not a position, and must not earn the +1.03% that SPY gapped on
   2020-03-09.

Accounting notes
----------------
A reversal is two traded units, not one. 0 -> +1 -> -1 -> 0 is four units on
three fill events, and commission and slippage are charged on units.
`reversal_order_model` selects whether the minimum commission applies once (a
single 2N-share order) or twice (close then reverse).

No new position is opened on the final scheduled bar: it would be liquidated at
the same price moments later, contributing zero P&L and two charges. The
notebook avoids this only as a side effect of its row shift.

`sigma_open` rolls over the full scheduled minute grid, so a session that is
merely missing that minute still consumes a slot in the 14-session window.
Rolling over existing rows turns "previous 14 eligible sessions" into "previous
14 rows that happen to exist".

Daily returns spanning an invalid close are excluded from the volatility window
outright; the window is not back-filled with older returns to make up the count.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, asdict, field, replace
from pathlib import Path

import numpy as np
import pandas as pd

ENGINE_PATH = Path(__file__).resolve()

PROFILES = ("official_sample_compatible", "paper_spec", "corrected_execution")


@dataclass
class Cfg:
    profile: str = "paper_spec"

    # signal
    trade_freq: int = 30
    sigma_window: int = 14
    sigma_min_periods: int | None = None      # None -> sigma_window (paper)
    band_mult: float = 1.0
    use_vwap: bool = True
    vwap_source: str = "hlc3"                 # hlc3 | ohlc4 | vendor_bar_vwap

    # sizing
    sizing: str = "vol_target"                # vol_target | flat
    target_vol: float = 0.02
    max_lev: float = 4.0
    flat_lev: float = 1.0
    dvol_lag: int = 1                         # 1 = paper (t-1..t-14); 2 = notebook
    nanvol_action: str = "skip"               # skip | max_lev (notebook bug)

    # execution
    fill_price: str = "next_executable_open"  # next_executable_open | signal_bar_close
    pending_order_policy: str = "cancel_if_next_unavailable"  # | queue_until_executable
    exec_lag_minutes: int = 1

    strict_eligible_rolling: bool = True      # False = notebook (existing rows only)
    respect_return_validity: bool = True      # False = notebook (raw closes)
    share_rounding: str = "floor"             # floor (paper) | round (notebook)
    reversal_order_model: str = "single_order"  # single_order | two_orders

    # costs
    explicit_cost_model: str = "legacy"       # legacy | legacy_plus_section31
    comm_per_share: float = 0.0035
    min_comm: float = 0.35
    slip_per_share: float = 0.001             # paper's figure

    # accounting
    aum0: float = 100_000.0
    # Sharpe hurdle only. Actual cash accrual is cash_rate_annual; keep the two
    # consistent when you switch either on.
    rf_annual: float = 0.0
    unknown_exit_policy: str = "terminate"
    # terminate                      -> equity curve invalid from that day on
    # exclude_session_and_freeze_aum -> assume zero P&L that day (disclose it)
    # impute_last_observed           -> keep the guessed exit (sensitivity only)

    # Financing. Defaults are 0 so current results are unchanged, but the shape
    # is a cash account, not a flat rate on equity: a 4x long is ~3x borrowed
    # cash, so crediting rf on all of equity and then deducting a separate
    # leverage fee double-counts.
    cash_rate_annual: float = 0.0             # earned on positive cash
    funding_rate_annual: float = 0.0          # paid on borrowed cash
    borrow_rate_annual: float = 0.0           # paid on short notional
    financing_daycount_fraction: float = 6.5 / 24.0   # intraday holding only

    tier: str = "paper_ready"                 # paper_ready | halt_aware | exploratory
    require_dividends: bool = True
    ignore_dividends: bool = False
    require_config_match: bool = False         # assert data-layer default_config


_ENUMS = {
    "profile": PROFILES,
    "vwap_source": ("hlc3", "ohlc4", "vendor_bar_vwap"),
    "sizing": ("vol_target", "flat"),
    "nanvol_action": ("skip", "max_lev"),
    "fill_price": ("next_executable_open", "signal_bar_close"),
    "pending_order_policy": ("cancel_if_next_unavailable", "queue_until_executable"),
    "share_rounding": ("floor", "round"),
    "reversal_order_model": ("single_order", "two_orders"),
    "explicit_cost_model": ("legacy", "legacy_plus_section31"),
    "unknown_exit_policy": ("terminate", "exclude_session_and_freeze_aum",
                            "impute_last_observed"),
    "tier": ("paper_ready", "halt_aware", "exploratory"),
}


def validate(cfg: "Cfg") -> None:
    """Fail on an unsupported enum before any data is touched."""
    for field_name, allowed in _ENUMS.items():
        val = getattr(cfg, field_name)
        if val not in allowed:
            raise ValueError(f"{field_name}={val!r} not in {allowed}")
    if cfg.trade_freq <= 0 or cfg.sigma_window <= 0 or cfg.exec_lag_minutes < 1:
        raise ValueError("trade_freq, sigma_window must be > 0; "
                         "exec_lag_minutes must be >= 1")
    if cfg.dvol_lag < 1:
        raise ValueError("dvol_lag must be >= 1 (0 would use today's return)")
    if min(cfg.comm_per_share, cfg.min_comm, cfg.slip_per_share) < 0:
        raise ValueError("commission, minimum commission and slippage must be nonnegative")


def profile_cfg(profile: str, **over) -> Cfg:
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {PROFILES}")
    if profile == "official_sample_compatible":
        base = Cfg(profile=profile, sigma_min_periods=13, dvol_lag=2,
                   nanvol_action="max_lev", fill_price="signal_bar_close",
                   pending_order_policy="queue_until_executable",
                   slip_per_share=0.0, require_dividends=False,
                   share_rounding="round", reversal_order_model="two_orders",
                   strict_eligible_rolling=False, respect_return_validity=False)
    elif profile == "paper_spec":
        base = Cfg(profile=profile, fill_price="signal_bar_close",
                   pending_order_policy="cancel_if_next_unavailable",
                   slip_per_share=0.001)
    else:
        base = Cfg(profile=profile, fill_price="next_executable_open",
                   pending_order_policy="cancel_if_next_unavailable",
                   slip_per_share=0.005)
    out = replace(base, **over)
    validate(out)
    return out


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #

def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while b := fh.read(8 << 20):
            h.update(b)
    return h.hexdigest()


def load_section31_rates(path: str | Path) -> pd.DataFrame:
    """Load and strictly validate an effective-date Section 31 schedule.

    Intervals are calendar-date intervals because SEC advisories specify charge
    dates, not exchange sessions. A continuous calendar schedule makes weekend
    transition dates (for example 2026-04-04) explicit and auditable.
    """
    path = Path(path)
    rates = pd.read_csv(path, dtype={"source_url": "string", "notes": "string"})
    required = (
        "effective_from", "effective_to", "rate_per_million", "rate_decimal",
        "source_url", "notes",
    )
    missing = sorted(set(required) - set(rates.columns))
    if missing:
        raise ValueError(f"Section 31 schedule missing columns: {missing}")
    rates = rates.loc[:, required].copy()
    rates["effective_from"] = pd.to_datetime(
        rates["effective_from"], errors="coerce").dt.normalize()
    rates["effective_to"] = pd.to_datetime(
        rates["effective_to"], errors="coerce").dt.normalize()
    rates["rate_per_million"] = pd.to_numeric(
        rates["rate_per_million"], errors="coerce")
    rates["rate_decimal"] = pd.to_numeric(
        rates["rate_decimal"], errors="coerce")
    rates = rates.sort_values("effective_from").reset_index(drop=True)
    if rates.empty or rates["effective_from"].isna().any():
        raise ValueError("Section 31 schedule has no rows or an invalid start date")
    if rates["effective_from"].duplicated().any():
        raise ValueError("Section 31 schedule has duplicate effective_from dates")
    if rates["effective_to"].iloc[:-1].isna().any():
        raise ValueError("only the last Section 31 interval may be open-ended")
    finite_rates = rates[["rate_per_million", "rate_decimal"]].to_numpy()
    if not np.isfinite(finite_rates).all() or (finite_rates < 0).any():
        raise ValueError("Section 31 schedule rates must be finite and nonnegative")
    expected_decimal = rates["rate_per_million"] / 1_000_000.0
    if not np.allclose(rates["rate_decimal"], expected_decimal, rtol=0, atol=1e-15):
        raise ValueError("Section 31 decimal rates do not match rate_per_million")
    closed = rates["effective_to"].notna()
    if (rates.loc[closed, "effective_to"]
            < rates.loc[closed, "effective_from"]).any():
        raise ValueError("Section 31 schedule contains a reversed interval")
    for i in range(len(rates) - 1):
        expected_next = rates.loc[i, "effective_to"] + pd.Timedelta(days=1)
        if rates.loc[i + 1, "effective_from"] != expected_next:
            raise ValueError(
                "Section 31 schedule has a gap or overlap between "
                f"{rates.loc[i, 'effective_from'].date()} and "
                f"{rates.loc[i + 1, 'effective_from'].date()}")
    if not rates["source_url"].str.startswith("https://www.sec.gov/").all():
        raise ValueError("every Section 31 interval must cite an SEC HTTPS URL")
    rates.attrs["path"] = str(path.resolve())
    rates.attrs["sha256"] = _sha(path)
    return rates


def section31_rates_for_sessions(
        schedule: pd.DataFrame, sessions: pd.DatetimeIndex) -> pd.Series:
    """Map each session to its effective Section 31 decimal rate."""
    sessions = pd.DatetimeIndex(sessions).tz_localize(None).normalize()
    out = pd.Series(np.nan, index=sessions, dtype=float,
                    name="section31_rate_decimal")
    for row in schedule.itertuples(index=False):
        end = row.effective_to
        mask = sessions >= row.effective_from
        if pd.notna(end):
            mask &= sessions <= end
        out.loc[mask] = float(row.rate_decimal)
    missing = out.index[out.isna()]
    if len(missing):
        sample = ", ".join(str(day.date()) for day in missing[:5])
        raise ValueError(
            f"Section 31 schedule misses {len(missing)} sessions (first: {sample})")
    return out


def _git() -> dict:
    try:
        root = ENGINE_PATH.parent
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5)
        if commit.returncode != 0:
            return {"commit": None, "dirty": None}
        return {"commit": commit.stdout.strip(),
                "dirty": bool(dirty.stdout.strip())}
    except Exception:
        return {"commit": None, "dirty": None}


def _load_bundle_manifest(
        bundle_dir: Path) -> tuple[dict, dict, dict[str, Path | None]]:
    """Resolve either a pipeline run or the immutable data-release layout."""
    run_manifest_path = bundle_dir / "reports" / "data_manifest.json"
    if run_manifest_path.exists():
        manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
        dividend_manifest = manifest.get("dividends")
        paths: dict[str, Path | None] = {
            "clean": bundle_dir / manifest["outputs"]["clean"]["path"],
            "vmin": bundle_dir / "feature_validity_minute.parquet",
            "vsess": bundle_dir / "feature_validity_session.parquet",
            "dividends": (bundle_dir / dividend_manifest["output"]
                          if dividend_manifest is not None else None),
        }
        bundle_meta = {
            "type": "pipeline_run",
            "run_id": manifest.get("run_id"),
            "manifest_path": str(run_manifest_path),
            "manifest_sha256": _sha(run_manifest_path),
        }
        return manifest, bundle_meta, paths

    release_manifest_path = bundle_dir / "data_manifest.json"
    source_manifest_path = bundle_dir / "source_run_manifest.json"
    if not release_manifest_path.exists() or not source_manifest_path.exists():
        raise FileNotFoundError(
            f"{bundle_dir} is neither a pipeline run nor an immutable data release.")

    release = json.loads(release_manifest_path.read_text(encoding="utf-8"))
    success_path = bundle_dir / "_SUCCESS"
    success = json.loads(success_path.read_text(encoding="utf-8"))
    release_manifest_sha = _sha(release_manifest_path)
    if success.get("data_manifest_sha256") != release_manifest_sha:
        raise RuntimeError(
            f"{bundle_dir} release manifest does not match its _SUCCESS marker.")
    if success.get("release_id") != release.get("release_id"):
        raise RuntimeError(
            f"{bundle_dir} release ID does not match its _SUCCESS marker.")

    source_file = (release.get("files") or {}).get("source_run_manifest.json", {})
    expected_source_sha = source_file.get("sha256")
    if expected_source_sha and _sha(source_manifest_path) != expected_source_sha:
        raise RuntimeError(
            f"{bundle_dir} source run manifest does not match the release manifest.")

    manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    paths = {
        "clean": bundle_dir / "clean.parquet",
        "vmin": bundle_dir / "feature_validity_minute.parquet",
        "vsess": bundle_dir / "feature_validity_session.parquet",
        "dividends": (bundle_dir / "spy_dividends_clean.csv"
                      if manifest.get("dividends") is not None else None),
    }
    bundle_meta = {
        "type": "immutable_release",
        "release_id": release.get("release_id"),
        "release_manifest_path": str(release_manifest_path),
        "release_manifest_sha256": release_manifest_sha,
        "source_run_id": release.get("source_run_id"),
        "verified_against_run_id": release.get("verified_against_run_id"),
        "git_commit": release.get("git_commit"),
    }
    return manifest, bundle_meta, paths


def load_run(run_dir: str | Path, cfg: Cfg) -> dict:
    run_dir = Path(run_dir)
    if not (run_dir / "_SUCCESS").exists():
        raise RuntimeError(f"{run_dir} has no _SUCCESS marker.")
    man, bundle_meta, paths = _load_bundle_manifest(run_dir)

    if cfg.require_config_match:
        dc = man.get("default_config", {})
        for k in ("trade_freq", "sigma_window"):
            if dc.get(k) is not None and dc[k] != getattr(cfg, k):
                raise RuntimeError(
                    f"--require-config-match: data layer built with {k}={dc[k]}, "
                    f"engine is running {k}={getattr(cfg, k)}.")

    div, div_meta = None, {"loaded": False}
    dman = man.get("dividends")
    if cfg.ignore_dividends:
        div_meta = {"loaded": False, "ignored_by_config": True,
                    "file_present_in_run": dman is not None}
    elif dman is not None:
        dpath = paths["dividends"]
        if dpath is None:
            raise RuntimeError("Dividend manifest exists but its file path is absent.")
        raw = pd.read_csv(dpath, parse_dates=["ex_date"])
        div = raw.set_index(pd.DatetimeIndex(raw["ex_date"]).normalize())["cash_amount"]
        div_meta = {"loaded": True, "path": str(dpath), "sha256": _sha(dpath),
                    "event_count": int(len(div)),
                    "first_ex_date": str(div.index.min().date()),
                    "last_ex_date": str(div.index.max().date())}
    elif not cfg.ignore_dividends and cfg.require_dividends:
        raise RuntimeError(
            "This run carries no cleaned dividend file, but the profile requires "
            "dividend-adjusted previous closes. Rerun the data pipeline with "
            "--dividends, or set Cfg(ignore_dividends=True) and disclose it.")

    return {"manifest": man, "run_dir": str(run_dir),
            "data_bundle": bundle_meta,
            "bars": pd.read_parquet(paths["clean"]),
            "vmin": pd.read_parquet(paths["vmin"]),
            "vsess": pd.read_parquet(paths["vsess"]),
            "dividends": div, "dividend_meta": div_meta}


# --------------------------------------------------------------------------- #
# features (full exchange calendar)
# --------------------------------------------------------------------------- #

def build_features(bars: pd.DataFrame, vmin: pd.DataFrame, vsess: pd.DataFrame,
                   cfg: Cfg, dividends: pd.Series | None
                   ) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = bars.sort_values(["session_date", "minute_of_session"]).copy()

    v = vmin.set_index(["session_date", "minute_of_session"])
    key = pd.MultiIndex.from_arrays([d["session_date"], d["minute_of_session"]])
    for c in ["is_halt_minute", "is_executable_minute", "bar_present",
              "move_open_obs_valid", "vwap_valid"]:
        d[c] = v[c].reindex(key).fillna(False).to_numpy()

    # Halt bars, if the vendor supplies them, carry no real volume and must not
    # enter the cumulative VWAP or the mark-to-market path.
    live = ~d["is_halt_minute"].to_numpy()
    g = d.groupby("session_date", sort=False)
    d["open_day"] = g["open"].transform("first")
    d["move_open"] = (d["close"] / d["open_day"] - 1).abs()

    if cfg.vwap_source == "hlc3":
        px = (d["high"] + d["low"] + d["close"]) / 3.0
    elif cfg.vwap_source == "ohlc4":
        px = (d["open"] + d["high"] + d["low"] + d["close"]) / 4.0
    elif cfg.vwap_source == "vendor_bar_vwap":
        if "vendor_bar_vwap" not in d.columns:
            raise RuntimeError("vwap_source='vendor_bar_vwap' but the column is absent.")
        px = d["vendor_bar_vwap"]
    else:
        raise ValueError(f"unknown vwap_source {cfg.vwap_source!r}")
    live_s = pd.Series(live, index=d.index)
    d["_vol_live"] = d["volume"].where(live_s, 0.0)
    d["_pv"] = d["_vol_live"] * px
    gg = d.groupby("session_date", sort=False)
    d["vwap"] = gg["_pv"].cumsum() / gg["_vol_live"].cumsum()

    minp = cfg.sigma_min_periods or cfg.sigma_window
    key2 = pd.MultiIndex.from_arrays([d["session_date"], d["minute_of_session"]])

    if cfg.strict_eligible_rolling:
        # Roll on the FULL scheduled grid so a session that is merely missing
        # this minute still consumes a slot. Rolling on existing rows silently
        # reaches one session further back and turns "previous 14 eligible
        # sessions" into "previous 14 rows that happen to exist".
        grid = vmin[["session_date", "minute_of_session",
                     "move_open_obs_valid"]].copy()
        grid = grid.merge(d[["session_date", "minute_of_session", "move_open"]],
                          on=["session_date", "minute_of_session"], how="left")
        grid["mo"] = grid["move_open"].where(grid["move_open_obs_valid"])
        grid = grid.sort_values(["minute_of_session", "session_date"])
        gm = grid.groupby("minute_of_session", sort=False)["mo"]
        grid["sigma_open"] = gm.transform(
            lambda x: x.rolling(cfg.sigma_window, min_periods=minp).mean().shift(1))
        grid["sigma_hist_n"] = gm.transform(
            lambda x: x.notna().rolling(cfg.sigma_window, min_periods=1).sum().shift(1))
        gi = grid.set_index(["session_date", "minute_of_session"])
        d["sigma_open"] = gi["sigma_open"].reindex(key2).to_numpy()
        d["sigma_hist_n"] = gi["sigma_hist_n"].reindex(key2).fillna(0).to_numpy()
    else:
        mo = d["move_open"].where(d["move_open_obs_valid"])
        roll = mo.groupby(d["minute_of_session"], sort=False).transform(
            lambda x: x.rolling(cfg.sigma_window, min_periods=minp).mean())
        d["sigma_open"] = roll.groupby(d["minute_of_session"]).shift(1)
        d["sigma_hist_n"] = (d["move_open_obs_valid"].astype("int64")
                             .groupby(d["minute_of_session"], sort=False)
                             .transform(lambda x: x.shift(1)
                                        .rolling(cfg.sigma_window, min_periods=1).sum())
                             .fillna(0))
    d["sigma_history_valid"] = (d["sigma_hist_n"] >= minp) & d["sigma_open"].notna()

    live_close = d["close"].where(live_s)
    #母表来自交易日历，不是"实际存在的行"。整日缺失的 session 必须保留一行 NaN
    # 并占据 rolling slot, otherwise a 14-session window silently reaches
    # further back the moment a whole day is missing.
    daily = pd.DataFrame(index=pd.Index(
        pd.to_datetime(vsess["session_date"]).sort_values().unique(),
        name="session_date"))
    daily["open"] = g["open"].first().reindex(daily.index)
    daily["close"] = live_close.groupby(d["session_date"]).last().reindex(daily.index)
    daily["ret"] = daily["close"].pct_change()
    vs = vsess.set_index("session_date")
    daily["close_valid"] = daily.index.map(vs["close_valid"]).fillna(False)
    daily["ret_valid"] = daily.index.map(vs["daily_ret_valid"]).fillna(False)
    # An invalid close corrupts two daily returns. Letting them into the vol
    # window changes the leverage. Strictly: if any of the previous
    # sigma_window eligible returns is invalid, dvol is invalid -- we do not
    # reach further back to make up the count.
    ret_for_vol = (daily["ret"].where(daily["ret_valid"])
                   if cfg.respect_return_validity else daily["ret"])
    daily["dvol"] = (ret_for_vol.rolling(cfg.sigma_window,
                                         min_periods=cfg.sigma_window).std()
                     .shift(cfg.dvol_lag))
    daily["dvol_n"] = (ret_for_vol.notna().rolling(cfg.sigma_window).sum()
                       .shift(cfg.dvol_lag).fillna(0))
    daily["prev_close"] = daily["close"].shift(1)
    daily["dividend"] = (dividends.reindex(daily.index).fillna(0.0)
                         if dividends is not None else 0.0)
    daily["prev_close_adj"] = daily["prev_close"] - daily["dividend"]
    return d, daily


def config_validity(d: pd.DataFrame, daily: pd.DataFrame, vsess: pd.DataFrame,
                    cfg: Cfg) -> pd.Series:
    """Rebuild the decision mask from primitives at THIS configuration."""
    vs = vsess.set_index("session_date")
    prev_close_valid = d["session_date"].map(vs["prev_close_valid"]).to_numpy()

    ok = (d["minute_of_session"].mod(cfg.trade_freq).eq(0).to_numpy()
          & d["move_open_obs_valid"].to_numpy()
          & d["sigma_history_valid"].to_numpy()
          & d["is_executable_minute"].to_numpy()
          & prev_close_valid)
    if cfg.use_vwap:
        ok &= d["vwap_valid"].to_numpy()
    if cfg.sizing == "vol_target" and cfg.nanvol_action == "skip":
        need = d["session_date"].map(daily["dvol_n"] >= cfg.sigma_window).to_numpy()
        ok &= need
    return pd.Series(ok, index=d.index)


# --------------------------------------------------------------------------- #
# order / fill state machine
# --------------------------------------------------------------------------- #

def _session_pnl(g: pd.DataFrame, signal: np.ndarray, admissible: np.ndarray,
                 shares: int, cfg: Cfg, equity: float | None = None,
                 eod_execution: dict | None = None,
                 section31_rate_decimal: float = 0.0) -> dict:
    """One session, as an order/fill state machine.

    Marking: P&L accrues to the position held *before* a fill, marked from the
    last mark price to the fill price. A position carried into a halt earns the
    reopening gap; an order that could not fill before it earns nothing.

    Quantities: a reversal is two units, not one. 0 -> +1 -> -1 -> 0 is four
    traded units on 3 fill events, and cost is charged on units.
    """
    close = g["close"].to_numpy()
    open_ = g["open"].to_numpy()
    execable = g["is_executable_minute"].to_numpy()
    minute = g["minute_of_session"].to_numpy()
    last_scheduled = int(g["calendar_bars"].iloc[0])

    pos = 0.0
    last_mark = np.nan
    gross = 0.0
    fill_events = 0
    trade_units = 0.0
    shares_traded = 0.0
    traded_notional = 0.0
    commission = 0.0
    ibkr_commission = 0.0
    section31_fee = 0.0
    slippage = 0.0
    pending: float | None = None
    pending_due: int | None = None
    pending_order: dict | None = None
    last_mark_clock = np.nan
    holding_minutes = 0.0
    long_notional_minute_dollars = 0.0
    short_notional_minute_dollars = 0.0
    borrowed_cash_minute_dollars = 0.0
    scheduled_minutes = float(last_scheduled)
    positive_cash_minute_dollars = (
        float(equity) * scheduled_minutes if equity is not None else 0.0)
    signals: list[dict] = []
    orders: list[dict] = []
    fills: list[dict] = []
    round_trips: list[dict] = []
    active_trip: dict | None = None

    def mark_to(price: float, clock: float) -> None:
        nonlocal gross, last_mark, last_mark_clock, holding_minutes
        nonlocal long_notional_minute_dollars, short_notional_minute_dollars
        nonlocal positive_cash_minute_dollars, borrowed_cash_minute_dollars
        nonlocal active_trip
        if pos != 0.0 and np.isfinite(last_mark):
            pnl = pos * (price - last_mark) * shares
            gross += pnl
            elapsed = max(float(clock - last_mark_clock), 0.0)
            signed_notional = pos * last_mark * shares
            holding_minutes += elapsed
            if signed_notional > 0:
                long_notional_minute_dollars += signed_notional * elapsed
            else:
                short_notional_minute_dollars += -signed_notional * elapsed
            if equity is not None:
                # The default flat-state cash integral was seeded above.
                positive_cash_minute_dollars -= float(equity) * elapsed
                cash = float(equity) - signed_notional
                positive_cash_minute_dollars += max(cash, 0.0) * elapsed
                borrowed_cash_minute_dollars += max(-cash, 0.0) * elapsed
            if active_trip is not None:
                active_trip["gross"] += pnl
                active_trip["holding_minutes"] += elapsed
        last_mark = price
        last_mark_clock = clock

    def execute(target: float, price: float, minute_value: int, clock: float,
                order: dict, reason: str,
                extra_slippage_per_share: float = 0.0) -> None:
        nonlocal pos, fill_events, trade_units, shares_traded, traded_notional
        nonlocal commission, ibkr_commission, section31_fee, slippage
        nonlocal active_trip
        delta = abs(target - pos)
        if delta == 0.0:
            order["status"] = "no_change"
            return
        mark_to(price, clock)
        previous_pos = pos
        qty = delta * shares
        n_orders = 2 if (delta > 1.0 and cfg.reversal_order_model == "two_orders") else 1
        fill_commission = n_orders * max(
            cfg.min_comm, cfg.comm_per_share * qty / n_orders)
        sell_qty = max((previous_pos - target) * shares, 0.0)
        sell_notional = sell_qty * price
        fill_section31 = sell_notional * section31_rate_decimal
        fill_explicit_cost = fill_commission + fill_section31
        extra_slippage = extra_slippage_per_share * qty
        fill_slippage = cfg.slip_per_share * qty + extra_slippage
        ibkr_commission += fill_commission
        section31_fee += fill_section31
        # Backward-compatible aggregate: downstream code historically calls
        # this column "commission". Under the opt-in model it means all
        # explicit costs currently implemented (IBKR base + Section 31).
        commission += fill_explicit_cost
        slippage += fill_slippage
        if active_trip is not None and previous_pos != target:
            active_trip.update({
                "exit_minute": int(minute_value),
                "exit_price": float(price),
                "exit_reason": reason,
            })
            round_trips.append(active_trip)
            active_trip = None
        pos = target
        fill_events += 1
        trade_units += delta
        shares_traded += qty
        traded_notional += qty * price
        order.update({
            "status": "filled",
            "fill_minute": int(minute_value),
            "fill_price": float(price),
        })
        fills.append({
            "order_id": order["order_id"],
            "minute": int(minute_value),
            "price": float(price),
            "from_position": float(previous_pos),
            "to_position": float(target),
            "trade_units": float(delta),
            "shares": float(qty),
            "buy_shares": float(max((target - previous_pos) * shares, 0.0)),
            "sell_shares": float(sell_qty),
            "sell_notional": float(sell_notional),
            "order_count": int(n_orders),
            "ibkr_commission": float(fill_commission),
            "section31_rate_decimal": float(section31_rate_decimal),
            "section31_fee": float(fill_section31),
            "total_explicit_cost": float(fill_explicit_cost),
            "commission": float(fill_explicit_cost),
            "slippage": float(fill_slippage),
            "extra_eod_cost": float(extra_slippage),
            "market_impact": 0.0,
            "total_execution_cost": float(fill_explicit_cost + fill_slippage),
            "reason": reason,
        })
        if target != 0.0:
            active_trip = {
                "direction": "long" if target > 0 else "short",
                "entry_minute": int(minute_value),
                "entry_price": float(price),
                "shares": int(shares),
                "gross": 0.0,
                "holding_minutes": 0.0,
            }

    def new_order(target: float, submit_minute: int, due_minute: int,
                  reason: str) -> dict:
        order = {
            "order_id": len(orders) + 1,
            "submit_minute": int(submit_minute),
            "due_minute": int(due_minute),
            "target_position": float(target),
            "reason": reason,
            "status": "pending",
        }
        orders.append(order)
        return order

    for j in range(len(g)):
        if not execable[j]:
            continue

        if pending is not None:
            if minute[j] < pending_due:
                pass                                   # not due yet
            elif minute[j] == pending_due:
                execute(pending, open_[j], int(minute[j]),
                        float(minute[j] - 1), pending_order, "scheduled_fill")
                pending, pending_due, pending_order = None, None, None
            elif cfg.pending_order_policy == "cancel_if_next_unavailable":
                pending_order.update({
                    "status": "cancelled",
                    "cancel_minute": int(minute[j]),
                    "cancel_reason": "intended_minute_unavailable",
                })
                pending, pending_due, pending_order = None, None, None
            else:
                execute(pending, open_[j], int(minute[j]),
                        float(minute[j] - 1), pending_order,
                        "queued_until_executable")
                pending, pending_due, pending_order = None, None, None

        mark_to(close[j], float(minute[j]))

        # No new entry on the final scheduled bar: it would be liquidated at the
        # same price a moment later, producing zero P&L and two charges. The
        # notebook avoids this only as a side effect of its row shift.
        if admissible[j]:
            signals.append({
                "minute": int(minute[j]),
                "signal": float(signal[j]),
                "position_before_order": float(pos),
                "is_final_scheduled_bar": bool(minute[j] >= last_scheduled),
            })
            if minute[j] < last_scheduled and signal[j] != pos:
                if cfg.fill_price == "signal_bar_close":
                    order = new_order(signal[j], int(minute[j]), int(minute[j]),
                                      "signal")
                    execute(signal[j], close[j], int(minute[j]),
                            float(minute[j]), order, "signal_bar_close")
                else:
                    pending = signal[j]
                    pending_due = int(minute[j] + cfg.exec_lag_minutes)
                    pending_order = new_order(
                        pending, int(minute[j]), pending_due, "signal")

    exec_idx = np.where(execable)[0]
    tail_ok = bool(len(exec_idx)) and int(minute[exec_idx[-1]]) == last_scheduled
    exposed_to_unknown_tail = (not tail_ok) and pos != 0.0
    if pending_order is not None:
        pending_order.update({
            "status": "cancelled",
            "cancel_minute": (int(minute[exec_idx[-1]]) if len(exec_idx) else None),
            "cancel_reason": "end_of_session",
        })
    if pos != 0.0 and len(exec_idx):
        exit_minute = int(minute[exec_idx[-1]])
        exit_price = float(close[exec_idx[-1]])
        extra_cost_per_share = 0.0
        if eod_execution is not None:
            exit_price = float(eod_execution["price"])
            extra_cost_per_share = float(
                eod_execution.get("extra_cost_per_share", 0.0))
            if not np.isfinite(exit_price) or exit_price <= 0:
                raise ValueError(
                    f"invalid EOD execution price {exit_price!r}")
            if (not np.isfinite(extra_cost_per_share)
                    or extra_cost_per_share < 0):
                raise ValueError(
                    "EOD extra_cost_per_share must be finite and nonnegative")
        order = new_order(0.0, exit_minute, exit_minute, "end_of_session")
        execute(0.0, exit_price, exit_minute, float(exit_minute),
                order, "end_of_session", extra_cost_per_share)
    eod_fills = [fill for fill in fills if fill["reason"] == "end_of_session"]
    return {"gross": gross, "commission": commission,
            "ibkr_commission": ibkr_commission,
            "section31_fee": section31_fee,
            "total_explicit_cost": commission,
            "slippage": slippage,
            "market_impact": 0.0,
            "total_execution_cost": commission + slippage,
            "fill_events": float(fill_events), "trade_units": trade_units,
            "shares_traded": shares_traded,
            "traded_notional": float(traded_notional),
            "long_gross": float(sum(
                trip["gross"] for trip in round_trips
                if trip["direction"] == "long")),
            "short_gross": float(sum(
                trip["gross"] for trip in round_trips
                if trip["direction"] == "short")),
            "holding_minutes": float(holding_minutes),
            "positive_cash_minute_dollars": float(positive_cash_minute_dollars),
            "borrowed_cash_minute_dollars": float(borrowed_cash_minute_dollars),
            "long_notional_minute_dollars":
                float(long_notional_minute_dollars),
            "short_notional_minute_dollars":
                float(short_notional_minute_dollars),
            "scheduled_minutes": scheduled_minutes,
            "eod_exit_fills": float(len(eod_fills)),
            "eod_exit_notional": float(sum(
                fill["shares"] * fill["price"] for fill in eod_fills)),
            "eod_extra_cost": float(sum(
                fill.get("extra_eod_cost", 0.0) for fill in eod_fills)),
            "exit_at_scheduled_close": tail_ok,
            "exposed_to_unknown_tail": bool(exposed_to_unknown_tail),
            "signals": signals, "orders": orders, "fills": fills,
            "round_trips": round_trips}


def _session_pnl_notebook(g: pd.DataFrame, signal: np.ndarray,
                          admissible: np.ndarray, shares: int,
                          cfg: Cfg | None = None,
                          equity: float | None = None,
                          section31_rate_decimal: float = 0.0) -> dict:
    """Bit-compatible with the published notebook: exposure shifted one *data
    row*, PnL = exposure * close.diff(). Retained only for parity; it is the
    formula that hands a reopening gap to an unfilled order."""
    close = g["close"].to_numpy()
    pos = np.zeros(len(g))
    cur = 0.0
    for j in range(len(g)):
        if admissible[j]:
            cur = signal[j]
        pos[j] = cur
    exposure = np.concatenate([[0.0], pos[:-1]])
    units = float(np.abs(np.diff(np.append(exposure, 0.0))).sum())
    gross = float(np.nansum(exposure * np.diff(close, prepend=np.nan))) * shares
    pnl_path = exposure * np.diff(close, prepend=np.nan) * shares
    held = exposure != 0.0
    signed_notional = exposure * close * shares
    eq = float(equity) if equity is not None else 0.0
    cash = eq - signed_notional
    cfg = cfg or profile_cfg("official_sample_compatible")
    minute = g["minute_of_session"].to_numpy()
    last_scheduled = int(g["calendar_bars"].iloc[0])
    signals = [{
        "minute": int(minute[j]),
        "signal": float(signal[j]),
        "position_before_order": float(pos[j - 1] if j else 0.0),
        "is_final_scheduled_bar": bool(minute[j] >= last_scheduled),
    } for j in range(len(g)) if admissible[j]]
    orders: list[dict] = []
    fills: list[dict] = []
    round_trips: list[dict] = []
    ledger_pos = 0.0
    active_trip: dict | None = None
    for j in range(len(g)):
        if active_trip is not None:
            active_trip["gross"] += float(np.nan_to_num(pnl_path[j]))
            active_trip["holding_minutes"] += float(held[j])
        if not admissible[j] or minute[j] >= last_scheduled:
            continue
        target = float(signal[j])
        if target == ledger_pos:
            continue
        delta = abs(target - ledger_pos)
        qty = delta * shares
        n_orders = (
            2 if delta > 1.0 and cfg.reversal_order_model == "two_orders" else 1)
        fill_commission = n_orders * max(
            cfg.min_comm, cfg.comm_per_share * qty / n_orders)
        sell_qty = max((ledger_pos - target) * shares, 0.0)
        sell_notional = sell_qty * close[j]
        fill_section31 = sell_notional * section31_rate_decimal
        fill_explicit_cost = fill_commission + fill_section31
        fill_slippage = cfg.slip_per_share * qty
        order_id = len(orders) + 1
        orders.append({
            "order_id": order_id,
            "submit_minute": int(minute[j]),
            "due_minute": int(minute[j]),
            "target_position": target,
            "reason": "notebook_shifted_close",
            "status": "filled",
            "fill_minute": int(minute[j]),
            "fill_price": float(close[j]),
        })
        fills.append({
            "order_id": order_id,
            "minute": int(minute[j]),
            "price": float(close[j]),
            "from_position": float(ledger_pos),
            "to_position": target,
            "trade_units": float(delta),
            "shares": float(qty),
            "buy_shares": float(max((target - ledger_pos) * shares, 0.0)),
            "sell_shares": float(sell_qty),
            "sell_notional": float(sell_notional),
            "order_count": int(n_orders),
            "ibkr_commission": float(fill_commission),
            "section31_rate_decimal": float(section31_rate_decimal),
            "section31_fee": float(fill_section31),
            "total_explicit_cost": float(fill_explicit_cost),
            "commission": float(fill_explicit_cost),
            "slippage": float(fill_slippage),
            "market_impact": 0.0,
            "total_execution_cost": float(fill_explicit_cost + fill_slippage),
            "reason": "notebook_shifted_close",
        })
        if active_trip is not None:
            active_trip.update({
                "exit_minute": int(minute[j]),
                "exit_price": float(close[j]),
                "exit_reason": "notebook_shifted_close",
            })
            round_trips.append(active_trip)
            active_trip = None
        ledger_pos = target
        if target != 0.0:
            active_trip = {
                "direction": "long" if target > 0 else "short",
                "entry_minute": int(minute[j]),
                "entry_price": float(close[j]),
                "shares": int(shares),
                "gross": 0.0,
                "holding_minutes": 0.0,
            }
    if ledger_pos != 0.0:
        delta = abs(ledger_pos)
        qty = delta * shares
        fill_commission = max(cfg.min_comm, cfg.comm_per_share * qty)
        sell_qty = max(ledger_pos * shares, 0.0)
        sell_notional = sell_qty * close[-1]
        fill_section31 = sell_notional * section31_rate_decimal
        fill_explicit_cost = fill_commission + fill_section31
        fill_slippage = cfg.slip_per_share * qty
        order_id = len(orders) + 1
        orders.append({
            "order_id": order_id,
            "submit_minute": int(minute[-1]),
            "due_minute": int(minute[-1]),
            "target_position": 0.0,
            "reason": "end_of_session",
            "status": "filled",
            "fill_minute": int(minute[-1]),
            "fill_price": float(close[-1]),
        })
        fills.append({
            "order_id": order_id,
            "minute": int(minute[-1]),
            "price": float(close[-1]),
            "from_position": float(ledger_pos),
            "to_position": 0.0,
            "trade_units": float(delta),
            "shares": float(qty),
            "buy_shares": float(max(-ledger_pos * shares, 0.0)),
            "sell_shares": float(sell_qty),
            "sell_notional": float(sell_notional),
            "order_count": 1,
            "ibkr_commission": float(fill_commission),
            "section31_rate_decimal": float(section31_rate_decimal),
            "section31_fee": float(fill_section31),
            "total_explicit_cost": float(fill_explicit_cost),
            "commission": float(fill_explicit_cost),
            "slippage": float(fill_slippage),
            "market_impact": 0.0,
            "total_execution_cost": float(fill_explicit_cost + fill_slippage),
            "reason": "end_of_session",
        })
        if active_trip is not None:
            active_trip.update({
                "exit_minute": int(minute[-1]),
                "exit_price": float(close[-1]),
                "exit_reason": "end_of_session",
            })
            round_trips.append(active_trip)
    ledger_units = float(sum(fill["trade_units"] for fill in fills))
    if abs(ledger_units - units) > 1e-9:
        raise RuntimeError(
            f"notebook ledger units {ledger_units} != accounting units {units}")
    return {"gross": gross, "commission": None,
            "ibkr_commission": None,
            "section31_fee": float(sum(
                fill["section31_fee"] for fill in fills)),
            "total_explicit_cost": None,
            "slippage": None, "market_impact": 0.0,
            "total_execution_cost": None,
            "fill_events": units, "trade_units": units,
            "shares_traded": units * shares,
            "traded_notional": float(sum(
                fill["shares"] * fill["price"] for fill in fills)),
            "long_gross": float(np.nansum(pnl_path[exposure > 0])),
            "short_gross": float(np.nansum(pnl_path[exposure < 0])),
            "holding_minutes": float(held.sum()),
            "positive_cash_minute_dollars": float(np.maximum(cash, 0.0).sum()),
            "borrowed_cash_minute_dollars": float(np.maximum(-cash, 0.0).sum()),
            "long_notional_minute_dollars":
                float(np.maximum(signed_notional, 0.0).sum()),
            "short_notional_minute_dollars":
                float(np.maximum(-signed_notional, 0.0).sum()),
            "scheduled_minutes": float(g["calendar_bars"].iloc[0]),
            "exit_at_scheduled_close": True, "exposed_to_unknown_tail": False,
            "signals": signals, "orders": orders, "fills": fills,
            "round_trips": round_trips}


# --------------------------------------------------------------------------- #
# backtest
# --------------------------------------------------------------------------- #

def backtest(
        data: dict, cfg: Cfg, collect_ledger: bool = False,
        financing_rates: pd.DataFrame | None = None,
        eod_execution: pd.DataFrame | None = None,
        section31_rates: pd.DataFrame | None = None) -> pd.DataFrame:
    validate(cfg)
    bars, vmin, vsess = data["bars"], data["vmin"], data["vsess"]
    d, daily = build_features(bars, vmin, vsess, cfg, data["dividends"])
    admissible_all = config_validity(d, daily, vsess, cfg)
    rate_columns = (
        "cash_rate_annual", "funding_rate_annual", "borrow_rate_annual")
    rates = None
    if financing_rates is not None:
        rates = financing_rates.copy()
        if "session_date" in rates.columns:
            rates["session_date"] = pd.to_datetime(
                rates["session_date"]).dt.normalize()
            rates = rates.set_index("session_date")
        rates.index = pd.DatetimeIndex(rates.index).tz_localize(None).normalize()
        if rates.index.duplicated().any():
            raise ValueError("financing rates contain duplicate session dates")
        missing_columns = sorted(set(rate_columns) - set(rates.columns))
        if missing_columns:
            raise ValueError(
                f"financing rates missing columns: {missing_columns}")
        rates = rates.loc[:, rate_columns].apply(
            pd.to_numeric, errors="coerce").reindex(daily.index)
        missing_sessions = rates.index[rates.isna().any(axis=1)]
        if len(missing_sessions):
            sample = ", ".join(
                str(day.date()) for day in missing_sessions[:5])
            raise ValueError(
                f"financing rates missing {len(missing_sessions)} sessions "
                f"(first: {sample})")
        if not np.isfinite(rates.to_numpy()).all():
            raise ValueError("financing rates contain non-finite values")

    if cfg.explicit_cost_model == "legacy":
        if section31_rates is not None:
            raise ValueError(
                "section31_rates were supplied but explicit_cost_model is legacy")
        section31_daily = None
    else:
        if section31_rates is None:
            raise ValueError(
                "legacy_plus_section31 requires an explicit Section 31 schedule")
        section31_daily = section31_rates_for_sessions(
            section31_rates, daily.index)

    eod = None
    if eod_execution is not None:
        eod = eod_execution.copy()
        if "session_date" in eod.columns:
            eod["session_date"] = pd.to_datetime(
                eod["session_date"]).dt.normalize()
            eod = eod.set_index("session_date")
        eod.index = pd.DatetimeIndex(eod.index).tz_localize(None).normalize()
        if eod.index.duplicated().any():
            raise ValueError("EOD execution input contains duplicate sessions")
        if "price" not in eod.columns:
            raise ValueError("EOD execution input must contain a price column")
        if "extra_cost_per_share" not in eod.columns:
            eod["extra_cost_per_share"] = 0.0
        eod = eod[["price", "extra_cost_per_share"]].apply(
            pd.to_numeric, errors="coerce")
        invalid = (
            ~np.isfinite(eod["price"]) | (eod["price"] <= 0)
            | ~np.isfinite(eod["extra_cost_per_share"])
            | (eod["extra_cost_per_share"] < 0)
        )
        if invalid.any():
            raise ValueError(
                f"EOD execution input has {int(invalid.sum())} invalid rows")

    tier_col = {"paper_ready": "is_paper_ready", "halt_aware": "is_halt_usable",
                "exploratory": "is_exploratory"}[cfg.tier]
    tradable = set(bars.loc[bars[tier_col], "session_date"].unique())
    vs = vsess.set_index("session_date")

    rows = []
    aum = cfg.aum0
    groups = dict(tuple(d.groupby("session_date", sort=False)))
    terminated_from = None
    ledgers: dict[str, list[dict]] = {
        "signals": [], "orders": [], "fills": [], "round_trips": []}
    previous_day = None
    # Drive the loop from the exchange calendar, so a session with no bars at
    # all still produces a row instead of vanishing from the time axis.
    for day in daily.index:
        prev_aum = aum
        status, gross, cost = "active", 0.0, 0.0
        fill_events = units = traded_shares = 0.0
        commission = ibkr_commission = section31_fee = 0.0
        slippage = cash_interest = financing = 0.0
        holding_minutes = 0.0
        positive_cash_integral = borrowed_cash_integral = 0.0
        long_notional_integral = short_notional_integral = 0.0
        known_partial_gross = known_partial_commission = 0.0
        known_partial_ibkr_commission = known_partial_section31_fee = 0.0
        known_partial_slippage = known_partial_cash_interest = 0.0
        known_partial_financing = 0.0
        signal_count = 0
        traded_notional = long_gross = short_gross = 0.0
        eod_exit_fills = eod_exit_notional = eod_extra_cost = 0.0
        row = daily.loc[day]
        g = groups.get(day)
        res = None
        section31_rate = (
            float(section31_daily.loc[day])
            if section31_daily is not None else 0.0)

        if terminated_from is not None:
            status = "after_unknown_exit"
        elif g is None or len(g) == 0:
            status = "absent_session"
        elif day not in tradable:
            status = "tier_excluded"
        elif not np.isfinite(row["prev_close_adj"]):
            status = "warmup_no_prev_close"
        else:
            sig_o = g["sigma_open"].to_numpy()
            if not np.isfinite(sig_o).any():
                status = "warmup_no_band"
            else:
                dv = row["dvol"]
                if cfg.sizing == "flat":
                    lev = cfg.flat_lev
                elif not np.isfinite(dv):
                    if cfg.nanvol_action == "skip":
                        status = "invalid_feature_dvol"
                        lev = 0.0
                    else:
                        lev = cfg.max_lev
                else:
                    lev = min(cfg.target_vol / dv, cfg.max_lev)

                if status == "active":
                    op = g["open"].iloc[0]
                    raw_shares = prev_aum / op * lev
                    if cfg.share_rounding == "round":
                        shares = int(np.round(raw_shares))
                    elif cfg.share_rounding == "floor":
                        shares = int(np.floor(raw_shares))
                    else:
                        raise ValueError(
                            f"unknown share_rounding {cfg.share_rounding!r}")
                    if shares <= 0:
                        status = "zero_size"
                    else:
                        cp = g["close"].to_numpy()
                        ub = max(op, row["prev_close_adj"]) * (1 + cfg.band_mult * sig_o)
                        lb = min(op, row["prev_close_adj"]) * (1 - cfg.band_mult * sig_o)
                        vw = g["vwap"].to_numpy()
                        s = np.zeros(len(g))
                        if cfg.use_vwap:
                            s[(cp > ub) & (cp > vw)] = 1
                            s[(cp < lb) & (cp < vw)] = -1
                        else:
                            s[cp > ub] = 1
                            s[cp < lb] = -1
                        adm = admissible_all.loc[g.index].to_numpy()
                        if cfg.profile == "official_sample_compatible":
                            res = _session_pnl_notebook(
                                g, s, adm, shares, cfg=cfg, equity=prev_aum,
                                section31_rate_decimal=section31_rate)
                        else:
                            eod_row = None
                            if eod is not None:
                                if day not in eod.index:
                                    raise ValueError(
                                        f"EOD execution input misses {day.date()}")
                                eod_row = eod.loc[day].to_dict()
                            res = _session_pnl(
                                g, s, adm, shares, cfg, equity=prev_aum,
                                eod_execution=eod_row,
                                section31_rate_decimal=section31_rate)
                        gross = res["gross"]
                        fill_events = res["fill_events"]
                        if res["commission"] is None:
                            # notebook path: charge per traded unit
                            ibkr_commission = res["trade_units"] * max(
                                cfg.min_comm, cfg.comm_per_share * shares)
                            section31_fee = res["section31_fee"]
                            commission = ibkr_commission + section31_fee
                            slippage = (res["trade_units"]
                                        * cfg.slip_per_share * shares)
                        else:
                            commission = res["commission"]
                            ibkr_commission = res["ibkr_commission"]
                            section31_fee = res["section31_fee"]
                            slippage = res["slippage"]
                        cost = commission + slippage
                        units = res["trade_units"]
                        traded_shares = res["shares_traded"]
                        traded_notional = res.get("traded_notional", 0.0)
                        long_gross = res.get("long_gross", 0.0)
                        short_gross = res.get("short_gross", 0.0)
                        eod_exit_fills = res.get("eod_exit_fills", 0.0)
                        eod_exit_notional = res.get("eod_exit_notional", 0.0)
                        eod_extra_cost = res.get("eod_extra_cost", 0.0)
                        holding_minutes = res.get("holding_minutes", 0.0)
                        positive_cash_integral = res.get(
                            "positive_cash_minute_dollars", 0.0)
                        borrowed_cash_integral = res.get(
                            "borrowed_cash_minute_dollars", 0.0)
                        long_notional_integral = res.get(
                            "long_notional_minute_dollars", 0.0)
                        short_notional_integral = res.get(
                            "short_notional_minute_dollars", 0.0)
                        signal_count = len(res.get("signals", []))
                        if collect_ledger:
                            for ledger_name in ledgers:
                                for item in res.get(ledger_name, []):
                                    ledgers[ledger_name].append(
                                        {"session_date": day, **item})
                        if units == 0:
                            status = "no_signal"
                        # Only a position still open when the tape stops has an
                        # unknown exit. Being flat before the gap is fine.
                        if res["exposed_to_unknown_tail"]:
                            status = "unknown_exit"

        # Cash account. A supplied daily curve uses the frozen ACT/360 policy;
        # scalar rates retain the historical BUS/252 behaviour. The book is
        # flat overnight. Intraday positive cash, borrowed cash and short
        # notional are accumulated separately and must never net each other.
        if rates is None:
            cash_rate = cfg.cash_rate_annual
            funding_rate = cfg.funding_rate_annual
            borrow_rate = cfg.borrow_rate_annual
            total_dcf = 1.0 / 252.0
            intraday_dcf = (
                cfg.financing_daycount_fraction / 252.0)
        else:
            cash_rate = float(rates.loc[day, "cash_rate_annual"])
            funding_rate = float(rates.loc[day, "funding_rate_annual"])
            borrow_rate = float(rates.loc[day, "borrow_rate_annual"])
            if res is not None:
                scheduled_minutes = max(
                    float(res.get("scheduled_minutes", 0.0)), 1.0)
            elif g is not None and len(g):
                scheduled_minutes = max(
                    float(g["calendar_bars"].iloc[0]), 1.0)
            else:
                scheduled_minutes = (
                    cfg.financing_daycount_fraction * 24.0 * 60.0)
            intraday_days = scheduled_minutes / (24.0 * 60.0)
            elapsed_days = (
                float((day - previous_day).days)
                if previous_day is not None else intraday_days)
            total_dcf = elapsed_days / 360.0
            intraday_dcf = min(intraday_days / 360.0, total_dcf)
        overnight_dcf = max(total_dcf - intraday_dcf, 0.0)
        cash_hurdle_ret = cash_rate * total_dcf
        if res is not None:
            scheduled = max(float(res.get("scheduled_minutes", 0.0)), 1.0)
            avg_positive_cash = positive_cash_integral / scheduled
            avg_borrowed_cash = borrowed_cash_integral / scheduled
            avg_short_notional = short_notional_integral / scheduled
            cash_interest = (
                prev_aum * cash_rate * overnight_dcf
                + avg_positive_cash * cash_rate * intraday_dcf)
            financing = -(
                avg_borrowed_cash * funding_rate * intraday_dcf
                + avg_short_notional * borrow_rate * intraday_dcf)
        else:
            cash_interest = prev_aum * cash_rate * total_dcf
        net = gross - cost + cash_interest + financing

        if status == "after_unknown_exit":
            net = 0.0
            cash_interest = 0.0
            financing = 0.0
            cash_hurdle_ret = 0.0
        if status == "unknown_exit":
            # The exit price is a guess, so this day's P&L must not silently
            # compound into every later session's position size.
            if cfg.unknown_exit_policy == "terminate":
                terminated_from = day
            elif cfg.unknown_exit_policy == "exclude_session_and_freeze_aum":
                pass                           # assumes zero P&L: disclose it
            elif cfg.unknown_exit_policy != "impute_last_observed":
                raise ValueError(
                    f"unknown unknown_exit_policy {cfg.unknown_exit_policy!r}")
            if cfg.unknown_exit_policy != "impute_last_observed":
                # Preserve the auditable known prefix in separate columns, but
                # keep the formal accounting row internally consistent with
                # the frozen-AUM assumption.
                known_partial_gross = gross
                known_partial_commission = commission
                known_partial_ibkr_commission = ibkr_commission
                known_partial_section31_fee = section31_fee
                known_partial_slippage = slippage
                known_partial_cash_interest = cash_interest
                known_partial_financing = financing
                gross = commission = ibkr_commission = section31_fee = 0.0
                slippage = cost = 0.0
                cash_interest = financing = net = 0.0

        aum = prev_aum + net
        rows.append((day, status, gross, commission, ibkr_commission,
                     section31_fee, commission, slippage, cost,
                     cash_interest, financing, net, signal_count, fill_events,
                     units, traded_shares, holding_minutes,
                     traded_notional, long_gross, short_gross,
                     eod_exit_fills, eod_exit_notional, eod_extra_cost,
                     positive_cash_integral, borrowed_cash_integral,
                     long_notional_integral, short_notional_integral,
                     known_partial_gross, known_partial_commission,
                     known_partial_ibkr_commission,
                     known_partial_section31_fee,
                     known_partial_slippage, known_partial_cash_interest,
                     known_partial_financing,
                     section31_rate * 1_000_000.0,
                     cash_rate, funding_rate, borrow_rate,
                     total_dcf, intraday_dcf, cash_hurdle_ret,
                     prev_aum, aum,
                     bool(vs.loc[day, "close_valid"]) if day in vs.index else False))
        previous_day = day

    r = pd.DataFrame(rows, columns=["session_date", "status", "gross",
                                    "commission", "ibkr_commission",
                                    "section31_fee", "total_explicit_cost",
                                    "slippage", "cost",
                                    "cash_interest", "financing", "net",
                                    "signal_count", "fill_events", "trade_units",
                                    "shares_traded", "holding_minutes",
                                    "traded_notional", "long_gross",
                                    "short_gross",
                                    "eod_exit_fills", "eod_exit_notional",
                                    "eod_extra_cost",
                                    "positive_cash_minute_dollars",
                                    "borrowed_cash_minute_dollars",
                                    "long_notional_minute_dollars",
                                    "short_notional_minute_dollars",
                                    "known_partial_gross",
                                    "known_partial_commission",
                                    "known_partial_ibkr_commission",
                                    "known_partial_section31_fee",
                                    "known_partial_slippage",
                                    "known_partial_cash_interest",
                                    "known_partial_financing",
                                    "section31_rate_per_million_used",
                                    "cash_rate_annual_used",
                                    "funding_rate_annual_used",
                                    "borrow_rate_annual_used",
                                    "total_daycount_fraction",
                                    "intraday_daycount_fraction",
                                    "cash_hurdle_ret",
                                    "prev_aum", "aum",
                                    "close_valid"]).set_index("session_date")
    r["ret"] = r["net"] / r["prev_aum"]
    r["market_impact"] = 0.0
    r["total_execution_cost"] = r["cost"]
    # A session whose exit price is a guess does not belong in headline
    # performance, so it leaves the evaluation window entirely.
    r["is_evaluation"] = ~r["status"].isin(
        ["warmup_no_prev_close", "warmup_no_band", "unknown_exit",
         "after_unknown_exit", "absent_session"])
    r.loc[~r["is_evaluation"], "ret"] = np.nan
    if collect_ledger:
        r.attrs["ledger"] = {
            name: pd.DataFrame(items) for name, items in ledgers.items()}
        r.attrs["daily_features"] = daily[["dvol"]].copy()
    r.attrs["financing_rates_supplied"] = financing_rates is not None
    r.attrs["eod_execution_supplied"] = eod_execution is not None
    r.attrs["explicit_cost_model"] = cfg.explicit_cost_model
    r.attrs["section31_rates_supplied"] = section31_rates is not None
    if section31_rates is not None:
        r.attrs["section31_schedule_sha256"] = section31_rates.attrs.get("sha256")
        r.attrs["section31_schedule_path"] = section31_rates.attrs.get("path")
    return r


# --------------------------------------------------------------------------- #
# statistics
# --------------------------------------------------------------------------- #

def stats(r: pd.DataFrame, rf_annual: float = 0.0) -> dict:
    """Calendarised. Every session in the evaluation window contributes a row,
    including flat ones, so the time axis is not compressed by skipped days."""
    ev = r.loc[r["is_evaluation"]]
    if ev.empty:
        return {}
    x = ev["ret"].fillna(0.0)
    hurdle = (
        ev["cash_hurdle_ret"].fillna(0.0)
        if "cash_hurdle_ret" in ev else rf_annual / 252.0)
    first, last = ev.index.min(), ev.index.max()
    years = (last - first).days / 365.2425
    total = float((1 + x).prod())
    cum = (1 + x).cumprod()
    dd = cum / cum.cummax() - 1
    active = x[ev["status"].eq("active")]
    nz = x[x != 0]
    return {
        "TotRet%": round((total - 1) * 100, 1),
        "CAGR%": round((total ** (1 / years) - 1) * 100, 2),
        "Vol%": round(float(x.std() * np.sqrt(252)) * 100, 2),
        "Sharpe_calendar": (round(float(
            (x - hurdle).mean() / x.std() * np.sqrt(252)), 2)
                            if float(x.std()) > 0 else None),
        # NOT annualised. Scaling active-only returns by sqrt(252) when there
        # are ~153 active sessions a year manufactures a higher number that,
        # rescaled by sqrt(252 * active/calendar), collapses back onto
        # Sharpe_calendar. Report the raw conditional moments instead.
        "ActiveMeanRet_bp": (round(float(active.mean()) * 1e4, 2)
                             if len(active) else None),
        "ActiveStd_bp": (round(float(active.std()) * 1e4, 2)
                         if len(active) > 2 else None),
        "MDD%": round(float(dd.min()) * 100, 1),
        "WorstDay%": round(float(x.min()) * 100, 2),
        "Skew": round(float(x.skew()), 2),
        "Hit%": round(float((nz > 0).mean()) * 100, 1) if len(nz) else None,
        "TradeUnits": int(ev["trade_units"].sum()),
        "SharesTraded": int(ev["shares_traded"].sum()),
        "CostPerTradedShare_c": (round(float(ev["cost"].sum()
                                             / ev["shares_traded"].sum()) * 100, 3)
                                 if ev["shares_traded"].sum() > 0 else None),
        "UnknownExitSessions": int(r["status"].eq("unknown_exit").sum()),
        "EvalSessions": int(len(ev)),
        "ActiveSessions": int(ev["status"].eq("active").sum()),
        "CalendarYears": round(years, 2),
        "First": str(first.date()), "Last": str(last.date()),
    }


def status_breakdown(r: pd.DataFrame) -> pd.Series:
    return r["status"].value_counts()


def benchmark(data: dict, cfg: Cfg, r: pd.DataFrame) -> dict:
    """SPY computed inside this engine, on exactly the same sessions, calendar,
    dividend file and warm-up cutoff as the strategy. No external annualised
    approximations."""
    d, daily = build_features(data["bars"], data["vmin"], data["vsess"], cfg,
                              data["dividends"])
    ev = r.loc[r["is_evaluation"]]
    idx = ev.index

    # A trailing-truncated session's "close" is the last available bar, not the
    # closing auction. Using it corrupts that day's and the next day's SPY
    # return, and through them beta, alpha and the information ratio.
    close_valid = daily["close_valid"].reindex(daily.index).fillna(False)
    px_all = daily["close"].where(close_valid)

    # Anchor one session before the evaluation start, otherwise the strategy
    # trades on day one while the benchmark's first return is silently zero.
    all_idx = daily.index
    first_loc = int(all_idx.get_loc(idx.min()))
    anchor_idx = all_idx[max(0, first_loc - 1):]
    px_anchored = px_all.reindex(anchor_idx)
    div_anchored = daily["dividend"].reindex(anchor_idx).fillna(0.0)

    price_ret = px_anchored.pct_change(fill_method=None).reindex(idx)
    total_ret = ((px_anchored + div_anchored)
                 / px_anchored.shift(1) - 1).reindex(idx)

    years = (idx.max() - idx.min()).days / 365.2425
    strat = ev["ret"].fillna(0.0)

    def ann(x):
        c = float((1 + x.dropna()).prod())
        return c ** (1 / years) - 1

    n_missing = int(total_ret.isna().sum())
    aligned = pd.DataFrame({"s": strat, "b": total_ret}).dropna()
    beta = float(np.cov(aligned["s"], aligned["b"])[0, 1] / np.var(aligned["b"]))
    alpha_d = float(aligned["s"].mean() - beta * aligned["b"].mean())
    excess = aligned["s"] - aligned["b"]
    return {
        "spy_price_CAGR%": round(ann(price_ret) * 100, 2),
        "spy_total_CAGR%": round(ann(total_ret) * 100, 2),
        "spy_total_Vol%": round(float(total_ret.std() * np.sqrt(252)) * 100, 2),
        "spy_total_Sharpe": round(float(total_ret.mean() / total_ret.std()
                                        * np.sqrt(252)), 2),
        "strategy_CAGR%": round(ann(strat) * 100, 2),
        "excess_CAGR%": round((ann(strat) - ann(total_ret)) * 100, 2),
        "beta_vs_spy_total": round(beta, 3),
        "alpha_annualised%": round(alpha_d * 252 * 100, 2),
        "InfoRatio": round(float(excess.mean() / excess.std() * np.sqrt(252)), 2),
        "dividends_used": data["dividend_meta"].get("loaded", False),
        "benchmark_sessions": int(len(idx)),
        "benchmark_valid_sessions": int(total_ret.notna().sum()),
        "benchmark_missing_close_sessions": n_missing,
        "regression_aligned_sessions": int(len(aligned)),
        "first_day_anchored": bool(first_loc > 0),
        "years": round(years, 2),
        "note": "benchmark returns come from the same minute file. Where a "
                "session has no valid close the return is NaN, never the last "
                "available bar. For a production benchmark prefer an "
                "independent daily raw-close + dividend series with its own "
                "provenance.",
    }


def report(run_dir: str | Path, cfg: Cfg,
           tiers: tuple[str, ...] = ("paper_ready", "halt_aware", "exploratory"),
           section31_rates: pd.DataFrame | None = None,
           ) -> pd.DataFrame:
    """Full metric set per tier. Never mix metrics across tiers: they are
    different equity curves, so CAGR(A)/MDD(B) is not a defined quantity."""
    data = load_run(run_dir, cfg)
    man = data["manifest"]
    rows = []
    for tier in tiers:
        c = replace(cfg, tier=tier)
        st = stats(backtest(data, c, section31_rates=section31_rates), c.rf_annual)
        st["tier"] = tier
        st["DataSessions"] = int(man["outputs"][tier]["sessions"])
        rows.append(st)
    out = pd.DataFrame(rows).set_index("tier")
    out.attrs["provenance"] = {
        "profile": cfg.profile,
        "engine_script_sha256": _sha(ENGINE_PATH)[:16],
        "git": _git(),
        "data_run_id": man["run_id"],
        "data_script_sha256": man["script_sha256"][:16],
        "source_sha256": man["source_sha256"][:16],
        "dividends": data["dividend_meta"],
        "data_bundle": data["data_bundle"],
        "engine_config": asdict(cfg),
        "section31_schedule": ({
            "path": section31_rates.attrs.get("path"),
            "sha256": section31_rates.attrs.get("sha256"),
        } if section31_rates is not None else None),
    }
    return out
