#!/usr/bin/env python3
"""Fail when a file that /usr/bin/python3 (3.9 on macOS) runs does not compile under Python 3.9 (#112).

hk runs this under /usr/bin/python3 when that interpreter is 3.9, which is authoritative. Elsewhere (Linux CI,
where /usr/bin/python3 is newer) it falls back to ast.parse(feature_version=(3, 9)), which catches most newer
grammar (unparenthesized except tuples, match, ...) but not every case (e.g. nested same-quote f-strings).
Stdlib only; must itself stay 3.9-compatible.
"""

from __future__ import annotations

import ast
import sys


def problems(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
    except (OSError, UnicodeDecodeError) as error:
        return [f"{path}: unreadable: {error}"]
    try:
        if sys.version_info[:2] == (3, 9):
            compile(source, path, "exec", dont_inherit=True)
        else:
            ast.parse(source, path, feature_version=(3, 9))
    except SyntaxError as error:
        return [f"{path}:{error.lineno}: not Python 3.9 syntax: {error.msg}"]
    return []


def main(argv: list[str]) -> int:
    found = [line for path in argv for line in problems(path)]
    if found:
        sys.stderr.write("\n".join(found) + "\n")
        sys.stderr.write(
            "These files run under /usr/bin/python3 (3.9.6 on macOS); use 3.9 syntax "
            "(parenthesized except tuples, no match, no nested same-quote f-strings).\n"
        )
        return 1
    mode = (
        "compiled" if sys.version_info[:2] == (3, 9) else "ast feature_version=(3, 9)"
    )
    sys.stdout.write(f"py39-compat: {len(argv)} file(s) clean ({mode})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
