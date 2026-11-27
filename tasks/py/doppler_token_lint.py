"""Flag scripts that store a Doppler token in the system keyring (#91).

Setting the `token` key via `doppler configure` writes it into the OS keyring
(PR #88); #89 moved pc's service token to a 0600 file read via `DOPPLER_TOKEN`.
`pc:doctor` only warns at runtime, so this blocks reintroducing the pattern in any
tracked file.

Matching works on words, not raw text, so shell strings, argv lists
(`["doppler", "configure", ...]`), `\\`-continued lines, lists split one item per
line, and interleaved flags (`doppler --scope / configure ...`) are all caught.
Comments (`# `, `// `, `#!`; not Pkl's `#"` raw strings) are dropped first, and a
command stops at `;`, `&`, or `|`.

Allow a reviewed invocation with `doppler-token: ok <reason>` on one of its own
lines or on a comment-only line directly above it. The reason is required.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ALLOW = re.compile(r"doppler-token:\s*ok\s+\w")
# `#`/`//` starts a comment at line start or after whitespace, only when followed by
# whitespace, `!`, or end of line. That leaves Pkl `#"..."#` raw strings, `${#x}`, and URLs as code.
COMMENT = re.compile(r"(?:^|(?<=\s))(?:#+|//+)(?=[\s!]|$)")
SEGMENT_END = re.compile(r"&&|\|\||[;&|]")
NOT_WORD = re.compile(r"""["'`,\[\](){}]""")
JOINERS = ("\\", ",", "[", "(")


def in_quotes(prefix: str) -> bool:
    """True if `prefix` ends inside a '...' or "..." string (backslash escapes count outside '...')."""
    quote = ""
    escaped = False
    for ch in prefix:
        if escaped:
            escaped = False
        elif ch == "\\" and quote != "'":
            escaped = True
        elif quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
    return bool(quote)


def code_part(line: str) -> str:
    """The line up to its first comment marker that is not inside a quoted string."""
    for m in COMMENT.finditer(line):
        if not in_quotes(line[: m.start()]):
            return line[: m.start()]
    return line


def is_comment_only(line: str) -> bool:
    return bool(line.strip()) and not code_part(line).strip()


def logical_lines(lines: list[str]) -> list[tuple[int, int, str]]:
    """Join `\\` continuations and lines ending in `,`, `[`, `(`; return (first, last, code)."""
    out: list[tuple[int, int, str]] = []
    i = 0
    while i < len(lines):
        start, buf = i, code_part(lines[i])
        while i + 1 < len(lines):
            tail = buf.rstrip()
            if not tail.endswith(JOINERS):
                break
            if tail.endswith("\\"):
                tail = tail[:-1]
            i += 1
            buf = tail + " " + code_part(lines[i])
        out.append((start, i, buf))
        i += 1
    return out


def skip_to(words: list[str], i: int, target: str) -> int | None:
    """Index after `target`, allowing only flags and their values before it."""
    takes_value = False
    while i < len(words):
        w = words[i]
        if w == target:
            return i + 1
        if w.startswith("-"):
            takes_value = "=" not in w
        elif takes_value:
            takes_value = False
        else:
            return None
        i += 1
    return None


def sets_token(words: list[str], i: int) -> bool:
    """After `set`: is `token` among the keys (past flags, flag values, and other key=value pairs)?"""
    takes_value = False
    for w in words[i:]:
        if w == "token" or w.startswith("token="):
            return True
        if w.startswith("-"):
            takes_value = "=" not in w
        elif takes_value or "=" in w:
            takes_value = False
        else:
            return False
    return False


def invokes(code: str) -> bool:
    for segment in SEGMENT_END.split(code):
        words = NOT_WORD.sub(" ", segment).split()
        for k, w in enumerate(words):
            if w != "doppler" and not w.endswith("/doppler"):
                continue
            j = skip_to(words, k + 1, "configure")
            j = None if j is None else skip_to(words, j, "set")
            if j is not None and sets_token(words, j):
                return True
    return False


def scan_text(text: str) -> list[tuple[int, str]]:
    """Return (first line, source line) for each unallowed hit."""
    lines = text.splitlines()
    hits: list[tuple[int, str]] = []
    for start, end, code in logical_lines(lines):
        if not invokes(code):
            continue
        if any(ALLOW.search(line) for line in lines[start : end + 1]):
            continue
        above = lines[start - 1] if start else ""
        if is_comment_only(above) and ALLOW.search(above):
            continue
        hits.append((start + 1, lines[start].strip()))
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
