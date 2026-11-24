"""Pure validation, rendering, and agentsview offload helpers for the pc Hub stack."""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

from dotfiles_py.generated.pc_hub_schema import (
    AgentsViewOffloadOutcome,
    PcHubErrorCode,
    PcHubErrorEntry,
    PcHubPortMap,
    PcHubReturnCode,
    PcHubReturnEntry,
    PcHubValidationOutcome,
)
from dotfiles_py.logging import get_logger
from dotfiles_py.time_ns import now_ns_pair

LOGGER = get_logger("pc_hub")

BESZEL_VERSION = "0.21.2"
QMD_VERSION = "2.9.0"
CODEBASE_MEMORY_MCP_VERSION = "0.12.1"
CODEBASE_MEMORY_MCP_LINUX_AMD64_SHA256 = (
    "8453015145f072c1b9b019cbdd02cea8cc6f18cc5e116036dd20992631c638c7"
)

OCCUPIED_PC_PORTS = frozenset({3142, 8080, 8081, 44589})

DEFAULT_PORTS: dict[str, int] = {
    "caddy": 8880,
    "beszel": 8090,
    "beszel_agent": 45876,
    "docker_socket_proxy": 2375,
    "searxng": 8888,
    "vercel_board": 4100,
    "agentsview_hub": 8082,
    "qmd": 8181,
}

FORBIDDEN_OTEL_IMAGES = (
    "grafana/alloy",
    "otel/opentelemetry-collector",
    "otelcol",
    "grafana/grafana",
    "victoriametrics",
    "grafana/tempo",
    "grafana/loki",
)

COMPOSE_SCHEMA_HEADER = "# yaml-language-server: $schema=https://raw.githubusercontent.com/compose-spec/compose-spec/master/schema/compose-spec.json"
PINNED_IMAGE_RE = re.compile(
    r"^\s*image:\s*([A-Za-z0-9._/-]+:[^\s@]+@sha256:[0-9a-f]{64})\s*$"
)
RAW_IMAGE_RE = re.compile(r"^\s*image:\s*(.+?)\s*$")
HOST_PORT_RE = re.compile(r'^\s*-\s*"?([^":\s]+):(\d+):(\d+)"?\s*$')


def default_port_map() -> PcHubPortMap:
    """Return the validated default loopback port map avoiding occupied pc ports."""
    return PcHubPortMap(**DEFAULT_PORTS)


def _check_compose_file(
    compose_path: Path,
    repo_root: Path,
    *,
    require_beszel_hub: bool,
) -> list[str]:
    rel = compose_path.relative_to(repo_root).as_posix()
    if not compose_path.is_file():
        return [f"{rel}: missing compose file"]
    text = compose_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    violations: list[str] = []

    if not lines or lines[0].strip() != COMPOSE_SCHEMA_HEADER:
        violations.append(f"{rel}: missing compose-spec $schema header on line 1")

    lowered = text.lower()
    for forbidden in FORBIDDEN_OTEL_IMAGES:
        if forbidden in lowered:
            violations.append(
                f"{rel}: forbidden Tier-0 B1 OTel/Grafana component `{forbidden}` found"
            )
    if "codebase-memory-mcp" in lowered:
        violations.append(
            f"{rel}: codebase-memory-mcp must never run in Compose/HTTP exposure"
        )

    for lineno, line in enumerate(lines, 1):
        if RAW_IMAGE_RE.match(line) and not PINNED_IMAGE_RE.match(line):
            violations.append(
                f"{rel}:{lineno}: image must be tag+sha256 digest pinned ({line.strip()})"
            )
        port_match = HOST_PORT_RE.match(line)
        if port_match:
            bind_ip, host_port_str, _ = port_match.groups()
            host_port = int(host_port_str)
            if bind_ip != "127.0.0.1":
                violations.append(
                    f"{rel}:{lineno}: host port binding must stay on 127.0.0.1 (got {bind_ip!r})"
                )
            if host_port in OCCUPIED_PC_PORTS:
                violations.append(
                    f"{rel}:{lineno}: port {host_port} collides with occupied pc port"
                )

    if not re.search(r"^\s*POST:\s*['\"]?0['\"]?\s*$", text, re.MULTILINE):
        violations.append(f"{rel}: docker-socket-proxy must enforce POST=0")
    if not re.search(r"^\s*CONTAINERS:\s*['\"]?1['\"]?\s*$", text, re.MULTILINE):
        violations.append(f"{rel}: docker-socket-proxy must enable CONTAINERS=1")

    if require_beszel_hub:
        for required_snippet in (
            "henrygd/beszel:0.21.2@sha256:",
            "henrygd/beszel-agent:0.21.2@sha256:",
            "127.0.0.1:8090",
            "TRUSTED_AUTH_HEADER: Tailscale-User-Login",
            "TRUSTED_PROXY_IPS: 127.0.0.1",
            "DOCKER_HOST:",
            "tcp://127.0.0.1:2375",
            "searxng/searxng:",
            "caddy:2.11.9-alpine@sha256:",
        ):
            if required_snippet not in text:
                violations.append(
                    f"{rel}: missing required pc-hub stanza `{required_snippet}`"
                )
        # Ensure beszel-agent never mounts /var/run/docker.sock directly.
        agent_block = _extract_yaml_service_block(lines, "beszel-agent")
        if "/var/run/docker.sock" in agent_block:
            violations.append(
                f"{rel}: beszel-agent must use docker-socket-proxy instead of /var/run/docker.sock"
            )

    return violations


