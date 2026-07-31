"""Focused accounting and period tests for the attribution report."""

from __future__ import annotations

import pandas as pd

from evaluation.make_attribution_report import COMPONENTS, linked_contributions


def main() -> int:
    frame = pd.DataFrame({
        "ret": [0.10, -0.03],
        **{
            f"ret_{name}": values for name, values in {
                "long": [0.08, -0.03],
                "short": [0.03, 0.01],
                "commission": [-0.005, -0.005],
                "slippage": [-0.005, -0.005],
                "cash_interest": [0.001, 0.001],
                "funding": [-0.0005, -0.0005],
                "borrow": [-0.0005, -0.0005],
            }.items()
        },
    })
    assert max(abs(
        frame["ret"] - frame[[f"ret_{x}" for x in COMPONENTS]].sum(axis=1)
    )) < 1e-12
    linked = linked_contributions(frame)
    total = (1.10 * 0.97) - 1.0
    assert abs(sum(linked.values()) - total) < 1e-12
    assert abs(linked["long"] - (0.08 * 0.97 - 0.03)) < 1e-12
    print("ATTRIBUTION REPORT TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
