#!/usr/bin/env python3
"""Check relative links and heading anchors in the project's Markdown.

External URLs are not fetched: the check is offline by design. It verifies
that every relative file link resolves and that every in-repository
``#anchor`` names a heading in its target file.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent

TARGETS = [
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "CODE_OF_CONDUCT.md",
    ROOT / "CHANGELOG.md",
    *sorted((ROOT / "docs").rglob("*.md")),
    *sorted((ROOT / ".github").rglob("*.md")),
]

LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)>\s]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
FENCE = re.compile(r"^(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def strip_code(text: str) -> str:
    """Drop fenced code blocks so links inside examples are ignored."""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if FENCE.match(line.strip()):
            inside = not inside
            continue
        if not inside:
            out.append(line)
    return "\n".join(out)


def slug(heading: str) -> str:
    """GitHub's heading slug: lowercase, punctuation removed, spaces to hyphens."""
    text = heading.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", "-", text)


def anchors(path: Path) -> set[str]:
    text = strip_code(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    counts: dict[str, int] = {}
    for line in text.splitlines():
        match = HEADING.match(line)
        if not match:
            continue
        base = slug(match.group(2))
        count = counts.get(base, 0)
        counts[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    return found


def main() -> int:
    failures: list[str] = []
    checked = 0
    cache: dict[Path, set[str]] = {}

    for source in TARGETS:
        if not source.is_file():
            failures.append(f"{source.relative_to(ROOT)}: target file is missing")
            continue
        text = strip_code(source.read_text(encoding="utf-8"))
        for raw in LINK.findall(text):
            target = unquote(raw)
            if not target or SCHEME.match(target) or target.startswith("//"):
                continue
            checked += 1
            path_part, _, anchor = target.partition("#")
            destination = source if not path_part else (source.parent / path_part)
            try:
                destination = destination.resolve()
            except OSError:
                failures.append(f"{source.relative_to(ROOT)}: bad path {target}")
                continue
            if not destination.exists():
                failures.append(f"{source.relative_to(ROOT)}: missing file for {target}")
                continue
            if anchor and destination.suffix.lower() == ".md":
                if destination not in cache:
                    cache[destination] = anchors(destination)
                if slug(anchor) not in cache[destination]:
                    failures.append(
                        f"{source.relative_to(ROOT)}: missing anchor #{anchor} in "
                        f"{destination.relative_to(ROOT)}"
                    )

    if failures:
        print(f"Link check failed ({len(failures)} problem(s)):")
        for problem in failures:
            print(f"  - {problem}")
        return 1

    print(f"Link check passed: {checked} relative links across {len(TARGETS)} files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
