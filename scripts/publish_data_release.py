"""Atomically publish a verified data-pipeline run as data-v1.0."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path


REQUIRED_REPORTS = {
    "conflicting_ohlcv.csv",
    "conflicting_optional_metadata.csv",
    "extreme_continuous_1min_returns.csv",
    "gap_returns.csv",
    "halt_minutes.csv",
    "halt_reopen_returns.csv",
    "invalid_ohlc_rows.csv",
    "longest_stale_bar_runs.csv",
    "minute_coverage.csv",
    "off_grid_rows.csv",
    "outside_rth_rows.csv",
    "session_quality.csv",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("verified_against", type=Path)
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--release-id", default="data-v1.0")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(run_dir: Path) -> tuple[Path, dict]:
    path = run_dir / "reports" / "data_manifest.json"
    if not path.exists() or not (run_dir / "_SUCCESS").exists():
        raise ValueError(f"Run is not successfully published: {run_dir}")
    return path, json.loads(path.read_text(encoding="utf-8"))


def verify_acceptance(manifest: dict, release_id: str) -> None:
    duplicate = manifest["duplicate_audit"]
    counts = manifest["counts"]
    boundary = manifest["boundary_sessions_missing"]
    halt = manifest["halt_validation"]
    tests = manifest["data_self_tests"]
    git = manifest["git"]
    contract = manifest.get("release_contract") or {}
    failures = []

    checks = {
        "release_id": contract.get("release_id") == release_id,
        "release_contract_status":
            contract.get("status") == "frozen_contract",
        "expected_boundaries": boundary.get("known")
            and boundary.get("total") == 0,
        "ohlcv_conflicts":
            duplicate.get("conflicting_ohlcv_timestamps") == 0,
        "optional_metadata_conflicts":
            duplicate.get("conflicting_optional_metadata_timestamps") == 0,
        "off_grid": counts.get("off_grid_rows") == 0,
        "invalid_rows": counts.get("invalid_ohlc_rows_dropped") == 0,
        "halt_minutes": halt.get(
            "sessions_with_bars_present_during_halt") == 0
            and halt.get("sessions_with_unexpected_minutes") == 0,
        "self_tests": tests.get("passed") and tests.get("checks") == 28,
        "git_provenance": git.get("available") and not git.get("dirty")
            and git.get("script_matches_head"),
    }
    failures.extend(name for name, passed in checks.items() if not passed)
    if failures:
        raise ValueError(f"Release acceptance gates failed: {failures}")


def deterministic_paths(run_dir: Path, manifest: dict) -> list[Path]:
    paths = []
    for name, output in manifest["outputs"].items():
        if name != "audit_summary":
            paths.append(run_dir / output["path"])
    paths.append(run_dir / manifest["dividends"]["output"])
    report_dir = run_dir / manifest["reports"]["dir"]
    report_names = {path.name for path in report_dir.glob("*.csv")}
    missing = sorted(REQUIRED_REPORTS - report_names)
    if missing:
        raise ValueError(f"Required audit reports are missing: {missing}")
    paths.extend(report_dir / name for name in sorted(REQUIRED_REPORTS))
    return paths


def verify_reproduction(
        first_dir: Path, first: dict, second_dir: Path, second: dict) -> int:
    invariant_fields = [
        "source_sha256", "script_sha256", "expected_start", "expected_end",
        "observed_start", "observed_end", "bar_label",
    ]
    for field in invariant_fields:
        if first.get(field) != second.get(field):
            raise ValueError(f"Formal runs disagree on {field}.")
    if first["git"]["commit"] != second["git"]["commit"]:
        raise ValueError("Formal runs were not made from the same Git commit.")
    if (first["release_contract"]["sha256"]
            != second["release_contract"]["sha256"]):
        raise ValueError("Formal runs used different release contracts.")
    if first["environment_lock"]["sha256"] != second["environment_lock"]["sha256"]:
        raise ValueError("Formal runs used different dependency locks.")

    first_paths = deterministic_paths(first_dir, first)
    second_paths = deterministic_paths(second_dir, second)
    first_by_rel = {
        path.relative_to(first_dir): path for path in first_paths
    }
    second_by_rel = {
        path.relative_to(second_dir): path for path in second_paths
    }
    if first_by_rel.keys() != second_by_rel.keys():
        raise ValueError("Formal runs published different deterministic files.")
    mismatches = [
        str(relative) for relative in first_by_rel
        if sha256_file(first_by_rel[relative])
        != sha256_file(second_by_rel[relative])
    ]
    if mismatches:
        raise ValueError(
            f"Formal-run output hashes differ: {mismatches}")
    return len(first_by_rel)


def copy_file(source: Path, destination: Path) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return {
        "sha256": sha256_file(destination),
        "bytes": destination.stat().st_size,
    }


def main() -> None:
    args = parse_args()
    repo = Path(__file__).resolve().parents[1]
    run_dir = args.run_dir.resolve()
    verified_dir = args.verified_against.resolve()
    release_dir = args.release_dir.resolve()
    if repo not in release_dir.parents or release_dir == repo:
        raise ValueError("Release directory must be a child of the repository.")
    if release_dir.exists():
        raise FileExistsError(
            f"Refusing to overwrite an existing release: {release_dir}")

    manifest_path, manifest = load_manifest(run_dir)
    verified_manifest_path, verified = load_manifest(verified_dir)
    verify_acceptance(manifest, args.release_id)
    verify_acceptance(verified, args.release_id)
    compared_files = verify_reproduction(
        run_dir, manifest, verified_dir, verified)

    temp = release_dir.parent / (
        f".{release_dir.name}.tmp-{uuid.uuid4().hex}")
    if temp.exists():
        raise FileExistsError(f"Temporary release path exists: {temp}")
    temp.mkdir(parents=True)
    try:
        source_map = {
            "clean.parquet": run_dir / manifest["outputs"]["clean"]["path"],
            "feature_validity_session.parquet":
                run_dir / manifest["outputs"]["feature_validity_session"]["path"],
            "feature_validity_minute.parquet":
                run_dir / manifest["outputs"]["feature_validity_minute"]["path"],
            "session_quality.csv":
                run_dir / "reports" / "session_quality.csv",
            "minute_coverage.csv":
                run_dir / "reports" / "minute_coverage.csv",
            "spy_dividends_clean.csv":
                run_dir / manifest["dividends"]["output"],
            "audit_summary.md": run_dir / "audit_summary.md",
            "source_run_manifest.json": manifest_path,
        }
        for report_name in sorted(REQUIRED_REPORTS):
            source_map[f"reports/{report_name}"] = (
                run_dir / "reports" / report_name)

        files = {}
        for relative, source in source_map.items():
            if not source.exists():
                raise FileNotFoundError(source)
            files[relative] = {
                **copy_file(source, temp / relative),
                "source": str(source.relative_to(run_dir)),
            }

        release_manifest = {
            "release_manifest_schema_version": 1,
            "release_id": args.release_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_run_id": manifest["run_id"],
            "verified_against_run_id": verified["run_id"],
            "deterministic_files_compared": compared_files,
            "git_commit": manifest["git"]["commit"],
            "source_sha256": manifest["source_sha256"],
            "script_sha256": manifest["script_sha256"],
            "release_config_sha256":
                manifest["release_contract"]["sha256"],
            "requirements_lock_sha256":
                manifest["environment_lock"]["sha256"],
            "expected_start": manifest["expected_start"],
            "expected_end": manifest["expected_end"],
            "files": files,
        }
        release_manifest_path = temp / "data_manifest.json"
        release_manifest_path.write_text(
            json.dumps(release_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        success = {
            "release_id": args.release_id,
            "data_manifest_sha256": sha256_file(release_manifest_path),
            "git_commit": manifest["git"]["commit"],
        }
        (temp / "_SUCCESS").write_text(
            json.dumps(success, sort_keys=True) + "\n", encoding="utf-8")
        temp.replace(release_dir)
    except Exception:
        if (temp.exists() and temp.parent == release_dir.parent
                and temp.name.startswith(f".{release_dir.name}.tmp-")):
            shutil.rmtree(temp)
        raise

    print(f"release_dir={release_dir}")
    print(f"source_run={manifest['run_id']}")
    print(f"verified_against={verified['run_id']}")
    print(f"deterministic_files_compared={compared_files}")
    print(f"git_commit={manifest['git']['commit']}")


if __name__ == "__main__":
    main()
