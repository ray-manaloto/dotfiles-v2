"""Cyclopts-backed CLI entrypoint for `codex_trust` (`codex:hook-trust`, `codex:plugin-update`)."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import cyclopts
import orjson
from cyclopts.exceptions import CycloptsError
from pydantic import BaseModel

from codex_trust.trust import (
    inspect_hook_trust,
    repair_hook_trust,
    update_plugin,
)
from dotfiles_py.logging import configure_logging


def _emit_json(model_obj: BaseModel) -> None:
    sys.stdout.write(
        orjson.dumps(model_obj.model_dump(mode="json")).decode("utf-8") + "\n"
    )


def _default_log_path() -> Path:
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = (
        Path(os.path.expanduser(xdg_state))
        if xdg_state
        else Path.home() / ".local" / "state"
    )
    return base / "codex-trust" / "codex-trust.log.jsonl"


def _configure_logging(*, json_mode: bool, mutating: bool = False) -> None:
    level = logging.WARNING if json_mode else logging.INFO
    jsonl_path = _default_log_path() if mutating else None
    configure_logging(
        jsonl_path=jsonl_path,
        level=level,
        file_level=logging.INFO,
        logger_name="codex_trust",
    )


def _build_app() -> cyclopts.App:
    app = cyclopts.App(
        name="codex-trust",
        help="Schema-first Codex plugin hook-trust inspection, repair, and pinned updates.",
        version_flags=(),
    )

    @app.command(name="hook-trust")
    def hook_trust_cmd(
        *,
        check: Annotated[bool, cyclopts.Parameter(name="--check", negative="")] = True,
        repair: Annotated[
            bool, cyclopts.Parameter(name="--repair", negative="")
        ] = False,
        json_out: Annotated[
            bool, cyclopts.Parameter(name="--json", negative="")
        ] = False,
        cwd: Annotated[Path | None, cyclopts.Parameter(name="--cwd")] = None,
        codex_home: Annotated[
            Path | None, cyclopts.Parameter(name="--codex-home")
        ] = None,
    ) -> int:
        """Inspect Codex user and plugin hook trust, or repair plugin hook trust."""
        _ = check
        _configure_logging(json_mode=json_out, mutating=repair)
        resp = (
            repair_hook_trust(cwd=cwd, codex_home=codex_home)
            if repair
            else inspect_hook_trust(cwd=cwd, codex_home=codex_home)
        )
        if json_out:
            _emit_json(resp)
        else:
            status_lbl = "OK" if resp.rc == 0 else "FAIL"
            sys.stdout.write(
                f"{status_lbl}  codex:hook-trust: codex={resp.codex_version or '?'} "
                f"findings={len(resp.findings)} unmigrated={len(resp.unmigrated_keys)} "
                f"orphans={len(resp.orphaned_keys)} repaired={len(resp.repaired_keys)}\n"
            )
            for finding in resp.findings:
                sys.stdout.write(
                    f"  - [{finding.status.value}] {finding.key}: {finding.issue}\n"
                )
            for err in resp.errors:
                sys.stderr.write(f"ERROR {err.code.value}: {err.message or ''}\n")
        return resp.rc

    @app.command(name="plugin-update")
    def plugin_update_cmd(
        plugin: Annotated[str, cyclopts.Parameter()],
        *,
        ref: Annotated[str | None, cyclopts.Parameter(name="--ref")] = None,
        apply: Annotated[bool, cyclopts.Parameter(name="--apply", negative="")] = False,
        trust_hook_diff: Annotated[
            bool, cyclopts.Parameter(name="--trust-hook-diff", negative="")
        ] = False,
        json_out: Annotated[
            bool, cyclopts.Parameter(name="--json", negative="")
        ] = False,
        cwd: Annotated[Path | None, cyclopts.Parameter(name="--cwd")] = None,
        codex_home: Annotated[
            Path | None, cyclopts.Parameter(name="--codex-home")
        ] = None,
    ) -> int:
        """Stage a pinned Codex plugin ref, gate hook-surface diffs, and additively install/trust."""
        _configure_logging(json_mode=json_out, mutating=apply)
        resp = update_plugin(
            plugin,
            ref=ref,
            apply=apply,
            trust_hook_diff=trust_hook_diff,
            cwd=cwd,
            codex_home=codex_home,
        )
        if json_out:
            _emit_json(resp)
        else:
            status_lbl = "OK" if resp.rc == 0 else "FAIL"
            sys.stdout.write(
                f"{status_lbl}  codex:plugin-update {resp.plugin_id}: "
                f"ref={resp.new_ref or '-'} ver={resp.new_version or '-'} "
                f"hook_surface_unchanged={resp.hook_surface_unchanged} applied={resp.applied}\n"
            )
            if resp.hook_surface_diff and not resp.hook_surface_unchanged:
                sys.stdout.write(resp.hook_surface_diff + "\n")
            for err in resp.errors:
                sys.stderr.write(f"ERROR {err.code.value}: {err.message or ''}\n")
        return resp.rc

    return app


def main(argv: Sequence[str] | None = None) -> int:
    app = _build_app()
    try:
        result = app(
            list(argv) if argv is not None else sys.argv[1:], exit_on_error=False
        )
    except CycloptsError as exc:
        sys.stderr.write(f"codex-trust: {exc}\n")
        return 2
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