def _extract_yaml_service_block(lines: list[str], service_name: str) -> str:
    in_services = False
    in_target = False
    collected: list[str] = []
    for line in lines:
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services and re.match(r"^[A-Za-z0-9_-]+:\s*$", line):
            break
        if in_services and re.match(rf"^  {re.escape(service_name)}:\s*$", line):
            in_target = True
            continue
        if in_target:
            if re.match(r"^  [A-Za-z0-9_-]+:\s*$", line):
                break
            collected.append(line)
    return "\n".join(collected)


def _check_caddyfile(caddyfile_path: Path, repo_root: Path) -> list[str]:
    rel = caddyfile_path.relative_to(repo_root).as_posix()
    if not caddyfile_path.is_file():
        return [f"{rel}: missing Caddyfile"]
    text = caddyfile_path.read_text(encoding="utf-8")
    active_lines = [
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ]
    active_text = "\n".join(active_lines)
    violations: list[str] = []
    lowered = active_text.lower()
    if "codebase-memory-mcp" in lowered or "codebase-memory" in lowered:
        violations.append(
            f"{rel}: codebase-memory-mcp must have ZERO Caddy / HTTP exposure"
        )
    for required_token in (
        "bind 127.0.0.1",
        "handle_path /beszel*",
        "/api/beszel/agent-connect*",
        "not path /api/beszel/agent-connect*",
        "not header Tailscale-User-Login *@*",
        "header_up -Tailscale-User-Login",
        "read_timeout 360s",
        "127.0.0.1:8090",
        "127.0.0.1:8082",
        "127.0.0.1:8888",
        "127.0.0.1:8181",
        "127.0.0.1:4100",
    ):
        if required_token not in text:
            violations.append(
                f"{rel}: missing required Caddy directive `{required_token}`"
            )
    return violations


def _check_searxng_settings(settings_path: Path, repo_root: Path) -> list[str]:
    rel = settings_path.relative_to(repo_root).as_posix()
    if not settings_path.is_file():
        return [f"{rel}: missing SearXNG settings.yml"]
    text = settings_path.read_text(encoding="utf-8")
    violations: list[str] = []
    for required_token in (
        "use_default_settings: true",
        "- json",
        "valkey://valkey:6379/0",
    ):
        if required_token not in text:
            violations.append(f"{rel}: missing `{required_token}`")
    return violations


