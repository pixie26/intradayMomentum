"""Check repository-local Markdown targets without accessing the network."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote


LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "#")
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[/\\]")


def tracked_markdown() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard",
         "--", "*.md"],
        text=True, encoding="utf-8")
    return [Path(line) for line in output.splitlines() if line]


def target_path(raw_target: str) -> str:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1:target.index(">")]
    else:
        target = target.split(maxsplit=1)[0]
    return unquote(target.split("#", 1)[0])


def main() -> int:
    root = Path.cwd().resolve()
    errors: list[str] = []
    checked = 0

    for relative in tracked_markdown():
        text = relative.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            raw = match.group(1).strip()
            line = text.count("\n", 0, match.start()) + 1
            if raw.startswith(EXTERNAL_PREFIXES):
                continue
            if raw.startswith("file:") or WINDOWS_ABSOLUTE_RE.match(raw):
                errors.append(
                    f"{relative}:{line}: local absolute link is not portable: {raw}")
                continue
            local = target_path(raw)
            if not local:
                continue
            checked += 1
            resolved = (relative.parent / local).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                errors.append(
                    f"{relative}:{line}: target leaves repository: {raw}")
                continue
            if not resolved.exists():
                errors.append(
                    f"{relative}:{line}: missing local target: {raw}")

    if errors:
        print("MARKDOWN LINK CHECK FAILED")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"MARKDOWN LINK CHECK PASSED ({checked} local targets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
