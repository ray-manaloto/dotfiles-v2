"""Offline fail-closed tests for the API-credit `claude -p` runner (issue #3).

A fake `claude` binary replays stream-json events so no credential or network is used.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from api_runner import cli, runner
from dotfiles_py.generated.api_runner_schema import (
    ApiRunnerErrorCode,
    ApiRunnerModel,
    ApiRunnerResponse,
)

ROOT = Path(__file__).resolve().parents[1]

FAKE = """#!{python}
import json, os, sys
prompt = sys.stdin.read()
mode = os.environ.get("FAKE_MODE", "ok")
with open(os.environ["FAKE_RECORD"], "w") as fh:
    json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "prompt": prompt}}, fh)
def emit(event):
    sys.stdout.write(json.dumps(event) + "\\n"); sys.stdout.flush()
if mode == "crash":
    sys.stderr.write("error: unknown option sk-ant-SECRET123\\n"); sys.exit(3)
source = "none" if mode == "subscription" else "ANTHROPIC_API_KEY"
tools = ["Read", "Bash"] if mode == "tools" else ["Read"]
mcp = [{{"name": "x"}}] if mode == "mcp" else []
if mode == "no_init":
    emit({{"type": "assistant"}})
else:
    emit({{"type": "system", "subtype": "init", "apiKeySource": source, "tools": tools,
          "mcp_servers": mcp}})
if mode == "hang":
    import time; time.sleep(30)
subtype = {{"budget": "error_max_budget_usd", "model_error": "error_during_execution"}}.get(mode, "success")
emit({{"type": "result", "subtype": subtype, "is_error": mode in ("budget", "model_error"),
      "total_cost_usd": 0.0123, "num_turns": 2, "duration_ms": 1500,
      "usage": {{"input_tokens": 100, "output_tokens": 20}}, "result": "REVIEW BODY"}})
