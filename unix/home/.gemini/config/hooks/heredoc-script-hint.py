"""Non-blocking PreToolUse hint: put multi-line interpreter heredocs in script files (#93).

Shared by Antigravity (`hooks.json`), Claude Code, and Codex CLI (`dcg-hooks.json` assets).
dcg often blocks inline scripts (heredoc bodies that mention secrets tooling, `mv`/`rm`
under home, inline launchers); the fix every time was to write the body to a file first.
This hook only ever adds a hint: it never denies, and it exits 0 on any error.
Codex also runs it as PostToolUse (`--event post-tool-use`); see `render()` for why.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

HOOK_KEY = "heredoc-script-hint"
HARNESSES = ("agy", "claude", "codex")
WRITE_TOOL = {
    "agy": "`write_to_file`",
    "claude": "the Write/Edit tool",
    "codex": "`apply_patch`",
}

MAX_CHARS = 200_000
MAX_DEPTH = 8  # nested `$(...)` / `sh -c '...'` / `ssh host '...'` levels
REMOTE = "\x00remote"  # _command_of() marker: args[0] is a remote/inline command string
ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
INTERPRETER_RE = re.compile(
    r"(?:python(?:\d+(?:\.\d+)?)?|pypy3?|node|nodejs|deno|bun|ruby|perl|php|"
    r"lua(?:jit)?|Rscript|osascript|tclsh|pwsh|bash|sh|zsh|dash|ksh|mksh|fish)"
)
SUFFIX = {
    "python": ".py",
    "pypy": ".py",
    "node": ".mjs",
    "nodejs": ".mjs",
    "deno": ".ts",
    "bun": ".ts",
    "ruby": ".rb",
    "perl": ".pl",
    "php": ".php",
    "lua": ".lua",
    "luajit": ".lua",
    "Rscript": ".R",
    "osascript": ".applescript",
    "tclsh": ".tcl",
    "pwsh": ".ps1",
}


SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "mksh", "fish"}
# Leading words that are not the command: reserved words and transparent prefixes.
PREFIX_WORDS = {
    "!", "{", "then", "do", "else", "elif", "if", "while", "until", "time",
    "exec", "command", "builtin", "nohup", "noglob",
}  # fmt: skip
# Builtins that do not consume stdin, so stdin falls through to the next command.
NON_READERS = {
    "cd", "export", "set", "unset", "echo", "printf", "true", ":", "test", "[",
    "mkdir", "umask", "ulimit", "trap", "shopt", "local", "pushd", "popd",
}  # fmt: skip
SUDO_ARG_OPTS = {"-u", "-g", "-p", "-C", "-D", "-h", "-r", "-t", "-U", "-T"}
ENV_ARG_OPTS = {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}
SSH_ARG_OPTS = {
    "-b", "-B", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l",
    "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w",
}  # fmt: skip
DOCKER_ARG_OPTS = {
    "-e", "--env", "-u", "--user", "-w", "--workdir", "--env-file", "--detach-keys",
}  # fmt: skip
UV_ARG_OPTS = {
    "--with", "-w", "--python", "-p", "--project", "--directory", "--extra",
    "--group", "--env-file", "--package", "--with-requirements", "--with-editable",
    "--index", "--only-group", "--no-group",
}  # fmt: skip
NODE_ARG_OPTS = {
    "-r", "--require", "--import", "--loader", "--experimental-loader", "-C",
    "--conditions", "--input-type", "--env-file",
}  # fmt: skip
CODE_LETTERS = {
    "ruby": "e", "perl": "eE", "php": "r", "lua": "e", "luajit": "e",
    "Rscript": "e", "osascript": "e",
}  # fmt: skip
E_ARG_OPTS = {
    "ruby": {"-r", "-I", "-C", "-E"},
    "perl": {"-I"},
    "php": {"-c", "-d", "-z"},
    "lua": {"-l"},
    "luajit": {"-l", "-j"},
    "osascript": {"-l", "-s"},
}
PWSH_ARG_OPTS = {
    "executionpolicy", "ex", "ep", "workingdirectory", "wd", "outputformat", "of",
    "inputformat", "if", "configurationname", "settingsfile", "custompipename",
    "windowstyle",
}  # fmt: skip


class _Word:
    """One shell word: unquoted `value`, source text `raw`, and commands substituted in it."""

    __slots__ = ("raw", "subs", "value")

    def __init__(self, value: str, raw: str = "", subs: list[_Cmd] | None = None):
        self.value = value
        self.raw = raw or value
        self.subs = subs or []


class _Heredoc:
    __slots__ = ("body", "delim")

    def __init__(self, delim: str) -> None:
        self.delim = delim
        self.body = 0  # non-blank body lines


class _Cmd:
    """A simple command; `stdin` is its last stdin redirect, `pipe_from` the previous stage."""

    __slots__ = ("pipe_from", "stdin", "words")

    def __init__(self, pipe_from: _Cmd | None = None) -> None:
        self.words: list[_Word] = []
        self.stdin: _Heredoc | str | None = None
        self.pipe_from = pipe_from


class _Parser:
    """Small quote-aware shell lexer: enough structure to find who reads each heredoc.

    Tracks quotes, `\\`-newline continuations, `$(...)`/backtick substitutions (parsed
    as nested commands), `$((...))` arithmetic (so `<<` there is a shift), pipelines,
    list operators, redirects, and heredoc bodies (read at the next unquoted newline,
    as the shell does, including inside `$(...)`). It never raises on odd input.
    """

    def __init__(self, text: str) -> None:
        self.s = text[:MAX_CHARS]
        self.n = len(self.s)
        self.i = 0
        self.cmds: list[_Cmd] = []
        self.pending: list[_Heredoc] = []
        self.closers: list[str] = []

    def parse(self) -> list[_Cmd]:
        self._list(None)
        return self.cmds

    def _new_cmd(self, pipe_from: _Cmd | None = None) -> _Cmd:
        cmd = _Cmd(pipe_from)
        self.cmds.append(cmd)
        return cmd

    def _skip_blanks(self) -> None:
        while self.i < self.n and self.s[self.i] in " \t":
            self.i += 1

    def _skip_parens(self, depth: int) -> None:
        while self.i < self.n and depth:
            c = self.s[self.i]
            depth += (c == "(") - (c == ")")
            self.i += 1

    def _list(self, closer: str | None) -> None:
        s = self.s
        cmd = self._new_cmd()
        parens = 0
        self.closers.append(closer or "")
        try:
            while self.i < self.n:
                c = s[self.i]
                start = self.i
                if closer is not None and c == closer and not (c == ")" and parens):
                    self.i += 1
                    return
                if c in " \t":
                    self.i += 1
                elif c == "\\" and s.startswith("\n", self.i + 1):
                    self.i += 2
                elif c == "\n":
                    self.i += 1
                    self._read_bodies()
                    cmd = self._new_cmd()
                elif c == "#" and (self.i == 0 or s[self.i - 1] in " \t\n;&|()"):
                    nl = s.find("\n", self.i)
                    self.i = self.n if nl < 0 else nl
                elif c == "&" and s.startswith(">", self.i + 1):
                    self.i += 1
                    self._redirect(cmd)
                elif c in "|&;":
                    op = s[self.i : self.i + 2]
                    if c == "|" and op != "||":
                        self.i += 2 if op == "|&" else 1
                        cmd = self._new_cmd(pipe_from=cmd)
                    else:
                        self.i += 2 if op in ("||", "&&", ";;") else 1
                        cmd = self._new_cmd()
                elif c == "(" and s.startswith("((", self.i) and not cmd.words:
                    self.i += 2
                    self._skip_parens(2)  # `(( x << 2 ))` arithmetic command
                elif c in "()":
                    parens = max(0, parens + (1 if c == "(" else -1))
                    self.i += 1
                    cmd = self._new_cmd()
                elif c in "<>":
                    self._redirect(cmd)
                else:
                    word = self._word()
                    if word.raw.isdigit() and self.i < self.n and s[self.i] in "<>":
                        pass  # file-descriptor number of a redirect such as `2>`
                    elif word.raw:
                        cmd.words.append(word)
                if self.i == start:
                    self.i += 1  # guarantee progress on unexpected input
        finally:
            self.closers.pop()

    def _redirect(self, cmd: _Cmd) -> None:
        s = self.s
        if s.startswith("<<<", self.i):
            self.i += 3
            self._skip_blanks()
            self._word()
            cmd.stdin = "herestring"
        elif s.startswith("<<", self.i):
            self.i += 2
            if s.startswith("-", self.i):
                self.i += 1
            self._skip_blanks()
            delim = self._word()
            if delim.raw:
                doc = _Heredoc(delim.value)
                self.pending.append(doc)
                cmd.stdin = doc
        elif s.startswith(("<(", ">("), self.i):
            self.i += 2
            self._sub(")")
        else:
            is_input = s[self.i] == "<"
            self.i += 1
            if self.i < self.n and s[self.i] in ">&|":
                self.i += 1
            self._skip_blanks()
            self._word()
            if is_input:
                cmd.stdin = "file"

    def _read_bodies(self) -> None:
        pending, self.pending = self.pending, []
        for doc in pending:
            while self.i < self.n:
                nl = self.s.find("\n", self.i)
                end = self.n if nl < 0 else nl
                line = self.s[self.i : end].strip()
                self.i = end + 1
                if line == doc.delim:
                    break
                if line:
                    doc.body += 1

    def _sub(self, closer: str) -> list[_Cmd]:
        before = len(self.cmds)
        if len(self.closers) > 4 * MAX_DEPTH:
            end = self.s.find(closer, self.i)
            self.i = self.n if end < 0 else end + 1
        else:
            self._list(closer)
        return self.cmds[before:]

    def _in_backticks(self) -> bool:
        return bool(self.closers) and self.closers[-1] == "`"

    def _word(self) -> _Word:
        s = self.s
        start = self.i
        val: list[str] = []
        subs: list[_Cmd] = []
        while self.i < self.n:
            c = s[self.i]
            if c in " \t\n|&;()<>" or (c == "`" and self._in_backticks()):
                break
            if c == "\\":
                if not s.startswith("\n", self.i + 1):
                    val.append(s[self.i + 1 : self.i + 2])
                self.i += 2
            elif c == "'":
                end = s.find("'", self.i + 1)
                end = self.n if end < 0 else end
                val.append(s[self.i + 1 : end])
                self.i = end + 1
            elif c == '"':
                self.i += 1
                self._dquote(val, subs)
            elif c == "$":
                self._dollar(val, subs)
            elif c == "`":
                self.i += 1
                subs.extend(self._sub("`"))
                val.append("\x00")
            else:
                val.append(c)
                self.i += 1
        return _Word("".join(val), s[start : self.i], subs)

    def _dquote(self, val: list[str], subs: list[_Cmd]) -> None:
        s = self.s
        while self.i < self.n:
            c = s[self.i]
            if c == '"':
                self.i += 1
                return
            if c == "\\":
                nxt = s[self.i + 1 : self.i + 2]
                val.append(nxt if nxt in '$`"\\' else ("" if nxt == "\n" else c + nxt))
                self.i += 2
            elif c == "$":
                self._dollar(val, subs)
            elif c == "`":
                if self._in_backticks():
                    return
                self.i += 1
                subs.extend(self._sub("`"))
                val.append("\x00")
            else:
                val.append(c)
                self.i += 1

    def _dollar(self, val: list[str], subs: list[_Cmd]) -> None:
        s = self.s
        if s.startswith("$((", self.i):
            self.i += 3
            self._skip_parens(2)  # `$(( x << n ))` is a shift, not a heredoc
            val.append("\x00")
        elif s.startswith("$(", self.i):
            self.i += 2
            subs.extend(self._sub(")"))
            val.append("\x00")
        elif s.startswith("${", self.i):
            end = s.find("}", self.i + 2)
            self.i = self.n if end < 0 else end + 1
            val.append("\x00")
        elif s.startswith("$'", self.i):
            j = self.i + 2
            while j < self.n and s[j] != "'":
                j += 2 if s[j] == "\\" else 1
            val.append(s[self.i + 2 : j])
            self.i = j + 1
        else:
            val.append("$")
            self.i += 1


def _family(name: str) -> str:
    base = re.sub(r"[\d.]+$", "", name)
    if base in SHELLS:
        return "shell"
    if base in ("python", "pypy"):
        return "python"
    if base in ("node", "nodejs", "deno", "bun"):
        return "node"
    return base


def _arg(args: list[_Word], j: int) -> _Word | None:
    return args[j] if j < len(args) else None


def _skip_options(words: list[_Word], i: int, arg_opts: set[str]) -> int:
    """Skip `-x`/`--long` options (and the values of those in `arg_opts`); stop after `--`."""
    while i < len(words) and words[i].value.startswith("-") and words[i].value != "-":
        opt = words[i].value
        i += 1
        if opt == "--":
            break
        if opt in arg_opts:
            i += 1
    return i


def _command_of(words: list[_Word]) -> tuple[str, list[_Word]] | None:
    """Resolve the command word past assignments and wrappers; return (name, args).

    `ssh host 'remote cmd'` returns (REMOTE, [Word(remote cmd)]); `ssh host` alone
    returns ("sh", []) because the remote login shell reads its script from stdin.
    """
    i, n = 0, len(words)
    while i < n:
        word = words[i]
        name = word.value.rsplit("/", 1)[-1]
        nxt = words[i + 1].value if i + 1 < n else ""
        if ASSIGN_RE.match(word.raw) or word.value in PREFIX_WORDS:
            i += 1
        elif name in ("sudo", "doas"):
            i = _skip_options(words, i + 1, SUDO_ARG_OPTS)
        elif name == "env":
            i = _skip_options(words, i + 1, ENV_ARG_OPTS)
        elif name in ("nice", "stdbuf", "caffeinate"):
            i = _skip_options(words, i + 1, {"-n"})
        elif name == "timeout":
            i = (
                _skip_options(words, i + 1, {"-s", "--signal", "-k", "--kill-after"})
                + 1
            )
        elif name == "ssh":
            i = _skip_options(words, i + 1, SSH_ARG_OPTS) + 1  # options, then host
            if i >= n:
                return ("sh", [])
            return (REMOTE, [_Word(" ".join(w.value for w in words[i:]))])
        elif name in ("docker", "podman") and nxt == "exec":
            i = (
                _skip_options(words, i + 2, DOCKER_ARG_OPTS) + 1
            )  # options, then container
        elif (name == "kubectl" and nxt == "exec") or (
            name == "mise" and nxt in ("x", "exec")
        ):
            rest = [w.value for w in words[i + 2 :]]
            if "--" not in rest:
                return None
            i += 3 + rest.index("--")
        elif name == "uv" and nxt == "run":
            i = _skip_options(words, i + 2, UV_ARG_OPTS)
        else:
            return (name, words[i + 1 :])
    return None


def _program_source(name: str, args: list[_Word]) -> tuple[str, _Word | None]:
    """Where `name args...` reads its program: ("stdin"|"code"|"script"|"module"|"none", code)."""
    fam = _family(name)
    vals = [w.value for w in args]
    i = 0
    if fam == "shell":
        code = False
        while i < len(vals):
            a = vals[i]
            i += 1
            if a in ("-", "--"):
                break
            if a.startswith("--"):
                if a in ("--version", "--help"):
                    return ("none", None)
                if a.startswith("--command="):
                    return ("code", _Word(a.split("=", 1)[1]))
                code = code or a == "--command"
                i += a in ("--rcfile", "--init-file")
            elif a[:1] in "-+" and len(a) > 1:
                if a[0] == "-" and "s" in a[1:]:
                    return ("stdin", None)
                code = code or "c" in a[1:]
                i += a[-1] in "oO"
            else:
                i -= 1
                break
        if code:
            return ("code", _arg(args, i))
        return ("script", None) if i < len(vals) else ("stdin", None)
    if fam == "python":
        while i < len(vals):
            a = vals[i]
            if a == "-":
                return ("stdin", None)
            if a == "--":
                i += 1
                break
            if not a.startswith("-"):
                break
            if a in ("--version", "--help"):
                return ("none", None)
            for j, ch in enumerate(a[1:] if a[1:2] != "-" else "", start=1):
                rest = a[j + 1 :]
                if ch == "c":
                    return ("code", _Word(rest) if rest else _arg(args, i + 1))
                if ch == "m":
                    return ("module", None)
                if ch in "WX":
                    i += not rest
                    break
            i += 1
        return ("script", None) if i < len(vals) else ("stdin", None)
    if fam == "node":
        if vals[:1] == ["eval"]:
            return ("code", _arg(args, 1))
        i = 1 if vals[:1] == ["run"] else 0
        while i < len(vals):
            a = vals[i]
            if a == "-":
                return ("stdin", None)
            if a in ("-e", "--eval", "-p", "--print", "-pe"):
                return ("code", _arg(args, i + 1))
            if a.startswith(("--eval=", "--print=")):
                return ("code", _Word(a.split("=", 1)[1]))
            if a in ("--version", "-v", "--help", "-h"):
                return ("none", None)
            if a == "--" or not a.startswith("-"):
                i += a == "--"
                break
            i += 2 if a in NODE_ARG_OPTS else 1
        return ("script", None) if i < len(vals) else ("stdin", None)
    if fam == "pwsh":
        while i < len(vals):
            key = vals[i].lower()
            if not key.startswith("-"):
                return ("script", None)
            key = key.lstrip("-")
            nxt = _arg(args, i + 1)
            if key in ("c", "command", "f", "file"):
                if nxt is None or nxt.value == "-":
                    return ("stdin", None)
                return ("code", nxt) if key in ("c", "command") else ("script", None)
            if key in ("encodedcommand", "e", "ec"):
                return ("code", None)
            i += 2 if key in PWSH_ARG_OPTS else 1
        return ("stdin", None)
    letters = CODE_LETTERS.get(fam, "")
    arg_opts = E_ARG_OPTS.get(fam, set())
    while i < len(vals):
        a = vals[i]
        if a == "-":
            return ("stdin", None)
        if a == "--" or not a.startswith("-"):
            i += a == "--"
            break
        if a in ("--version", "--help"):
            return ("none", None)
        if fam == "php" and a == "-f":
            return ("script", None)
        if not a.startswith("--") and letters:
            if a[1] in letters:
                return ("code", _Word(a[2:]) if a[2:] else _arg(args, i + 1))
            if a[1:].isalpha() and len(a) <= 5 and a[-1] in letters:  # -ne, -lne, -pe
                return ("code", _arg(args, i + 1))
        i += 2 if a in arg_opts else 1
    return ("script", None) if i < len(vals) else ("stdin", None)


def _interp_of(words: list[_Word]) -> tuple[str, list[_Word]] | None:
    res = _command_of(words)
    if res is None or (res[0] != REMOTE and not INTERPRETER_RE.fullmatch(res[0])):
        return None
    return res


def _has_body(stdin: _Heredoc | str | None) -> bool:
    return isinstance(stdin, _Heredoc) and stdin.body > 0


def _is_cat_heredoc(cmd: _Cmd) -> bool:
    if not _has_body(cmd.stdin):
        return False
    res = _command_of(cmd.words)
    return res is not None and res[0] == "cat" and all(w.value == "-" for w in res[1])


def _first_stdin_consumer_in(text: str, depth: int) -> str | None:
    """For an inline command string that inherits stdin, the interpreter that reads it."""
    if depth >= MAX_DEPTH:
        return None
    for cmd in _Parser(text).parse():
        if not cmd.words or cmd.pipe_from is not None or cmd.stdin is not None:
            continue
        interp = _stdin_consumer(cmd.words, depth + 1)
        if interp:
            return interp
        res = _command_of(cmd.words)
        if res is not None and res[0] not in NON_READERS:
            return None  # this command would read stdin first (e.g. `cat > file`)
    return None


def _stdin_consumer(words: list[_Word], depth: int) -> str | None:
    """The interpreter that would execute this command's stdin as its program, if any."""
    res = _interp_of(words)
    if res is None:
        return None
    name, args = res
    if name == REMOTE:
        return _first_stdin_consumer_in(args[0].value, depth)
    mode, code = _program_source(name, args)
    if mode == "stdin":
        return name
    if mode == "code" and code is not None and _family(name) == "shell":
        return _first_stdin_consumer_in(code.value, depth)  # `sh -c 'python3 -'`
    return None


