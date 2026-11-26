"""Offline unit tests for pc Hub Compose/Caddy/SearXNG, schema-first pc_hub, agentsview offload, and tasks/hub/*."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

from dotfiles_py.generated.pc_hub_schema import (
    AgentsViewOffloadOutcome,
    PcHubStatusResponse,
    PcHubValidationOutcome,
)
from pc_hub.cli import main as pc_hub_main
from pc_hub.config import (
    BESZEL_VERSION,
    CODEBASE_MEMORY_MCP_LINUX_AMD64_SHA256,
    QMD_VERSION,
    default_port_map,
    offload_agentsview_local,
    render_launchd_beszel_agent_plist,
    render_pc_hub_env_file,
    render_systemd_units,
    validate_pc_hub_repo,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HUB_TASKS_DIR = REPO_ROOT / "tasks" / "hub"
AV_WRAPPER = (
    REPO_ROOT
    / "unix"
    / "home"
    / ".config"
    / "mise"
    / "assets"
    / "agentsview"
    / "agentsview-wrapper.sh"
)
VB_WRAPPER = (
    REPO_ROOT
    / "unix"
    / "home"
    / ".config"
    / "mise"
    / "assets"
    / "board"
    / "vb-proxy-wrapper.sh"
)


def _seed_minimal_hub_repo(root: Path) -> None:
    (root / "docker" / "pc-hub" / "searxng").mkdir(parents=True)
    (root / "docker" / "mac-agent").mkdir(parents=True)
    (root / "unix" / "home" / ".config" / "mise").mkdir(parents=True)
    for rel in (
        "docker/pc-hub/compose.yaml",
        "docker/pc-hub/Caddyfile",
        "docker/pc-hub/searxng/settings.yml",
        "docker/mac-agent/compose.yaml",
        "unix/home/.config/mise/config.toml",
    ):
        shutil.copy2(REPO_ROOT / rel, root / rel)


def test_validate_pc_hub_repo_passes_on_real_repo() -> None:
    outcome = validate_pc_hub_repo(REPO_ROOT)
    assert outcome.ok is True, outcome.violations
    assert outcome.violations == []
    assert len(outcome.checked_files) == 4
    ports = default_port_map()
    assert ports.beszel == 8090
    assert ports.caddy == 8880
    assert ports.agentsview_hub == 8082
    assert ports.vercel_board == 4100


def test_validate_pc_hub_repo_rejects_security_and_b1_violations(
    tmp_path: Path,
) -> None:
    # 1. Direct docker.sock mount on beszel-agent + missing POST=0 + OTel image + occupied port
    r1 = tmp_path / "r1"
    _seed_minimal_hub_repo(r1)
    compose = r1 / "docker" / "pc-hub" / "compose.yaml"
    compose.write_text(
        compose.read_text(encoding="utf-8")
        .replace("POST: '0'", "POST: '1'")
        .replace(
            "      - ${PC_HUB_STATE_DIR:-./state}/beszel_agent_data:/var/lib/beszel-agent",
            "      - ${PC_HUB_STATE_DIR:-./state}/beszel_agent_data:/var/lib/beszel-agent\n"
            "      - /var/run/docker.sock:/var/run/docker.sock:ro",
        )
        + "\n  alloy:\n    image: grafana/alloy:latest\n",
        encoding="utf-8",
    )
    out1 = validate_pc_hub_repo(r1)
    assert out1.ok is False
    joined1 = "\n".join(out1.violations)
    assert "POST=0" in joined1
    assert "docker-socket-proxy instead of /var/run/docker.sock" in joined1
    assert "grafana/alloy" in joined1
    assert "digest pinned" in joined1

    # 2. codebase-memory-mcp exposed in Caddyfile + missing agent-connect exclusion
    r2 = tmp_path / "r2"
    _seed_minimal_hub_repo(r2)
    caddy = r2 / "docker" / "pc-hub" / "Caddyfile"
    caddy.write_text(
        caddy.read_text(encoding="utf-8").replace(
            "not path /api/beszel/agent-connect*", "not path /api/beszel/other*"
        )
        + "\nhandle /codebase-memory* {\n  reverse_proxy 127.0.0.1:9999\n}\n",
        encoding="utf-8",
    )
    out2 = validate_pc_hub_repo(r2)
    assert out2.ok is False
    joined2 = "\n".join(out2.violations)
    assert "codebase-memory-mcp must have ZERO Caddy" in joined2
    assert "not path /api/beszel/agent-connect*" in joined2

    # 3. Occupied port 8080 or 0.0.0.0 bind + non-registry beszel/macmon in [tools]
    r3 = tmp_path / "r3"
    _seed_minimal_hub_repo(r3)
    mac_cmp = r3 / "docker" / "mac-agent" / "compose.yaml"
    mac_cmp.write_text(
        mac_cmp.read_text(encoding="utf-8").replace(
            "127.0.0.1:2375:2375", "0.0.0.0:8080:2375"
        ),
        encoding="utf-8",
    )
    cfg = r3 / "unix" / "home" / ".config" / "mise" / "config.toml"
    cfg.write_text('[tools]\nbeszel = "0.21.2"\nmacmon = "0.12.0"\n', encoding="utf-8")
    out3 = validate_pc_hub_repo(r3)
    assert out3.ok is False
    joined3 = "\n".join(out3.violations)
    assert "127.0.0.1" in joined3
    assert "8080" in joined3
    assert "beszel, macmon" in joined3


def test_render_env_systemd_and_launchd_plists() -> None:
    env_txt = render_pc_hub_env_file(
        ts_host="hub-node.tailnet.ts.net",
        state_dir="/home/dev/.local/state/pc-hub",
        beszel_public_key="ssh-ed25519 AAAA",
        beszel_token="tok-123",
        searxng_secret="sec-456",
        beszel_user_email="owner@example.com",
    )
    assert "PC_HUB_TS_HOST=hub-node.tailnet.ts.net" in env_txt
    assert "BESZEL_AGENT_HUB_URL=http://127.0.0.1:8090" in env_txt
    assert "BESZEL_DISABLE_PASSWORD_AUTH=true" in env_txt

    units = render_systemd_units(
        home_dir="/home/dev",
        ts_host="hub-node.tailnet.ts.net",
    )
    assert set(units) == {
        "dev.mise.vercel-board.service",
        "dev.mise.agentsview-hub.service",
        "dev.mise.qmd-hub.service",
    }
    assert "BOARD_PORT=4100" in units["dev.mise.vercel-board.service"]
    for unit_name, unit_text in units.items():
        assert "Environment=PATH=/home/dev/.local/share/mise/shims:" in unit_text, (
            unit_name
        )
    assert (
        "--port 8082 --base-path /agentsview --public-url https://hub-node.tailnet.ts.net --require-auth"
        in units["dev.mise.agentsview-hub.service"]
    )
    assert (
        "--public-url https://hub-node.tailnet.ts.net/agentsview"
        not in units["dev.mise.agentsview-hub.service"]
    )
    assert f"v{QMD_VERSION}" in units["dev.mise.qmd-hub.service"]
    assert (
        "mcp --http --host 127.0.0.1 --port 8181" in units["dev.mise.qmd-hub.service"]
    )

    mac_plist = plistlib.loads(
        render_launchd_beszel_agent_plist(
            agent_bin="/home/alice/.local/bin/beszel-agent",
            hub_url="https://hub-node.tailnet.ts.net/beszel",
            public_key="ssh-ed25519 AAAA",
            token_file="/home/alice/.config/beszel/agent-token",
            log_dir="/home/alice/Library/Logs",
            mac_host_mode=True,
        )
    )
    assert mac_plist["EnvironmentVariables"]["GPU_COLLECTOR"] == "macmon"
    assert mac_plist["EnvironmentVariables"]["DOCKER_HOST"] == "tcp://127.0.0.1:2375"
    assert "ALL_PROXY" not in mac_plist["EnvironmentVariables"]

    vm_plist = plistlib.loads(
        render_launchd_beszel_agent_plist(
            agent_bin="/home/admin/.local/bin/beszel-agent",
            hub_url="https://hub-node.tailnet.ts.net/beszel",
            public_key="ssh-ed25519 AAAA",
            token_file="/home/admin/.config/beszel/agent-token",
            log_dir="/home/admin/Library/Logs",
            mac_host_mode=False,
            vm_socks_proxy="socks5://127.0.0.1:1055",
        )
    )
    assert "GPU_COLLECTOR" not in vm_plist["EnvironmentVariables"]
    assert vm_plist["EnvironmentVariables"]["ALL_PROXY"] == "socks5://127.0.0.1:1055"


def test_offload_agentsview_local_dry_run_and_apply(
    tmp_path: Path, monkeypatch
) -> None:
    fake_home = tmp_path / "home"
    la_dir = fake_home / "Library" / "LaunchAgents"
    la_dir.mkdir(parents=True)
    plist = la_dir / "dev.mise.com.raymanaloto.agentsview-native.plist"
    plist.write_text("<plist><dict/></plist>\n", encoding="utf-8")

    skills_task = tmp_path / "bootstrap-skills"
    skills_log = tmp_path / "skills.log"
    skills_task.write_text(
        f'#!/usr/bin/env bash\nprintf "%s|%s\\n" "$AGENTSVIEW_SKILLS_SERVER" "$AGENTSVIEW_SKILLS_SERVER_TOKEN_FILE" > "{skills_log}"\n',
        encoding="utf-8",
    )
    skills_task.chmod(0o755)

    dry = offload_agentsview_local(
        home_dir=fake_home,
        server_url="https://hub-node.tailnet.ts.net/agentsview",
        token_value="av-secret-tok",
        apply=False,
        bootstrap_skills_task=skills_task,
    )
    assert dry.ok is True
    assert dry.dry_run is True
    assert dry.launchagent_disabled is False
    assert plist.is_file()

    monkeypatch.setattr("pc_hub.config.shutil.which", lambda _cmd: None)
    app = offload_agentsview_local(
        home_dir=fake_home,
        server_url="https://hub-node.tailnet.ts.net/agentsview",
        token_value="av-secret-tok",
        apply=True,
        bootstrap_skills_task=skills_task,
    )
    assert app.ok is True
    assert app.dry_run is False
    assert app.launchagent_disabled is True
    assert app.skills_refreshed is True
    assert not plist.exists()
    assert (
        fake_home
        / ".local"
        / "state"
        / "dotfiles-v2"
        / "backups"
        / "dev.mise.com.raymanaloto.agentsview-native.plist.bak"
    ).is_file()
    tok_file = fake_home / ".config" / "agentsview" / "server-token"
    env_file = fake_home / ".config" / "agentsview" / "env.sh"
    assert tok_file.read_text(encoding="utf-8").strip() == "av-secret-tok"
    assert (tok_file.stat().st_mode & 0o777) == 0o600
    assert "AGENTSVIEW_NO_DAEMON=1" in env_file.read_text(encoding="utf-8")
    assert (
        skills_log.read_text(encoding="utf-8").strip()
        == f"https://hub-node.tailnet.ts.net/agentsview|{tok_file}"
    )


def test_pc_hub_cli_validate_and_build_status(capsys) -> None:
    rc = pc_hub_main(["validate", "--repo", str(REPO_ROOT), "--json"])
    assert rc == 0
    val_out = PcHubValidationOutcome.model_validate_json(capsys.readouterr().out)
    assert val_out.ok is True

    rows = json.dumps(
        [
            {
                "service": "beszel",
                "state": "OK",
                "bind_target": "127.0.0.1:8090",
                "exposed_http": True,
                "detail": f"v{BESZEL_VERSION}",
            },
            {
                "service": "codebase-memory-mcp",
                "state": "OK",
                "bind_target": "cli-only",
                "exposed_http": False,
                "detail": "cli-only",
            },
        ]
    )
    rc2 = pc_hub_main(
        [
            "build-status",
            "--command",
            "hub:status",
            "--ssh-host",
            "pc",
            "--tailscale-host",
            "hub-node.tailnet.ts.net",
            "--repo",
            str(REPO_ROOT),
            "--rows-json",
            rows,
        ]
    )
    assert rc2 == 0
    status_out = PcHubStatusResponse.model_validate_json(capsys.readouterr().out)
    assert status_out.rc == 0
    assert status_out.ports.beszel == 8090
    assert len(status_out.services) == 2

    rc3 = pc_hub_main(
        [
            "agentsview-offload",
            "--server-url",
            "https://hub-node.tailnet.ts.net/agentsview",
            "--json",
        ]
    )
    assert rc3 == 0
    off_out = AgentsViewOffloadOutcome.model_validate_json(capsys.readouterr().out)
    assert off_out.dry_run is True


def test_agentsview_wrapper_routes_when_configured_and_passes_through_otherwise(
    tmp_path: Path,
) -> None:
    fake_home = tmp_path / "home"
    data_dir = fake_home / ".local" / "share" / "mise"
    install_dir = data_dir / "installs" / "github-kenn-io-agentsview" / "0.48.0"
    install_dir.mkdir(parents=True)
    bin_stub = tmp_path / "bin"
    bin_stub.mkdir()

    fake_av = install_dir / "agentsview"
    fake_av.write_text(
        '#!/usr/bin/env bash\nprintf "NO_DAEMON=%s|%s\\n" "${AGENTSVIEW_NO_DAEMON:-}" "$*"\n',
        encoding="utf-8",
    )
    fake_av.chmod(0o755)
    (bin_stub / "mise").write_text(
        f'#!/usr/bin/env bash\nif [ "$1" = "which" ]; then echo "{fake_av}"; exit 0; fi\nexit 1\n',
        encoding="utf-8",
    )
    (bin_stub / "mise").chmod(0o755)

    base_env = {
        "HOME": str(fake_home),
        "MISE_DATA_DIR": str(data_dir),
        "PATH": f"{bin_stub}:/usr/bin:/bin:/usr/sbin:/sbin",
    }

    # Without ~/.config/agentsview/env.sh -> passes arguments through untouched.
    plain = subprocess.run(
        ["bash", str(AV_WRAPPER), "session", "list", "--limit", "1"],
        env=base_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert plain.stdout.strip() == "NO_DAEMON=|session list --limit 1"

    # With ~/.config/agentsview/env.sh + server-token -> injects --server & --server-token-file.
    cfg_dir = fake_home / ".config" / "agentsview"
    cfg_dir.mkdir(parents=True)
    tok_file = cfg_dir / "server-token"
    tok_file.write_text("tok\n", encoding="utf-8")
    (cfg_dir / "env.sh").write_text(
        f'export AGENTSVIEW_SERVER="https://hub.example.ts.net/agentsview"\n'
        f'export AGENTSVIEW_SERVER_TOKEN_FILE="{tok_file}"\n'
        f"export AGENTSVIEW_NO_DAEMON=1\n",
        encoding="utf-8",
    )

    routed = subprocess.run(
        ["bash", str(AV_WRAPPER), "session", "list", "--limit", "1", "--json"],
        env=base_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert routed.stdout.strip() == (
        f"NO_DAEMON=1|session --server https://hub.example.ts.net/agentsview "
        f"--server-token-file {tok_file} list --limit 1 --json"
    )

    # Explicit --server or --help is never duplicated/overridden.
    explicit = subprocess.run(
        [
            "bash",
            str(AV_WRAPPER),
            "session",
            "list",
            "--server",
            "http://127.0.0.1:9999",
        ],
        env=base_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert (
        explicit.stdout.strip()
        == "NO_DAEMON=1|session list --server http://127.0.0.1:9999"
    )


def _is_tailscale_proxy_active() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 1055), timeout=0.2):
            return True
    except OSError:
        return False


def test_proxy_wrappers_clear_fnox_proxy_on_native_tailscale(
    tmp_path: Path,
) -> None:
    fake_home = tmp_path / "home"
    data_dir = fake_home / ".local" / "share" / "mise"
    av_install_dir = data_dir / "installs" / "github-kenn-io-agentsview" / "0.48.0"
    av_install_dir.mkdir(parents=True)
    vb_bin_dir = (
        data_dir / "installs" / "npm-vercel-board" / "latest" / "node_modules" / ".bin"
    )
    vb_bin_dir.mkdir(parents=True)

    dump_script = (
        "#!/usr/bin/env bash\n"
        'printf "HTTPS_PROXY=%s|HTTP_PROXY=%s|https_proxy=%s|http_proxy=%s|ALL_PROXY=%s|all_proxy=%s\\n" '
        '"${HTTPS_PROXY:-}" "${HTTP_PROXY:-}" "${https_proxy:-}" "${http_proxy:-}" '
        '"${ALL_PROXY:-}" "${all_proxy:-}"\n'
    )
    fake_av = av_install_dir / "agentsview"
    fake_av.write_text(dump_script, encoding="utf-8")
    fake_av.chmod(0o755)
    fake_vb = vb_bin_dir / "vb"
    fake_vb.write_text(dump_script, encoding="utf-8")
    fake_vb.chmod(0o755)

    vb_entry = tmp_path / "vb"
    vb_entry.symlink_to(VB_WRAPPER)

    expected_http = "http://127.0.0.1:1055" if _is_tailscale_proxy_active() else ""
    expected_dump = (
        f"HTTPS_PROXY={expected_http}|HTTP_PROXY={expected_http}|"
        f"https_proxy={expected_http}|http_proxy={expected_http}|"
        "ALL_PROXY=|all_proxy="
    )

    fnox_http_proxy = f"http://{'fnox'}:{'tok'}@127.0.0.1:64729"
    fnox_socks_proxy = f"socks5://{'fnox'}:{'tok'}@127.0.0.1:64729"
    fnox_proxy_vars = {
        "HTTPS_PROXY": fnox_http_proxy,
        "HTTP_PROXY": fnox_http_proxy,
        "https_proxy": fnox_http_proxy,
        "http_proxy": fnox_http_proxy,
        "ALL_PROXY": fnox_socks_proxy,
        "all_proxy": fnox_socks_proxy,
    }
    base_env = {
        "HOME": str(fake_home),
        "MISE_DATA_DIR": str(data_dir),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        **fnox_proxy_vars,
    }

    av_env_res = subprocess.run(
        ["bash", str(AV_WRAPPER), "session", "list"],
        env={**base_env, "AGENTSVIEW_SERVER": "https://hub.tailnet.ts.net/agentsview"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert av_env_res.stdout.strip() == expected_dump

    av_arg_res = subprocess.run(
        [
            "bash",
            str(AV_WRAPPER),
            "session",
            "list",
            "--server",
            "https://hub.tailnet.ts.net/agentsview",
        ],
        env=base_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert av_arg_res.stdout.strip() == expected_dump

    vb_env_res = subprocess.run(
        [str(vb_entry), "board", "read"],
        env={**base_env, "BOARD_URL": "https://hub.tailnet.ts.net/board"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert vb_env_res.stdout.strip() == expected_dump

    vb_arg_res = subprocess.run(
        [str(vb_entry), "--url=https://hub.tailnet.ts.net/board"],
        env=base_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert vb_arg_res.stdout.strip() == expected_dump


def test_hub_tasks_dry_run_and_container_refusal() -> None:
    env_clean = {
        k: v
        for k, v in os.environ.items()
        if k not in {"container", "usage_dry_run", "usage_offline", "usage_json"}
    }
    env_clean["PATH"] = f"{Path(sys.executable).parent}:/usr/bin:/bin:/usr/sbin:/sbin"

    for task_name in ("up", "down", "agent-up", "sync-clients"):
        res = subprocess.run(
            ["bash", str(HUB_TASKS_DIR / task_name)],
            env={**env_clean, "usage_dry_run": "true"},
            capture_output=True,
            text=True,
            check=True,
        )
        assert "DRY-RUN" in res.stdout

    doc = subprocess.run(
        ["bash", str(HUB_TASKS_DIR / "doctor")],
        env={**env_clean, "usage_offline": "true", "usage_json": "true"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert PcHubValidationOutcome.model_validate_json(doc.stdout).ok is True

    # beszel-agent MUST refuse inside dev containers (0 MB policy).
    refused = subprocess.run(
        ["bash", str(HUB_TASKS_DIR / "agent-up")],
        env={**env_clean, "container": "docker"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode == 1
    assert "0 MB in dev containers" in refused.stderr


def test_hub_up_script_invariants() -> None:
    up_text = (HUB_TASKS_DIR / "up").read_text(encoding="utf-8")

    # All three macOS -> pc tar streams must disable AppleDouble metadata and clean stale ._*.
    assert up_text.count("COPYFILE_DISABLE=1 tar ") == 3
    assert "rm -f '${stack_dir}'/._* '${stack_dir}'/searxng/._*" in up_text
    assert "rm -f '${remote_home}/.config/systemd/user'/._dev.mise.*.service" in up_text

    # ssh-keygen writes ${key_priv}.pub, so migrate legacy .key.pub and rename after keygen.
    assert 'if [[ ! -s ${key_pub} && -s "${key_priv}.pub" ]]; then' in up_text
    assert 'mv -f "${key_priv}.pub" "${key_pub}"' in up_text
    assert 'rm -f "${key_priv}" "${key_priv}.pub" "${key_pub}"' in up_text

    # pc host lacks ~/.config/mise/config.toml so agentsview is symlinked via mise where and vb is installed via npm --prefix.
    assert (
        'mise -C "${REMOTE_HOME}" where "github:kenn-io/agentsview@0.48.0"' in up_text
    )
    assert (
        'ln -sfn "${av_dir}/agentsview" "${REMOTE_HOME}/.local/bin/agentsview"'
        in up_text
    )
    assert (
        CODEBASE_MEMORY_MCP_LINUX_AMD64_SHA256
        == "8c3d0341064301e78ecc84cf7348d4e2c6562a0f57448243ea560aaa34de36c1"
    )
    assert "codebase-memory-mcp-linux-amd64-portable.tar.gz" in up_text
    assert CODEBASE_MEMORY_MCP_LINUX_AMD64_SHA256 in up_text
    assert 'grep -Eq "not a dynamic|statically linked"' in up_text
    assert (
        'npm install --no-audit --no-fund --prefix "${board_prefix}" "@vercel/board@0.56.1"'
        in up_text
    )
    assert 'ln -sfn "${vb_bin}" "${REMOTE_HOME}/.local/bin/vb"' in up_text
    assert 'mise -C "${REMOTE_HOME}" where "npm:@vercel/board@0.56.1"' not in up_text
    assert (
        'if [[ -x "$HOME/.local/bin/agentsview" ]]; then printf "%s/.local/bin/agentsview" "$HOME";'
        in up_text
    )


def test_hub_status_and_agent_up_invariants() -> None:
    status_text = (HUB_TASKS_DIR / "status").read_text(encoding="utf-8")
    assert 'check_http("http://127.0.0.1:8888/healthz", ok_codes=(200,))' in status_text
    assert 'check_http("http://127.0.0.1:8888/"' not in status_text

    agent_up_text = (HUB_TASKS_DIR / "agent-up").read_text(encoding="utf-8")
    assert "trap 'rm -rf \"${tmp_dir:-}\"; trap - RETURN' RETURN" in agent_up_text
    assert "trap 'rm -f \"${plist_tmp:-}\"; trap - RETURN' RETURN" in agent_up_text
    assert 'rm -rf "${tmp_dir}"' in agent_up_text
    assert 'rm -f "${plist_tmp}"' in agent_up_text
    assert agent_up_text.count("trap - RETURN") >= 4


def _hub_up_agentsview_config_script() -> str:
    text = (REPO_ROOT / "tasks" / "hub" / "up").read_text(encoding="utf-8")
    start = text.index('python3 - "${REMOTE_HOME}/.agentsview/config.toml"')
    body = text[text.index("\n", start) + 1 :]
    return body[: body.index("\nPY\n")]


def test_hub_up_agentsview_config_rewrite_is_idempotent(tmp_path: Path) -> None:
    script = tmp_path / "rewrite.py"
    script.write_text(_hub_up_agentsview_config_script(), encoding="utf-8")
    cfg = tmp_path / "config.toml"
    # Simulates a config corrupted by the pre-fix rewrite: the Mac [[remote_hosts]]
    # entry lost its `host` key, and a top-level `host` bind must still be dropped.
    cfg.write_text(
        'host = "0.0.0.0"\nport = 8080\nauth_token = "old"\n\n'
        '[[remote_hosts]]\ntransport = "ssh"\ndestination = "me@mac"\ninterval = "5m"\n\n'
        '[[remote_hosts]]\nhost = "other"\ndestination = "x@y"\n',
        encoding="utf-8",
    )
    outputs = []
    for _ in range(3):
        subprocess.run(
            [sys.executable, "-I", "-B", str(script), str(cfg), 'tok"1', "me@mac"],
            check=True,
        )
        outputs.append(cfg.read_text(encoding="utf-8"))
    assert outputs[0] == outputs[1] == outputs[2]
    data = tomllib.loads(outputs[-1])
    assert data["auth_token"] == 'tok"1'
    assert data["require_auth"] is True
    assert "host" not in data
    assert data["port"] == 8080
    hosts = data["remote_hosts"]
    # The Mac entry is repaired in place (keeping its position and interval).
    assert [h.get("host") for h in hosts] == ["mac", "other"]
    assert hosts[0]["destination"] == "me@mac"
    assert all("host" in h for h in hosts)
    assert cfg.stat().st_mode & 0o777 == 0o600

    # Headers with trailing comments and single-quoted keys are still recognised.
    cfg.write_text(
        "port = 8080\n\n[[remote_hosts]] # mac\nhost = 'mac'\ndestination = 'old@mac'\n",
        encoding="utf-8",
    )
    subprocess.run(
        [sys.executable, "-I", "-B", str(script), str(cfg), "tok", "me@mac"],
        check=True,
    )
    hosts = tomllib.loads(cfg.read_text(encoding="utf-8"))["remote_hosts"]
    assert [(h["host"], h["destination"]) for h in hosts] == [("mac", "me@mac")]


def test_hub_up_agentsview_config_rewrite_preserves_owner_content(
    tmp_path: Path,
) -> None:
    script = tmp_path / "rewrite.py"
    script.write_text(_hub_up_agentsview_config_script(), encoding="utf-8")
    cfg = tmp_path / "config.toml"

    def run(text: str) -> subprocess.CompletedProcess[str]:
        cfg.write_text(text, encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-I", "-B", str(script), str(cfg), "tok", "me@mac"],
            capture_output=True,
            text=True,
            check=False,
        )

    # A bracketed line inside a multi-line string or array is not a table header.
    assert run('motd = """\n[fake]\nx\n"""\ngrid = [\n [1],\n [2]\n]\n').returncode == 0
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["motd"] == "[fake]\nx\n"
    assert data["grid"] == [[1], [2]]

    # A customised Mac entry keeps its own host name and extra keys.
    assert (
        run(
            '[[remote_hosts]]\nhost = "box"\ndestination = "me@mac"\n'
            'interval = "1m"\nuser_note = "keep"\n'
        ).returncode
        == 0
    )
    (host,) = tomllib.loads(cfg.read_text(encoding="utf-8"))["remote_hosts"]
    assert host == {
        "host": "box",
        "destination": "me@mac",
        "interval": "1m",
        "user_note": "keep",
        "transport": "ssh",
    }

    # Shapes the rewrite cannot handle fail closed and leave the file untouched.
    for bad in ('remote_hosts = [{host = "x"}]\n', "auth_token = \n"):
        result = run(bad)
        assert result.returncode != 0
        assert "left unchanged" in result.stderr
        assert cfg.read_text(encoding="utf-8") == bad