def validate_pc_hub_repo(repo_root: Path) -> PcHubValidationOutcome:
    """Validate committed pc-hub and mac-agent Compose/Caddy/SearXNG files offline."""
    pc_compose = repo_root / "docker" / "pc-hub" / "compose.yaml"
    caddyfile = repo_root / "docker" / "pc-hub" / "Caddyfile"
    searxng_cfg = repo_root / "docker" / "pc-hub" / "searxng" / "settings.yml"
    mac_compose = repo_root / "docker" / "mac-agent" / "compose.yaml"
    checked = [
        p.relative_to(repo_root).as_posix()
        for p in (pc_compose, caddyfile, searxng_cfg, mac_compose)
    ]

    violations: list[str] = []
    violations.extend(
        _check_compose_file(pc_compose, repo_root, require_beszel_hub=True)
    )
    violations.extend(_check_caddyfile(caddyfile, repo_root))
    violations.extend(_check_searxng_settings(searxng_cfg, repo_root))
    violations.extend(
        _check_compose_file(mac_compose, repo_root, require_beszel_hub=False)
    )

    # Ensure global [tools] never adds non-registry beszel, macmon, or OTel collectors.
    global_cfg = repo_root / "unix" / "home" / ".config" / "mise" / "config.toml"
    if global_cfg.is_file():
        cfg_data = tomllib.loads(global_cfg.read_text(encoding="utf-8"))
        tools_tbl = cfg_data.get("tools") if isinstance(cfg_data, dict) else None
        if isinstance(tools_tbl, dict):
            forbidden_tools = {
                "beszel",
                "beszel-agent",
                "macmon",
                "alloy",
                "otelcol",
                "grafana",
            }
            found = sorted(set(str(k) for k in tools_tbl) & forbidden_tools)
            if found:
                violations.append(
                    f"unix/home/.config/mise/config.toml: forbidden tools in [tools]: {', '.join(found)}"
                )

    ts, ns = now_ns_pair()
    ok = not violations
    errors = (
        [
            PcHubErrorEntry(
                code=PcHubErrorCode.ERR_VALIDATION_FAILED,
                message=f"{len(violations)} pc-hub invariant violation(s)",
                detail={"violations": violations},
            )
        ]
        if violations
        else []
    )
    returns = [
        PcHubReturnEntry(
            code=PcHubReturnCode.RET_OK if ok else PcHubReturnCode.RET_PARTIAL,
            message="pc-hub offline config invariants validated"
            if ok
            else "pc-hub offline validation found violations",
        )
    ]
    return PcHubValidationOutcome(
        ok=ok,
        checked_files=checked,
        violations=violations,
        timestamp=ts,
        timestamp_unix_ns=ns,
        errors=errors,
        returns=returns,
    )


def render_pc_hub_env_file(
    *,
    ts_host: str,
    state_dir: str,
    beszel_public_key: str,
    beszel_token: str,
    searxng_secret: str,
    beszel_user_email: str = "",
    caddy_port: int = 8880,
) -> str:
    """Render `.env` content for `docker/pc-hub/compose.yaml` on `pc`."""
    lines = [
        f"PC_HUB_TS_HOST={ts_host}",
        f"PC_HUB_CADDY_PORT={caddy_port}",
        f"PC_HUB_STATE_DIR={state_dir}",
        f"BESZEL_AGENT_HUB_URL=http://127.0.0.1:{DEFAULT_PORTS['beszel']}",
        f"BESZEL_PUBLIC_KEY={beszel_public_key}",
        f"BESZEL_TOKEN={beszel_token}",
        "BESZEL_DISABLE_PASSWORD_AUTH=true",
        f"BESZEL_USER_EMAIL={beszel_user_email}",
        f"SEARXNG_SECRET={searxng_secret}",
    ]
    return "\n".join(lines) + "\n"