def _embedded(words: list[_Word], depth: int) -> str | None:
    """Heredoc programs passed inline: `bash -c "$(cat <<EOF ...)"`, `ssh h 'py - <<EOF'`."""
    res = _interp_of(words)
    if res is None:
        return None
    name, args = res
    if name == REMOTE:
        return find_interpreter_heredoc(args[0].value, depth + 1)
    mode, code = _program_source(name, args)
    if mode != "code" or code is None:
        return None
    if any(sub.pipe_from is None and _is_cat_heredoc(sub) for sub in code.subs):
        return name
    if _family(name) == "shell":
        return find_interpreter_heredoc(code.value, depth + 1)
    return None


def find_interpreter_heredoc(command: str, depth: int = 0) -> str | None:
    """Return the interpreter whose program is a non-empty heredoc in `command`, if any.

    Only the command word of each pipeline stage counts (after `VAR=` assignments and
    wrappers such as sudo/env/ssh/docker exec/mise x --/uv run), and only when that
    interpreter reads its program from stdin (no script operand, no `-c`/`-e`/`-m`, or
    an explicit `-`/`-s`) or gets it via `-c "$(cat <<EOF ...)"`. Heredocs that are data
    for a script, module or `-c` program, or that feed non-interpreters, stay quiet.
    """
    if depth > MAX_DEPTH:
        return None
    for cmd in _Parser(command).parse():
        if not cmd.words:
            continue
        interp = None
        if _has_body(cmd.stdin) or (
            cmd.stdin is None and cmd.pipe_from and _is_cat_heredoc(cmd.pipe_from)
        ):
            interp = _stdin_consumer(cmd.words, depth)
        interp = interp or _embedded(cmd.words, depth)
        if interp:
            return interp
    return None


