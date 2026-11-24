"""Cyclopts-backed CLI entrypoint for `mise run env:inventory`."""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import cyclopts
import orjson
from pydantic import ValidationError

from dotfiles_py.generated import (
    EnvInventoryResponse,
    EnvInventorySummary,
    InventoryErrorCode,
    InventoryErrorEntry,
    InventoryReturnCode,
    InventoryReturnEntry,
)
from dotfiles_py.logging import configure_logging
from dotfiles_py.time_ns import now_ns_pair
from env_inventory.collector import (
    DEFAULT_IMAGE_TAG,
    collect_inventory,
    resolve_inventory_state_dir,
)


def _configure_inventory_logging(
    state_dir: Path | None,
    *,
    json_mode: bool,
    read_only: bool,
) -> None:
    resolved = resolve_inventory_state_dir(state_dir)
    logger_level = logging.WARNING if read_only else logging.INFO
    jsonl_path = None if read_only else resolved / "env-inventory.log.jsonl"
    logger = configure_logging(
        jsonl_path=jsonl_path,
        level=logger_level,
        logger_name="env_inventory",
    )
    if json_mode:
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(logging.WARNING)


def _build_app() -> cyclopts.App:
    app = cyclopts.App(
        name="env-inventory",
        help="Schema-first dual-layer native environment inventory CLI.",
        version_flags=(),
    )

    @app.default
    def inventory_cmd(
        *,
        target: Annotated[str, cyclopts.Parameter(name="--target")] = "host",
        ssh_host: Annotated[str, cyclopts.Parameter(name="--host")] = "pc",
        tag: Annotated[str, cyclopts.Parameter(name="--tag")] = DEFAULT_IMAGE_TAG,
        write_repo: Annotated[
            bool, cyclopts.Parameter(name="--write-repo", negative="")
        ] = False,
        repo: Annotated[Path | None, cyclopts.Parameter(name="--repo")] = None,
        out_dir: Annotated[Path | None, cyclopts.Parameter(name="--out-dir")] = None,
        state_dir: Annotated[
            Path | None, cyclopts.Parameter(name="--state-dir")
        ] = None,
        json_out: Annotated[
            bool, cyclopts.Parameter(name="--json", negative="")
        ] = True,
    ) -> int:
        """Collect sanitized native mise, OCI/BuildKit, and AI harness inventory."""
        read_only = not (write_repo or out_dir is not None)
        _configure_inventory_logging(state_dir, json_mode=json_out, read_only=read_only)
        resp = collect_inventory(
            requested_target=target,
            ssh_host=ssh_host,
            image_tag=tag,
            write_repo=write_repo,
            repo_root=repo,
            out_dir=out_dir,
            state_dir=state_dir,
        )
        if json_out:
            sys.stdout.write(
                orjson.dumps(resp.model_dump(mode="json")).decode("utf-8") + "\n"
            )
        else:
            sys.stdout.write(
                f"env:inventory target={resp.summary.requested_target} "
                f"collected={resp.summary.targets_collected} "
                f"reachable={resp.summary.targets_reachable} rc={resp.rc}\n"
            )
        return resp.rc

    return app


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments via `cyclopts` and return deterministic exit code."""
    arg_list = list(sys.argv[1:] if argv is None else argv)
    app = _build_app()
    try:
        result = app(
            arg_list,
            exit_on_error=False,
            print_error=False,
            result_action="return_value",
        )
        return int(result) if isinstance(result, int) else 0
    except (cyclopts.CycloptsError, ValidationError) as err:
        ts, ns = now_ns_pair()
        err_entry = InventoryErrorEntry(
            code=InventoryErrorCode.ERR_INVALID_INPUT,
            message=str(err),
        )
        ret_entry = InventoryReturnEntry(
            code=InventoryReturnCode.RET_SKIPPED,
            message="invalid cli arguments",
        )
        resp = EnvInventoryResponse(
            command="env:inventory",
            rc=2,
            summary=EnvInventorySummary(
                requested_target="invalid",
                targets_collected=0,
                targets_reachable=0,
                timestamp=ts,
                timestamp_unix_ns=ns,
                errors=[err_entry],
                returns=[ret_entry],
            ),
            inventories=[],
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[err_entry],
            returns=[ret_entry],
        )
        sys.stderr.write(
            orjson.dumps(resp.model_dump(mode="json")).decode("utf-8") + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
