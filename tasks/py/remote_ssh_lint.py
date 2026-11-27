#!/usr/bin/env python3
"""Flag ssh calls whose remote command the remote login shell would parse wrongly.

ssh joins its arguments into one command line that the remote *login shell* re-parses (zsh on pc).

- ssh-glob: an unquoted `*` or `?` in the remote command. zsh aborts on a glob with no match
  ("no matches found"), and bash would expand it instead. Use `find ... -name '<pattern>'` or send a
  `/bin/bash -euo pipefail <<'EOF'` heredoc.
- ssh-env: `ssh host env NAME=<expansion>`. A value holding a space or a quote is split again
  remotely. Build the command with `printf -v cmd 'env NAME=%q ...' "$value"` and pass "$cmd".

Heredoc bodies are skipped: they are fed to an explicit remote interpreter, not the login shell.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SSH_WORD = re.compile(r"(?:^|[\s(|;&!])ssh\s")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
ENV_ASSIGN = re.compile(r"""^["']?[A-Za-z_][A-Za-z0-9_]*=""")
SKIP_OPTS_WITH_ARG = {
    "-o",
    "-i",
    "-p",
    "-l",
    "-F",
    "-J",
    "-E",
    "-L",
    "-R",
    "-D",
    "-W",
    "-b",
    "-c",
    "-m",
    "-O",
    "-Q",
    "-S",
    "-w",
    "-B",
    "-e",
}
TERMINATORS = ("|", ";", "&&", "||", "<", ">", ")", "&")


def logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash continuations and drop heredoc bodies; keep the first line number."""
    out: list[tuple[int, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        start, buf = i + 1, lines[i]
        while buf.endswith("\\") and i + 1 < len(lines):
            i += 1
            buf = buf[:-1] + " " + lines[i].lstrip()
        out.append((start, buf))
        terms = [m.group(2) for m in HEREDOC.finditer(buf)]
        for term in terms:  # skip each heredoc body in order
            i += 1
            while i < len(lines) and lines[i].strip() != term:
                i += 1
        i += 1
    return out


def tokens(s: str) -> list[str]:
    """Split a shell fragment into raw words, keeping quotes; stop at a top-level terminator."""
    words: list[str] = []
    cur, i, depth = "", 0, 0
    while i < len(s):
        c = s[i]
        if c in "'\"":
            j = i + 1
            while j < len(s) and s[j] != c:
                j += 2 if c == '"' and s[j] == "\\" else 1
            cur += s[i : j + 1]
            i = j + 1
            continue
        if c == "$" and s.startswith("$(", i):
            depth += 1
        elif c == ")" and depth:
            depth -= 1
        elif depth == 0 and (
            c.isspace() or any(s.startswith(t, i) for t in TERMINATORS)
        ):
            if cur:
                words.append(cur)
                cur = ""
            if not c.isspace():
                break
            i += 1
            continue
        cur += c
        i += 1
    if cur:
        words.append(cur)
    return words


def remote_view(word: str) -> str:
    """Text the remote shell sees, minus the parts it will itself treat as quoted."""
    if len(word) >= 2 and word[0] == word[-1] == "'":
        body = word[1:-1]
    elif len(word) >= 2 and word[0] == word[-1] == '"':
        body = word[1:-1].replace('\\"', '"').replace("\\$", "$")
    else:
        body = word
    out, i = "", 0
    while i < len(body):
        c = body[i]
        if c in "'\"":
            j = i + 1
            while j < len(body) and body[j] != c:
                j += 2 if c == '"' and body[j] == "\\" else 1
            i = j + 1
            continue
        if c == "\\":
            i += 2
            continue
        out += c
        i += 1
    return out


def check_line(path: str, lineno: int, line: str) -> list[str]:
    found: list[str] = []
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return found
    for m in SSH_WORD.finditer(line):
        words = tokens(line[m.end() :])
        k = 0
        while k < len(words) and words[k].startswith("-"):  # options
            k += 2 if words[k] in SKIP_OPTS_WITH_ARG else 1
        if k >= len(words):
            continue
        remote = words[k + 1 :]  # after the host
        if remote and remote[0] == "env":
            for w in remote[1:]:
                if not ENV_ASSIGN.match(w):
                    break
                if "$" in w:
                    found.append(
                        f"{path}:{lineno}: ssh-env: `{w}` is re-split by the remote shell; build the command with printf %q"
                    )
        for w in remote:
            if (
                "$" in w
                and w.strip("\"'").startswith("${")
                and w.strip("\"'").endswith("}")
                and w.count("$") == 1
            ):
                continue  # one prebuilt command variable, e.g. "${probe_cmd}"
            view = remote_view(w)
            if re.search(r"[*?]", re.sub(r"\$[?*#@]", "", view)):
                found.append(
                    f"{path}:{lineno}: ssh-glob: unquoted glob in remote command `{w[:80]}`; zsh aborts on no match, use find -name '<pattern>' or a bash heredoc"
                )
    return found


def main(argv: list[str]) -> int:
    violations: list[str] = []
    checked = 0
    for name in argv:
        p = Path(name)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        if not text.startswith("#!") or "sh" not in text.splitlines()[0]:
            continue
        checked += 1
        for lineno, line in logical_lines(text):
            violations += check_line(name, lineno, line)
    if violations:
        sys.stderr.write("\n".join(violations) + "\n")
        return 1
    sys.stdout.write(f"remote-ssh-lint: {checked} shell task(s) clean\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
