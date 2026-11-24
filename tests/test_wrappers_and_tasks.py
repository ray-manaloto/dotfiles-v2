"""Tests for operational host, secret, Remote Control, and env:enter tasks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

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
        assert proc.returncode == 0, proc.stdout + proc.stderr
        parsed = json.loads(proc.stdout)
        assert parsed["token_ok"] is True
        assert parsed["rc_running"] is True
        assert parsed["issues"] == []

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
