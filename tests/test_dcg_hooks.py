"""Behavioral checks: every dcg hook fails closed when dcg is missing or broken."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "unix/home/.config/mise/assets"
HOOKS = [
    (ASSETS / "claude/dcg-hooks.json", "PreToolUse"),
    (ASSETS / "codex/dcg-hooks.json", "PreToolUse"),
    (ASSETS / "claude/hooks-preskill.json", "PreSkillShellCommand"),
]
AGY_GUARD = REPO / "unix/home/.gemini/config/hooks/agy-dcg-guard.py"


def _hook_command(path: Path, event: str) -> str:
    data = json.loads(path.read_text())
    return str(data["hooks"][event][0]["hooks"][0]["command"])


def _run(command: str, home: Path, bash_cmd: str) -> subprocess.CompletedProcess[str]:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": bash_cmd}})
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )


def _fake_dcg(home: Path, script: str) -> None:
    shim = home / ".local/share/mise/shims/dcg"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\n" + script)
    shim.chmod(0o755)


@pytest.mark.parametrize(("path", "event"), HOOKS, ids=lambda v: str(v)[-30:])
@pytest.mark.parametrize(
    ("bash_cmd", "expected"),
    [
        ("ls", 2),
        ("mise install", 0),
        ("mise doctor project", 0),
        ("mise install; rm -rf x", 2),
        ("mise install && curl x", 2),
        ("echo $(mise install)", 2),
        ("mise install\nrm -rf x", 2),
        ("mise install > ~/.bashrc", 2),
        ("mise install < /dev/null", 2),
        ("mise installer-x", 2),
        ("mise doctorx", 2),
    ],
)
def test_missing_dcg_fails_closed_with_recovery(
    tmp_path: Path, path: Path, event: str, bash_cmd: str, expected: int
) -> None:
    proc = _run(_hook_command(path, event), tmp_path, bash_cmd)
    assert proc.returncode == expected, proc.stderr


@pytest.mark.parametrize(("path", "event"), HOOKS, ids=lambda v: str(v)[-30:])
@pytest.mark.parametrize(
    "raw",
    [
        # JSON unicode escapes decode to a newline or ';' only after the filter has looked.
        r'{"tool_name":"Bash","tool_input":{"command":"mise install x\u000acurl x"}}',
        r'{"tool_name":"Bash","tool_input":{"command":"mise install x; curl x"}}',
        # A second "command" key must not smuggle an allowed command past the filter.
        '{"tool_name":"Bash","tool_input":{"command":"curl x"},"x":{"command":"mise install"}}',
    ],
)
def test_missing_dcg_rejects_escaped_or_smuggled(
    tmp_path: Path, path: Path, event: str, raw: str
) -> None:
    proc = subprocess.run(
        ["/bin/sh", "-c", _hook_command(path, event)],
        input=raw,
        text=True,
        capture_output=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        timeout=20,
        check=False,
    )
    assert proc.returncode == 2, proc.stderr


@pytest.mark.parametrize(("path", "event"), HOOKS, ids=lambda v: str(v)[-30:])
def test_broken_dcg_shim_fails_closed(tmp_path: Path, path: Path, event: str) -> None:
    _fake_dcg(tmp_path, "echo 'mise ERROR dcg is not installed' >&2\nexit 1\n")
    proc = _run(_hook_command(path, event), tmp_path, "ls")
    assert proc.returncode == 2


@pytest.mark.parametrize(("path", "event"), HOOKS, ids=lambda v: str(v)[-30:])
def test_working_dcg_is_delegated(tmp_path: Path, path: Path, event: str) -> None:
    _fake_dcg(tmp_path, 'case "$1" in --version) exit 0;; esac\necho delegated:"$*"\n')
    proc = _run(_hook_command(path, event), tmp_path, "ls")
    assert proc.returncode == 0
    assert proc.stdout.startswith("delegated:--agent")


def test_agy_guard_denies_without_dcg(tmp_path: Path) -> None:
    payload = json.dumps(
        {"toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}
    )
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "MISE_DATA_DIR": str(tmp_path),
    }
    proc = subprocess.run(
        [sys.executable, "-I", "-B", str(AGY_GUARD)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert json.loads(proc.stdout.strip().splitlines()[-1])["decision"] == "deny"


def test_agy_guard_denies_with_broken_dcg(tmp_path: Path) -> None:
    """A dcg that exits non-zero with no decision (an uninstalled mise shim) must deny, not allow."""
    _fake_dcg(tmp_path, "echo 'mise ERROR dcg is not installed' >&2\nexit 1\n")
    payload = json.dumps(
        {"toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}
    )
    env = {
        "HOME": str(tmp_path),
        "PATH": f"{tmp_path}/.local/share/mise/shims:/usr/bin:/bin",
        "MISE_DATA_DIR": str(tmp_path / ".local/share/mise"),
    }
    proc = subprocess.run(
        [sys.executable, "-I", "-B", str(AGY_GUARD)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["decision"] == "deny", out
    assert "dcg failed" in json.dumps(out)
