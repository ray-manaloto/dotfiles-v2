"""Non-blocking PreToolUse hint: put multi-line interpreter heredocs in script files (#93).

Shared by Antigravity (`hooks.json`), Claude Code, and Codex CLI (`dcg-hooks.json` assets).
dcg often blocks inline scripts (heredoc bodies that mention secrets tooling, `mv`/`rm`
under home, inline launchers); the fix every time was to write the body to a file first.
This hook only ever adds a hint: it never denies, and it exits 0 on any error.
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

# `<<WORD`, `<<-WORD`, `<<'WORD'`, `<<"WORD"`, `<<\WORD`; never the `<<<` here-string.
HEREDOC_RE = re.compile(
    r"(?<!<)<<(?!<)(?P<dash>-?)[ \t]*"
    r"(?:(?P<qc>['\"])(?P<quoted>[^'\"\s]+)(?P=qc)|\\?(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)
SEP_RE = re.compile(r"&&|\|\||;")
TOKEN_SPLIT_RE = re.compile(r"[\s'\"`(){}]+")
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


def _interpreter_in(segment: str) -> str | None:
    # Drop the heredoc operators so a delimiter named like an interpreter (`<< sh`) is not one.
    for tok in TOKEN_SPLIT_RE.split(HEREDOC_RE.sub(" ", segment)):
        name = tok.rsplit("/", 1)[-1]
        if name and INTERPRETER_RE.fullmatch(name):
            return name
    return None


def _segment_at(line: str, pos: int) -> str:
    start, end = 0, len(line)
    for sep in SEP_RE.finditer(line):
        if sep.end() <= pos:
            start = sep.end()
        elif sep.start() >= pos:
            end = sep.start()
            break
    return line[start:end]


def find_interpreter_heredoc(command: str) -> str | None:
    """Return the interpreter fed by the first multi-line heredoc in `command`, if any."""
    lines = command.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        pending: list[tuple[str, str | None]] = []
        for m in HEREDOC_RE.finditer(line):
            delim = m.group("quoted") or m.group("bare")
            pending.append((delim, _interpreter_in(_segment_at(line, m.start()))))
        for delim, interp in pending:
            body = 0
            while i < len(lines) and lines[i].strip() != delim:
                if lines[i].strip():
                    body += 1
                i += 1
            i += 1  # skip the terminator line
            if interp and body:
                return interp
    return None


def _script_name(interp: str) -> str:
    base = re.sub(r"[\d.]+$", "", interp)
    return "probe" + SUFFIX.get(base, ".sh")


def hint_text(interp: str, harness: str) -> str:
    script = "<scratch-dir>/" + _script_name(interp)
    return (
        f"{HOOK_KEY} (#93): this command feeds a multi-line heredoc into `{interp}`. "
        "dcg often blocks inline scripts, and rewording rarely helps. Prefer the script-file "
        f"form: write the body to `{script}` with {WRITE_TOOL[harness]}, then run "
        f"`{interp} {script}` (remote: `ssh <host> {interp} - < {script}` or copy it first). "
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


def render(harness: str, hint: str | None) -> dict[str, Any]:
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
    if harness == "codex":
        # Codex PreToolUse ignores additionalContext; systemMessage is its supported channel.
        return {"systemMessage": hint}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": hint,
        }
    }


def run_hook(raw: str, harness: str) -> dict[str, Any]:
    if os.environ.get("HEREDOC_HINT_DISABLE") == "1":
        return render(harness, None)
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    cmd = extract_command(payload)
    interp = find_interpreter_heredoc(cmd) if cmd else None
    return render(harness, hint_text(interp, harness) if interp else None)


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
        "cat << sh\nx\nsh",
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
    checks.append("harness_outputs_non_blocking")

    if hooks_json_path is not None:
        _check_hooks_json(hooks_json_path)
        checks.append("hooks_json_registered")
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", default="agy")
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
    try:
        out = run_hook(sys.stdin.read(), harness)
    except Exception:
        out = render(harness, None)
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
