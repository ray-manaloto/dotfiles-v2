"""Replay the dcg repro fixtures (#116) through the installed dcg hook and report each decision.

Each `tests/fixtures/dcg/*.json` holds one Bash command and the decision dcg should reach.
The command goes to dcg exactly as Claude Code's PreToolUse hook sends it. `REPRO` marks a
fixture whose decision still differs (the upstream bug is present); exit 1 while any does.
Set DCG_BIN to test another dcg build. See docs/dcg-upstream-reports.md.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "dcg"


def decide(dcg: str, command: str) -> tuple[str, str]:
    """Return ("allow" | "block", first reason line) for one Bash command."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = {**os.environ, "DCG_NO_SELF_HEAL": "1", "DCG_NO_UPDATE_CHECK": "1"}
    proc = subprocess.run(
        [dcg, "--agent", "claude-code"],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    lines = proc.stdout.strip().splitlines()
    parsed = json.loads(lines[-1]) if lines else {}
    hook = parsed.get("hookSpecificOutput") if isinstance(parsed, dict) else None
    hook = hook if isinstance(hook, dict) else {}
    if hook.get("permissionDecision") in ("deny", "ask") or proc.returncode == 2:
        reason = str(hook.get("permissionDecisionReason") or proc.stderr).strip()
        return "block", (reason.splitlines() or [""])[0]
    if proc.returncode != 0:
        raise RuntimeError(f"dcg exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    return "allow", ""


def main() -> int:
    dcg = os.environ.get("DCG_BIN") or shutil.which("dcg")
    if not dcg:
        sys.stderr.write("dcg:repro: dcg not found; run `mise install`\n")
        return 2
    repro = False
    for fixture in sorted(FIXTURES.glob("*.json")):
        case = json.loads(fixture.read_text(encoding="utf-8"))
        try:
            got, reason = decide(dcg, case["command"])
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
            sys.stderr.write(f"dcg:repro: {fixture.name}: {exc}\n")
            return 2
        status = "OK" if got == case["expect"] else "REPRO"
        repro = repro or status == "REPRO"
        detail = f" [{reason}]" if reason else ""
        sys.stdout.write(
            f"{status:<5}  {fixture.stem}: {got} (expected {case['expect']}){detail}\n"
        )
    return 1 if repro else 0


if __name__ == "__main__":
    raise SystemExit(main())
