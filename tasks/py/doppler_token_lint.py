"""Flag scripts that store a Doppler token in the system keyring (#91).

Setting the `token` key via `doppler configure` writes it into the OS keyring
(PR #88); #89 moved pc's service token to a 0600 file read via `DOPPLER_TOKEN`.
`pc:doctor` only warns at runtime, so this blocks reintroducing the pattern in any
tracked file. Comment lines (`#`, `//`) that merely mention it are skipped.

Allow a reviewed line (same line or the line above) with `doppler-token: ok <reason>`;
the reason is required.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Matches `doppler configure set token ...` and `doppler configure set token=... project=...`.
PATTERN = re.compile(r"\bdoppler\s+configure\s+set\b[^\n]*?(?<![\w-])token\b")
ALLOW = re.compile(r"doppler-token:\s*ok\s+\S")
COMMENT_PREFIXES = ("#", "//")


def scan_text(text: str) -> list[tuple[int, str]]:
    """Return (line, source line) for each unallowed hit."""
    lines = text.splitlines()
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        if not PATTERN.search(line):
            continue
        if ALLOW.search(line) or (i > 0 and ALLOW.search(lines[i - 1])):
            continue
        if line.lstrip().startswith(COMMENT_PREFIXES):
            continue
        hits.append((i + 1, line.strip()))
    return hits


def read_text(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    data = path.read_bytes()
    if b"\0" in data:
        return None  # binary
    return data.decode("utf-8", errors="replace")


def main(argv: list[str]) -> int:
    bad = 0
    for arg in argv:
        path = Path(arg)
        text = read_text(path)
        if text is None:
            continue
        for line_no, src in scan_text(text):
            bad += 1
            sys.stderr.write(
                f"{path}:{line_no}: doppler-keyring-token: stores the Doppler token in the system keyring: {src}\n"
                "  keep the token in a 0600 file and export DOPPLER_TOKEN (see #89), or add "
                "`doppler-token: ok <reason>`\n"
            )
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