"""


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    path = tmp_path / "claude"
    path.write_text(FAKE.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def make_spec(tmp_path: Path, fake: Path, **kw: object) -> runner.RunSpec:
    base = {
        "model": ApiRunnerModel.SONNET,
        "effort": "medium",
        "cwd": str(tmp_path),
        "max_turns": 5,
        "max_budget_usd": 1.0,
        "timeout_sec": 20,
        "config_dir": str(tmp_path / "cfg"),
        "claude_bin": str(fake),
    }
    base.update(kw)
    return runner.RunSpec.model_validate(base)


def parent_env(tmp_path: Path, mode: str = "ok", **extra: str) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "ANTHROPIC_API_KEY": "fnox-placeholder-anthropic-runner",
        "HTTPS_PROXY": "http://127.0.0.1:1",
        "CLAUDE_CODE_OAUTH_TOKEN": "subscription-token",
        "CLAUDE_CODE_PLUGIN_DIRS": "/tmp/mods",
        "GH_TOKEN": "gh-secret",
        "FAKE_MODE": mode,
        "FAKE_RECORD": str(tmp_path / "record.json"),
    }
    env.update(extra)
    return env


def test_argv_isolates_and_keeps_prompt_off_argv(tmp_path, fake_claude):
    argv = runner.build_argv(make_spec(tmp_path, fake_claude))
    for flag in (
        "--bare",
        "--restricted",
        "--disable-slash-commands",
        "--strict-mcp-config",
    ):
        assert flag in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--tools") + 1] == "Read"
    assert "fable" not in argv


def test_fable_is_not_a_valid_model():
    with pytest.raises(ValueError):
        ApiRunnerModel("fable")


def test_missing_key_fails_closed(tmp_path):
    with pytest.raises(runner.RunnerError) as exc:
        runner.build_env({"PATH": "/bin"}, tmp_path)
    assert exc.value.entry.code is ApiRunnerErrorCode.ERR_MISSING_CREDENTIAL


def test_placeholder_without_proxy_fails_closed(tmp_path):
    with pytest.raises(runner.RunnerError) as exc:
        runner.build_env({"ANTHROPIC_API_KEY": "fnox-placeholder-x"}, tmp_path)
    assert exc.value.entry.code is ApiRunnerErrorCode.ERR_MISSING_CREDENTIAL


def test_success_scrubs_env_and_reports_usage(tmp_path, fake_claude):
    env = parent_env(tmp_path)
    out = runner.run(
        make_spec(tmp_path, fake_claude),
        "PROMPT",
        env,
        popen=_popen_with(env, mode=False),
    )
    assert out.guard_errors == []
    assert out.api_key_source == "ANTHROPIC_API_KEY"
    assert out.usage is not None and out.usage.total_cost_usd == pytest.approx(0.0123)
    assert out.result_text == "REVIEW BODY"
    record = json.loads((tmp_path / "record.json").read_text())
    child_env = record["env"]
    for leaked in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_PLUGIN_DIRS", "GH_TOKEN"):
        assert leaked not in child_env
    assert child_env["DISABLE_TELEMETRY"] == "1"
    assert child_env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")
    assert record["prompt"] == "PROMPT"
    assert "PROMPT" not in record["argv"]


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("subscription", ApiRunnerErrorCode.ERR_WRONG_AUTH_SOURCE),
        ("tools", ApiRunnerErrorCode.ERR_UNEXPECTED_TOOLS),
        ("mcp", ApiRunnerErrorCode.ERR_UNEXPECTED_TOOLS),
        ("no_init", ApiRunnerErrorCode.ERR_WRONG_AUTH_SOURCE),
        ("budget", ApiRunnerErrorCode.ERR_BUDGET_EXCEEDED),
    ],
)
def test_guards_fail_closed(tmp_path, fake_claude, mode, code):
    env = parent_env(tmp_path, mode)
    out = runner.run(
        make_spec(tmp_path, fake_claude),
        "PROMPT",
        env,
        popen=_popen_with(env),
    )
    assert [e.code for e in out.guard_errors] == [code]
    if mode in ("subscription", "tools", "mcp", "no_init"):
        assert out.usage is None, "must stop before the result event is consumed"


def test_watchdog_times_out(tmp_path, fake_claude):
    env = parent_env(tmp_path, "hang")
    out = runner.run(
        make_spec(tmp_path, fake_claude, timeout_sec=1),
        "PROMPT",
        env,
        popen=_popen_with(env),
    )
    assert [e.code for e in out.guard_errors][-1] is ApiRunnerErrorCode.ERR_TIMEOUT


@pytest.mark.parametrize(
    "override",
    [
        {"max_budget_usd": 100.0},
        {"max_budget_usd": 0.0},
        {"effort": "max"},
        {"max_turns": 0},
        {"model": "fable"},
    ],
)
def test_spec_bounds_rejected(tmp_path, fake_claude, override):
    with pytest.raises(ValidationError):
        make_spec(tmp_path, fake_claude, **override)


def test_missing_cwd_rejected(tmp_path, fake_claude):
    with pytest.raises(runner.RunnerError):
        runner.validate_spec(
            make_spec(tmp_path, fake_claude, cwd=str(tmp_path / "nope"))
        )


def test_cli_dry_run_needs_no_credentials(tmp_path, monkeypatch, capsys, fake_claude):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        runner, "resolve_claude_bin", lambda home=None: str(fake_claude)
    )
    monkeypatch.setattr(cli, "resolve_claude_bin", lambda: str(fake_claude))
    prompt = tmp_path / "p.md"
    prompt.write_text("SECRET-PROMPT-TEXT")
    rc = cli.main(
        [
            "--prompt-file",
            str(prompt),
            "--cwd",
            str(tmp_path),
            "--state-dir",
            str(tmp_path / "state"),
            "--dry-run",
            "--json",
        ]
    )
    assert rc == 0
    resp = ApiRunnerResponse.model_validate_json(capsys.readouterr().out.strip())
    assert resp.dry_run and resp.errors == []
    log = (tmp_path / "state" / "usage.log.jsonl").read_text()
    assert "api_review_finished" in log
    assert "SECRET-PROMPT-TEXT" not in log


def test_task_wrapper_routes_through_scoped_fnox_profile():
    text = (ROOT / "tasks/api/review").read_text()
    assert "-P api-credit --no-defaults proxy run" in text
    fnox = (ROOT / "unix/home/.config/fnox/api-credit.toml").read_text()
    assert 'egress = "strict"' in fnox
    assert 'domain = "api.anthropic.com"' in fnox


def _popen_with(env: dict[str, str], *, mode: bool = True):
    """Popen that forwards the FAKE_* test controls the allowlist would strip."""
    import subprocess

    def _popen(argv, **kw):
        extra = {"FAKE_RECORD": env["FAKE_RECORD"]}
        if mode:
            extra["FAKE_MODE"] = env["FAKE_MODE"]
        kw["env"] = {**kw["env"], **extra}
        return subprocess.Popen(argv, **kw)

    return _popen


def test_raw_key_rejected_outside_proxy(tmp_path):
    with pytest.raises(runner.RunnerError) as exc:
        runner.build_env(
            {"ANTHROPIC_API_KEY": "sk-ant-real", "HTTPS_PROXY": "http://x"}, tmp_path
        )
    assert exc.value.entry.code is ApiRunnerErrorCode.ERR_MISSING_CREDENTIAL


def test_run_returns_typed_error_instead_of_raising(tmp_path, fake_claude):
    out = runner.run(make_spec(tmp_path, fake_claude), "PROMPT", {"PATH": "/bin"})
    assert [e.code for e in out.guard_errors] == [
        ApiRunnerErrorCode.ERR_MISSING_CREDENTIAL
    ]


def test_early_exit_reports_scrubbed_stderr(tmp_path, fake_claude):
    env = parent_env(tmp_path, "crash")
    out = runner.run(
        make_spec(tmp_path, fake_claude), "P" * 200_000, env, popen=_popen_with(env)
    )
    assert [e.code for e in out.guard_errors] == [ApiRunnerErrorCode.ERR_COMMAND_FAILED]
    detail = out.guard_errors[0].detail or {}
    tail = str(detail.get("stderr_tail", "")) + str(out.guard_errors[0].message)
    assert "SECRET123" not in tail


def test_popen_oserror_returns_typed_error(tmp_path, fake_claude):
    def boom(*_a, **_k):
        raise FileNotFoundError("claude not found")

    out = runner.run(
        make_spec(tmp_path, fake_claude), "PROMPT", parent_env(tmp_path), popen=boom
    )
    assert [e.code for e in out.guard_errors] == [ApiRunnerErrorCode.ERR_COMMAND_FAILED]


def test_no_proxy_is_never_forwarded(tmp_path, fake_claude):
    env = parent_env(tmp_path, NO_PROXY="*", no_proxy="api.anthropic.com")
    runner.run(
        make_spec(tmp_path, fake_claude),
        "PROMPT",
        env,
        popen=_popen_with(env, mode=False),
    )
    child_env = json.loads((tmp_path / "record.json").read_text())["env"]
    assert "NO_PROXY" not in child_env and "no_proxy" not in child_env


def test_budget_cap_never_rounds_up(tmp_path, fake_claude):
    argv = runner.build_argv(make_spec(tmp_path, fake_claude, max_budget_usd=1.999))
    assert argv[argv.index("--max-budget-usd") + 1] == "1.99"


def test_model_error_message_excludes_result_text(tmp_path, fake_claude):
    env = parent_env(tmp_path, "model_error")
    out = runner.run(
        make_spec(tmp_path, fake_claude), "PROMPT", env, popen=_popen_with(env)
    )
    assert [e.code for e in out.guard_errors] == [ApiRunnerErrorCode.ERR_MODEL_ERROR]
    assert "REVIEW BODY" not in str(out.guard_errors[0].message or "")


def test_relative_state_dir_is_resolved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert (
        cli.resolve_state_dir(Path("rel/state")) == (tmp_path / "rel/state").resolve()
    )


def test_cli_json_reports_invalid_input(tmp_path, monkeypatch, capsys, fake_claude):
    monkeypatch.setattr(cli, "resolve_claude_bin", lambda: str(fake_claude))
    empty = tmp_path / "empty.md"
    empty.write_text("   ")
    rc = cli.main(
        [
            "--prompt-file",
            str(empty),
            "--cwd",
            str(tmp_path),
            "--state-dir",
            str(tmp_path / "state"),
            "--dry-run",
            "--json",
        ]
    )
    assert rc == 2
    resp = ApiRunnerResponse.model_validate_json(capsys.readouterr().out.strip())
    assert [e.code for e in resp.errors] == [ApiRunnerErrorCode.ERR_INVALID_INPUT]


@pytest.mark.parametrize("opt", [["--max-turns", "0"], ["--max-budget-usd", "30"]])
def test_cli_json_reports_rejected_numeric_option(
    tmp_path, monkeypatch, capsys, fake_claude, opt
):
    monkeypatch.setattr(cli, "resolve_claude_bin", lambda: str(fake_claude))
    prompt = tmp_path / "p.md"
    prompt.write_text("x")
    rc = cli.main(
        [
            "--prompt-file",
            str(prompt),
            "--cwd",
            str(tmp_path),
            "--state-dir",
            str(tmp_path / "state"),
            "--dry-run",
            "--json",
            *opt,
        ]
    )
    assert rc == 2
    resp = ApiRunnerResponse.model_validate_json(capsys.readouterr().out.strip())
    assert [e.code for e in resp.errors] == [ApiRunnerErrorCode.ERR_INVALID_INPUT]
    assert resp.argv == []
