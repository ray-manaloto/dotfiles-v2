"""Cyclopts-backed CLI entrypoint for `pc_hub` validation, rendering, and offload."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import cyclopts
import orjson
from pydantic import BaseModel

from dotfiles_py.generated.pc_hub_schema import (
    PcHubCheckState,
    PcHubErrorCode,
    PcHubErrorEntry,
    PcHubReturnCode,
    PcHubReturnEntry,
    PcHubServiceName,
    PcHubServiceStatus,
    PcHubStatusResponse,
)
from dotfiles_py.logging import configure_logging, get_logger
from dotfiles_py.time_ns import now_ns_pair
from pc_hub.config import (
    DEFAULT_PORTS,
    default_port_map,
    offload_agentsview_local,
    render_launchd_beszel_agent_plist,
    render_pc_hub_env_file,
    render_systemd_units,
    validate_pc_hub_repo,
)

LOGGER = get_logger("pc_hub")


def _configure_hub_logging(*, json_mode: bool) -> None:
    level = logging.WARNING if json_mode else logging.INFO
    configure_logging(jsonl_path=None, level=level, logger_name="pc_hub")


def _emit_json(model: BaseModel) -> None:
    sys.stdout.write(orjson.dumps(model.model_dump(mode="json")).decode("utf-8") + "\n")


def _build_app() -> cyclopts.App:
    app = cyclopts.App(
        name="pc-hub",
        help="Validate and render pc Hub Compose, Caddy, systemd, launchd, and agentsview offload state.",
        version_flags=(),
    )

    @app.command(name="validate")
    def validate_cmd(
        *,
        repo: Annotated[Path, cyclopts.Parameter(name="--repo")] = Path("."),
        json_out: Annotated[
            bool, cyclopts.Parameter(name="--json", negative="")
        ] = False,
    ) -> int:
        """Validate committed pc-hub and mac-agent Compose/Caddy/SearXNG invariants offline."""
        _configure_hub_logging(json_mode=json_out)
        outcome = validate_pc_hub_repo(repo.resolve())
        if json_out:
            _emit_json(outcome)
        else:
            status_tag = "OK" if outcome.ok else "FAIL"
            sys.stdout.write(
                f"{status_tag} checked={len(outcome.checked_files)} violations={len(outcome.violations)}\n"
            )
            for item in outcome.violations:
                sys.stdout.write(f"  - {item}\n")
        return 0 if outcome.ok else 1

    @app.command(name="render-env")
    def render_env_cmd(
        *,
        ts_host: Annotated[str, cyclopts.Parameter(name="--ts-host")],
        state_dir: Annotated[str, cyclopts.Parameter(name="--state-dir")],
        beszel_public_key: Annotated[
            str, cyclopts.Parameter(name="--beszel-public-key")
        ] = "",
        beszel_token: Annotated[str, cyclopts.Parameter(name="--beszel-token")] = "",
        searxng_secret: Annotated[
            str, cyclopts.Parameter(name="--searxng-secret")
        ] = "",
        beszel_user_email: Annotated[
            str, cyclopts.Parameter(name="--beszel-user-email")
        ] = "",
        caddy_port: Annotated[
            int, cyclopts.Parameter(name="--caddy-port")
        ] = DEFAULT_PORTS["caddy"],
    ) -> int:
        """Render `.env` content for `docker/pc-hub/compose.yaml` to stdout."""
        sys.stdout.write(
            render_pc_hub_env_file(
                ts_host=ts_host,
                state_dir=state_dir,
                beszel_public_key=beszel_public_key,
                beszel_token=beszel_token,
                searxng_secret=searxng_secret,
                beszel_user_email=beszel_user_email,
                caddy_port=caddy_port,
            )
        )
        return 0

    @app.command(name="render-systemd")
    def render_systemd_cmd(
        *,
        home_dir: Annotated[str, cyclopts.Parameter(name="--home-dir")],
        ts_host: Annotated[str, cyclopts.Parameter(name="--ts-host")],
        node_bin: Annotated[
            str, cyclopts.Parameter(name="--node-bin")
        ] = "/usr/bin/node",
        agentsview_bin: Annotated[
            str, cyclopts.Parameter(name="--agentsview-bin")
        ] = "",
        qmd_bin: Annotated[str, cyclopts.Parameter(name="--qmd-bin")] = "",
        board_server_mjs: Annotated[
            str, cyclopts.Parameter(name="--board-server-mjs")
        ] = "",
        ollama_bin: Annotated[str, cyclopts.Parameter(name="--ollama-bin")] = "",
        out_dir: Annotated[Path | None, cyclopts.Parameter(name="--out-dir")] = None,
    ) -> int:
        """Render or write user systemd unit files for vercel-board, agentsview-hub, qmd, and ollama."""
        units = render_systemd_units(
            home_dir=home_dir,
            ts_host=ts_host,
            node_bin=node_bin,
            agentsview_bin=agentsview_bin,
            qmd_bin=qmd_bin,
            board_server_mjs=board_server_mjs,
            ollama_bin=ollama_bin,
        )
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            for name, body in units.items():
                (out_dir / name).write_text(body, encoding="utf-8")
        else:
            sys.stdout.write(orjson.dumps(units).decode("utf-8") + "\n")
        return 0

    @app.command(name="render-launchd")
    def render_launchd_cmd(
        *,
        agent_bin: Annotated[str, cyclopts.Parameter(name="--agent-bin")],
        hub_url: Annotated[str, cyclopts.Parameter(name="--hub-url")],
        public_key: Annotated[str, cyclopts.Parameter(name="--public-key")],
        token_file: Annotated[str, cyclopts.Parameter(name="--token-file")],
        log_dir: Annotated[str, cyclopts.Parameter(name="--log-dir")],
        label: Annotated[
            str, cyclopts.Parameter(name="--label")
        ] = "dev.mise.beszel-agent",
        vm_socks_proxy: Annotated[
            str, cyclopts.Parameter(name="--vm-socks-proxy")
        ] = "",
        mac_host: Annotated[bool, cyclopts.Parameter(name="--mac-host")] = True,
    ) -> int:
        """Render a LaunchAgent plist for macOS or VM `beszel-agent` to stdout."""
        plist_bytes = render_launchd_beszel_agent_plist(
            label=label,
            agent_bin=agent_bin,
            hub_url=hub_url,
            public_key=public_key,
            token_file=token_file,
            mac_host_mode=mac_host,
            vm_socks_proxy=vm_socks_proxy,
            log_dir=log_dir,
        )
        sys.stdout.write(plist_bytes.decode("utf-8"))
        return 0

    @app.command(name="agentsview-offload")
    def agentsview_offload_cmd(
        *,
        server_url: Annotated[str, cyclopts.Parameter(name="--server-url")],
        token: Annotated[str | None, cyclopts.Parameter(name="--token")] = None,
        home_dir: Annotated[Path | None, cyclopts.Parameter(name="--home-dir")] = None,
        apply: Annotated[bool, cyclopts.Parameter(name="--apply", negative="")] = False,
        refresh_skills: Annotated[
            bool, cyclopts.Parameter(name="--refresh-skills")
        ] = True,
        bootstrap_skills_task: Annotated[
            Path | None, cyclopts.Parameter(name="--bootstrap-skills-task")
        ] = None,
        json_out: Annotated[
            bool, cyclopts.Parameter(name="--json", negative="")
        ] = False,
    ) -> int:
        """Route local agentsview CLI/skills to pc Hub and disable heavy Mac LaunchAgent."""
        _configure_hub_logging(json_mode=json_out)
        target_home = (home_dir or Path.home()).expanduser()
        outcome = offload_agentsview_local(
            home_dir=target_home,
            server_url=server_url,
            token_value=token,
            apply=apply,
            refresh_skills=refresh_skills,
            bootstrap_skills_task=bootstrap_skills_task,
        )
        if json_out:
            _emit_json(outcome)
        else:
            mode = "APPLY" if apply else "DRY-RUN"
            sys.stdout.write(
                f"{mode} ok={outcome.ok} server={outcome.server_url} "
                f"launchagent_disabled={outcome.launchagent_disabled} "
                f"skills_refreshed={outcome.skills_refreshed}\n"
            )
        return 0 if outcome.ok else 1

    @app.command(name="build-status")
    def build_status_cmd(
        *,
        command: Annotated[str, cyclopts.Parameter(name="--command")],
        ssh_host: Annotated[str, cyclopts.Parameter(name="--ssh-host")] = "pc",
        tailscale_host: Annotated[
            str | None, cyclopts.Parameter(name="--tailscale-host")
        ] = None,
        dry_run: Annotated[
            bool, cyclopts.Parameter(name="--dry-run", negative="")
        ] = False,
        repo: Annotated[Path, cyclopts.Parameter(name="--repo")] = Path("."),
        rows_json: Annotated[str, cyclopts.Parameter(name="--rows-json")] = "[]",
    ) -> int:
        """Emit a schema-validated `PcHubStatusResponse` JSON envelope."""
        _configure_hub_logging(json_mode=True)
        validation = validate_pc_hub_repo(repo.resolve())
        raw_rows = orjson.loads(rows_json)
        services: list[PcHubServiceStatus] = []
        failed = not validation.ok
        for row in raw_rows if isinstance(raw_rows, list) else []:
            if not isinstance(row, dict):
                continue
            st = PcHubCheckState(str(row.get("state", "WARN")))
            if st == PcHubCheckState.FAIL:
                failed = True
            services.append(
                PcHubServiceStatus(
                    service=PcHubServiceName(str(row["service"])),
                    state=st,
                    bind_target=str(row["bind_target"]),
                    exposed_http=bool(row.get("exposed_http", False)),
                    detail=row.get("detail"),
                )
            )
        rc = 1 if failed else 0
        ts, ns = now_ns_pair()
        errors = (
            [
                PcHubErrorEntry(
                    code=PcHubErrorCode.ERR_VALIDATION_FAILED,
                    message="pc-hub status/doctor reported failing checks",
                    detail={"violations": validation.violations},
                )
            ]
            if failed
            else []
        )
        ret_code = (
            PcHubReturnCode.RET_DRY_RUN
            if dry_run
            else (PcHubReturnCode.RET_OK if rc == 0 else PcHubReturnCode.RET_PARTIAL)
        )
        resp = PcHubStatusResponse(
            command=command,
            rc=rc,
            dry_run=dry_run,
            ssh_host=ssh_host,
            tailscale_host=tailscale_host or None,
            ports=default_port_map(),
            services=services,
            violations=validation.violations,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=errors,
            returns=[PcHubReturnEntry(code=ret_code)],
        )
        _emit_json(resp)
        return rc

    return app


def main(argv: Sequence[str] | None = None) -> int:
    """Run the `pc-hub` CLI entrypoint."""
    app = _build_app()
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        result = app(
            args,
            result_action="return_value",
            exit_on_error=False,
            print_error=True,
        )
        return int(result) if isinstance(result, int) else 0
    except cyclopts.CycloptsError as exc:
        sys.stderr.write(f"pc-hub: {exc}\n")
        return 2
    except Exception as exc:
        LOGGER.error("pc_hub_cli_error", error=str(exc))
        if os.environ.get("PC_HUB_DEBUG") == "1":
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