def render_systemd_units(
    *,
    home_dir: str,
    ts_host: str,
    node_bin: str = "/usr/bin/node",
    agentsview_bin: str = "",
    qmd_bin: str = "",
    board_server_mjs: str = "",
) -> dict[str, str]:
    """Render user-level systemd unit definitions for pc Hub services."""
    av_exec = agentsview_bin or f"{home_dir}/.local/share/mise/shims/agentsview"
    qmd_exec = qmd_bin or f"{home_dir}/.local/bin/qmd"
    board_mjs = board_server_mjs or f"{home_dir}/.config/mise/assets/board/server.mjs"
    board_port = DEFAULT_PORTS["vercel_board"]
    av_port = DEFAULT_PORTS["agentsview_hub"]
    qmd_port = DEFAULT_PORTS["qmd"]
    service_path = f"{home_dir}/.local/share/mise/shims:{home_dir}/.local/bin:/usr/local/bin:/usr/bin:/bin"

    board_unit = "\n".join(
        [
            "[Unit]",
            "Description=dotfiles-v2 @vercel/board hub server",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"Environment=PATH={service_path}",
            f"Environment=BOARD_PORT={board_port}",
            f"Environment=BOARD_DIR={home_dir}/.local/state/vercel-board",
            f"Environment=BOARD_TOKEN_FILE={home_dir}/.config/board/agent-token",
            f"Environment=BOARD_TAILSCALE_HOST={ts_host}",
            f"Environment=MISE_DATA_DIR={home_dir}/.local/share/mise",
            f"ExecStart={node_bin} {board_mjs}",
            "Restart=on-failure",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )

    agentsview_unit = "\n".join(
        [
            "[Unit]",
            "Description=dotfiles-v2 AgentsView central session server",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"Environment=PATH={service_path}",
            f"Environment=AGENTSVIEW_DATA_DIR={home_dir}/.agentsview",
            "Environment=AGENTSVIEW_DISABLE_UPDATE_CHECK=1",
            "Environment=AGENTSVIEW_TELEMETRY_ENABLED=0",
            f"ExecStart={av_exec} serve --host 127.0.0.1 --port {av_port} --base-path /agentsview --public-url https://{ts_host}/agentsview --require-auth --no-browser --no-update-check",
            "Restart=on-failure",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )

    qmd_unit = "\n".join(
        [
            "[Unit]",
            f"Description=dotfiles-v2 @tobilu/qmd v{QMD_VERSION} HTTP server",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"Environment=PATH={service_path}",
            f"Environment=QMD_ALLOWED_ORIGINS=https://{ts_host}",
            f"Environment=QMD_ALLOWED_HOSTS=127.0.0.1:{qmd_port},localhost:{qmd_port},{ts_host}",
            f"ExecStart={qmd_exec} mcp --http --host 127.0.0.1 --port {qmd_port}",
            "Restart=on-failure",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )

    return {
        "dev.mise.vercel-board.service": board_unit,
        "dev.mise.agentsview-hub.service": agentsview_unit,
        "dev.mise.qmd-hub.service": qmd_unit,
    }


def render_launchd_beszel_agent_plist(
    *,
    label: str = "dev.mise.beszel-agent",
    agent_bin: str,
    hub_url: str,
    public_key: str,
    token_file: str,
    listen: str = "127.0.0.1:45876",
    mac_host_mode: bool = True,
    vm_socks_proxy: str = "",
    log_dir: str,
) -> bytes:
    """Render a macOS LaunchAgent plist for host-only or VM `beszel-agent`."""
    env: dict[str, str] = {
        "LISTEN": listen,
        "HUB_URL": hub_url,
        "KEY": public_key,
        "TOKEN_FILE": token_file,
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    if mac_host_mode:
        env["GPU_COLLECTOR"] = "macmon"
        env["DOCKER_HOST"] = f"tcp://127.0.0.1:{DEFAULT_PORTS['docker_socket_proxy']}"
    if vm_socks_proxy:
        env["ALL_PROXY"] = vm_socks_proxy
        env["all_proxy"] = vm_socks_proxy

    payload = {
        "Label": label,
        "ProgramArguments": [agent_bin],
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 15,
        "StandardOutPath": f"{log_dir}/{label}.out.log",
        "StandardErrorPath": f"{log_dir}/{label}.err.log",
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def render_agentsview_env_sh(*, server_url: str, token_file: str) -> str:
    """Render shell exports routing `agentsview` CLI and skills to the pc Hub."""
    q_url = shlex.quote(server_url)
    q_tok = shlex.quote(token_file)
    return "\n".join(
        [
            "# shellcheck shell=sh",
            f"export AGENTSVIEW_SERVER={q_url}",
            f"export AGENTSVIEW_SERVER_TOKEN_FILE={q_tok}",
            f"export AGENTSVIEW_SKILLS_SERVER={q_url}",
            f"export AGENTSVIEW_SKILLS_SERVER_TOKEN_FILE={q_tok}",
            "export AGENTSVIEW_NO_DAEMON=1",
            "",
        ]
    )


def offload_agentsview_local(
    *,
    home_dir: Path,
    server_url: str,
    token_value: str | None = None,
    apply: bool = False,
    refresh_skills: bool = True,
    bootstrap_skills_task: Path | None = None,
) -> AgentsViewOffloadOutcome:
    """Configure remote `pc` agentsview routing and disable heavy Mac native LaunchAgent."""
    cfg_dir = home_dir / ".config" / "agentsview"
    token_path = cfg_dir / "server-token"
    env_path = cfg_dir / "env.sh"
    plist_path = (
        home_dir
        / "Library"
        / "LaunchAgents"
        / "dev.mise.com.raymanaloto.agentsview-native.plist"
    )
    backup_dir = home_dir / ".local" / "state" / "dotfiles-v2" / "backups"

    errors: list[PcHubErrorEntry] = []
    returns: list[PcHubReturnEntry] = []
    launchagent_disabled = not plist_path.exists()
    skills_refreshed = False

    if apply:
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_dir.chmod(0o700)
        if token_value is not None:
            tmp_tok = cfg_dir / f".server-token.tmp.{os.getpid()}"
            tmp_tok.write_text(token_value.strip() + "\n", encoding="utf-8")
            tmp_tok.chmod(0o600)
            tmp_tok.replace(token_path)

        env_tmp = cfg_dir / f".env.sh.tmp.{os.getpid()}"
        env_tmp.write_text(
            render_agentsview_env_sh(server_url=server_url, token_file=str(token_path)),
            encoding="utf-8",
        )
        env_tmp.chmod(0o600)
        env_tmp.replace(env_path)

        if plist_path.exists() or plist_path.is_symlink():
            if shutil.which("launchctl"):
                uid = os.getuid()
                subprocess.run(
                    [
                        "launchctl",
                        "bootout",
                        f"gui/{uid}/dev.mise.com.raymanaloto.agentsview-native",
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    check=False,
                )
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / f"{plist_path.name}.bak"
            shutil.move(str(plist_path), str(backup_path))
            launchagent_disabled = True
            LOGGER.info(
                "agentsview_launchagent_offloaded",
                plist=str(plist_path),
                backup=str(backup_path),
            )

        if shutil.which("agentsview"):
            subprocess.run(
                ["agentsview", "daemon", "stop"],
                env={**os.environ, "HOME": str(home_dir), "AGENTSVIEW_NO_DAEMON": "1"},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
            )

        if (
            refresh_skills
            and bootstrap_skills_task is not None
            and bootstrap_skills_task.is_file()
        ):
            skill_env = {
                **os.environ,
                "HOME": str(home_dir),
                "AGENTSVIEW_SKILLS_SERVER": server_url,
                "AGENTSVIEW_SKILLS_SERVER_TOKEN_FILE": str(token_path),
            }
            proc = subprocess.run(
                ["bash", str(bootstrap_skills_task)],
                env=skill_env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                skills_refreshed = True
            else:
                errors.append(
                    PcHubErrorEntry(
                        code=PcHubErrorCode.ERR_VALIDATION_FAILED,
                        message="bootstrap:skills failed after agentsview offload",
                        detail={"rc": proc.returncode, "stderr": proc.stderr[-400:]},
                    )
                )
        returns.append(
            PcHubReturnEntry(
                code=PcHubReturnCode.RET_OK
                if not errors
                else PcHubReturnCode.RET_PARTIAL,
                message="agentsview client routing updated and native Mac daemon disabled",
            )
        )
    else:
        returns.append(
            PcHubReturnEntry(
                code=PcHubReturnCode.RET_DRY_RUN,
                message="dry-run preview; pass --apply to write env.sh and disable Mac LaunchAgent",
                detail={"plist_exists": plist_path.exists(), "env_path": str(env_path)},
            )
        )

    ts, ns = now_ns_pair()
    return AgentsViewOffloadOutcome(
        ok=not errors,
        dry_run=not apply,
        server_url=server_url,
        launchagent_disabled=launchagent_disabled,
        skills_refreshed=skills_refreshed,
        timestamp=ts,
        timestamp_unix_ns=ns,
        errors=errors,
        returns=returns,
    )
