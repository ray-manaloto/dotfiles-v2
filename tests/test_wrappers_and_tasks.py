"""Tests for operational host, secret, Remote Control, and env:enter tasks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_DIR = REPO_ROOT / "tasks"
MISE_TASKS_DIR = REPO_ROOT / "unix" / "home" / ".config" / "mise" / "tasks"


def _make_stub(directory: Path, name: str, script_body: str) -> Path:
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{script_body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_secret_set_and_delete_clear_fnox_daemon_and_reject_existing(
    tmp_path: Path,
) -> None:
    bin_stub = tmp_path / "bin"
    bin_stub.mkdir()
    call_log = tmp_path / "calls.log"
    _make_stub(
        bin_stub,
        "doppler",
        f'printf "doppler %s\\n" "$*" >> "{call_log}"\n'
        'if [ "$1" = "secrets" ] && [ "$2" = "--only-names" ]; then\n'
        "  echo '{\"EXISTING_KEY\":{}}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )
    (tmp_path / ".config" / "fnox").mkdir(parents=True)
    (tmp_path / ".config" / "fnox" / "config.toml").write_text("", encoding="utf-8")
    _make_stub(
        bin_stub,
        "fnox",
        f'printf "fnox %s\\n" "$*" >> "{call_log}"\nexit 0\n',
    )
    base_env = {
        "HOME": str(tmp_path),
        "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    set_task = MISE_TASKS_DIR / "secret" / "set"
    del_task = MISE_TASKS_DIR / "secret" / "delete"

    bad_name = subprocess.run(
        ["bash", str(set_task)],
        env={**base_env, "usage_name": "lowercase_bad"},
        input="val\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_name.returncode == 1
    assert "UPPER_SNAKE" in bad_name.stderr

    dup = subprocess.run(
        ["bash", str(set_task)],
        env={**base_env, "usage_name": "EXISTING_KEY"},
        input="val\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert dup.returncode == 1
    assert "--replace" in dup.stderr

    ok_set = subprocess.run(
        ["bash", str(set_task)],
        env={**base_env, "usage_name": "NEW_KEY"},
        input="secret-value\n",
        capture_output=True,
        text=True,
        check=True,
    )
    assert "stored NEW_KEY" in ok_set.stdout

    ok_rep = subprocess.run(
        ["bash", str(set_task)],
        env={**base_env, "usage_name": "EXISTING_KEY", "usage_replace": "true"},
        input="updated\n",
        capture_output=True,
        text=True,
        check=True,
    )
    assert "stored EXISTING_KEY" in ok_rep.stdout

    ok_del = subprocess.run(
        ["bash", str(del_task)],
        env={**base_env, "usage_names": "NEW_KEY EXISTING_KEY"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "deleted NEW_KEY EXISTING_KEY" in ok_del.stdout

    log_txt = call_log.read_text(encoding="utf-8")
    assert log_txt.count("daemon clear") == 3
    assert "doppler secrets set NEW_KEY" in log_txt
    assert "doppler secrets delete NEW_KEY EXISTING_KEY" in log_txt


def test_host_prune_cleans_zsh_completions_and_reads_imported_fnox_secrets(
    tmp_path: Path,
) -> None:
    bin_stub = tmp_path / "bin"
    bin_stub.mkdir()
    fake_home = tmp_path / "home"
    (fake_home / ".cache" / "zsh" / "completions").mkdir(parents=True)
    (fake_home / ".config" / "fnox").mkdir(parents=True)
    mise_dir = fake_home / ".config" / "mise"
    mise_dir.mkdir(parents=True)
    (mise_dir / "config.toml").write_text("[tools]\n", encoding="utf-8")
    clean_git_env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    subprocess.run(
        ["git", "-C", str(mise_dir), "init", "-q"],
        env=clean_git_env,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(mise_dir), "config", "user.email", "test@example.com"],
        env=clean_git_env,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(mise_dir), "config", "user.name", "Test User"],
        env=clean_git_env,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(mise_dir), "add", "config.toml"],
        env=clean_git_env,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(mise_dir), "commit", "-q", "--no-verify", "-m", "init"],
        env=clean_git_env,
        check=True,
    )
    config_local = mise_dir / "config.local.toml"
    config_local.write_text(
        '[env]\nCLOUDFLARE_ACCOUNT_ID = "acct-123"\n', encoding="utf-8"
    )

    (fake_home / ".config" / "fnox" / "shared.toml").write_text(
        '[secrets.CLOUDFLARE_ACCOUNT_ID]\nprovider = "age"\n', encoding="utf-8"
    )
    (fake_home / ".config" / "fnox" / "config.toml").write_text(
        'import = ["./shared.toml"]\n', encoding="utf-8"
    )

    zcomp = fake_home / ".cache" / "zsh" / "completions"
    dead_link = zcomp / "_sbx"
    dead_link.symlink_to(fake_home / "missing-comp")
    zero_file = zcomp / "._empty123"
    zero_file.write_text("", encoding="utf-8")
    keep_file = zcomp / "_valid"
    keep_file.write_text("#compdef mise\n", encoding="utf-8")

    fnox_log = tmp_path / "fnox.log"
    _make_stub(bin_stub, "mise", 'if [ "$1" = "registry" ]; then exit 0; fi; exit 0')
    _make_stub(
        bin_stub,
        "fnox",
        f'printf "%s\\n" "$*" >> "{fnox_log}"\nexit 0',
    )

    prune_task = MISE_TASKS_DIR / "host" / "prune"
    env = {
        "HOME": str(fake_home),
        "MISE_DATA_DIR": str(tmp_path / "mise_data"),
        "PATH": f"{bin_stub}:{Path(sys.executable).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    dry = subprocess.run(
        ["bash", str(prune_task)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"WOULD REMOVE dead symlink: {dead_link}" in dry.stdout
    assert f"WOULD REMOVE zero-byte file: {zero_file}" in dry.stdout
    assert f"WOULD BACK UP & REMOVE untracked {config_local}" in dry.stdout
    assert dead_link.is_symlink()
    assert zero_file.exists()
    assert config_local.is_file()

    app = subprocess.run(
        ["bash", str(prune_task)],
        env={**env, "usage_apply": "true"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"REMOVED dead symlink: {dead_link}" in app.stdout
    assert f"REMOVED zero-byte file: {zero_file}" in app.stdout
    assert f"BACKED UP & REMOVED untracked {config_local}" in app.stdout
    assert not dead_link.is_symlink()
    assert not zero_file.exists()
    assert not config_local.exists()
    assert list(
        (fake_home / ".local" / "state" / "dotfiles-v2").glob("config.local.toml.bak.*")
    )
    assert "check --all --strict" in fnox_log.read_text(encoding="utf-8")
    assert keep_file.is_file()


def test_host_prune_mcps_audits_and_prunes_preserving_backups(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    (fake_home / ".gemini" / "config").mkdir(parents=True)
    (fake_home / ".codex").mkdir(parents=True)
    mcp_cfg = fake_home / ".gemini" / "config" / "mcp_config.json"
    mcp_cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "exa": {"url": "https://mcp.exa.ai/mcp"},
                    "custom-keep": {"command": "echo"},
                }
            }
        ),
        encoding="utf-8",
    )
    agy_cfg = fake_home / ".gemini" / "config" / "config.json"
    agy_cfg.write_text(
        json.dumps(
            {
                "enabledPlugins": {
                    "apify@apify-plugins": True,
                    "apify-other@custom": True,
                }
            }
        ),
        encoding="utf-8",
    )
    claude_json = fake_home / ".claude.json"
    claude_json.write_text(
        json.dumps({"mcpServers": {"mcp-jdx": {}, "keep": {}}}), encoding="utf-8"
    )
    codex_cfg = fake_home / ".codex" / "config.toml"
    codex_cfg.write_text(
        '[mcp_servers.context7]\nurl = "https://mcp.context7.com/mcp"\n'
        '[[projects."/workspace/repo"]]\ntrust_level = "trusted"\n'
        '[mcp_servers.keepme]\ncommand = "echo"\n',
        encoding="utf-8",
    )
    task = MISE_TASKS_DIR / "host" / "prune-mcps"
    chk = subprocess.run(
        ["bash", str(task)],
        env={
            "HOME": str(fake_home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_check": "true",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert "FOUND gemini-mcp" in chk.stdout
    assert "FOUND gemini-plugin" in chk.stdout
    assert "FOUND claude-mcp" in chk.stdout
    assert "FOUND codex-mcp" in chk.stdout

    app = subprocess.run(
        ["bash", str(task)],
        env={
            "HOME": str(fake_home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_apply": "true",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert "PRUNED" in app.stdout
    assert "exa" not in json.loads(mcp_cfg.read_text())["mcpServers"]
    assert "custom-keep" in json.loads(mcp_cfg.read_text())["mcpServers"]
    enabled = json.loads(agy_cfg.read_text())["enabledPlugins"]
    assert "apify@apify-plugins" not in enabled
    assert "apify-other@custom" in enabled
    assert "mcp-jdx" not in json.loads(claude_json.read_text())["mcpServers"]
    codex_after = codex_cfg.read_text()
    assert "context7" not in codex_after
    assert '[[projects."/workspace/repo"]]' in codex_after
    assert "keepme" in codex_after
    backups = list(
        (fake_home / ".local" / "state" / "dotfiles-v2" / "backups").glob("*.bak")
    )
    assert len(backups) == 4


def test_host_prune_mcps_handles_claude_config_dir_and_dedup(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    claude_dir = fake_home / ".claude"
    custom_dir = tmp_path / "custom-claude"
    claude_dir.mkdir(parents=True)
    custom_dir.mkdir(parents=True)

    home_claude = fake_home / ".claude.json"
    home_claude.write_text(
        json.dumps({"mcpServers": {"exa": {}, "keep-home": {}}}), encoding="utf-8"
    )
    dir_claude = claude_dir / ".claude.json"
    dir_claude.write_text(
        json.dumps({"mcpServers": {"firecrawl": {}, "keep-dir": {}}}), encoding="utf-8"
    )
    custom_claude = custom_dir / ".claude.json"
    custom_claude.write_text(
        json.dumps({"mcpServers": {"context7": {}, "keep-custom": {}}}),
        encoding="utf-8",
    )

    task = MISE_TASKS_DIR / "host" / "prune-mcps"
    chk = subprocess.run(
        ["bash", str(task)],
        env={
            "HOME": str(fake_home),
            "CLAUDE_CONFIG_DIR": str(custom_dir),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_check": "true",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"FOUND claude-mcp {home_claude}: mcpServers.exa" in chk.stdout
    assert f"FOUND claude-mcp {custom_claude}: mcpServers.context7" in chk.stdout
    assert f"FOUND claude-mcp {dir_claude}: mcpServers.firecrawl" in chk.stdout

    app = subprocess.run(
        ["bash", str(task)],
        env={
            "HOME": str(fake_home),
            "CLAUDE_CONFIG_DIR": str(custom_dir),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_apply": "true",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert app.stdout.count("PRUNED claude-mcp") == 3
    assert json.loads(home_claude.read_text()) == {"mcpServers": {"keep-home": {}}}
    assert json.loads(custom_claude.read_text()) == {"mcpServers": {"keep-custom": {}}}
    assert json.loads(dir_claude.read_text()) == {"mcpServers": {"keep-dir": {}}}
    backups = list(
        (fake_home / ".local" / "state" / "dotfiles-v2" / "backups").glob("*.bak")
    )
    assert len(backups) == 3

    # Deduplicate when CLAUDE_CONFIG_DIR == ~/.claude and ~/.claude/.claude.json symlinks to ~/.claude.json.
    dedup_home = tmp_path / "dedup-home"
    (dedup_home / ".claude").mkdir(parents=True)
    dedup_root = dedup_home / ".claude.json"
    dedup_root.write_text(
        json.dumps({"mcpServers": {"exa": {}, "keep": {}}}), encoding="utf-8"
    )
    (dedup_home / ".claude" / ".claude.json").symlink_to(dedup_root)

    dedup_chk = subprocess.run(
        ["bash", str(task)],
        env={
            "HOME": str(dedup_home),
            "CLAUDE_CONFIG_DIR": str(dedup_home / ".claude"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_check": "true",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert dedup_chk.stdout.count("FOUND claude-mcp") == 1

    dedup_app = subprocess.run(
        ["bash", str(task)],
        env={
            "HOME": str(dedup_home),
            "CLAUDE_CONFIG_DIR": str(dedup_home / ".claude"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_apply": "true",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert dedup_app.stdout.count("PRUNED claude-mcp") == 1
    assert json.loads(dedup_root.read_text()) == {"mcpServers": {"keep": {}}}
    dedup_backups = list(
        (dedup_home / ".local" / "state" / "dotfiles-v2" / "backups").glob("*.bak")
    )
    assert len(dedup_backups) == 1


def test_host_prune_removes_dangling_unix_home_symlinks_in_local_bin(
    tmp_path: Path,
) -> None:
    bin_stub = tmp_path / "bin"
    bin_stub.mkdir()
    fake_home = tmp_path / "home"
    local_bin = fake_home / ".local" / "bin"
    local_bin.mkdir(parents=True)

    valid_target = tmp_path / "repo" / "unix" / "home" / ".local" / "bin" / "keep-tool"
    valid_target.parent.mkdir(parents=True)
    valid_target.write_text("#!/bin/sh\n", encoding="utf-8")

    valid_link = local_bin / "keep-tool"
    valid_link.symlink_to(valid_target)

    dangling_dotfiles = local_bin / "agy-hc"
    dangling_dotfiles.symlink_to(
        tmp_path / "repo" / "unix" / "home" / ".local" / "bin" / "agy-hc"
    )

    dangling_custom = local_bin / "custom-missing"
    dangling_custom.symlink_to(tmp_path / "other" / "bin" / "custom-missing")

    _make_stub(bin_stub, "mise", 'if [ "$1" = "registry" ]; then exit 0; fi; exit 0')
    prune_task = MISE_TASKS_DIR / "host" / "prune"
    env = {
        "HOME": str(fake_home),
        "MISE_DATA_DIR": str(tmp_path / "mise_data"),
        "PATH": f"{bin_stub}:{Path(sys.executable).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
    }

    dry = subprocess.run(
        ["bash", str(prune_task)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"WOULD REMOVE dangling dotfiles symlink: {dangling_dotfiles}" in dry.stdout
    assert str(dangling_custom) not in dry.stdout
    assert str(valid_link) not in dry.stdout
    assert dangling_dotfiles.is_symlink()

    app = subprocess.run(
        ["bash", str(prune_task)],
        env={**env, "usage_apply": "true"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"REMOVED dangling dotfiles symlink: {dangling_dotfiles}" in app.stdout
    assert not dangling_dotfiles.is_symlink()
    assert valid_link.is_symlink() and valid_link.exists()
    assert dangling_custom.is_symlink() and not dangling_custom.exists()


def test_agy_rc_guard_check_and_never_runs_serve_help(tmp_path: Path) -> None:
    guard_script = MISE_TASKS_DIR / "host" / "agy-rc-guard"
    txt = guard_script.read_text(encoding="utf-8")
    # Issue #1742 invariant: serve --help leaks zombie remote-control listener
    non_comment_lines = "\n".join(
        line for line in txt.splitlines() if not line.lstrip().startswith("#")
    )
    assert "serve --help" not in non_comment_lines
    assert "kickstart" in txt  # #1269 handling
    assert "antigravity-oauth-token" in txt  # #1401 token check

    # Mock LaunchAgent + clean token passes --check --json
    bin_stub = tmp_path / "bin"
    bin_stub.mkdir()
    fake_home = tmp_path / "home"
    (fake_home / "Library" / "LaunchAgents").mkdir(parents=True)
    (fake_home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    token_file = fake_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    token_file.write_text("tok\n", encoding="utf-8")
    plist = (
        fake_home
        / "Library"
        / "LaunchAgents"
        / "com.google.antigravity-cli-daemon.plist"
    )
    plist.write_text(
        "<plist><dict><key>Label</key><string>com.google.antigravity-cli-daemon</string></dict></plist>\n",
        encoding="utf-8",
    )
    _make_stub(
        bin_stub,
        "agy",
        'if [ "$1" = "remote-control" ] && [ "$2" = "status" ]; then echo "Daemon status: running"; exit 0; fi; exit 1',
    )
    _make_stub(
        bin_stub,
        "launchctl",
        'echo "state = running"; exit 0',
    )

    if os.uname().sysname == "Darwin":
        # agy 1.5.16 prints `Daemon state = running`; older builds print `Daemon status: running`.
        for status_line in ("Daemon state = running", "Daemon status: running"):
            _make_stub(
                bin_stub,
                "agy",
                'if [ "$1" = "remote-control" ] && [ "$2" = "status" ]; then echo "'
                + status_line
                + '"; exit 0; fi; exit 1',
            )
            proc = subprocess.run(
                ["bash", str(guard_script)],
                env={
                    "HOME": str(fake_home),
                    "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
                    "usage_check": "true",
                    "usage_json": "true",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            assert proc.returncode == 0, status_line + proc.stdout + proc.stderr
            parsed = json.loads(proc.stdout)
            assert parsed["token_ok"] is True
            assert parsed["token_source"] == "file"
            assert parsed["rc_running"] is True, status_line
            assert parsed["issues"] == []

        # Keychain-authenticated agy 1.5.16 daemon (`antigravity-oauth-token` absent, log shows `Authenticated as `)
        token_file.unlink()
        daemon_log = fake_home / "Library" / "Logs" / "antigravity-cli-daemon.log"
        daemon_log.parent.mkdir(parents=True, exist_ok=True)
        daemon_log.write_text(
            "starting daemon\nAuthenticated as user@example.com\n", encoding="utf-8"
        )
        _make_stub(
            bin_stub,
            "agy",
            'if [ "$1" = "remote-control" ] && [ "$2" = "status" ]; then echo "Daemon state = running"; exit 0; fi; exit 1',
        )
        kc_proc = subprocess.run(
            ["bash", str(guard_script)],
            env={
                "HOME": str(fake_home),
                "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
                "usage_check": "true",
                "usage_json": "true",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert kc_proc.returncode == 0, kc_proc.stdout + kc_proc.stderr
        kc_parsed = json.loads(kc_proc.stdout)
        assert kc_parsed["token_ok"] is True
        assert kc_parsed["token_source"] == "keychain-daemon"
        assert kc_parsed["auth_error_in_log"] is False
        assert kc_parsed["issues"] == []

        # Later auth error overrides earlier `Authenticated as`
        daemon_log.write_text(
            "Authenticated as user@example.com\nerror getting token source: expired\n",
            encoding="utf-8",
        )
        err_proc = subprocess.run(
            ["bash", str(guard_script)],
            env={
                "HOME": str(fake_home),
                "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
                "usage_check": "true",
                "usage_json": "true",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert err_proc.returncode == 1
        err_parsed = json.loads(err_proc.stdout)
        assert err_parsed["auth_error_in_log"] is True
        assert err_parsed["token_ok"] is False
        assert err_parsed["token_source"] == "missing"
        assert any("recent OAuth failure" in i for i in err_parsed["issues"])

        # Recovery (`Authenticated as` after earlier auth error) restores keychain-daemon health
        daemon_log.write_text(
            "You are not logged into Antigravity\nAuthenticated as user@example.com\n",
            encoding="utf-8",
        )
        rec_proc = subprocess.run(
            ["bash", str(guard_script)],
            env={
                "HOME": str(fake_home),
                "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
                "usage_check": "true",
                "usage_json": "true",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert rec_proc.returncode == 0, rec_proc.stdout + rec_proc.stderr
        rec_parsed = json.loads(rec_proc.stdout)
        assert rec_parsed["auth_error_in_log"] is False
        assert rec_parsed["token_ok"] is True
        assert rec_parsed["token_source"] == "keychain-daemon"
        assert rec_parsed["issues"] == []

        # Long (>33 KiB) trailing diagnostic line does not hide an earlier `Authenticated as` line
        daemon_log.write_text(
            "Authenticated as user@example.com\n" + ("x" * 34000) + "\n",
            encoding="utf-8",
        )
        long_proc = subprocess.run(
            ["bash", str(guard_script)],
            env={
                "HOME": str(fake_home),
                "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
                "usage_check": "true",
                "usage_json": "true",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert long_proc.returncode == 0, long_proc.stdout + long_proc.stderr
        assert json.loads(long_proc.stdout)["token_source"] == "keychain-daemon"

        # 80 lines spanning >32 KiB (crossing chunk boundaries + UTF-8) detect error cleanly;
        # an error line quoting `"Authenticated as "` stays a failure.
        token_file.write_text("tok\n", encoding="utf-8")
        long_post_line = ("é" * 350) + " post\n"
        daemon_log.write_text(
            ("pad line\n" * 4500)
            + "error getting token source: expected 'Authenticated as '\n"
            + (long_post_line * 79),
            encoding="utf-8",
        )
        bnd_proc = subprocess.run(
            ["bash", str(guard_script)],
            env={
                "HOME": str(fake_home),
                "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
                "usage_check": "true",
                "usage_json": "true",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert bnd_proc.returncode == 1
        bnd_parsed = json.loads(bnd_proc.stdout)
        assert bnd_parsed["auth_error_in_log"] is True
        assert bnd_parsed["token_source"] == "file"

        # Aligned 4 MiB boundary preserves a complete failure record at exact cap start
        four_mib = 4 * 1024 * 1024
        err_rec = b"error getting token source: aligned\n"
        trailing = b"ok diag line\n" * 10
        prefix_len = four_mib - (len(err_rec) + len(trailing))
        assert prefix_len > 0
        daemon_log.write_bytes((b"a" * (prefix_len - 1)) + b"\n" + err_rec + trailing)
        aligned_proc = subprocess.run(
            ["bash", str(guard_script)],
            env={
                "HOME": str(fake_home),
                "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
                "usage_check": "true",
                "usage_json": "true",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert aligned_proc.returncode == 1
        assert json.loads(aligned_proc.stdout)["auth_error_in_log"] is True

        # Missing token file and no daemon log fails closed
        token_file.unlink()
        daemon_log.unlink()
        miss_proc = subprocess.run(
            ["bash", str(guard_script)],
            env={
                "HOME": str(fake_home),
                "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
                "usage_check": "true",
                "usage_json": "true",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert miss_proc.returncode == 1
        miss_parsed = json.loads(miss_proc.stdout)
        assert miss_parsed["token_ok"] is False
        assert miss_parsed["token_source"] == "missing"
        assert any(
            "antigravity-oauth-token" in i and "#1401" in i
            for i in miss_parsed["issues"]
        )
        token_file.write_text("tok\n", encoding="utf-8")

        # Nonzero exit status from `agy remote-control status` fails even if text matches.
        _make_stub(
            bin_stub,
            "agy",
            'if [ "$1" = "remote-control" ] && [ "$2" = "status" ]; then echo "Daemon state = running"; exit 1; fi; exit 1',
        )
        nz_proc = subprocess.run(
            ["bash", str(guard_script)],
            env={
                "HOME": str(fake_home),
                "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
                "usage_check": "true",
                "usage_json": "true",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert nz_proc.returncode == 1
        assert json.loads(nz_proc.stdout)["rc_running"] is False

        # #1269 stuck loaded LaunchAgent triggers launchctl kickstart -k (not stop/start)
        rc_log = tmp_path / "rc.log"
        _make_stub(
            bin_stub,
            "agy",
            f'printf "agy %s\\n" "$*" >> "{rc_log}"\n'
            'if [ "$1" = "remote-control" ] && [ "$2" = "status" ]; then\n'
            '  if [ -f "'
            + str(tmp_path / "kicked")
            + '" ]; then echo "Daemon status: running"; else echo "Daemon status: stopped"; fi\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
        )
        _make_stub(
            bin_stub,
            "launchctl",
            f'printf "launchctl %s\\n" "$*" >> "{rc_log}"\n'
            'if [ "$1" = "kickstart" ]; then\n'
            f'  touch "{tmp_path / "kicked"}"\n'
            "  exit 0\n"
            "fi\n"
            f'if [ -f "{tmp_path / "kicked"}" ]; then echo "state = running"; else echo "state = waiting"; fi\n'
            "exit 0\n",
        )
        rep = subprocess.run(
            ["bash", str(guard_script)],
            env={
                "HOME": str(fake_home),
                "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
                "usage_repair": "true",
                "usage_json": "true",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert rep.returncode == 0, rep.stdout + rep.stderr
        rc_calls = rc_log.read_text(encoding="utf-8")
        assert "launchctl kickstart -k" in rc_calls
        assert "agy remote-control stop" not in rc_calls

        # #1401 missing token triggers clean stop + reseed + start
        token_file.unlink()
        rc_log.write_text("", encoding="utf-8")
        _make_stub(
            bin_stub,
            "agy",
            f'printf "agy %s\\n" "$*" >> "{rc_log}"\n'
            'if [ "$1" = "--print" ]; then printf "tok" > "'
            + str(token_file)
            + '"; exit 0; fi\n'
            'if [ "$1" = "remote-control" ] && [ "$2" = "status" ]; then echo "Daemon status: running"; exit 0; fi\n'
            "exit 0\n",
        )
        rep2 = subprocess.run(
            ["bash", str(guard_script)],
            env={
                "HOME": str(fake_home),
                "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
                "usage_repair": "true",
                "usage_json": "true",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert rep2.returncode == 0, rep2.stdout + rep2.stderr
        rc_calls2 = rc_log.read_text(encoding="utf-8")
        assert "agy remote-control stop" in rc_calls2
        assert "agy --print /status" in rc_calls2
        assert "agy remote-control start" in rc_calls2

    # Non-Darwin skip path works everywhere (stubbed Linux uname)
    non_darwin_bin = tmp_path / "non_darwin_bin"
    non_darwin_bin.mkdir()
    _make_stub(non_darwin_bin, "uname", 'echo "Linux"')
    skip_proc = subprocess.run(
        ["bash", str(guard_script)],
        env={
            "HOME": str(fake_home),
            "PATH": f"{non_darwin_bin}:/usr/bin:/bin",
            "usage_check": "true",
            "usage_json": "true",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(skip_proc.stdout) == {"status": "skipped", "reason": "non-darwin"}


def test_env_enter_falls_back_to_docker_when_tailscale_ssh_unavailable(
    tmp_path: Path,
) -> None:
    bin_stub = tmp_path / "bin"
    bin_stub.mkdir()
    log_file = tmp_path / "calls.log"
    _make_stub(bin_stub, "tailscale", "exit 1")
    _make_stub(bin_stub, "ssh", "exit 255")
    _make_stub(
        bin_stub,
        "docker",
        f'printf "%s\\n" "$*" >> "{log_file}"; exit 0',
    )

    proc = subprocess.run(
        ["bash", str(MISE_TASKS_DIR / "env" / "enter")],
        env={
            "HOME": str(tmp_path),
            "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_target": "pc",
            "usage_project": "kb-v2",
            "usage_cmd": "pwd",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert "falling back to direct runtime exec" in proc.stderr
    logged = log_file.read_text(encoding="utf-8")
    assert "exec -i dotfiles-v2 /bin/bash -lc" in logged
    assert "/workspace-kb" in logged
    assert "exit 1" in logged  # fails closed when workspace dir missing

    # --exec bypasses Tailscale SSH probe even if ssh succeeds, and aliases (df/kb/cpp) resolve cleanly
    _make_stub(bin_stub, "ssh", "echo SHOULD_NOT_PROBE_SSH >&2; exit 0")
    exec_log = tmp_path / "exec-calls.log"
    _make_stub(
        bin_stub,
        "docker",
        f'printf "%s\\n" "$*" >> "{exec_log}"; exit 0',
    )
    for alias_proj, want_dir in (
        ("df", "/workspace"),
        ("kb", "/workspace-kb"),
        ("cpp", "/workspace-cpp"),
    ):
        p_alias = subprocess.run(
            ["bash", str(MISE_TASKS_DIR / "env" / "enter")],
            env={
                "HOME": str(tmp_path),
                "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
                "usage_target": "mac",
                "usage_project": alias_proj,
                "usage_exec": "true",
            },
            capture_output=True,
            text=True,
            check=True,
        )
        assert "falling back" not in p_alias.stderr
        assert "SHOULD_NOT_PROBE_SSH" not in p_alias.stderr
        alias_logged = exec_log.read_text(encoding="utf-8").splitlines()[-1]
        assert want_dir in alias_logged
        assert "exec zsh -il" in alias_logged

    # container/shell & vm/shell fail closed if workspace dir does not exist
    shell_txt = (TASKS_DIR / "container" / "shell").read_text(encoding="utf-8")
    vm_txt = (TASKS_DIR / "vm" / "shell").read_text(encoding="utf-8")
    assert "/bin/bash -lc" in shell_txt
    assert "/bin/zsh -lc" in vm_txt
    assert "exit 1" in shell_txt
    assert "exit 1" in vm_txt


def test_sync_gemini_and_6harness_parity_retire_legacy_mirrors(
    tmp_path: Path, monkeypatch
) -> None:
    from tasks_py_loader import load_module

    doc_mod = load_module("doctor_checks")
    fx_repo = tmp_path / "fx_repo"
    fx_mise = fx_repo / "unix" / "home" / ".config" / "mise"
    fx_mise.mkdir(parents=True)
    (fx_repo / "unix" / "home" / ".zshenv").write_text("# zshenv\n", encoding="utf-8")
    fx_prog = fx_repo / "unix" / "home" / ".config" / "herdr-app"
    fx_prog.mkdir(parents=True)
    (fx_prog / "programs.json").write_text(
        json.dumps(
            {
                "programs": {
                    "antigravity": {"run": ["agy"]},
                    "codex": {"run": ["codex", "--no-daemon"]},
                    "opencode": {"run": ["opencode", "--standalone"]},
                }
            }
        ),
        encoding="utf-8",
    )
    (fx_mise / "config.toml").write_text(
        '[dotfiles]\n"~/.zshenv" = { main_checkout = true }\n'
        '"~/.config/herdr-app/programs.json" = { main_checkout = true }\n'
        '"~/.gemini/GEMINI.md" = { mode = "absent" }\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(doc_mod, "_load_mise_registry_map", lambda: {})
    assert doc_mod.check_config_hygiene(fx_repo) == []

    # An `absent` target must NOT satisfy another entry's `source` dependency.
    (fx_mise / "config.toml").write_text(
        '[dotfiles]\n"~/.zshenv" = { main_checkout = true }\n'
        '"~/.config/herdr-app/programs.json" = { main_checkout = true }\n'
        '"~/.gemini/GEMINI.md" = { mode = "absent" }\n'
        '"~/.gemini/config/AGENTS.md" = { source = "~/.gemini/GEMINI.md" }\n',
        encoding="utf-8",
    )
    absent_dep_errs = doc_mod.check_config_hygiene(fx_repo)
    assert any("undeclared tilde source" in e for e in absent_dep_errs)

    fake_home = tmp_path / "home"
    (fake_home / ".gemini" / "config" / "skills" / "pwf-status").mkdir(parents=True)
    (
        (fake_home / ".gemini" / "config" / "skills" / "pwf-status") / "SKILL.md"
    ).symlink_to("../../.agents/skills/pwf-status/SKILL.md")
    zcode_skills = fake_home / ".zcode" / "skills"
    zcode_skills.mkdir(parents=True)
    (zcode_skills / "pwf-status").symlink_to("../../.agents/skills/pwf-status")
    (zcode_skills / "custom-alias").symlink_to("../../.agents/skills/vercel-board")
    (zcode_skills / "custom-user-skill").mkdir()
    (zcode_skills / "custom-user-skill" / "SKILL.md").write_text(
        "# custom\n", encoding="utf-8"
    )

    test_path = f"{Path(sys.executable).parent}:/usr/bin:/bin:/usr/sbin:/sbin"
    apply_proc = subprocess.run(
        ["bash", str(TASKS_DIR / "host" / "sync-gemini")],
        cwd=REPO_ROOT,
        env={
            "HOME": str(fake_home),
            "PATH": test_path,
            "usage_check": "false",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert "legacy zcode mirror(s) retired" in apply_proc.stdout
    assert not (fake_home / ".gemini" / "config" / "skills").exists()
    assert not (zcode_skills / "pwf-status").is_symlink()
    assert (zcode_skills / "custom-alias").is_symlink()
    assert (zcode_skills / "custom-user-skill" / "SKILL.md").is_file()

    check_proc = subprocess.run(
        ["bash", str(TASKS_DIR / "host" / "sync-gemini")],
        cwd=REPO_ROOT,
        env={
            "HOME": str(fake_home),
            "PATH": test_path,
            "usage_check": "true",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert "OK:" in check_proc.stdout

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(
        doc_mod,
        "_is_dotfiles_v2_mise_cutover",
        lambda home, repo_root=None: (True, "ok"),
    )
    monkeypatch.setattr(
        doc_mod.shutil,
        "which",
        lambda name: "/usr/local/bin/agentsview" if name == "agentsview" else None,
    )

    missing_av, _ = doc_mod.check_skills_6harness_parity(REPO_ROOT)
    assert any("agentsview-finding-history must be a real" in e for e in missing_av)

    av_dir = fake_home / ".agents" / "skills" / "agentsview-finding-history"
    av_dir.mkdir(parents=True)
    (av_dir / "SKILL.md").write_text("# agentsview\n", encoding="utf-8")
    # Real Claude dir (not symlink) fails the agentsview symlink assertion.
    claude_av_dir = fake_home / ".claude" / "skills" / "agentsview-finding-history"
    claude_av_dir.mkdir(parents=True)
    (claude_av_dir / "SKILL.md").write_text("# agentsview\n", encoding="utf-8")
    bad_claude_errs, _ = doc_mod.check_skills_6harness_parity(REPO_ROOT)
    assert any(
        "~/.claude/skills/agentsview-finding-history must symlink" in e
        for e in bad_claude_errs
    )
    (claude_av_dir / "SKILL.md").unlink()
    claude_av_dir.rmdir()
    claude_av_dir.symlink_to("../../.agents/skills/agentsview-finding-history")
    clean_errs, _ = doc_mod.check_skills_6harness_parity(REPO_ROOT)
    assert clean_errs == []

    # Missing canonical required skill on host fails 6-harness parity.
    saved_req = (fake_home / ".agents" / "skills" / "pwf-status").resolve()
    (fake_home / ".agents" / "skills" / "pwf-status").unlink()
    missing_req_errs, _ = doc_mod.check_skills_6harness_parity(REPO_ROOT)
    assert any(
        "missing canonical host skill ~/.agents/skills/pwf-status" in e
        for e in missing_req_errs
    )
    (fake_home / ".agents" / "skills" / "pwf-status").symlink_to(saved_req)

    # Reintroducing a legacy same-name ~/.zcode/skills mirror trips 6-harness parity.
    (zcode_skills / "pwf-status").symlink_to("../../.agents/skills/pwf-status")
    stale_errs, _ = doc_mod.check_skills_6harness_parity(REPO_ROOT)
    assert any("stale ~/.zcode/skills/pwf-status" in e for e in stale_errs)
    (zcode_skills / "pwf-status").unlink()

    # Whole-directory ~/.zcode/skills symlink to ~/.agents/skills also fails.
    (zcode_skills / "custom-user-skill" / "SKILL.md").unlink()
    (zcode_skills / "custom-user-skill").rmdir()
    (zcode_skills / "custom-alias").unlink()
    zcode_skills.rmdir()
    zcode_skills.symlink_to(fake_home / ".agents" / "skills")
    root_zcode_errs, _ = doc_mod.check_skills_6harness_parity(REPO_ROOT)
    assert any("stale ~/.zcode/skills root symlink" in e for e in root_zcode_errs)

    # Git config [hook] blocks in unix/home/.config/mise/config.toml must parse cleanly without `;` comment truncation.
    import tomllib

    mise_cfg = tomllib.loads(
        (REPO_ROOT / "unix" / "home" / ".config" / "mise" / "config.toml").read_text(
            encoding="utf-8"
        )
    )
    gc = tmp_path / "gitconfig"
    gc.write_text(
        mise_cfg["dotfiles"]["~/.gitconfig/gh-doctor"]["block"]
        + "\n"
        + mise_cfg["dotfiles"]["~/.gitconfig/hk"]["block"],
        encoding="utf-8",
    )
    pre_commit_cmd = subprocess.check_output(
        ["git", "config", "--file", str(gc), "--get", "hook.hk-pre-commit.command"],
        text=True,
    ).strip()
    pre_push_cmd = subprocess.check_output(
        [
            "git",
            "config",
            "--file",
            str(gc),
            "--get",
            "hook.gh-doctor-pre-push.command",
        ],
        text=True,
    ).strip()
    assert pre_commit_cmd.endswith("hk run pre-commit --from-hook --staged")
    assert pre_push_cmd.endswith("doctor project'")


# Pure live-host checks implemented as mise tasks: wrapped by tasks/py/host_check.sh.
WRAPPED_HOST_DOCTOR_CHECKS = ("host-gemini-sync", "agy-learn-heal")
# Python checks that mix committed-file assertions with live-host probes: they skip
# only the live-host part in repo scope (doctor_checks._live_host_skipped).
SCOPE_AWARE_DOCTOR_CHECKS = (
    "host-option-a",
    "mise-doctor-clean",
    "host-hygiene",
    "herdr-app-bundle",
    "codex-hook-trust",
    "mise-binary-integrity",
    "agy-rc-health",
    "skills-6harness-parity",
)


def test_host_doctor_checks_skip_in_repo_scope_and_pre_push_sets_it() -> None:
    """Pushes must depend on the branch, not on live-host drift (owner decision 2026-11-25)."""
    import tomllib

    repo = Path(__file__).resolve().parents[1]
    checks = tomllib.loads((repo / "mise.toml").read_text(encoding="utf-8"))["doctor"][
        "checks"
    ]
    gate = "sh tasks/py/host_check.sh "
    for name in WRAPPED_HOST_DOCTOR_CHECKS:
        assert checks[name]["run"].startswith(gate), name
    others = set(checks) - set(WRAPPED_HOST_DOCTOR_CHECKS)
    assert not any("host_check.sh" in checks[n]["run"] for n in others)
    for name in SCOPE_AWARE_DOCTOR_CHECKS:
        assert checks[name]["run"].startswith(
            "python3 -I -B tasks/py/doctor_checks.py "
        ), name
    gc = repo / "unix/home/.config/mise/config.toml"
    cfg = tomllib.loads(gc.read_text(encoding="utf-8"))
    block = cfg["dotfiles"]["~/.gitconfig/gh-doctor"]["block"]
    pre_push = subprocess.run(
        ["git", "config", "--file", "-", "--get", "hook.gh-doctor-pre-push.command"],
        input=block,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "DOTFILES_DOCTOR_SCOPE=repo sh -c" in pre_push
    assert pre_push.rstrip().endswith("doctor project'")


def test_host_check_helper_rejects_missing_command() -> None:
    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        ["sh", str(repo / "tasks/py/host_check.sh")],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2


@pytest.mark.parametrize(
    ("scope", "rc", "out"),
    [("repo", 0, "SKIP: host check"), ("full", 7, ""), (None, 7, "")],
)
def test_host_check_helper_runs_or_skips(scope: str | None, rc: int, out: str) -> None:
    repo = Path(__file__).resolve().parents[1]
    env = {"PATH": "/usr/bin:/bin"}
    if scope is not None:
        env["DOTFILES_DOCTOR_SCOPE"] = scope
    proc = subprocess.run(
        ["sh", str(repo / "tasks/py/host_check.sh"), "sh", "-c", "exit 7"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == rc
    assert proc.stdout.startswith(out)
