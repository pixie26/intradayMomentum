"""Extract an exact SPY dividend CSV from State Street's official XLSX."""

from __future__ import annotations

import argparse
import hashlib
import re
import zipfile
from csv import writer
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree as ET


NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
SHARED_STRINGS = "xl/sharedStrings.xml"
DIVIDEND_SHEET = "xl/worksheets/sheet1.xml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_xlsx", type=Path)
    parser.add_argument("output_csv", type=Path)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--ticker", default="SPY")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cell_column(reference: str) -> int:
    match = re.match(r"[A-Z]+", reference)
    if match is None:
        raise ValueError(f"Invalid XLSX cell reference: {reference!r}")
    result = 0
    for letter in match.group():
        result = result * 26 + ord(letter) - ord("A") + 1
    return result - 1


def load_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    strings: list[str] = []
    with archive.open(SHARED_STRINGS) as stream:
        for _, elem in ET.iterparse(stream, events=("end",)):
            if elem.tag == f"{NS}si":
                strings.append("".join(
                    node.text or "" for node in elem.iter(f"{NS}t")
                ))
                elem.clear()
    return strings


def extract_rows(
        archive: zipfile.ZipFile, strings: list[str], ticker: str,
        start: date, end: date) -> list[tuple[date, str]]:
    rows: list[tuple[date, str]] = []
    with archive.open(DIVIDEND_SHEET) as stream:
        for _, elem in ET.iterparse(stream, events=("end",)):
            if elem.tag != f"{NS}row":
                continue
            values: list[str | None] = [None] * 10
            for cell in elem.findall(f"{NS}c"):
                column = cell_column(cell.attrib["r"])
                if column >= len(values):
                    continue
                value = cell.find(f"{NS}v")
                raw = None if value is None else value.text
                if raw is not None and cell.attrib.get("t") == "s":
                    raw = strings[int(raw)]
                values[column] = raw
            if values[1] == ticker:
                ex_date = datetime.strptime(values[3], "%m/%d/%Y").date()
                if start <= ex_date <= end:
                    rows.append((ex_date, values[6]))
            elem.clear()
    rows.sort()
    if len(rows) != len({ex_date for ex_date, _ in rows}):
        raise ValueError("Official workbook contains duplicate ex-dates.")
    return rows


def main() -> None:
    args = parse_args()
    if args.start > args.end:
        raise ValueError("--start must not be after --end.")
    with zipfile.ZipFile(args.input_xlsx) as archive:
        rows = extract_rows(
            archive, load_shared_strings(archive), args.ticker,
            args.start, args.end)

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open("w", newline="", encoding="utf-8") as stream:
        csv_writer = writer(stream, lineterminator="\n")
        csv_writer.writerow(["Date", "Dividend"])
        csv_writer.writerows(rows)

    print(f"rows={len(rows)}")
    print(f"source_sha256={sha256_file(args.input_xlsx)}")
    print(f"output_sha256={sha256_file(args.output_csv)}")


if __name__ == "__main__":
    main()
