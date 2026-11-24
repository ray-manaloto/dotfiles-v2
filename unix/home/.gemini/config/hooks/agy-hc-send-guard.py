#!/usr/bin/env python3
"""Antigravity PreToolUse guard preventing payloads or interrupts via `hc send` on peer agy tabs."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

WAKE_DOORBELL_RE = re.compile(r"^wake\s+doorbell\s*:", re.IGNORECASE)
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
SHELL_WRAPPERS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh"})
MAX_SHELL_UNWRAP_DEPTH = 4
MAX_DOORBELL_LEN = 220
PUNCT_SEP_CHARS = frozenset(";|&()\n")
SEPARATORS = frozenset(
    {";", "&&", "||", "|", "&", "\n", "then", "do", "else", "elif", "{", "}", "(", ")"}
)


def _allow() -> dict[str, Any]:
    return {"decision": "allow"}


def _deny(reason: str) -> dict[str, Any]:
    return {"decision": "deny", "reason": reason}


def _strip_wrapper_prefix(segment: list[str]) -> list[str]:
    idx = 0
    while idx < len(segment):
        tok = segment[idx]
        if ENV_ASSIGN_RE.match(tok):
            idx += 1
            continue
        if Path(tok).name == "env":
            idx += 1
            while idx < len(segment) and segment[idx].startswith("-"):
                flag = segment[idx]
                if flag == "--":
                    idx += 1
                    break
                if flag in {"-S", "--split-string"} and idx + 1 < len(segment):
                    try:
                        expanded = shlex.split(segment[idx + 1])
                    except ValueError:
                        expanded = []
                    segment = [*segment[: idx + 2], *expanded, *segment[idx + 2 :]]
                    idx += 2
                    continue
                if flag.startswith("--split-string="):
                    try:
                        expanded = shlex.split(flag.split("=", 1)[1])
                    except ValueError:
                        expanded = []
                    segment = [*segment[: idx + 1], *expanded, *segment[idx + 1 :]]
                    idx += 1
                    continue
                if flag in {
                    "-u",
                    "--unset",
                    "-C",
                    "--chdir",
                } and idx + 1 < len(segment):
                    idx += 2
                    continue
                idx += 1
            continue
        if Path(tok).name == "exec":
            idx += 1
            continue
        break
    return segment[idx:]


def _unwrap_shell_command(segment: list[str]) -> str | None:
    stripped = _strip_wrapper_prefix(segment)
    if not stripped:
        return None
    if Path(stripped[0]).name not in SHELL_WRAPPERS:
        return None
    i = 1
    while i < len(stripped):
        tok = stripped[i]
        if tok == "--":
            break
        if tok in {"-o", "+o"} and i + 1 < len(stripped):
            i += 2
            continue
        if tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]:
            if i + 1 < len(stripped):
                return stripped[i + 1]
            return None
        if not tok.startswith("-"):
            break
        i += 1
    return None


def _split_shell_segments(command: str, *, _depth: int = 0) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";|&()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []

    raw_segments: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in SEPARATORS or (tok and all(ch in PUNCT_SEP_CHARS for ch in tok)):
            if cur:
                raw_segments.append(cur)
                cur = []
        else:
            cur.append(tok)
    if cur:
        raw_segments.append(cur)

    if _depth >= MAX_SHELL_UNWRAP_DEPTH:
        return raw_segments

    segments: list[list[str]] = []
    for seg in raw_segments:
        inner_cmd = _unwrap_shell_command(seg)
        if inner_cmd is not None:
            inner_segs = _split_shell_segments(inner_cmd, _depth=_depth + 1)
            if inner_segs:
                segments.extend(inner_segs)
                continue
        segments.append(seg)
    return segments


def _parse_hc_send_segment(segment: list[str]) -> tuple[str, str | None, str] | None:
    segment = _strip_wrapper_prefix(segment)
    if len(segment) < 2:
        return None
    if Path(segment[0]).name != "hc" or segment[1] != "send":
        return None

    args = segment[2:]
    if_flag: str | None = None
    positionals: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            positionals.extend(args[i + 1 :])
            break
        if tok == "--if" and i + 1 < len(args):
            if_flag = args[i + 1]
            i += 2
            continue
        if tok.startswith("--if="):
            if_flag = tok.split("=", 1)[1]
            i += 1
            continue
        if tok == "--timeout" and i + 1 < len(args):
            i += 2
            continue
        if tok.startswith("--timeout="):
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        positionals.append(tok)
        i += 1

    if not positionals:
        return None
    target = positionals[0]
    text = " ".join(positionals[1:])
    return target, if_flag, text


def _resolve_hc_bin() -> str | None:
    found = shutil.which("hc")
    if found and os.access(found, os.X_OK):
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "hc",
        Path("/usr/local/bin/hc"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _inspect_terminal(target: str) -> dict[str, Any] | None:
    hc_bin = _resolve_hc_bin()
    if hc_bin is None:
        return {"_hc_error": "hc binary not found in PATH or ~/.local/bin/hc"}
    try:
        proc = subprocess.run(
            [hc_bin, "get", target, "--json"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"_hc_error": f"hc get failed or timed out: {exc}"}
    if proc.returncode != 0:
        err_msg = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        return {"_hc_error": f"hc get {target} exited {proc.returncode}: {err_msg}"}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"_hc_error": f"hc get {target} returned invalid JSON: {exc}"}
    if not isinstance(data, dict):
        return {"_hc_error": f"hc get {target} returned non-object payload"}
    return data


def _argv_has_agy(argv: Any) -> bool:
    if not isinstance(argv, list):
        return False
    for entry in argv:
        if not isinstance(entry, str):
            continue
        if entry == "--conversation" or Path(entry).name == "agy":
            return True
    return False


def _is_agy_terminal(info: dict[str, Any]) -> bool:
    status = info.get("status")
    if isinstance(status, dict) and status.get("app") == "antigravity":
        return True
    if info.get("agent") == "antigravity":
        return True
    fg = info.get("foreground")
    fg_argv = fg.get("argv") if isinstance(fg, dict) else None
    return _argv_has_agy(fg_argv) or _argv_has_agy(info.get("argv"))


def _check_command(
    command: str,
    inspector: Callable[[str], dict[str, Any] | None] = _inspect_terminal,
) -> dict[str, Any]:
    for segment in _split_shell_segments(command):
        parsed = _parse_hc_send_segment(segment)
        if parsed is None:
            continue
        target, if_flag, text = parsed
        info = inspector(target)
        if info is None:
            return _deny(
                f"agy-hc-send-guard: fail-closed because 'hc get {target} --json' returned no metadata."
            )
        if "_hc_error" in info:
            return _deny(
                f"agy-hc-send-guard: fail-closed on 'hc get {target} --json' error/timeout "
                f"({info['_hc_error']}); verify target tab before running 'hc send'."
            )
        if not _is_agy_terminal(info):
            continue

        status = info.get("status")
        raw_state = status.get("state") if isinstance(status, dict) else ""
        state = raw_state if isinstance(raw_state, str) else ""

        if state in ("working", "blocked"):
            return _deny(
                f"agy-hc-send-guard: Peer Antigravity tab '{target}' is currently {state}; "
                "do not run 'hc send'. Deliver messages via native 'send_message' instead."
            )

        if state == "done" and if_flag == "idle":
            return _deny(
                f"agy-hc-send-guard: Peer Antigravity tab '{target}' is in status.state == 'done', "
                "where 'hc send --if idle' exits 10; omit '--if idle' and send a 1-line "
                "'Wake doorbell: ...' nudge instead."
            )

        stripped = text.strip()
        if (
            not stripped
            or "\n" in stripped
            or "\r" in stripped
            or len(stripped) > MAX_DOORBELL_LEN
            or not WAKE_DOORBELL_RE.match(stripped)
        ):
            return _deny(
                "agy-hc-send-guard (agy-peer-messaging.md): Do not send status updates "
                f"or payloads to Antigravity tab '{target}' via 'hc send'. Deliver the "
                "full payload via native 'send_message' to the peer's conversationId first; "
                "only send a single-line 'hc send' doorbell (<= 220 chars) starting with "
                "'Wake doorbell:' when status.state is 'idle' or 'done'."
            )
    return _allow()


def _run_hook(
    payload: dict[str, Any],
    inspector: Callable[[str], dict[str, Any] | None] = _inspect_terminal,
) -> dict[str, Any]:
    if os.environ.get("AGY_DISABLE_HC_SEND_GUARD") == "1":
        return _allow()

    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        tool_name = tool_call.get("name")
        args = tool_call.get("args")
    else:
        tool_name = payload.get("tool_name")
        args = payload.get("tool_input")

    if tool_name != "run_command" or not isinstance(args, dict):
        return _allow()

    cmd = args.get("CommandLine")
    if not isinstance(cmd, str) or not cmd.strip():
        return _allow()
    return _check_command(cmd, inspector=inspector)


def _process_stdin_text(
    raw_stdin: str,
    inspector: Callable[[str], dict[str, Any] | None] = _inspect_terminal,
) -> dict[str, Any]:
    try:
        payload = json.loads(raw_stdin)
    except (json.JSONDecodeError, ValueError):
        return _allow()
    if not isinstance(payload, dict):
        return _allow()
    return _run_hook(payload, inspector=inspector)


def _check_hooks_json(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"invalid hooks.json root in {path}"
        raise TypeError(msg)
    if "agy-hc-send-guard" not in data:
        raise AssertionError(f"missing 'agy-hc-send-guard' in {path}")
    block = data["agy-hc-send-guard"]
    if not isinstance(block, dict):
        msg = f"invalid agy-hc-send-guard block in {path}"
        raise TypeError(msg)
    pre_handlers = block.get("PreToolUse")
    if not isinstance(pre_handlers, list) or not pre_handlers:
        raise AssertionError(
            f"missing PreToolUse handlers for agy-hc-send-guard in {path}"
        )
    first = pre_handlers[0]
    matcher = first.get("matcher", "") if isinstance(first, dict) else ""
    if not isinstance(matcher, str) or "run_command" not in matcher:
        raise AssertionError(f"unexpected PreToolUse matcher: {matcher!r}")
    inner = first.get("hooks") if isinstance(first, dict) else None
    if not isinstance(inner, list) or not inner:
        raise AssertionError(f"missing PreToolUse hooks list in {path}")
    cmd = inner[0].get("command", "") if isinstance(inner[0], dict) else ""
    if not isinstance(cmd, str) or "agy-hc-send-guard.py" not in cmd:
        raise AssertionError(f"unexpected hook command: {cmd!r}")
    if cmd.startswith("/") or "/Users/" in cmd:
        raise AssertionError(
            f"hook command must be portable relative path, got: {cmd!r}"
        )


def _run_self_check(hooks_json_path: Path | None) -> list[str]:
    checks: list[str] = []
    terminals: dict[str, dict[str, Any]] = {
        "codex-tab": {
            "agent": "codex",
            "status": {"app": "codex", "state": "idle"},
            "foreground": {"argv": ["codex"]},
        },
        "agy-idle": {
            "agent": "antigravity",
            "status": {"app": "antigravity", "state": "idle"},
            "foreground": {"argv": ["fnox", "proxy", "run", "--", "agy"]},
        },
        "agy-done": {
            "agent": "antigravity",
            "status": {"app": "antigravity", "state": "done"},
            "foreground": {"argv": ["agy", "--conversation", "uuid-1"]},
        },
        "agy-working": {
            "agent": "antigravity",
            "status": {"app": "antigravity", "state": "working"},
            "foreground": {"argv": ["agy", "--conversation", "uuid-1"]},
        },
        "agy-blocked": {
            "agent": "antigravity",
            "status": {"app": "antigravity", "state": "blocked"},
            "foreground": {"argv": ["agy", "--conversation", "uuid-2"]},
        },
    }

    def fake_inspect(target: str) -> dict[str, Any] | None:
        return terminals.get(target)

    def call_cmd(cmd: str) -> dict[str, Any]:
        return _run_hook(
            {"toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}},
            inspector=fake_inspect,
        )

    # 1. Non-hc-send commands allowed
    assert call_cmd("git status && hc get agy-idle --json") == _allow()
    checks.append("non_hc_send_allowed")

    # 2. Non-agy codex tab allowed to receive long multi-line hc send payload
    assert (
        call_cmd("hc send codex-tab 'Full status:\n1. Passed.\n2. Next step ready.'")
        == _allow()
    )
    checks.append("non_agy_codex_payload_allowed")

    # 3. Payload via hc send to agy tab denied
    res3 = call_cmd("hc send agy-idle 'Status: all checks passed on main.'")
    assert res3.get("decision") == "deny", f"case 3 failed: {res3}"
    checks.append("agy_payload_denied")

    # 4. Compound if/then wrapping hc send payload to agy tab denied
    res4 = call_cmd(
        'if [ "$s" = "idle" ]; then hc send agy-idle "Full status update"; fi'
    )
    assert res4.get("decision") == "deny", f"case 4 failed: {res4}"
    checks.append("agy_compound_if_payload_denied")

    # 5. Multiline or overlong Wake doorbell denied
    res5a = call_cmd('hc send agy-idle "Wake doorbell: line1\nline2"')
    assert res5a.get("decision") == "deny", f"case 5a failed: {res5a}"
    res5b = call_cmd(f'hc send agy-idle "Wake doorbell: {"x" * 220}"')
    assert res5b.get("decision") == "deny", f"case 5b failed: {res5b}"
    checks.append("agy_multiline_or_overlong_doorbell_denied")

    # 6. Working agy tab denies even valid Wake doorbell
    res6 = call_cmd(
        'hc send agy-working "Wake doorbell: check incoming native send_message."'
    )
    assert res6.get("decision") == "deny", f"case 6 failed: {res6}"
    checks.append("agy_working_doorbell_denied")

    # 7. Done agy tab with --if idle denies before exit 10
    res7 = call_cmd(
        'hc send --if idle agy-done "Wake doorbell: check incoming native send_message."'
    )
    assert res7.get("decision") == "deny", f"case 7 failed: {res7}"
    checks.append("agy_done_with_if_idle_denied")

    # 8. Idle and done agy tabs allow valid 1-line Wake doorbell (without --if idle on done)
    assert (
        call_cmd(
            'hc send agy-idle "Wake doorbell: check incoming native send_message from dotfiles-v2."'
        )
        == _allow()
    )
    assert (
        call_cmd(
            'hc send agy-done "Wake doorbell: check incoming native send_message from dotfiles-v2."'
        )
        == _allow()
    )
    checks.append("agy_idle_and_done_valid_doorbell_allowed")

    # 9. Blocked agy tab denies even valid Wake doorbell
    res9 = call_cmd(
        'hc send agy-blocked "Wake doorbell: check incoming native send_message."'
    )
    assert res9.get("decision") == "deny", f"case 9 failed: {res9}"
    checks.append("agy_blocked_doorbell_denied")

    # 10. Subshell () and newline-separated hc send plus env -u FOO are caught cleanly
    res10a = call_cmd('(hc send agy-idle "Full payload")')
    assert res10a.get("decision") == "deny", f"case 10a subshell failed: {res10a}"
    res10b = call_cmd('echo ok\nhc send agy-idle "Full payload"')
    assert res10b.get("decision") == "deny", f"case 10b newline failed: {res10b}"
    res10c = call_cmd('env -u FOO hc send agy-idle "Full payload"')
    assert res10c.get("decision") == "deny", f"case 10c env -u failed: {res10c}"
    res10d = call_cmd('echo ok\n\nhc send agy-idle "Full payload"')
    assert res10d.get("decision") == "deny", f"case 10d blank-line failed: {res10d}"
    res10e = call_cmd('echo ok;\nhc send agy-idle "Full payload"')
    assert res10e.get("decision") == "deny", f"case 10e semi-nl failed: {res10e}"
    res10f = call_cmd('env -S "hc send agy-idle \\"Full payload\\""')
    assert res10f.get("decision") == "deny", f"case 10f env -S failed: {res10f}"
    checks.append("subshell_newline_and_env_hc_send_caught")

    # 11. Shell-wrapped -c/-lc/-lic commands unwrap and catch hc send violations
    for wrap in (
        "zsh -lic 'hc send agy-idle \"Full payload\"'",
        "bash -c 'hc send agy-working \"Wake doorbell: ping\"'",
        "sh -lc 'echo ok && hc send agy-done --if idle \"Wake doorbell: ping\"'",
    ):
        res11 = call_cmd(wrap)
        assert res11.get("decision") == "deny", (
            f"case 11 shell-wrap failed ({wrap}): {res11}"
        )
    assert (
        call_cmd(
            "zsh -lic 'hc send agy-idle \"Wake doorbell: check incoming native send_message.\"'"
        )
        == _allow()
    )
    checks.append("shell_wrapper_unwrapping_hc_send")

    # 12. Fail closed when hc get errors/times out or returns None
    res12a = _run_hook(
        {
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": 'hc send unknown-tab "hi"'},
            }
        },
        inspector=fake_inspect,
    )
    assert res12a.get("decision") == "deny", (
        f"case 12a None inspector must deny: {res12a}"
    )
    res12b = _run_hook(
        {
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": 'hc send t1 "hi"'},
            }
        },
        inspector=lambda _t: {"_hc_error": "timed out after 3s"},
    )
    assert res12b.get("decision") == "deny", f"case 12b _hc_error must deny: {res12b}"
    checks.append("fail_closed_on_hc_get_error")

    if hooks_json_path is not None:
        _check_hooks_json(hooks_json_path)
        checks.append("hooks_json_portable")

    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Run built-in regression checks without modifying state.",
    )
    parser.add_argument(
        "--check-hooks-json",
        type=Path,
        default=None,
        help="Verify hooks.json registers agy-hc-send-guard cleanly.",
    )
    args = parser.parse_args()

    if args.self_check:
        checks = _run_self_check(args.check_hooks_json)
        sys.stdout.write(json.dumps({"status": "ok", "checks": checks}) + "\n")
        return 0

    if args.check_hooks_json is not None:
        _check_hooks_json(args.check_hooks_json)
        sys.stdout.write(
            json.dumps({"status": "ok", "checks": ["hooks_json_portable"]}) + "\n"
        )
        return 0

    result = _process_stdin_text(sys.stdin.read())
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