def _script_name(interp: str) -> str:
    base = re.sub(r"[\d.]+$", "", interp)
    return "probe" + SUFFIX.get(base, ".sh")


def hint_text(interp: str, harness: str, event: str = "pre-tool-use") -> str:
    script = "<scratch-dir>/" + _script_name(interp)
    what = (
        "the Bash command that just ran fed"
        if event == "post-tool-use"
        else "this command feeds"
    )
    return (
        f"{HOOK_KEY} (#93): {what} a multi-line heredoc program into `{interp}`. "
        "dcg often blocks inline scripts, and rewording rarely helps. Prefer the script-file "
        f"form for this and later probes: write the body to `{script}` with "
        f"{WRITE_TOOL[harness]}, then run `{interp} {script}` (remote: "
        f"`ssh <host> {interp} - < {script}` or copy it first). "
        "This is a non-blocking hint; the command still goes through the normal guards."
    )


def extract_command(payload: dict[str, Any]) -> str | None:
    tool_call = payload.get("toolCall")
    sources: list[Any] = []
    if isinstance(tool_call, dict):
        sources += [tool_call.get("args"), tool_call]
    sources.append(payload.get("tool_input"))
    for src in sources:
        if not isinstance(src, dict):
            continue
        cmd = src.get("CommandLine") or src.get("command")
        if isinstance(cmd, list):
            cmd = " ".join(str(part) for part in cmd)
        if isinstance(cmd, str) and cmd.strip():
            return cmd
    return None


