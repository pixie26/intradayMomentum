"""Focused tests for the executable evaluation preflight and matrix."""

from __future__ import annotations

import tempfile
from pathlib import Path

from evaluation import run_evaluation as R


def main() -> int:
    spec_path = Path("config/evaluation_spec_v1.yml")
    spec, spec_hash = R.load_spec(spec_path)
    cells = R.build_cells(spec)
    assert len(spec_hash) == 64
    assert len(cells) == 72
    assert len({cell.cell_id for cell in cells}) == 72
    assert cells[0].cell_id == (
        "official_sample_compatible__paper_ready__with_dividends__slip_0p0010")

    release = R.inspect_release(Path("data_release_v1"))
    assert release["release_id"] == "data-v1.0"
    assert release["expected_start"] == "2008-01-22"
    assert release["expected_end"] == "2026-07-09"
    periods = R.build_periods(
        spec, release["expected_start"], release["expected_end"])
    assert [period.name for period in periods] == list(R.SUBPERIODS)
    assert str(periods[2].start.date()) == "2024-05-01"

    plan = R.make_plan(spec_path, Path("data_release_v1"))
    assert plan["matrix"]["cells"] == 72
    assert plan["matrix"]["expected_summary_rows"] == 216
    assert not plan["formal_ready"]
    assert any("spec_version" in gap for gap in plan["formal_gaps"])
    assert any("financing rate release" in gap for gap in plan["formal_gaps"])

    financing = R.inspect_financing_release(
        Path("data/reference/financing_rates_v1"),
        R.load_spec(Path("config/evaluation_spec_v2.yml"))[0])
    assert financing["release_id"] == "financing-rates-v1"
    assert financing["rows"] == 4645
    assert financing["first"] == "2008-01-22"
    assert financing["last"] == "2026-07-09"
    rates = R.load_financing_rates(Path(financing["daily_rates_path"]))
    assert rates[[
        "cash_rate_annual", "funding_rate_annual", "borrow_rate_annual"
    ]].notna().all(axis=None)

    frozen_v2 = R.load_spec(Path("config/evaluation_spec_v2.yml"))[0]
    amended_v2 = R.load_spec(
        Path("config/evaluation_spec_v2_halt_headline.yml"))[0]
    assert R.headline_label(frozen_v2) == (
        "corrected_execution × paper_ready × with-dividends × "
        "$0.0050/share slippage")
    assert R.headline_label(amended_v2) == (
        "corrected_execution × halt_aware × with-dividends × "
        "$0.0025/share slippage")

    import pandas as pd
    cell = cells[0]
    idx = pd.to_datetime(["2024-05-01", "2024-05-02"])
    result = pd.DataFrame({
        "status": ["active", "no_signal"],
        "is_evaluation": [True, True],
        "long_gross": [10.0, 0.0],
        "short_gross": [-2.0, 0.0],
        "signal_count": [2, 1],
        "holding_minutes": [30.0, 0.0],
        "prev_aum": [100_000.0, 100_008.0],
        "traded_notional": [200_000.0, 0.0],
        "shares_traded": [2_000.0, 0.0],
        "gross": [8.0, 0.0],
        "cost": [2.0, 0.0],
        "financing": [-1.0, 0.0],
    }, index=idx)
    result.attrs["daily_features"] = pd.DataFrame(
        {"dvol": [0.01, 0.02]}, index=idx)
    rows = R.decomposition_rows(
        cell, periods[2], result, {"round_trips": pd.DataFrame()})
    assert {row["component"] for row in rows} >= {
        "long_vs_short", "signal_count", "turnover",
        "gross_edge_per_traded_share", "cost_per_traded_share"}
    tagged_parts = []
    for label in ("a", "b"):
        tagged = result.reset_index()
        tagged.attrs = {}
        tagged.insert(0, "cell_id", label)
        tagged_parts.append(tagged)
    combined = pd.concat(tagged_parts, ignore_index=True)
    assert len(combined) == 2 * len(result)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        frames = {"summary.csv": __import__("pandas").DataFrame({"x": [1]})}
        out = R.publish(
            root, "unit", frames,
            {"classification": "unit_test", "created_at_utc": "fixed"},
            text_files={"report.html": "<html>unit</html>"})
        assert (out / "_SUCCESS").exists()
        assert (out / "report.html").exists()
        try:
            R.publish(
                root, "unit", frames,
                {"classification": "unit_test", "created_at_utc": "fixed"})
            raise AssertionError("publish overwrote an existing output")
        except FileExistsError:
            pass

    print("EVALUATION RUNNER TESTS PASSED (matrix=72, rows=216)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
