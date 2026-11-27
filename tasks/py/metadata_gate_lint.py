#!/usr/bin/env python3
"""Flag security decisions keyed on unauthenticated commit/PR metadata (#99 no-security-gate-on-commit-metadata).

Commit author names/emails, `Co-Authored-By`/`Signed-off-by` trailers, and "bot-looking" name patterns are
all chosen by the PR author, so skipping review, trusting provenance, or granting privilege on them is
forgeable. Authenticate with an App-authored status/check-run, the event `sender`, or a permission lookup.

Checked: `.github/openai-review/*.py` (not `test_*.py`) and `.github/workflows/*.yml`.
Allow a reviewed line (same line or the line above) with `# metadata-gate: ok <reason>`; the reason is required.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "commit-author-identity",
        re.compile(
            r"""(?ix)
            \b(?:author|committer)_(?:name|email|login)\b
            | \b(?:head_commit|commit|commits?\[[^\]]*\]|pusher)\.(?:author|committer)\b
            | \.(?:author|committer)\.(?:name|email)\b
            | \b(?:author|committer)["']?\s*\]?\s*(?:==|!=)
            | --format=\S*%[ac][ne]\b
            """
        ),
    ),
    (
        "commit-trailer",
        re.compile(r"(?i)\b(?:co-authored-by|signed-off-by|reviewed-by|trailers?)\b"),
    ),
    (
        "bot-name-heuristic",
        re.compile(
            r"""(?ix)
            (?:endswith|startswith|contains|match|search)\b[^\n]{0,40}\[bot\]
            | \[bot\]["']?\s*\bin\b
            | \bin\b\s+[^\n]{0,40}\[bot\]
            | \buser\b[\]\["'.]*type["'\]]*\s*(?:==|!=)\s*["']?bot
            | \bis_bot\b
            """
        ),
    ),
)
ALLOW = re.compile(r"metadata-gate:\s*ok\s+\S")


def scan_text(text: str) -> list[tuple[int, str, str]]:
    """Return (line, rule, source line) for each unallowed hit."""
    lines = text.splitlines()
    hits: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines):
        if ALLOW.search(line) or (i > 0 and ALLOW.search(lines[i - 1])):
            continue
        if line.lstrip().startswith("#"):
            continue
        for rule, pat in RULES:
            if pat.search(line):
                hits.append((i + 1, rule, line.strip()))
                break
    return hits


def in_scope(path: Path) -> bool:
    posix = f"/{path.as_posix()}"
    if posix.endswith(".py"):
        return "/.github/openai-review/" in posix and not path.name.startswith("test_")
    return posix.endswith((".yml", ".yaml")) and "/.github/workflows/" in posix


def main(argv: list[str]) -> int:
    bad = 0
    for arg in argv:
        path = Path(arg)
        if not in_scope(path):
            continue
        for line_no, rule, src in scan_text(path.read_text(encoding="utf-8")):
            bad += 1
            sys.stderr.write(
                f"{path}:{line_no}: {rule}: security decision on forgeable commit/PR metadata: {src}\n"
                "  authenticate (App-authored check/status, event sender, permission API) or add "
                "`# metadata-gate: ok <reason>`\n"
            )
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
