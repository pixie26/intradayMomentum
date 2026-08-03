"""Post-result uncertainty addendum for the amended v2 economic headline.

This program reads an immutable formal run.  It does not rerun the strategy,
change a frozen specification, or overwrite the source run.  The exact formal
Sharpe statistic is resampled as paired (return, cash-hurdle) observations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE / "config.json")
    parser.add_argument("--output-root", type=Path, default=HERE / "results")
    parser.add_argument("--run-name", default=None)
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("schema_version") != 1:
        raise ValueError("unsupported config schema_version")
    if config.get("classification") != (
            "post_result_statistical_uncertainty_addendum"):
        raise ValueError("unexpected experiment classification")
    return config


def formal_sharpe(
        returns: np.ndarray, hurdles: np.ndarray, annualization: int) -> float:
    returns = np.asarray(returns, dtype=float)
    hurdles = np.asarray(hurdles, dtype=float)
    if len(returns) < 2 or len(returns) != len(hurdles):
        raise ValueError("Sharpe inputs must have equal length >= 2")
    std = float(returns.std(ddof=1))
    if not np.isfinite(std) or std <= 0:
        return math.nan
    return float(np.sqrt(annualization) * (returns - hurdles).mean() / std)


def bootstrap_sharpes(
        returns: np.ndarray, hurdles: np.ndarray, annualization: int,
        replications: int, block_length: int, seed: int,
        circular: bool, batch_size: int = 128) -> np.ndarray:
    """Moving-block bootstrap of the exact formal Sharpe statistic."""
    returns = np.asarray(returns, dtype=float)
    hurdles = np.asarray(hurdles, dtype=float)
    n = len(returns)
    if len(hurdles) != n or n < 2:
        raise ValueError("bootstrap inputs must have equal length >= 2")
    if not 1 <= block_length <= n:
        raise ValueError("block_length must be between 1 and sample length")
    if replications < 1:
        raise ValueError("replications must be positive")

    rng = np.random.default_rng(seed)
    blocks_per_draw = math.ceil(n / block_length)
    offsets = np.arange(block_length, dtype=np.int64)
    start_high = n if circular else n - block_length + 1
    output = np.empty(replications, dtype=float)

    for first in range(0, replications, batch_size):
        size = min(batch_size, replications - first)
        starts = rng.integers(
            0, start_high, size=(size, blocks_per_draw, 1), dtype=np.int64)
        indices = starts + offsets.reshape(1, 1, -1)
        if circular:
            indices %= n
        indices = indices.reshape(size, -1)[:, :n]
        sampled_returns = returns[indices]
        sampled_hurdles = hurdles[indices]
        std = sampled_returns.std(axis=1, ddof=1)
        output[first:first + size] = (
            np.sqrt(annualization)
            * (sampled_returns - sampled_hurdles).mean(axis=1) / std)
    return output


def long_run_covariance(values: np.ndarray, max_lag: int) -> np.ndarray:
    """Bartlett-kernel Newey-West long-run covariance of row observations."""
    values = np.asarray(values, dtype=float)
    if values.ndim != 2 or len(values) < 2:
        raise ValueError("HAC input must be a 2-D array with at least 2 rows")
    if not 0 <= max_lag < len(values):
        raise ValueError("max_lag must be in [0, n-1]")
    centered = values - values.mean(axis=0)
    n = len(centered)
    omega = centered.T @ centered / n
    for lag in range(1, max_lag + 1):
        gamma = centered[lag:].T @ centered[:-lag] / n
        weight = 1.0 - lag / (max_lag + 1.0)
        omega += weight * (gamma + gamma.T)
    return omega


def hac_sharpe(
        returns: np.ndarray, hurdles: np.ndarray, annualization: int,
        max_lag: int) -> tuple[float, float]:
    """Delta-method Sharpe SE using HAC covariance of the first two moments."""
    returns = np.asarray(returns, dtype=float)
    hurdles = np.asarray(hurdles, dtype=float)
    excess = returns - hurdles
    point = formal_sharpe(returns, hurdles, annualization)
    mean_return = float(returns.mean())
    second_moment = float(np.mean(returns ** 2))
    variance = second_moment - mean_return ** 2
    if variance <= 0:
        return point, math.nan
    sigma = math.sqrt(variance)
    mean_excess = float(excess.mean())
    root_a = math.sqrt(annualization)
    gradient = np.array([
        root_a / sigma,
        root_a * mean_excess * mean_return / sigma ** 3,
        -root_a * mean_excess / (2.0 * sigma ** 3),
    ])
    moments = np.column_stack((excess, returns, returns ** 2))
    covariance = long_run_covariance(moments, max_lag) / len(returns)
    variance_sharpe = float(gradient @ covariance @ gradient)
    return point, math.sqrt(max(variance_sharpe, 0.0))


def concentration_neff(excess_returns: np.ndarray) -> float:
    """Inverse Herfindahl of squared demeaned excess returns.

    This is a fourth-moment concentration diagnostic, not an estimator of the
    number of serially independent observations.
    """
    centered = np.asarray(excess_returns, dtype=float)
    centered = centered - centered.mean()
    squares = centered ** 2
    denominator = float(np.sum(squares ** 2))
    return float(np.sum(squares) ** 2 / denominator) if denominator > 0 else math.nan


def seed_for(base: int, method: str, block: int, period: str) -> int:
    payload = f"{base}|{method}|{block}|{period}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "little")


def quantile_record(
        draws: np.ndarray, point: float, level: float) -> dict[str, float]:
    tail = (1.0 - level) / 2.0
    return {
        "point": point,
        "bootstrap_median": float(np.median(draws)),
        "bootstrap_sd": float(draws.std(ddof=1)),
        "ci_low": float(np.quantile(draws, tail)),
        "ci_high": float(np.quantile(draws, 1.0 - tail)),
        "probability_draw_le_zero": float(np.mean(draws <= 0.0)),
    }


def normal_interval(point: float, se: float, level: float) -> tuple[float, float]:
    critical = NormalDist().inv_cdf((1.0 + level) / 2.0)
    return point - critical * se, point + critical * se


def audit_and_load(
        config: dict[str, Any]) -> tuple[Path, pd.DataFrame, pd.DataFrame]:
    run = (ROOT / config["source_run"]).resolve()
    manifest_path = run / "manifest.json"
    observed_manifest_hash = sha256(manifest_path)
    if observed_manifest_hash != config["expected_run_manifest_sha256"]:
        raise RuntimeError("source run manifest hash mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name in ("daily_results.parquet", "summary.csv"):
        expected = manifest["files"][name]["sha256"]
        if sha256(run / name) != expected:
            raise RuntimeError(f"source run file hash mismatch: {name}")

    cell_id = config["headline_cell_id"]
    daily = pd.read_parquet(
        run / "daily_results.parquet", filters=[("cell_id", "==", cell_id)])
    if daily.empty or daily["cell_id"].nunique() != 1:
        raise RuntimeError("headline cell is missing or non-unique")
    daily["session_date"] = pd.to_datetime(daily["session_date"]).dt.normalize()
    daily = daily[daily["is_evaluation"].astype(bool)].sort_values("session_date")
    if daily["session_date"].duplicated().any():
        raise RuntimeError("headline daily series has duplicate sessions")
    required = {"ret", "cash_hurdle_ret", "session_date"}
    if not required.issubset(daily.columns):
        raise RuntimeError(f"daily series misses columns: {sorted(required - set(daily))}")
    if daily[["ret", "cash_hurdle_ret"]].isna().any(axis=None):
        raise RuntimeError("evaluation daily series contains missing Sharpe inputs")
    summary = pd.read_csv(run / "summary.csv")
    summary = summary[summary["cell_id"].eq(cell_id)].copy()
    if set(summary["subperiod"]) != {
            "full_sample", "pre_publication", "post_publication"}:
        raise RuntimeError("headline summary does not contain the three periods")
    return run, daily.reset_index(drop=True), summary


def make_results(
        config: dict[str, Any], config_path: Path = HERE / "config.json",
        ) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    run, daily, formal_summary = audit_and_load(config)
    split = pd.Timestamp(config["post_publication_start"])
    periods = {
        "pre_publication": daily[daily["session_date"] < split].copy(),
        "post_publication": daily[daily["session_date"] >= split].copy(),
    }
    annualization = int(config["annualization_sessions"])
    point: dict[str, float] = {}
    audit_rows: list[dict[str, Any]] = []
    concentration_rows: list[dict[str, Any]] = []
    for name, frame in periods.items():
        point[name] = formal_sharpe(
            frame["ret"].to_numpy(), frame["cash_hurdle_ret"].to_numpy(),
            annualization)
        formal = formal_summary.loc[
            formal_summary["subperiod"].eq(name)].iloc[0]
        if int(formal["evaluation_sessions"]) != len(frame):
            raise RuntimeError(f"session-count mismatch for {name}")
        if not math.isclose(
                float(formal["sharpe_calendar"]), point[name],
                rel_tol=0.0, abs_tol=5e-12):
            raise RuntimeError(f"Sharpe point-estimate mismatch for {name}")
        excess = frame["ret"].to_numpy() - frame["cash_hurdle_ret"].to_numpy()
        neff = concentration_neff(excess)
        audit_rows.append({
            "period": name,
            "first": str(frame["session_date"].min().date()),
            "last": str(frame["session_date"].max().date()),
            "sessions": len(frame),
            "formal_sharpe": point[name],
        })
        concentration_rows.append({
            "period": name,
            "sessions": len(frame),
            "squared_return_concentration_neff": neff,
            "normal_fourth_moment_equivalent_sessions": 3.0 * neff,
        })

    bootstrap = config["bootstrap"]
    bootstrap_rows: list[dict[str, Any]] = []
    primary_draws: dict[str, np.ndarray] = {}
    methods = list(bootstrap["sensitivity_methods"])
    blocks = list(bootstrap["sensitivity_block_lengths_sessions"])
    for method in methods:
        circular = method == "circular_moving_block"
        if method not in {"circular_moving_block", "moving_block"}:
            raise ValueError(f"unsupported bootstrap method: {method}")
        for block in blocks:
            draws: dict[str, np.ndarray] = {}
            for name, frame in periods.items():
                draws[name] = bootstrap_sharpes(
                    frame["ret"].to_numpy(),
                    frame["cash_hurdle_ret"].to_numpy(), annualization,
                    int(bootstrap["replications"]), int(block),
                    seed_for(int(bootstrap["seed"]), method, int(block), name),
                    circular)
                if (method == bootstrap["primary_method"]
                        and block == bootstrap["primary_block_length_sessions"]):
                    primary_draws[name] = draws[name]
                for level in bootstrap["confidence_levels"]:
                    bootstrap_rows.append({
                        "method": method,
                        "block_length_sessions": block,
                        "period": name,
                        "confidence_level": level,
                        **quantile_record(draws[name], point[name], float(level)),
                        "centered_one_sided_p_decline": math.nan,
                        "centered_two_sided_p_difference": math.nan,
                    })
            difference_draws = draws["pre_publication"] - draws["post_publication"]
            difference_point = point["pre_publication"] - point["post_publication"]
            centered = difference_draws - difference_point
            for level in bootstrap["confidence_levels"]:
                bootstrap_rows.append({
                    "method": method,
                    "block_length_sessions": block,
                    "period": "pre_minus_post",
                    "confidence_level": level,
                    **quantile_record(
                        difference_draws, difference_point, float(level)),
                    "centered_one_sided_p_decline": float(
                        np.mean(centered >= difference_point)),
                    "centered_two_sided_p_difference": float(
                        np.mean(np.abs(centered) >= abs(difference_point))),
                })

    hac = config["hac"]
    hac_rows: list[dict[str, Any]] = []
    for lag in hac["sensitivity_max_lags_sessions"]:
        estimates = {
            name: hac_sharpe(
                frame["ret"].to_numpy(),
                frame["cash_hurdle_ret"].to_numpy(), annualization, int(lag))
            for name, frame in periods.items()
        }
        difference_point = estimates["pre_publication"][0] - estimates["post_publication"][0]
        difference_se = math.sqrt(
            estimates["pre_publication"][1] ** 2
            + estimates["post_publication"][1] ** 2)
        for name, (estimate, se) in estimates.items():
            for level in hac["confidence_levels"]:
                low, high = normal_interval(estimate, se, float(level))
                hac_rows.append({
                    "max_lag_sessions": lag,
                    "period": name,
                    "confidence_level": level,
                    "point": estimate,
                    "hac_se": se,
                    "ci_low": low,
                    "ci_high": high,
                    "one_sided_p_decline": math.nan,
                    "two_sided_p_difference": math.nan,
                })
        z_value = difference_point / difference_se
        one_sided = NormalDist().cdf(-z_value)
        two_sided = 2.0 * NormalDist().cdf(-abs(z_value))
        for level in hac["confidence_levels"]:
            low, high = normal_interval(difference_point, difference_se, float(level))
            hac_rows.append({
                "max_lag_sessions": lag,
                "period": "pre_minus_post",
                "confidence_level": level,
                "point": difference_point,
                "hac_se": difference_se,
                "ci_low": low,
                "ci_high": high,
                "one_sided_p_decline": one_sided,
                "two_sided_p_difference": two_sided,
            })

    primary_method = bootstrap["primary_method"]
    primary_block = bootstrap["primary_block_length_sessions"]
    primary_diff = primary_draws["pre_publication"] - primary_draws["post_publication"]
    decision = {
        "primary_bootstrap_method": primary_method,
        "primary_block_length_sessions": primary_block,
        "replications": bootstrap["replications"],
        "sharpe_pre": point["pre_publication"],
        "sharpe_post": point["post_publication"],
        "sharpe_difference_pre_minus_post": (
            point["pre_publication"] - point["post_publication"]),
        "post_bootstrap_90_interval": [
            float(np.quantile(primary_draws["post_publication"], 0.05)),
            float(np.quantile(primary_draws["post_publication"], 0.95)),
        ],
        "difference_bootstrap_90_interval": [
            float(np.quantile(primary_diff, 0.05)),
            float(np.quantile(primary_diff, 0.95)),
        ],
        "conclusion": (
            "The point estimate is lower post-publication, but the primary "
            "90% bootstrap interval for the Sharpe difference includes zero; "
            "do not call the decline statistically significant."
        ),
    }
    frames = {
        "point_estimate_audit.csv": pd.DataFrame(audit_rows),
        "bootstrap_summary.csv": pd.DataFrame(bootstrap_rows),
        "hac_sharpe_summary.csv": pd.DataFrame(hac_rows),
        "return_concentration.csv": pd.DataFrame(concentration_rows),
    }
    provenance = {
        "classification": config["classification"],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "config": config,
        "config_sha256": sha256(config_path),
        "script_sha256": sha256(Path(__file__)),
        "source_run": str(run.relative_to(ROOT)).replace("\\", "/"),
        "source_manifest_sha256": sha256(run / "manifest.json"),
        "source_daily_results_sha256": sha256(run / "daily_results.parquet"),
        "source_summary_sha256": sha256(run / "summary.csv"),
        "decision": decision,
        "interpretation_limits": [
            "post-result statistical addendum; frozen v2 point estimates are unchanged",
            "bootstrap intervals are conditional on events observed in each period",
            "percentile intervals are not posterior probabilities for the true Sharpe",
            "bootstrap P(draw <= 0) is diagnostic and is not labeled a p-value",
            "concentration N_eff is not a serial-independence effective sample size",
        ],
    }
    return frames, provenance


def publish(
        output_root: Path, run_name: str, frames: dict[str, pd.DataFrame],
        provenance: dict[str, Any]) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    final = output_root / run_name
    if final.exists():
        raise FileExistsError(f"output already exists: {final}")
    final.mkdir()
    try:
        files: dict[str, dict[str, Any]] = {}
        for name, frame in frames.items():
            path = final / name
            frame.to_csv(path, index=False)
            files[name] = {"sha256": sha256(path), "rows": len(frame)}
        manifest = {**provenance, "files": files}
        manifest_path = final / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        success = {
            "classification": provenance["classification"],
            "manifest_sha256": sha256(manifest_path),
            "run_name": run_name,
        }
        # _SUCCESS is deliberately last: a directory without it is partial.
        (final / "_SUCCESS").write_text(
            json.dumps(success, sort_keys=True) + "\n", encoding="utf-8")
    except Exception:
        shutil.rmtree(final, ignore_errors=True)
        raise
    return final


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    config = load_config(config_path)
    frames, provenance = make_results(config, config_path)
    created_at = datetime.fromisoformat(provenance["created_at_utc"])
    run_name = args.run_name or created_at.strftime(
        "%Y%m%dT%H%M%SZ_uncertainty_v1")
    output = publish(args.output_root.resolve(), run_name, frames, provenance)
    print(output)
    print(json.dumps(provenance["decision"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
