"""Post-result statistical uncertainty for the QQQ with-dividends headline.

Mirrors experiments/statistical_uncertainty_v1/run_uncertainty.py but reads the
QQQ with-dividends daily-return CSV instead of a frozen formal-run parquet, and
reuses that module's audited estimators (formal_sharpe, bootstrap_sharpes,
hac_sharpe, quantile_record, concentration_neff) by import - no statistical
code is duplicated.

Headline cell: corrected_execution x halt_aware x $0.0025 with dividends
(the QQQ analog of the amended SPY v2 headline). On QQQ the halt_aware tier is
identical to paper_ready (all 4 L1-halt sessions are halt_anomaly and excluded
from both), so this is also the paper_ready x $0.0025 series.

Financing uses financing-rates-v2 (2007-04-25..2026-07-31), matching the
with-dividends run's economics; cash_hurdle_ret is read from the CSV as-is.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SU = ROOT / "experiments" / "statistical_uncertainty_v1"
if str(SU) not in sys.path:
    sys.path.insert(0, str(SU))

import run_uncertainty as su  # noqa: E402  (audited estimators)

DAILY_CSV = (HERE / "daily_returns" /
             "corrected_execution__halt_aware__slip_0p0025.csv")
EVAL_START = "2024-05-01"
ANNUALIZATION = 252

BOOTSTRAP = {
    "replications": 8000,
    "seed": 20260804,
    "primary_method": "circular_moving_block",
    "primary_block_length_sessions": 20,
    "sensitivity_methods": ["circular_moving_block", "moving_block"],
    "sensitivity_block_lengths_sessions": [1, 5, 10, 20, 40, 60],
    "confidence_levels": [0.9, 0.95],
}
HAC = {
    "kernel": "bartlett_newey_west",
    "primary_max_lag_sessions": 20,
    "sensitivity_max_lags_sessions": [0, 5, 10, 20, 40, 60],
    "confidence_levels": [0.9, 0.95],
}


def sha256(path: Path) -> str:
    d = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            d.update(chunk)
    return d.hexdigest()


def load_daily() -> pd.DataFrame:
    daily = pd.read_csv(DAILY_CSV, parse_dates=["session_date"])
    daily["session_date"] = daily["session_date"].dt.normalize()
    daily = daily[daily["is_evaluation"].astype(bool)].sort_values("session_date")
    if daily["session_date"].duplicated().any():
        raise RuntimeError("duplicate sessions")
    if daily[["ret", "cash_hurdle_ret"]].isna().any(axis=None):
        raise RuntimeError("evaluation series has NaN Sharpe inputs")
    return daily.reset_index(drop=True)


def main() -> int:
    daily = load_daily()
    split = pd.Timestamp(EVAL_START)
    periods = {
        "pre_publication": daily[daily["session_date"] < split].copy(),
        "post_publication": daily[daily["session_date"] >= split].copy(),
    }
    point: dict[str, float] = {}
    audit_rows, concentration_rows = [], []
    for name, frame in periods.items():
        point[name] = su.formal_sharpe(
            frame["ret"].to_numpy(), frame["cash_hurdle_ret"].to_numpy(),
            ANNUALIZATION)
        excess = frame["ret"].to_numpy() - frame["cash_hurdle_ret"].to_numpy()
        audit_rows.append({
            "period": name, "first": str(frame["session_date"].min().date()),
            "last": str(frame["session_date"].max().date()),
            "sessions": len(frame), "formal_sharpe": point[name]})
        concentration_rows.append({
            "period": name, "sessions": len(frame),
            "squared_return_concentration_neff": su.concentration_neff(excess),
            "normal_fourth_moment_equivalent_sessions":
                3.0 * su.concentration_neff(excess)})

    bootstrap_rows, primary_draws = [], {}
    for method in BOOTSTRAP["sensitivity_methods"]:
        circular = method == "circular_moving_block"
        for block in BOOTSTRAP["sensitivity_block_lengths_sessions"]:
            draws = {}
            for name, frame in periods.items():
                draws[name] = su.bootstrap_sharpes(
                    frame["ret"].to_numpy(), frame["cash_hurdle_ret"].to_numpy(),
                    ANNUALIZATION, int(BOOTSTRAP["replications"]), int(block),
                    su.seed_for(int(BOOTSTRAP["seed"]), method, int(block), name),
                    circular)
                if (method == BOOTSTRAP["primary_method"]
                        and block == BOOTSTRAP["primary_block_length_sessions"]):
                    primary_draws[name] = draws[name]
                for level in BOOTSTRAP["confidence_levels"]:
                    bootstrap_rows.append({
                        "method": method, "block_length_sessions": block,
                        "period": name, "confidence_level": level,
                        **su.quantile_record(draws[name], point[name], float(level)),
                        "centered_one_sided_p_decline": math.nan,
                        "centered_two_sided_p_difference": math.nan})
            diff = draws["pre_publication"] - draws["post_publication"]
            diff_point = point["pre_publication"] - point["post_publication"]
            centered = diff - diff_point
            for level in BOOTSTRAP["confidence_levels"]:
                bootstrap_rows.append({
                    "method": method, "block_length_sessions": block,
                    "period": "pre_minus_post", "confidence_level": level,
                    **su.quantile_record(diff, diff_point, float(level)),
                    "centered_one_sided_p_decline": float(np.mean(centered >= diff_point)),
                    "centered_two_sided_p_difference": float(
                        np.mean(np.abs(centered) >= abs(diff_point)))})

    hac_rows = []
    for lag in HAC["sensitivity_max_lags_sessions"]:
        est = {n: su.hac_sharpe(f["ret"].to_numpy(), f["cash_hurdle_ret"].to_numpy(),
                                ANNUALIZATION, int(lag)) for n, f in periods.items()}
        diff_point = est["pre_publication"][0] - est["post_publication"][0]
        diff_se = math.sqrt(est["pre_publication"][1] ** 2 + est["post_publication"][1] ** 2)
        for n, (estimate, se) in est.items():
            for level in HAC["confidence_levels"]:
                low, high = su.normal_interval(estimate, se, float(level))
                hac_rows.append({"max_lag_sessions": lag, "period": n,
                    "confidence_level": level, "point": estimate, "hac_se": se,
                    "ci_low": low, "ci_high": high,
                    "one_sided_p_decline": math.nan, "two_sided_p_difference": math.nan})
        z = diff_point / diff_se if diff_se > 0 else math.nan
        one = float(su.NormalDist().cdf(-z)) if math.isfinite(z) else math.nan
        two = 2.0 * float(su.NormalDist().cdf(-abs(z))) if math.isfinite(z) else math.nan
        for level in HAC["confidence_levels"]:
            low, high = su.normal_interval(diff_point, diff_se, float(level))
            hac_rows.append({"max_lag_sessions": lag, "period": "pre_minus_post",
                "confidence_level": level, "point": diff_point, "hac_se": diff_se,
                "ci_low": low, "ci_high": high,
                "one_sided_p_decline": one, "two_sided_p_difference": two})

    primary_diff = primary_draws["pre_publication"] - primary_draws["post_publication"]
    decision = {
        "headline_cell": "corrected_execution__halt_aware__slip_0p0025 (with dividends)",
        "primary_bootstrap_method": BOOTSTRAP["primary_method"],
        "primary_block_length_sessions": BOOTSTRAP["primary_block_length_sessions"],
        "replications": BOOTSTRAP["replications"],
        "sharpe_pre": point["pre_publication"],
        "sharpe_post": point["post_publication"],
        "sharpe_difference_pre_minus_post": point["pre_publication"] - point["post_publication"],
        "post_bootstrap_90_interval": [
            float(np.quantile(primary_draws["post_publication"], 0.05)),
            float(np.quantile(primary_draws["post_publication"], 0.95))],
        "difference_bootstrap_90_interval": [
            float(np.quantile(primary_diff, 0.05)),
            float(np.quantile(primary_diff, 0.95))],
        "conclusion": (
            "QQQ shows no post-publication Sharpe decay (point estimate is "
            "essentially flat: pre 1.00, post 1.00, difference -0.001). The "
            "primary 90% bootstrap interval for the pre-minus-post difference "
            "includes zero. The post-publication Sharpe is above zero at the "
            "90% level under both bootstrap (lower bound 0.01) and HAC (lower "
            "bound 0.06), but the interval is wide because only 318 post-"
            "publication sessions are resampled. Same qualitative read as the "
            "SPY addendum's 'not a statistically significant decline', but "
            "here because there is no decline to begin with."),
    }

    out_dir = HERE / "uncertainty_results"
    out_dir.mkdir(exist_ok=True)
    frames = {
        "point_estimate_audit.csv": pd.DataFrame(audit_rows),
        "bootstrap_summary.csv": pd.DataFrame(bootstrap_rows),
        "hac_sharpe_summary.csv": pd.DataFrame(hac_rows),
        "return_concentration.csv": pd.DataFrame(concentration_rows),
    }
    for name, frame in frames.items():
        frame.to_csv(out_dir / name, index=False)
    provenance = {
        "classification": "post_result_statistical_uncertainty_addendum",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "headline_cell": decision["headline_cell"],
        "source_daily_returns_csv": str(DAILY_CSV.relative_to(ROOT)).replace("\\", "/"),
        "source_daily_returns_sha256": sha256(DAILY_CSV),
        "reuses_estimators_from": "experiments/statistical_uncertainty_v1/run_uncertainty.py",
        "reused_module_sha256": sha256(SU / "run_uncertainty.py"),
        "config": {"bootstrap": BOOTSTRAP, "hac": HAC,
                   "annualization_sessions": ANNUALIZATION,
                   "post_publication_start": EVAL_START},
        "decision": decision,
        "interpretation_limits": [
            "post-result statistical addendum; QQQ point estimates are unchanged",
            "bootstrap intervals are conditional on events observed in each period",
            "percentile intervals are not posterior probabilities for the true Sharpe",
            "bootstrap P(draw <= 0) is diagnostic and is not labeled a p-value",
            "concentration N_eff is not a serial-independence effective sample size",
            "financing uses financing-rates-v2 (cash SOFR/LIBOR-proxy - 50bp); cash_hurdle_ret read from CSV",
        ],
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(decision, indent=2))
    print("\npost 90% bootstrap Sharpe interval:",
          [round(x, 3) for x in decision["post_bootstrap_90_interval"]])
    print("HAC post Sharpe (lag=20):",
          [r for r in hac_rows if r["period"] == "post_publication"
           and r["max_lag_sessions"] == 20 and r["confidence_level"] == 0.9])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
