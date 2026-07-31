"""Rebuild docs/PAPER_TRADING_LOGIC.html from its markdown source.

The markdown uses \\(...\\) / \\[...\\] math delimiters. The locally available
pandoc (2.12) advertises the tex_math_single_backslash extension but does not
honour it, so this script first rewrites those delimiters to $...$ / $$...$$
(outside fenced code blocks and inline code spans), then runs pandoc with
tex_math_dollars (default), and finally re-appends the mermaid bootstrap that
turns <pre class="mermaid"> blocks into diagrams.

Verified: the pipeline reproduces the previously hand-built HTML byte-for-byte
(before later markdown edits).

Run:
    python scripts/build_trading_logic_html.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "docs" / "PAPER_TRADING_LOGIC_AND_IMPLEMENTATION_ZH.md"
OUTPUT = REPO_ROOT / "docs" / "PAPER_TRADING_LOGIC.html"
TITLE = "Paper Trading Logic and Implementation"
MATHJAX = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml-full.js"
MERMAID_SCRIPT = """<script type="module">
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11.16.0/dist/mermaid.esm.min.mjs';
mermaid.initialize({ startOnLoad: false });
document.querySelectorAll('pre.mermaid').forEach((el) => { el.textContent = el.textContent; });
await mermaid.run({ querySelector: 'pre.mermaid' });
</script>
</body>"""

FENCE_RE = re.compile(r"^(```|~~~)")
INLINE_CODE_RE = re.compile(r"(`[^`]*`)")


def dollar_math(markdown: str) -> str:
    """Convert \\( \\) \\[ \\] delimiters to $ $$ outside code regions."""
    out: list[str] = []
    in_fence = False
    for line in markdown.split("\n"):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        parts = INLINE_CODE_RE.split(line)
        for i, part in enumerate(parts):
            if i % 2 == 1:  # inline code span: leave untouched
                continue
            part = part.replace("\\[", "$$").replace("\\]", "$$")
            part = part.replace("\\(", "$").replace("\\)", "$")
            parts[i] = part
        out.append("".join(parts))
    return "\n".join(out)


def main() -> int:
    markdown = SOURCE.read_text(encoding="utf-8")
    preprocessed = dollar_math(markdown)
    result = subprocess.run(
        [
            "pandoc", "-f", "markdown", "-t", "html", "-s",
            f"--mathjax={MATHJAX}",
            "-M", f"title={TITLE}",
        ],
        input=preprocessed, capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        return result.returncode
    html = result.stdout.replace("</body>", MERMAID_SCRIPT)
    OUTPUT.write_text(html, encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size:,} bytes) from {SOURCE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
