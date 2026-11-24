"""Tests for smart CLI wrappers (`codex-wrapper`, `codex-hc`, `agy-wrapper`) and operational host/enter tasks."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = REPO_ROOT / "unix" / "home" / ".local" / "bin"
TASKS_DIR = REPO_ROOT / "tasks"
MISE_TASKS_DIR = REPO_ROOT / "unix" / "home" / ".config" / "mise" / "tasks"


def _make_stub(directory: Path, name: str, script_body: str) -> Path:
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{script_body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_codex_wrapper_strips_ephemeral_env_and_respects_herdr_mode(
    tmp_path: Path,
) -> None:
    bin_stub = tmp_path / "bin"
    bin_stub.mkdir()
    real_home = tmp_path / "home"
    (real_home / ".local" / "bin").mkdir(parents=True)
    (real_home / ".config" / "fnox").mkdir(parents=True)
    (real_home / ".config" / "fnox" / "config.toml").write_text("", encoding="utf-8")

    # Stub ~/.local/bin/codex and fnox recording argv + env keys cleanly
    _make_stub(
        real_home / ".local" / "bin",
        "codex",
        'python3 -c \'import json, os, sys; print(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}))\' "$@"',
    )
    _make_stub(
        bin_stub,
        "fnox",
        'while [ "$#" -gt 0 ]; do case "$1" in --) shift; break ;; *) shift ;; esac; done\nFNOX_WRAPPED=1 exec "$@"',
    )

    env = {
        "HOME": str(real_home),
        "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HTTP_PROXY": "http://127.0.0.1:9999",
        "HTTPS_PROXY": "http://127.0.0.1:9999",
        "ALL_PROXY": "http://127.0.0.1:9999",
        "SSL_CERT_FILE": "/tmp/fnox-proxy-ca-deleted.pem",
        "NODE_EXTRA_CA_CERTS": "/tmp/fnox-proxy-ca-deleted.pem",
        "REQUESTS_CA_BUNDLE": "/tmp/fnox-proxy-ca-deleted.pem",
        "CURL_CA_BUNDLE": "/tmp/fnox-proxy-ca-deleted.pem",
        "GIT_SSL_CAINFO": "/tmp/fnox-proxy-ca-deleted.pem",
        "CARGO_HTTP_CAINFO": "/tmp/fnox-proxy-ca-deleted.pem",
        "DENO_CERT": "/tmp/fnox-proxy-ca-deleted.pem",
        "RUST_LOG": "debug",
    }

    for sub in (
        ["app-server", "--listen", "stdio://"],
        ["remote-control"],
        ["update"],
        ["login"],
        ["doctor"],
        ["features", "list"],
        ["--version"],
        ["--help"],
        [],
    ):
        proc = subprocess.run(
            [str(BIN_DIR / "codex-wrapper"), *sub],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(proc.stdout)
        assert payload["argv"] == sub
        assert "--disable" not in payload["argv"]
        assert "-c" not in payload["argv"]
        assert "FNOX_WRAPPED" not in payload["env"]
        for stripped in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "SSL_CERT_FILE",
            "NODE_EXTRA_CA_CERTS",
            "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
            "GIT_SSL_CAINFO",
            "CARGO_HTTP_CAINFO",
            "DENO_CERT",
            "RUST_LOG",
        ):
            assert stripped not in payload["env"], stripped

    # Herdr isolation (CODEX_HERDR=1 / codex-hc) passes --disable daemon_auto_start without -c overrides
    hc_proc = subprocess.run(
        [str(BIN_DIR / "codex-wrapper"), "fix bug"],
        env={**env, "CODEX_HERDR": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    hc_payload = json.loads(hc_proc.stdout)
    assert hc_payload["argv"] == ["--disable", "daemon_auto_start", "fix bug"]
    assert hc_payload["env"].get("FNOX_WRAPPED") == "1"

    # Bash 3.2 set -u empty extra_flags + --remote-auth-token-env + -- + bypass subcommands + shim skip
    no_daemon = subprocess.run(
        [str(BIN_DIR / "codex-hc"), "--no-daemon", "fix bug"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    no_daemon_payload = json.loads(no_daemon.stdout)
    assert no_daemon_payload["argv"] == ["--no-daemon", "fix bug"]
    assert no_daemon_payload["env"].get("FNOX_WRAPPED") == "1"

    tok_bypass = subprocess.run(
        [
            str(BIN_DIR / "codex-wrapper"),
            "--remote-auth-token-env",
            "MY_TOK",
            "mcp",
            "list",
        ],
        env={**env, "CODEX_HERDR": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(tok_bypass.stdout)["env"].get("FNOX_WRAPPED") is None

    end_opts_bypass = subprocess.run(
        [str(BIN_DIR / "codex-wrapper"), "--", "exec-server", "--help"],
        env={**env, "CODEX_HERDR": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(end_opts_bypass.stdout)["env"].get("FNOX_WRAPPED") is None


def test_agy_wrapper_bypasses_fnox_for_management_subcommands(tmp_path: Path) -> None:
    bin_stub = tmp_path / "bin"
    bin_stub.mkdir()
    real_home = tmp_path / "home"
    (real_home / ".local" / "bin").mkdir(parents=True)
    (real_home / ".config" / "fnox").mkdir(parents=True)
    (real_home / ".config" / "fnox" / "config.toml").write_text("", encoding="utf-8")

    _make_stub(
        real_home / ".local" / "bin",
        "agy",
        'python3 -c \'import json, os, sys; print(json.dumps({"argv": sys.argv[1:], "fnox": os.environ.get("FNOX_WRAPPED"), "ssl": os.environ.get("SSL_CERT_FILE"), "rust_log": os.environ.get("RUST_LOG")}))\' "$@"',
    )
    _make_stub(
        bin_stub,
        "fnox",
        'while [ "$#" -gt 0 ]; do case "$1" in --) shift; break ;; *) shift ;; esac; done\nFNOX_WRAPPED=1 exec "$@"',
    )

    env = {
        "HOME": str(real_home),
        "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HTTPS_PROXY": "http://127.0.0.1:9999",
        "SSL_CERT_FILE": "/tmp/fnox-proxy-ca-deleted.pem",
        "RUST_LOG": "trace",
    }

    for sub in (
        ["remote-control", "status"],
        ["update"],
        ["changelog"],
        ["install"],
        ["models"],
        ["help"],
        ["--version"],
        ["--help"],
    ):
        proc = subprocess.run(
            [str(BIN_DIR / "agy-wrapper"), *sub],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(proc.stdout)
        assert data["argv"] == sub
        assert data["fnox"] is None
        assert data["ssl"] is None
        assert data["rust_log"] is None

    interactive = subprocess.run(
        [str(BIN_DIR / "agy-wrapper"), "chat"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    idata = json.loads(interactive.stdout)
    assert idata["argv"] == ["chat"]
    assert idata["fnox"] == "1"
    assert idata["rust_log"] is None

    # Boolean flags (--remote-agent, --sandbox) must NOT swallow bypass subcommand; root value flags (-v) DO
    bool_bypass = subprocess.run(
        [str(BIN_DIR / "agy-wrapper"), "--remote-agent", "--sandbox", "mic-serve"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(bool_bypass.stdout)["fnox"] is None

    val_interactive = subprocess.run(
        [str(BIN_DIR / "agy-wrapper"), "-v", "models", "chat"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(val_interactive.stdout)["fnox"] == "1"


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
    (fake_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").write_text(
        "tok\n", encoding="utf-8"
    )
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

        # Poisoned SSL plist triggers clean stop + start recreate (not kickstart)
        rc_log = tmp_path / "rc.log"
        _make_stub(
            bin_stub,
            "agy",
            f'printf "agy %s\\n" "$*" >> "{rc_log}"\n'
            'if [ "$1" = "remote-control" ] && [ "$2" = "status" ]; then echo "Daemon status: running"; exit 0; fi\n'
            'if [ "$1" = "remote-control" ] && [ "$2" = "start" ]; then\n'
            f'  cat <<EOF > "{plist}"\n<plist><dict><key>Label</key><string>com.google.antigravity-cli-daemon</string></dict></plist>\nEOF\n'
            "  exit 0\nfi\nexit 0\n",
        )
        plist.write_text(
            "<plist><dict><key>EnvironmentVariables</key><dict><key>SSL_CERT_FILE</key>"
            "<string>/tmp/fnox-proxy-ca-123.pem</string></dict></dict></plist>\n",
            encoding="utf-8",
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
        assert "agy remote-control stop" in rc_calls
        assert "agy remote-control start" in rc_calls

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


def test_codex_mcp_wrap_checks_and_preserves_comments_and_sections(
    tmp_path: Path,
) -> None:
    task = MISE_TASKS_DIR / "codex" / "mcp-wrap"
    codex_cfg = tmp_path / "codex_config.toml"
    fnox_cfg = tmp_path / "fnox.toml"
    fnox_cfg.write_text(
        '[[proxy.rules]]\ndomain = "api.exa.ai"\nsecret = "EXA_API_KEY"\n',
        encoding="utf-8",
    )
    # 1. inherit = "none" with HC_* still fails --check; inherit = "all" without HC_* also fails
    codex_cfg.write_text(
        '[shell_environment_policy]\ninherit = "none"\ninclude_only = ["PATH", "HC_*"]\n',
        encoding="utf-8",
    )
    chk_bad_inherit = subprocess.run(
        ["bash", str(task)],
        env={
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_check": "true",
            "usage_config": str(codex_cfg),
            "usage_fnox_config": str(fnox_cfg),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert chk_bad_inherit.returncode == 1
    assert "inherit" in chk_bad_inherit.stdout

    codex_cfg.write_text(
        '[shell_environment_policy]\ninherit = "all"\ninclude_only = ["PATH"]\n\n'
        "# trailing section comment must stay\n"
        '[mcp_servers."my.exa"]\n'
        'command = "npx"  # keep comment\n'
        'args = ["-y", "https://api.exa.ai/mcp"]\n\n'
        '[other]\nfoo = "bar"\n',
        encoding="utf-8",
    )
    chk_missing_hc = subprocess.run(
        ["bash", str(task)],
        env={
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_check": "true",
            "usage_config": str(codex_cfg),
            "usage_fnox_config": str(fnox_cfg),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert chk_missing_hc.returncode == 1
    assert "HC_*" in chk_missing_hc.stdout

    app = subprocess.run(
        ["bash", str(task)],
        env={
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "usage_apply": "true",
            "usage_config": str(codex_cfg),
            "usage_fnox_config": str(fnox_cfg),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert "updated" in app.stdout
    out_txt = codex_cfg.read_text(encoding="utf-8")
    assert "# trailing section comment must stay" in out_txt
    assert "# keep comment" in out_txt
    assert '[other]\nfoo = "bar"' in out_txt
    import tomllib

    parsed = tomllib.loads(out_txt)
    assert parsed["shell_environment_policy"]["inherit"] == "all"
    assert "HC_*" in parsed["shell_environment_policy"]["include_only"]
    assert "fnox" in parsed["mcp_servers"]["my.exa"]["command"]


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