def render(
    harness: str, hint: str | None, event: str = "pre-tool-use"
) -> dict[str, Any]:
    """Shape the hint for each harness's hook protocol; never a deny/block decision.

    Delivery channels (model-visible unless noted):
    * Claude PreToolUse: hookSpecificOutput.additionalContext, added before the tool runs.
    * Codex PreToolUse: systemMessage only, which Codex shows as a UI/event-stream warning
      (user-visible, not model context); Codex PreToolUse does not support
      additionalContext. The model-visible Codex channel is therefore the PostToolUse
      registration (`--event post-tool-use`), whose additionalContext Codex adds as
      developer context after the command ran, so the next probe uses a script file.
    * agy PreToolUse: decision=allow plus `reason` and an `injectSteps` systemMessage (the
      channel the SessionStart hooks use). Unverified on an allowed tool call.
    """
    if harness == "agy":
        if hint is None:
            return {"decision": "allow"}
        return {
            "decision": "allow",
            "reason": hint,
            "injectSteps": [{"systemMessage": {"systemMessage": hint}}],
        }
    if hint is None:
        return {}
    if harness == "codex" and event != "post-tool-use":
        return {"systemMessage": hint}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse"
            if event == "post-tool-use"
            else "PreToolUse",
            "additionalContext": hint,
        }
    }


