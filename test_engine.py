"""Engine tests. Run: python test_engine.py

These target the behaviours a data-layer test suite cannot see: parameter
plumbing, order/fill semantics around halts, cost decomposition and the
calendar time axis.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

import im_engine_v4 as E
from im_engine_v4 import Cfg, profile_cfg


FAILS: list[str] = []
N = 0


def check(cond: bool, msg: str) -> None:
    global N
    N += 1
    if not cond:
        FAILS.append(msg)


# --------------------------------------------------------------------------- #
# minimal session fixtures for the state machine
# --------------------------------------------------------------------------- #

def make_session(n: int = 60, halt: tuple[int, int] | None = None,
                 gap_at_reopen: float = 0.0, last_minute: int | None = None
                 ) -> pd.DataFrame:
    """n scheduled minutes, flat price 100, optional halt window (inclusive)
    with a price jump on reopening."""
    minute = np.arange(1, n + 1)
    price = np.full(n, 100.0)
    execable = np.ones(n, dtype=bool)
    if halt is not None:
        lo, hi = halt
        execable[(minute >= lo) & (minute <= hi)] = False
        price[minute > hi] += gap_at_reopen
    if last_minute is not None:
        execable[minute > last_minute] = False
    return pd.DataFrame({
        "minute_of_session": minute,
        "open": price, "high": price, "low": price, "close": price,
        "is_executable_minute": execable,
        "calendar_bars": n,
    })


def test_halt_semantics() -> None:
    # a position established well before the halt must earn the reopening gap
    g = make_session(60, halt=(35, 48), gap_at_reopen=10.0)
    sig = np.zeros(60); adm = np.zeros(60, dtype=bool)
    sig[9] = 1.0; adm[9] = True                      # long at minute 10
    for fp in ("signal_bar_close", "next_executable_open"):
        c = Cfg(fill_price=fp)
        r = E._session_pnl(g, sig, adm, shares=100, cfg=c)
        check(abs(r["gross"] - 100 * 10.0) < 1e-9,
              f"pre-existing position across halt must earn the gap "
              f"({fp}: got {r['gross']})")

    # a signal on the last bar before the halt: under the corrected convention
    # the order is due at minute 35, which is halted, so it is cancelled and
    # earns nothing
    sig2 = np.zeros(60); adm2 = np.zeros(60, dtype=bool)
    sig2[33] = 1.0; adm2[33] = True                  # minute 34
    c = profile_cfg("corrected_execution")
    r = E._session_pnl(g, sig2, adm2, 100, c)
    check(abs(r["gross"]) < 1e-9,
          f"unfilled pre-halt order must not earn the reopening gap "
          f"(got {r['gross']})")
    check(r["trade_units"] == 0, f"cancelled order should produce no fill ({r})")

    # queued to the reopen: fills at the reopening price, so still no gap P&L
    c2 = replace(c, pending_order_policy="queue_until_executable")
    r2 = E._session_pnl(g, sig2, adm2, 100, c2)
    check(abs(r2["gross"]) < 1e-9,
          f"queued order must start P&L only after it fills (got {r2['gross']})")
    check(r2["trade_units"] == 2, f"queued order should fill then flatten ({r2})")

    # the paper's own convention does hand the gap to that same signal --
    # this is the artefact, and it must be visible rather than hidden
    c3 = profile_cfg("paper_spec")
    r3 = E._session_pnl(g, sig2, adm2, 100, c3)
    check(abs(r3["gross"] - 100 * 10.0) < 1e-9,
          f"paper_spec should show the optimistic gap capture ({r3['gross']})")


def test_no_trading_inside_halt() -> None:
    g = make_session(60, halt=(20, 40), gap_at_reopen=5.0)
    sig = np.zeros(60); adm = np.zeros(60, dtype=bool)
    sig[24] = 1.0; adm[24] = True                    # minute 25, inside the halt
    r = E._session_pnl(g, sig, adm, 100, profile_cfg("corrected_execution"))
    check(r["trade_units"] == 0 and abs(r["gross"]) < 1e-9,
          f"a decision inside a halt must not trade ({r})")


def test_reversal_units_and_costs() -> None:
    g = make_session(60)
    sig = np.zeros(60); adm = np.zeros(60, dtype=bool)
    sig[9] = 1.0; adm[9] = True
    sig[19] = -1.0; adm[19] = True                   # reversal: two units
    c = profile_cfg("paper_spec", comm_per_share=0.01, min_comm=0.0,
                    slip_per_share=0.005)
    r = E._session_pnl(g, sig, adm, 100, c)
    check(r["trade_units"] == 4.0,
          f"0->+1->-1->0 is 4 traded units, got {r['trade_units']}")
    check(r["fill_events"] == 3.0,
          f"...on 3 fill events, got {r['fill_events']}")
    check(abs(r["shares_traded"] - 400) < 1e-9,
          f"400 shares should change hands, got {r['shares_traded']}")
    check(abs(r["slippage"] - 400 * 0.005) < 1e-9,
          f"slippage must be charged per traded share, got {r['slippage']}")
    # minimum commission: a reversal modelled as two orders is charged twice
    c1 = replace(c, min_comm=1.0, comm_per_share=0.0,
                 reversal_order_model="single_order")
    c2 = replace(c1, reversal_order_model="two_orders")
    r1 = E._session_pnl(g, sig, adm, 100, c1)
    r2 = E._session_pnl(g, sig, adm, 100, c2)
    check(abs(r1["commission"] - 3.0) < 1e-9,
          f"single-order reversal: 3 minimum charges, got {r1['commission']}")
    check(abs(r2["commission"] - 4.0) < 1e-9,
          f"two-order reversal: 4 minimum charges, got {r2['commission']}")


def test_no_final_bar_round_trip() -> None:
    g = make_session(60)
    sig = np.zeros(60); adm = np.zeros(60, dtype=bool)
    sig[59] = 1.0; adm[59] = True                    # signal on the last bar
    for prof in ("paper_spec", "corrected_execution"):
        r = E._session_pnl(g, sig, adm, 100, profile_cfg(prof))
        check(r["trade_units"] == 0.0,
              f"{prof}: a final-bar signal must not open a position that is "
              f"liquidated at the same price ({r['trade_units']} units)")


def test_exec_lag_timing() -> None:
    px = np.full(30, 100.0); px[3:] = 110.0
    g = pd.DataFrame({"minute_of_session": np.arange(1, 31),
                      "open": px, "high": px, "low": px, "close": px,
                      "is_executable_minute": True, "calendar_bars": 30})
    sig = np.zeros(30); adm = np.zeros(30, dtype=bool)
    sig[1] = 1.0; adm[1] = True                      # minute 2
    c = profile_cfg("corrected_execution", exec_lag_minutes=3, slip_per_share=0.0,
                    comm_per_share=0.0, min_comm=0.0)
    r = E._session_pnl(g, sig, adm, 100, c)
    check(abs(r["gross"]) < 1e-9,
          f"exec_lag_minutes=3 must fill at minute 5 (open 110), not minute 3 "
          f"(got gross {r['gross']})")


def test_flat_before_tail_gap_is_known() -> None:
    g = make_session(60, last_minute=50)
    sig = np.zeros(60); adm = np.zeros(60, dtype=bool)
    sig[9] = 1.0; adm[9] = True
    sig[19] = 0.0; adm[19] = True                    # flat well before the gap
    r = E._session_pnl(g, sig, adm, 100, profile_cfg("corrected_execution"))
    check(not r["exposed_to_unknown_tail"],
          "being flat before the missing tail must keep the P&L known")


def test_share_rounding() -> None:
    """The bug this test exists for: `share_rounding` was declared on Cfg and
    set by the notebook profile, but backtest() hard-coded np.floor."""
    import im_engine_v4 as M
    src = Path(M.__file__).read_text()
    # strip the dataclass body so declarations do not count as usage
    head = src.index("class Cfg:")
    tail = src.index("_ENUMS = {")
    body = src[:head] + src[tail:]
    unused = [f for f in Cfg.__dataclass_fields__ if f".{f}" not in body]
    check(not unused,
          f"Cfg fields declared but never read by the engine: {unused}")
    check(M.profile_cfg("official_sample_compatible").share_rounding == "round",
          "notebook profile must use round()")
    check(M.profile_cfg("paper_spec").share_rounding == "floor",
          "paper profile must use floor()")


def test_config_validation() -> None:
    for bad in [{"share_rounding": "ceil"}, {"unknown_exit_policy": "hope"},
                {"fill_price": "mid"}, {"tier": "everything"},
                {"exec_lag_minutes": 0}, {"dvol_lag": 0}]:
        try:
            profile_cfg("paper_spec", **bad)
            check(False, f"invalid config {bad} was accepted")
        except ValueError:
            check(True, "")


def test_unknown_exit() -> None:
    g = make_session(60, last_minute=50)
    sig = np.zeros(60); adm = np.zeros(60, dtype=bool)
    sig[9] = 1.0; adm[9] = True
    r = E._session_pnl(g, sig, adm, 100, profile_cfg("corrected_execution"))
    check(not r["exit_at_scheduled_close"],
          "trailing-truncated session must not claim a scheduled-close exit")


# --------------------------------------------------------------------------- #
# integration: parameter plumbing
# --------------------------------------------------------------------------- #

def build_run(tmp: Path) -> Path:
    import prepare_spy_data as P
    src, d = P._synth(tmp, first="2023-06-01", last="2023-12-15",
                      random_walk=True, seed=7)
    div = tmp / "div.csv"
    days = pd.to_datetime(pd.Series(d["caldt"].dt.normalize().unique()))
    pd.DataFrame({"symbol": ["SPY"], "ex_date": [str(days.iloc[40].date())],
                  "cash_amount": [1.50]}).to_csv(div, index=False)
    m = P.run(P.parse_args(P._base(src, tmp / "out", "--dividends", str(div))))
    return Path(m["run_dir"])


def test_parameter_plumbing(run_dir: Path) -> None:
    base = profile_cfg("paper_spec", require_dividends=True)
    data = E.load_run(run_dir, base)

    check(data["dividend_meta"]["loaded"] and data["dividend_meta"]["event_count"] == 1,
          f"dividends must load automatically ({data['dividend_meta']})")

    # trade_freq must actually change the decision grid
    for tf in (15, 30, 60):
        c = replace(base, trade_freq=tf)
        d, daily = E.build_features(data["bars"], data["vmin"], data["vsess"], c, data["dividends"])
        adm = E.config_validity(d, daily, data["vsess"], c)
        mins = sorted(d.loc[adm, "minute_of_session"].unique())
        check(all(m % tf == 0 for m in mins) and any(m % 30 != 0 for m in mins) == (tf == 15),
              f"trade_freq={tf} produced decision minutes {mins[:6]}")

    # sigma_window must drive BOTH the value and the mask
    for sw in (14, 20):
        c = replace(base, sigma_window=sw)
        d, daily = E.build_features(data["bars"], data["vmin"], data["vsess"], c, data["dividends"])
        n = d.loc[d["sigma_history_valid"], "sigma_hist_n"].min()
        check(n >= sw, f"sigma_window={sw} but mask admitted history of {n}")

    # use_vwap=False must not be blocked by vwap_valid
    c_novwap = replace(base, use_vwap=False)
    d, daily = E.build_features(data["bars"], data["vmin"], data["vsess"], c_novwap, data["dividends"])
    a1 = E.config_validity(d, daily, data["vsess"], c_novwap).sum()
    c_vwap = replace(base, use_vwap=True)
    a2 = E.config_validity(d, daily, data["vsess"], c_vwap).sum()
    check(a1 >= a2, "use_vwap=False must not be more restrictive than use_vwap=True")

    # flat sizing must not require daily-vol history
    c_flat = replace(base, sizing="flat", flat_lev=1.0)
    d, daily = E.build_features(data["bars"], data["vmin"], data["vsess"], c_flat, data["dividends"])
    a_flat = E.config_validity(d, daily, data["vsess"], c_flat).sum()
    c_vt = replace(base, sizing="vol_target")
    a_vt = E.config_validity(d, daily, data["vsess"], c_vt).sum()
    check(a_flat >= a_vt,
          f"flat sizing should not inherit the vol-history requirement "
          f"({a_flat} vs {a_vt})")

    # dividend adjustment must move the band anchor
    d, daily_div = E.build_features(data["bars"], data["vmin"], data["vsess"], base, data["dividends"])
    _, daily_nodiv = E.build_features(data["bars"], data["vmin"], data["vsess"], base, None)
    ex = data["dividends"].index[0]
    delta = daily_nodiv.loc[ex, "prev_close_adj"] - daily_div.loc[ex, "prev_close_adj"]
    check(abs(delta - 1.50) < 1e-9,
          f"ex-date prev_close must drop by the dividend (got {delta})")

    # strict eligible-session rolling: a session that is merely missing this
    # minute must still consume a slot in the 14-session window
    strict = replace(base, strict_eligible_rolling=True)
    loose = replace(base, strict_eligible_rolling=False)
    ds, _ = E.build_features(data["bars"], data["vmin"], data["vsess"], strict,
                             data["dividends"])
    dl, _ = E.build_features(data["bars"], data["vmin"], data["vsess"], loose,
                             data["dividends"])
    check(int(ds["sigma_history_valid"].sum()) <= int(dl["sigma_history_valid"].sum()),
          "strict eligible rolling must never admit more history than the loose "
          "row-based version")

    # invalid daily returns must not enter the volatility window
    c_strict = replace(base, respect_return_validity=True)
    c_loose = replace(base, respect_return_validity=False)
    _, dstrict = E.build_features(data["bars"], data["vmin"], data["vsess"],
                                  c_strict, data["dividends"])
    _, dloose = E.build_features(data["bars"], data["vmin"], data["vsess"],
                                 c_loose, data["dividends"])
    check(int(dstrict["dvol"].notna().sum()) <= int(dloose["dvol"].notna().sum()),
          "validity-filtered dvol must never be defined on more days")

    # ignore_dividends must really ignore an existing file
    ig = E.load_run(run_dir, replace(base, ignore_dividends=True))
    check(ig["dividends"] is None and ig["dividend_meta"].get("ignored_by_config"),
          f"ignore_dividends=True still loaded dividends: {ig['dividend_meta']}")

    # config-match guard
    try:
        E.load_run(run_dir, replace(base, trade_freq=15, require_config_match=True))
        check(False, "require_config_match did not raise on a mismatched trade_freq")
    except RuntimeError:
        check(True, "")


def test_costs_and_calendar(run_dir: Path) -> None:
    base = profile_cfg("paper_spec")
    data = E.load_run(run_dir, base)

    # commission and slippage must be separable; the minimum applies only to
    # commission
    c0 = replace(base, slip_per_share=0.0)
    c1 = replace(base, slip_per_share=0.001)
    r0, r1 = E.backtest(data, c0), E.backtest(data, c1)
    extra = (r1["cost"] - r0["cost"]).sum()
    check(extra > 0, "adding slippage did not raise total cost")
    # with a tiny share count the minimum commission binds; slippage must still
    # be charged on top rather than absorbed by max()
    tiny = replace(base, aum0=200.0, slip_per_share=1.0)
    rt = E.backtest(data, tiny)
    fills = rt["trade_units"].sum()
    if fills > 0:
        check((rt["cost"] > 0).any(),
              "slippage vanished under the minimum-commission max()")

    r = E.backtest(data, base)
    check(set(r.index) == set(pd.to_datetime(data["bars"]["session_date"].unique())),
          "the daily frame must carry every calendar session, not only traded ones")
    st = E.stats(r)
    ev = r.loc[r["is_evaluation"]]
    span = (ev.index.max() - ev.index.min()).days / 365.2425
    check(abs(st["CalendarYears"] - span) < 0.05,
          f"CAGR must use elapsed calendar time ({st['CalendarYears']} vs {span:.2f})")
    check("Sharpe_calendar" in st and "ActiveMeanRet_bp" in st,
          "conditional active moments must be reported un-annualised")
    check(st["EvalSessions"] >= st["ActiveSessions"],
          "evaluation sessions must include non-trading days")
    # unknown-exit sessions must leave the evaluation window entirely
    check(not r.loc[r["status"].eq("unknown_exit"), "is_evaluation"].any(),
          "unknown_exit sessions are still in the headline statistics")


def test_share_rounding_effect(run_dir: Path) -> None:
    base = profile_cfg("paper_spec")
    data = E.load_run(run_dir, base)
    rf = E.backtest(data, replace(base, share_rounding="floor"))
    rr = E.backtest(data, replace(base, share_rounding="round"))
    check(rf["shares_traded"].sum() != rr["shares_traded"].sum(),
          "share_rounding has no observable effect on traded shares")
    check(abs(rf["gross"].sum() - rr["gross"].sum()) > 0,
          "share_rounding has no observable effect on gross P&L")


def test_unknown_exit_does_not_pollute_aum(run_dir: Path) -> None:
    """A guessed exit price must not compound into every later position size."""
    base = profile_cfg("paper_spec", tier="exploratory")
    data = E.load_run(run_dir, base)
    for policy in ("terminate", "exclude_session_and_freeze_aum"):
        r = E.backtest(data, replace(base, unknown_exit_policy=policy))
        bad = r.index[r["status"].eq("unknown_exit")]
        for day in bad:
            loc = int(r.index.get_loc(day))
            check(abs(r["aum"].iloc[loc] - r["prev_aum"].iloc[loc]) < 1e-9,
                  f"{policy}: unknown-exit day changed AUM")
            if loc + 1 < len(r):
                check(abs(r["prev_aum"].iloc[loc + 1]
                          - r["prev_aum"].iloc[loc]) < 1e-9,
                      f"{policy}: next day inherited a contaminated AUM")
        if policy == "terminate" and len(bad):
            check(r["status"].iloc[int(r.index.get_loc(bad[0])) + 1:]
                  .eq("after_unknown_exit").all(),
                  "terminate policy must invalidate the curve from that day on")


def test_benchmark_hygiene(run_dir: Path) -> None:
    base = profile_cfg("paper_spec", tier="exploratory")
    data = E.load_run(run_dir, base)
    r = E.backtest(data, base)
    b = E.benchmark(data, base, r)
    check(b["first_day_anchored"],
          "benchmark must anchor one session before the evaluation start")
    check(b["benchmark_valid_sessions"] + b["benchmark_missing_close_sessions"]
          == b["benchmark_sessions"],
          f"benchmark session accounting does not add up: {b}")
    # a session with no valid close must contribute NO benchmark return
    vs = data["vsess"].set_index("session_date")
    invalid = vs.index[~vs["close_valid"].astype(bool)]
    ev = r.loc[r["is_evaluation"]]
    overlap = [x for x in invalid if x in ev.index]
    if overlap:
        check(b["benchmark_missing_close_sessions"] > 0,
              "an invalid close silently entered the benchmark series")


def test_cash_interest_not_levered(run_dir: Path) -> None:
    base = profile_cfg("paper_spec", cash_rate_annual=0.05, sizing="flat")
    data = E.load_run(run_dir, base)
    r1 = E.backtest(data, replace(base, flat_lev=1.0))
    r4 = E.backtest(data, replace(base, flat_lev=4.0))
    i1 = (r1["net"] - r1["gross"] + r1["cost"] - r1["financing"]).sum()
    i4 = (r4["net"] - r4["gross"] + r4["cost"] - r4["financing"]).sum()
    check(i1 > 0, "cash interest was not accrued")
    check(abs(i1 / max(abs(i4), 1e-9) - 1) < 0.5,
          "cash interest scales with leverage; it must accrue on equity, not "
          "on notional")


def test_absent_session_occupies_rolling_slot(run_dir: Path) -> None:
    base = profile_cfg("paper_spec")
    data = E.load_run(run_dir, base)
    bars = data["bars"]
    days = sorted(pd.to_datetime(bars["session_date"]).unique())
    drop = days[30]
    thinned = bars[bars["session_date"] != drop]
    d2 = dict(data, bars=thinned)
    r = E.backtest(d2, base)
    check(drop in r.index and r.loc[drop, "status"] == "absent_session",
          "a fully absent session must still appear on the time axis")


def test_profiles(run_dir: Path) -> None:
    out = {}
    for prof in E.PROFILES:
        cfg = profile_cfg(prof, require_dividends=(prof != "official_sample_compatible"))
        data = E.load_run(run_dir, cfg)
        out[prof] = E.stats(E.backtest(data, cfg))
    check(len(out) == 3, "all three profiles must run")
    check(out["official_sample_compatible"]["TradeUnits"] > 0,
          "parity profile produced no fills")
    return out


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_halt_semantics()
        test_no_trading_inside_halt()
        test_reversal_units_and_costs()
        test_no_final_bar_round_trip()
        test_exec_lag_timing()
        test_flat_before_tail_gap_is_known()
        test_unknown_exit()
        test_share_rounding()
        test_config_validation()
        run_dir = build_run(tmp)
        test_parameter_plumbing(run_dir)
        test_costs_and_calendar(run_dir)
        test_share_rounding_effect(run_dir)
        test_unknown_exit_does_not_pollute_aum(run_dir)
        test_benchmark_hygiene(run_dir)
        test_cash_interest_not_levered(run_dir)
        test_absent_session_occupies_rolling_slot(run_dir)
        prof = test_profiles(run_dir)

    if FAILS:
        print("ENGINE TESTS FAILED:")
        for f in FAILS:
            if f:
                print("  -", f)
        return 1
    print(f"ENGINE TESTS PASSED ({N} checks)")
    print("\nprofile smoke results (synthetic data, not economically meaningful):")
    print(pd.DataFrame(prof).T[["TotRet%", "CAGR%", "TradeUnits", "EvalSessions"]].to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
