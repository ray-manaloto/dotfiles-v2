"""Tests for the foreign-agent-hooks doctor check and `hooks:remove-foreign`."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from tasks_py_loader import load_module

from foreign_hooks import cli, core

REPO = Path(__file__).resolve().parent.parent
ASSET = REPO / "unix/home/.config/mise/assets/foreign-agent-hooks.json"
doctor = load_module("doctor_checks")

ORCA_CMD = 'ORCA_AGENT_HOOK_PORT=1 sh "$HOME/.orca/agent-hooks/claude-hook.sh"'
DCG = {"type": "command", "command": "dcg --hook"}


def _w(home: Path, rel: str, data: Any, mode: int = 0o600) -> Path:
    p = home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        data if isinstance(data, str) else json.dumps(data, indent=2), encoding="utf-8"
    )
    p.chmod(mode)
    return p


def _claude(home: Path) -> Path:
    return _w(
        home,
        ".claude/settings.json",
        {
            "theme": "dark",
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [DCG, {"type": "command", "command": ORCA_CMD}],
                    }
                ],
                "Stop": [{"hooks": [{"type": "command", "command": ORCA_CMD}]}],
            },
        },
        0o640,
    )


def _codex(home: Path) -> Path:
    cmd = "/x/.orca/agent-hooks/orca-agent-hook codex"
    return _w(
        home,
        ".codex/hooks.json",
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": cmd}, DCG]}]}},
    )


def _agy(home: Path) -> Path:
    return _w(
        home,
        ".gemini/config/hooks.json",
        {"orca-status": {"command": "x"}, "dcg": {"command": "dcg --hook"}},
    )


def _opencode(home: Path) -> tuple[Path, Path]:
    plug = home / ".config/opencode/plugins"
    plug.mkdir(parents=True)
    f = plug / "orca-opencode-status.js"
    f.write_text("//")
    d = home / ".config/opencode/orca-opencode-status-tui"
    d.mkdir()
    (d / "i.js").write_text("//")
    (plug / "keep.js").write_text("//")
    return f, d


@pytest.fixture
def markers() -> list[str]:
    return core.load_markers(ASSET)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("DOTFILES_DOCTOR_SCOPE", raising=False)
    return h


def test_clean_passes(home: Path) -> None:
    _w(home, ".claude/settings.json", {"hooks": {"Stop": [{"hooks": [DCG]}]}})
    errs, msg = doctor.check_foreign_agent_hooks(REPO)
    assert errs == [] and "no foreign" in msg


def test_missing_files_pass(home: Path) -> None:
    errs, _ = doctor.check_foreign_agent_hooks(REPO)
    assert errs == []


def test_skipped_in_repo_scope(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _claude(home)
    monkeypatch.setenv("DOTFILES_DOCTOR_SCOPE", "repo")
    errs, msg = doctor.check_foreign_agent_hooks(REPO)
    assert errs == [] and "DOTFILES_DOCTOR_SCOPE=repo" in msg


def test_claude_hit(home: Path) -> None:
    p = _claude(home)
    errs, _ = doctor.check_foreign_agent_hooks(REPO)
    text = "\n".join(errs)
    assert f"{p}:hooks.PreToolUse[0].hooks[1].command" in text
    assert f"{p}:hooks.Stop[0].hooks[0].command" in text
    assert "mise run hooks:remove-foreign" in text


def test_codex_hit(home: Path) -> None:
    p = _codex(home)
    errs, _ = doctor.check_foreign_agent_hooks(REPO)
    assert f"{p}:hooks.Stop[0].hooks[0].command" in "\n".join(errs)


def test_agy_hit(home: Path) -> None:
    p = _agy(home)
    errs, _ = doctor.check_foreign_agent_hooks(REPO)
    text = "\n".join(errs)
    assert f"{p}:orca-status" in text and f"{p}:dcg" not in text


def test_opencode_hit(home: Path) -> None:
    f, d = _opencode(home)
    errs, _ = doctor.check_foreign_agent_hooks(REPO)
    text = "\n".join(errs)
    assert str(f) in text and str(d) in text and "keep.js" not in text


def test_codex_toml_hit(home: Path) -> None:
    p = _w(home, ".codex/config.toml", "a = 1\n[hooks]\ncommand = 'orca-agent-hook'\n")
    errs, _ = doctor.check_foreign_agent_hooks(REPO)
    assert f"{p}:3" in "\n".join(errs)


def test_malformed_json_fails(home: Path) -> None:
    _w(home, ".claude/settings.json", "{not json")
    errs, _ = doctor.check_foreign_agent_hooks(REPO)
    assert any("cannot parse JSON" in e for e in errs)


def test_dry_run_writes_nothing(home: Path, markers: list[str]) -> None:
    c, x, a = _claude(home), _codex(home), _agy(home)
    f, d = _opencode(home)
    before = [p.read_bytes() for p in (c, x, a)]
    actions, errors = core.remove(home, markers, apply=False)
    assert errors == [] and actions
    assert [p.read_bytes() for p in (c, x, a)] == before
    assert f.exists() and d.exists()
    assert not (home / "dev/tmp/foreign-hook-removal").exists()
    assert not (home / ".Trash").exists()


def test_apply_removes_only_foreign(home: Path, markers: list[str]) -> None:
    c, x, a = _claude(home), _codex(home), _agy(home)
    f, d = _opencode(home)
    assert core.remove(home, markers, apply=True, platform="darwin")[1] == []

    claude = json.loads(c.read_text())
    assert claude["theme"] == "dark"
    assert claude["hooks"] == {"PreToolUse": [{"matcher": "Bash", "hooks": [DCG]}]}
    assert json.loads(x.read_text())["hooks"] == {"Stop": [{"hooks": [DCG]}]}
    assert json.loads(a.read_text()) == {"dcg": {"command": "dcg --hook"}}
    assert c.stat().st_mode & 0o777 == 0o640

    backups = sorted((home / "dev/tmp/foreign-hook-removal").glob("*/*"))
    assert {b.name for b in backups} == {
        "claude-settings.json",
        "codex-hooks.json",
        "agy-hooks.json",
    }
    assert not f.exists() and not d.exists()
    assert (home / ".config/opencode/plugins/keep.js").exists()
    assert (home / ".Trash/orca-opencode-status.js").exists()
    assert (home / ".Trash/orca-opencode-status-tui/i.js").exists()
    errs, _ = doctor.check_foreign_agent_hooks(REPO)
    assert errs == []


def test_backup_has_original_content(home: Path, markers: list[str]) -> None:
    c = _claude(home)
    original = c.read_text()
    core.remove(home, markers, apply=True)
    (backup,) = (home / "dev/tmp/foreign-hook-removal").glob("*/claude-settings.json")
    assert backup.read_text() == original


def test_malformed_not_written(home: Path, markers: list[str]) -> None:
    bad = _w(home, ".claude/settings.json", "{not json")
    _agy(home)
    actions, errors = core.remove(home, markers, apply=True)
    assert bad.read_text() == "{not json"
    assert any("cannot parse JSON" in e for e in errors)
    assert any("hooks.json" in a for a in actions)


def test_cli_dry_run_and_apply(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    c = _claude(home)
    before = c.read_bytes()
    assert cli.main([]) == 0
    assert c.read_bytes() == before
    assert "dry-run" in capsys.readouterr().out
    assert cli.main(["--apply"]) == 0
    assert ORCA_CMD not in c.read_text()


def test_cli_malformed_exit_nonzero(home: Path) -> None:
    _w(home, ".gemini/config/hooks.json", "[")
    assert cli.main(["--apply"]) == 1


def test_env_home_used(home: Path) -> None:
    assert Path.home() == Path(os.environ["HOME"])


def test_symlink_target_written_link_kept(home: Path, markers: list[str]) -> None:
    real = _w(home, "dots/claude-settings.json", json.loads(_claude(home).read_text()))
    link = home / ".claude/settings.json"
    link.unlink()
    link.symlink_to(real)
    _actions, errors = core.remove(home, markers, apply=True, platform="darwin")
    assert errors == []
    assert link.is_symlink()
    assert ORCA_CMD not in real.read_text()
    errs, _ = doctor.check_foreign_agent_hooks(REPO)
    assert errs == []


def test_symlink_into_git_checkout_refused(home: Path, markers: list[str]) -> None:
    repo = home / "repo"
    (repo / ".git").mkdir(parents=True)
    real = _w(repo, "claude.json", json.loads(_claude(home).read_text()))
    link = home / ".claude/settings.json"
    link.unlink()
    link.symlink_to(real)
    before = real.read_bytes()
    actions, errors = core.remove(home, markers, apply=True, platform="darwin")
    assert errors == [] and real.read_bytes() == before
    assert any(a.startswith("MANUAL") and str(real) in a for a in actions)
    assert not (home / "dev/tmp/foreign-hook-removal").exists()


def test_agy_matches_command_not_arbitrary_body(home: Path, markers: list[str]) -> None:
    p = _w(
        home,
        ".gemini/config/hooks.json",
        {
            "notes": {"description": "removed orca-status earlier"},
            "args-hook": {"command": "sh", "args": ["/x/orca-agent-hook"]},
        },
    )
    hits, _ = core.scan(home, markers)
    assert hits == [f"{p}:args-hook"]


def test_claude_args_url_prompt_matched(home: Path, markers: list[str]) -> None:
    p = _w(
        home,
        ".claude/settings.json",
        {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "sh",
                                "args": ["orca-agent-hook"],
                            },
                            {"type": "http", "url": "http://x/.orca/agent-hooks/y"},
                            {"type": "prompt", "prompt": "run ORCA_AGENT_HOOK_PORT"},
                            DCG,
                        ]
                    }
                ]
            }
        },
    )
    hits, _ = core.scan(home, markers)
    assert len(hits) == 3 and all(str(p) in h for h in hits)


def test_lost_update_aborts(
    home: Path, markers: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _claude(home)
    real_write = core._atomic_write_bytes

    def racing(target: Path, payload: bytes, expect: bytes) -> None:
        target.write_text('{"changed": true}')
        real_write(target, payload, expect)

    monkeypatch.setattr(core, "_atomic_write_bytes", racing)
    _a, errors = core.remove(home, markers, apply=True, platform="darwin")
    assert any("changed during the run" in e for e in errors)
    assert c.read_text() == '{"changed": true}'


def test_apply_continues_after_oserror(
    home: Path, markers: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _claude(home)
    a = _agy(home)

    def boom(target: Path, payload: bytes, expect: bytes) -> None:
        if target.name == "settings.json":
            raise OSError("disk full")
        Path(target).write_bytes(payload)

    monkeypatch.setattr(core, "_atomic_write_bytes", boom)
    _x, errors = core.remove(home, markers, apply=True, platform="darwin")
    assert any("disk full" in e for e in errors)
    assert "orca-status" not in a.read_text()


def test_cli_exit_1_on_error(home: Path) -> None:
    _claude(home)
    _w(home, ".gemini/config/hooks.json", "[")
    assert cli.main(["--apply"]) == 1


def test_linux_trash_and_backup_perms(home: Path, markers: list[str]) -> None:
    _claude(home)
    f, _d = _opencode(home)
    core.remove(home, markers, apply=True, platform="linux")
    trash = home / ".local/share/Trash/files"
    assert (trash / f.name).exists()
    assert trash.stat().st_mode & 0o777 == 0o700
    (bdir,) = (home / "dev/tmp/foreign-hook-removal").iterdir()
    assert bdir.stat().st_mode & 0o777 == 0o700
    assert str(os.getpid()) in bdir.name


def test_opencode_requires_prefix(home: Path, markers: list[str]) -> None:
    plug = home / ".config/opencode/plugins"
    plug.mkdir(parents=True)
    (plug / "my-orca-opencode-status-notes.md").write_text("x")
    (plug / "orca-opencode-status.js").write_text("x")
    assert [p.name for p in core.opencode_matches(home, markers)] == [
        "orca-opencode-status.js"
    ]


def test_toml_only_hint(home: Path) -> None:
    _w(home, ".codex/config.toml", "x = 'orca-agent-hook'\n")
    errs, _ = doctor.check_foreign_agent_hooks(REPO)
    assert "edit ~/.codex/config.toml manually" in errs[-1]
    assert "remove-foreign" not in errs[-1]
