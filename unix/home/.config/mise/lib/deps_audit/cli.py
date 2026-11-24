"""Cyclopts-backed CLI entrypoint for `mise run deps:audit`."""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import cyclopts
import orjson

from deps_audit.auditor import audit_dependencies, resolve_deps_audit_state_dir
from dotfiles_py.generated.deps_audit_schema import (
    DepsAuditResponse,
    DepsAuditSourceKind,
    DepsAuditStatus,
)
from dotfiles_py.logging import configure_logging


def _configure_audit_logging(
    state_dir: Path | None,
    *,
    json_mode: bool,
    write_log: bool,
) -> None:
    resolved = resolve_deps_audit_state_dir(state_dir)
    jsonl_path = (resolved / "deps-audit.log.jsonl") if write_log else None
    logger = configure_logging(
        jsonl_path=jsonl_path,
        level=logging.WARNING if json_mode else logging.INFO,
        file_level=logging.INFO,
        logger_name="deps_audit",
    )
    if json_mode:
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(logging.WARNING)


def _emit_json(resp: DepsAuditResponse) -> None:
    sys.stdout.write(orjson.dumps(resp.model_dump(mode="json")).decode("utf-8") + "\n")


def _build_app() -> cyclopts.App:
    app = cyclopts.App(
        name="deps-audit",
        help="Cross-OS dependency, OCI digest, Python, hk preset, harness, and OpenWiki audit.",
        version_flags=(),
    )

    @app.default
    def audit_cmd(
        *,
        repo: Annotated[Path | None, cyclopts.Parameter(name="--repo")] = None,
        offline: Annotated[
            bool, cyclopts.Parameter(name="--offline", negative="")
        ] = False,
        json_out: Annotated[
            bool, cyclopts.Parameter(name="--json", negative="")
        ] = False,
        source: Annotated[str | None, cyclopts.Parameter(name="--source")] = None,
        fail_on_outdated: Annotated[
            bool, cyclopts.Parameter(name="--fail-on-outdated", negative="")
        ] = False,
        state_dir: Annotated[
            Path | None, cyclopts.Parameter(name="--state-dir")
        ] = None,
        write_log: Annotated[
            bool, cyclopts.Parameter(name="--write-log", negative="")
        ] = False,
    ) -> int:
        """Audit cross-OS mise tools, OCI digests, uv deps, hk presets, harnesses, and kb."""
        _configure_audit_logging(state_dir, json_mode=json_out, write_log=write_log)
        selected_sources: set[DepsAuditSourceKind] | None = None
        if source:
            try:
                selected_sources = {DepsAuditSourceKind(source.strip())}
            except ValueError as exc:
                sys.stderr.write(f"deps:audit: invalid --source {source!r}: {exc}\n")
                return 2

        resp = audit_dependencies(
            repo or Path.cwd().resolve(),
            offline=offline,
            sources=selected_sources,
            fail_on_outdated=fail_on_outdated,
        )
        if json_out:
            _emit_json(resp)
        else:
            s = resp.summary
            sys.stdout.write(
                f"deps:audit total={s.total} current={s.current} "
                f"outdated={s.outdated} held={s.pinned_held} "
                f"unknown={s.unknown} error={s.error} offline={resp.offline}\n"
            )
            for item in resp.items:
                if item.status in {
                    DepsAuditStatus.OUTDATED,
                    DepsAuditStatus.ERROR,
                    DepsAuditStatus.UNKNOWN,
                }:
                    cmp_part = (
                        f" compare={item.compare_url}" if item.compare_url else ""
                    )
                    note_part = f" ({item.note})" if item.note else ""
                    sys.stdout.write(
                        f"  [{item.status.value}] {item.source_kind.value} {item.name}: "
                        f"{item.current_version or '?'} -> {item.latest_version or '?'}"
                        f"{cmp_part}{note_part}\n"
                    )
        return resp.rc

    return app


def main(argv: Sequence[str] | None = None) -> int:
    app = _build_app()
    result = app(argv, result_action="return_value")
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
