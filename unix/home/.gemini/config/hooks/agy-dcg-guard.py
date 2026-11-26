#!/usr/bin/env python3
"""Antigravity PreToolUse adapter delegating run_command checks to dcg."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any


def _allow() -> int:
    sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
    return 0


def _deny(reason: str) -> int:
    sys.stdout.write(json.dumps({"decision": "deny", "reason": reason}) + "\n")
    return 0


def _extract_command(payload: dict[str, Any]) -> str | None:
    tool_call = payload.get("toolCall")
    if isinstance(tool_call, dict):
        args = tool_call.get("args")
        if isinstance(args, dict):
            cmd = args.get("CommandLine") or args.get("command")
            if isinstance(cmd, str) and cmd:
                return cmd
        cmd = tool_call.get("CommandLine") or tool_call.get("command")
        if isinstance(cmd, str) and cmd:
            return cmd
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        cmd = tool_input.get("CommandLine") or tool_input.get("command")
        if isinstance(cmd, str) and cmd:
            return cmd
    return None


def _resolve_dcg_bin() -> str | None:
    mise_data = os.environ.get("MISE_DATA_DIR") or os.path.expanduser(
        "~/.local/share/mise"
    )
    candidates = [
        os.environ.get("DCG_BIN", ""),
        shutil.which("dcg") or "",
        os.path.join(mise_data, "shims", "dcg"),
        "/opt/mise/shims/dcg",
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return _allow()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _deny(f"Blocked by agy-dcg-guard: invalid hook JSON payload ({exc})")
    if not isinstance(payload, dict):
        return _deny("Blocked by agy-dcg-guard: hook payload must be a JSON object")

    cmd = _extract_command(payload)
    if not cmd:
        return _allow()

    dcg = _resolve_dcg_bin()
    if dcg is None:
        # Fail closed: a missing guard binary must not silently disable every check.
        return _deny(
            "Blocked by agy-dcg-guard: dcg not found; run `mise install` to restore the guard"
        )

    sub_env = dict(os.environ)
    sub_env.setdefault("DCG_NO_SELF_HEAL", "1")
    sub_env.setdefault("DCG_NO_UPDATE_CHECK", "1")
    sub_env.setdefault("DCG_ROBOT", "1")

    req = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    try:
        proc = subprocess.run(
            [dcg, "--agent", "antigravity"],
            input=req,
            text=True,
            capture_output=True,
            env=sub_env,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _deny(f"Blocked by agy-dcg-guard: dcg invocation failed ({exc})")

    parsed: dict[str, Any] = {}
    out_text = proc.stdout.strip()
    if out_text:
        try:
            loaded = json.loads(out_text.splitlines()[-1])
            if isinstance(loaded, dict):
                parsed = loaded
        except json.JSONDecodeError as exc:
            if proc.returncode != 0:
                return _deny(f"Blocked by dcg (malformed guard output: {exc})")
            return _allow()

    hook = parsed.get("hookSpecificOutput")
    hook_dict = hook if isinstance(hook, dict) else {}
    perm = hook_dict.get("permissionDecision")
    if (
        perm in ("deny", "ask")
        or parsed.get("decision") == "deny"
        or proc.returncode == 2
    ):
        reason = (
            hook_dict.get("permissionDecisionReason")
            or parsed.get("reason")
            or proc.stderr.strip()
            or "Blocked by dcg"
        )
        return _deny(str(reason))
    if proc.returncode != 0:
        # A broken guard (for example a mise shim for an uninstalled dcg) must not allow everything.
        detail = proc.stderr.strip()[:200] or f"exit {proc.returncode}"
        return _deny(
            f"Blocked by agy-dcg-guard: dcg failed ({detail}); run `mise install` to restore the guard"
        )
    return _allow()


raise SystemExit(main())
