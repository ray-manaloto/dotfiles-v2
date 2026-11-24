"""Cyclopts-backed CLI entrypoint for `mise run env:stats`."""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import cyclopts
import orjson

from dotfiles_py.logging import configure_logging
from env_stats.collector import collect_env_stats, resolve_cache_dir


def _configure_stats_logging(*, cache_home: Path | None, json_mode: bool) -> None:
    resolved_cache = resolve_cache_dir(cache_home)
    log_dir = resolved_cache / "usage-telemetry"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = configure_logging(
        jsonl_path=log_dir / "env-stats.log.jsonl",
        level=logging.WARNING if json_mode else logging.INFO,
        logger_name="env_stats",
    )
    if json_mode:
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(logging.WARNING)


def _build_app() -> cyclopts.App:
    app = cyclopts.App(
        name="env-stats",
        help="Schema-first multi-environment stats and Antigravity session telemetry CLI.",
        version_flags=(),
    )

    @app.default
    def snapshot_cmd(
        *,
        cache_home: Annotated[
            Path | None, cyclopts.Parameter(name="--cache-home")
        ] = None,
        herdr_home: Annotated[
            Path | None, cyclopts.Parameter(name="--herdr-home")
        ] = None,
        json_out: Annotated[
            bool, cyclopts.Parameter(name="--json", negative="")
        ] = True,
    ) -> int:
        """Emit multi-environment metrics, agy session snapshots, and Herdr sync state."""
        _configure_stats_logging(cache_home=cache_home, json_mode=json_out)
        resp = collect_env_stats(cache_home=cache_home, herdr_home=herdr_home)
        if json_out:
            sys.stdout.write(
                orjson.dumps(resp.model_dump(mode="json")).decode("utf-8") + "\n"
            )
        else:
            sys.stdout.write(
                f"env:stats environments={len(resp.environments)} "
                f"agy_sessions={len(resp.agy_sessions)} "
                f"env_stats_mod={resp.herdr.env_stats_installed} "
                f"usage_limits_mod={resp.herdr.usage_limits_installed} rc={resp.rc}\n"
            )
        return int(resp.rc)

    return app


def main(argv: Sequence[str] | None = None) -> int:
    """Run the `env_stats` CLI."""
    app = _build_app()
    result = app(list(argv) if argv is not None else sys.argv[1:], exit_on_error=False)
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
