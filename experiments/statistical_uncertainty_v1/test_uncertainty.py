"""Focused tests for the statistical-uncertainty addendum."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

import run_uncertainty as U


def main() -> int:
    returns = np.array([0.01, -0.005, 0.002, 0.004, -0.001])
    hurdles = np.array([0.0001] * len(returns))
    expected = np.sqrt(252) * np.mean(returns - hurdles) / np.std(
        returns, ddof=1)
    assert math.isclose(U.formal_sharpe(returns, hurdles, 252), expected)

    iid_a = U.bootstrap_sharpes(
        returns, hurdles, 252, 64, 1, seed=123, circular=True)
    iid_b = U.bootstrap_sharpes(
        returns, hurdles, 252, 64, 1, seed=123, circular=False)
    assert np.array_equal(iid_a, iid_b)
    assert np.array_equal(
        iid_a,
        U.bootstrap_sharpes(
            returns, hurdles, 252, 64, 1, seed=123, circular=True))

    circular = U.bootstrap_sharpes(
        returns, hurdles, 252, 64, 3, seed=321, circular=True)
    moving = U.bootstrap_sharpes(
        returns, hurdles, 252, 64, 3, seed=321, circular=False)
    assert circular.shape == moving.shape == (64,)
    assert np.isfinite(circular).all() and np.isfinite(moving).all()
    assert not np.array_equal(circular, moving)

    omega = U.long_run_covariance(np.column_stack((returns, returns ** 2)), 0)
    centered = np.column_stack((returns, returns ** 2))
    centered -= centered.mean(axis=0)
    assert np.allclose(omega, centered.T @ centered / len(centered))
    point, se = U.hac_sharpe(returns, hurdles, 252, 1)
    assert math.isclose(point, expected)
    assert np.isfinite(se) and se >= 0

    repeated = np.tile(np.array([-2.0, -1.0, 1.0, 2.0]), 10)
    assert math.isclose(U.concentration_neff(repeated), 29.41176470588235)

    config = U.load_config(Path(__file__).with_name("config.json"))
    _, daily, summary = U.audit_and_load(config)
    split = np.datetime64(config["post_publication_start"])
    pre = daily[daily["session_date"].to_numpy() < split]
    post = daily[daily["session_date"].to_numpy() >= split]
    assert len(pre) == 4055
    assert len(post) == 548
    for name, frame in (("pre_publication", pre), ("post_publication", post)):
        observed = U.formal_sharpe(
            frame["ret"].to_numpy(), frame["cash_hurdle_ret"].to_numpy(), 252)
        expected_formal = float(summary.loc[
            summary["subperiod"].eq(name), "sharpe_calendar"].iloc[0])
        assert math.isclose(observed, expected_formal, abs_tol=5e-12)

    print("STATISTICAL UNCERTAINTY TESTS PASSED (unit + formal-run audit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