def run_hook(raw: str, harness: str, event: str = "pre-tool-use") -> dict[str, Any]:
    if os.environ.get("HEREDOC_HINT_DISABLE") == "1":
        return render(harness, None, event)
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    cmd = extract_command(payload)
    interp = find_interpreter_heredoc(cmd) if cmd else None
    hint = hint_text(interp, harness, event) if interp else None
    return render(harness, hint, event)


def _check_hooks_json(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    block = data.get(HOOK_KEY) if isinstance(data, dict) else None
    if not isinstance(block, dict):
        raise AssertionError(f"missing '{HOOK_KEY}' in {path}")
    pre = block.get("PreToolUse")
    first = pre[0] if isinstance(pre, list) and pre else {}
    if "run_command" not in str(first.get("matcher", "")):
        raise AssertionError(f"{HOOK_KEY} must match run_command in {path}")
    inner = first.get("hooks") or [{}]
    cmd = str(inner[0].get("command", ""))
    if "./hooks/heredoc-script-hint.py" not in cmd or "--harness agy" not in cmd:
        raise AssertionError(f"unexpected {HOOK_KEY} command: {cmd!r}")


def _run_self_check(hooks_json_path: Path | None) -> list[str]:
    checks: list[str] = []
    hinted = {
        "python3 - <<'PY'\nimport os\nprint(os.getcwd())\nPY": "python3",
        'cd /tmp && /usr/bin/python3 <<"EOF"\nprint(1)\nprint(2)\nEOF': "python3",
        "ssh pc bash -s <<EOF\nuname -a\nEOF": "bash",
        "cat <<'JS' | node\nconsole.log(1)\nJS": "node",
        "docker exec -i c python3.12 - <<-PY\n\tprint(1)\n\tPY": "python3.12",
        'python3 - "$a" \\\n  "$b" <<\'PY\'\nprint(1)\nPY': "python3",
        "bash -c \"$(cat <<'EOF'\necho hi\nEOF\n)\"": "bash",
        "ssh pc 'python3 - <<EOF\nprint(1)\nEOF'": "python3",
        "sudo -u root env A=1 python3 <<'PY'\nprint(1)\nPY": "python3",
        "node -e 'f(1 << n)'\nperl <<'PL'\nprint 1;\nPL": "perl",
    }
    for cmd, interp in hinted.items():
        assert find_interpreter_heredoc(cmd) == interp, cmd
    checks.append("interpreter_heredocs_hinted")
    quiet = (
        "ls -la",
        "python3 tasks/probe.py",
        "git commit -F - <<'EOF'\nfeat: x\nEOF",
        "cat > notes.md <<'EOF'\npython3 is fine here\nEOF",
        "python3 - <<'PY'\nPY",
        "python3 <<< 'print(1)'",
        "echo $((1 << 2))",
        "echo $(( x << n ))\nls\ncat <<EOF\nx\nEOF",
        "cat << sh\nx\nsh",
        'gh pr create --title "fix(zsh): prompt" --body "$(cat <<\'EOF\'\nbash\nEOF\n)"',
        "grep -c bash <<'EOF'\nbash\nEOF",
        "python3 -m json.tool <<'EOF'\n{}\nEOF",
        "python3 tasks/x.py <<'EOF'\ndata\nEOF",
        "sudo sh -c 'cat > /etc/x.conf' <<'EOF'\nk=1\nEOF",
    )
    for cmd in quiet:
        assert find_interpreter_heredoc(cmd) is None, cmd
    checks.append("non_interpreter_or_empty_heredocs_quiet")

    payloads = {
        "agy": {"toolCall": {"name": "run_command", "args": {"CommandLine": "x"}}},
        "claude": {"tool_name": "Bash", "tool_input": {"command": "x"}},
        "codex": {"tool_name": "Bash", "tool_input": {"command": "x"}},
    }
    heredoc = "python3 - <<'PY'\nprint(1)\nPY"
    for harness, payload in payloads.items():
        raw = json.dumps(payload).replace('"x"', json.dumps(heredoc))
        out = json.dumps(run_hook(raw, harness))
        assert "script-file" in out and "deny" not in out, (harness, out)
        assert run_hook(json.dumps(payload), harness) in ({}, {"decision": "allow"})
    assert run_hook("not json", "claude") == {}
    post = run_hook(raw, "codex", "post-tool-use")["hookSpecificOutput"]
    assert (
        post["hookEventName"] == "PostToolUse"
        and "script-file" in post["additionalContext"]
    )
    checks.append("harness_outputs_non_blocking")

    if hooks_json_path is not None:
        _check_hooks_json(hooks_json_path)
        checks.append("hooks_json_registered")
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", default="agy")
    parser.add_argument("--event", default="pre-tool-use")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--check-hooks-json", type=Path, default=None)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        # Exit code 2 would block the tool call in Claude/Codex; a hint hook must never block.
        return 0
    if args.self_check or args.check_hooks_json is not None:
        if args.self_check:
            checks = _run_self_check(args.check_hooks_json)
        else:
            _check_hooks_json(args.check_hooks_json)
            checks = ["hooks_json_registered"]
        sys.stdout.write(json.dumps({"status": "ok", "checks": checks}) + "\n")
        return 0

    harness = args.harness if args.harness in HARNESSES else "claude"
    event = "post-tool-use" if args.event == "post-tool-use" else "pre-tool-use"
    try:
        out = run_hook(sys.stdin.read(), harness, event)
    except Exception:
        out = render(harness, None, event)
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
