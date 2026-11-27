"""Behavioral checks for the non-blocking heredoc-script-hint PreToolUse hook (#93)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "unix/home/.config/mise/assets"
HOOK = REPO / "unix/home/.gemini/config/hooks/heredoc-script-hint.py"
HOOKS_JSON = REPO / "unix/home/.gemini/config/hooks.json"

HINTED = [
    ("python3 - <<'PY'\nimport os\nprint(os.getcwd())\nPY", "python3"),
    ('cd /tmp && python3 <<"EOF"\nprint(1)\nEOF', "python3"),
    ("ssh pc bash -s <<'EOF'\nuname -a\nEOF", "bash"),
    ("cat <<'JS' | node\nconsole.log(1)\nJS", "node"),
    # Backslash continuation before the heredoc operator.
    ('python3 - "$a" \\\n  "$b" <<\'PY\'\nprint(1)\nPY', "python3"),
    # The heredoc is the -c program.
    ("bash -c \"$(cat <<'EOF'\necho hi\nEOF\n)\"", "bash"),
    # The heredoc is inside a quoted remote command.
    ("ssh pc 'python3 - <<EOF\nprint(1)\nEOF'", "python3"),
    # Wrappers and assignments before the command word.
    ("sudo -u root env A=1 python3 <<'PY'\nprint(1)\nPY", "python3"),
    ("mise x -- python3 - <<'PY'\nprint(1)\nPY", "python3"),
    ("uv run --with rich python - <<'PY'\nprint(1)\nPY", "python"),
    # A quoted `<<` shift operator must not swallow the real heredoc after it.
    ("node -e 'f(1 << n)'\nperl <<'PL'\nprint 1;\nPL", "perl"),
]
QUIET = [
    "ls -la",
    "python3 tasks/probe.py",
    "git commit -F - <<'EOF'\nfeat: x\nEOF",
    "cat > notes.md <<'EOF'\nrun python3 later\nEOF",
    "python3 - <<'PY'\nPY",
    "python3 <<< 'print(1)'",
    # Interpreter names in quoted argument text (the PR/issue idiom).
    'gh pr create --title "fix(zsh): prompt" --body "$(cat <<\'EOF\'\n'
    '## Summary\n- bash and python3 tweaks\nEOF\n)"',
    "gh issue comment 9 --body \"$(cat <<'EOF'\nnode and ruby\nEOF\n)\"",
    # Interpreter name as an argument, not the command word.
    "grep -c bash <<'EOF'\nbash\nEOF",
    # The heredoc is data for a module, a script or a -c program.
    "python3 -m json.tool <<'EOF'\n{\"a\": 1}\nEOF",
    "python3 tasks/x.py <<'EOF'\ndata\nEOF",
    "sudo sh -c 'cat > /etc/x.conf' <<'EOF'\nkey=1\nEOF",
    "bash -c 'echo hi' <<'EOF'\ndata\nEOF",
    "node -e 'process.stdin.pipe(process.stdout)' <<'EOF'\ndata\nEOF",
    # A shift operator is not a heredoc.
    "echo $(( x << n ))\nls\ncat <<EOF\nx\nEOF",
    "(( y = x << 2 ))\nwc -l <<EOF\nx\nEOF",
]


def _asset_command(harness: str, event: str = "PreToolUse") -> str:
    data = json.loads((ASSETS / harness / "dcg-hooks.json").read_text())
    for group in data["hooks"][event]:
        for hook in group["hooks"]:
            if "heredoc-script-hint.py" in hook["command"]:
                assert group["matcher"] == "Bash"
                return str(hook["command"])
    raise AssertionError(f"{harness} dcg-hooks.json lacks heredoc-script-hint")


def _home_with_hook(tmp_path: Path) -> Path:
    dst = tmp_path / ".gemini/config/hooks/heredoc-script-hint.py"
    dst.parent.mkdir(parents=True)
    dst.symlink_to(HOOK)
    return tmp_path


def _run_asset(
    harness: str,
    home: Path,
    command: str,
    event: str = "PreToolUse",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = {"HOME": str(home), "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin"}
    return subprocess.run(
        ["/bin/sh", "-c", _asset_command(harness, event)],
        input=payload,
        text=True,
        capture_output=True,
        env={**env, **(extra_env or {})},
        timeout=20,
        check=False,
    )


def _run_agy(command: str) -> dict[str, object]:
    payload = json.dumps(
        {"toolCall": {"name": "run_command", "args": {"CommandLine": command}}}
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-B", str(HOOK), "--harness", "agy"],
        input=payload,
        text=True,
        capture_output=True,
        timeout=20,
        check=True,
    )
    return json.loads(proc.stdout)


@pytest.mark.parametrize(("command", "interp"), HINTED)
def test_claude_hint_is_additional_context(
    tmp_path: Path, command: str, interp: str
) -> None:
    proc = _run_asset("claude", _home_with_hook(tmp_path), command)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in out
    assert f"`{interp} <scratch-dir>/probe" in out["additionalContext"]
    assert "Write/Edit tool" in out["additionalContext"]


@pytest.mark.parametrize(("command", "interp"), HINTED)
def test_codex_hint_is_system_message(
    tmp_path: Path, command: str, interp: str
) -> None:
    proc = _run_asset("codex", _home_with_hook(tmp_path), command)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    # Codex PreToolUse supports only systemMessage, a UI/event-stream warning for the user.
    assert set(out) == {"systemMessage"}
    assert f"`{interp} <scratch-dir>/probe" in out["systemMessage"]


@pytest.mark.parametrize(("command", "interp"), HINTED)
def test_codex_post_tool_use_hint_is_model_context(
    tmp_path: Path, command: str, interp: str
) -> None:
    """The model-visible Codex channel: PostToolUse additionalContext (developer context)."""
    proc = _run_asset("codex", _home_with_hook(tmp_path), command, "PostToolUse")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert set(out) == {"hookSpecificOutput"}
    ctx = out["hookSpecificOutput"]
    assert ctx["hookEventName"] == "PostToolUse"
    assert "decision" not in out and "permissionDecision" not in ctx
    assert f"`{interp} <scratch-dir>/probe" in ctx["additionalContext"]
    assert "`apply_patch`" in ctx["additionalContext"]


@pytest.mark.parametrize(("command", "interp"), HINTED)
def test_agy_hint_allows_with_message(command: str, interp: str) -> None:
    out = _run_agy(command)
    assert out["decision"] == "allow"
    assert "script-file form" in str(out["reason"])
    assert interp in json.dumps(out["injectSteps"])


@pytest.mark.parametrize(
    ("harness", "event"),
    [("claude", "PreToolUse"), ("codex", "PreToolUse"), ("codex", "PostToolUse")],
)
@pytest.mark.parametrize("command", QUIET)
def test_no_hint_cases(tmp_path: Path, harness: str, event: str, command: str) -> None:
    proc = _run_asset(harness, _home_with_hook(tmp_path), command, event)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {}
    assert _run_agy(command) == {"decision": "allow"}


@pytest.mark.parametrize(
    ("harness", "event"),
    [("claude", "PreToolUse"), ("codex", "PreToolUse"), ("codex", "PostToolUse")],
)
def test_kill_switch_silences_hint(tmp_path: Path, harness: str, event: str) -> None:
    proc = _run_asset(
        harness,
        _home_with_hook(tmp_path),
        HINTED[0][0],
        event,
        extra_env={"HEREDOC_HINT_DISABLE": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {}


def test_kill_switch_silences_agy_hint() -> None:
    payload = json.dumps(
        {"toolCall": {"name": "run_command", "args": {"CommandLine": HINTED[0][0]}}}
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-B", str(HOOK), "--harness", "agy"],
        input=payload,
        text=True,
        capture_output=True,
        env={"HEREDOC_HINT_DISABLE": "1", "PATH": "/usr/bin:/bin"},
        timeout=20,
        check=True,
    )
    assert json.loads(proc.stdout) == {"decision": "allow"}


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_missing_hook_file_is_silent(tmp_path: Path, harness: str) -> None:
    proc = _run_asset(harness, tmp_path, HINTED[0][0])
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}


@pytest.mark.parametrize(
    "raw", ["", "not json", "[1]", '{"tool_input": {"command": 5}}']
)
def test_malformed_payloads_never_block(raw: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-I", "-B", str(HOOK), "--harness", "claude"],
        input=raw,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}


def test_bad_flag_exits_zero_not_two() -> None:
    proc = subprocess.run(
        [sys.executable, "-I", "-B", str(HOOK), "--bogus"],
        input="{}",
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0


def test_self_check_and_agy_registration() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(HOOK),
            "--self-check",
            "--check-hooks-json",
            str(HOOKS_JSON),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert "hooks_json_registered" in json.loads(proc.stdout)["checks"]
