"""Cyclopts CLI for `plugins:update` (exit 0 ok, 1 error, 3 owner review needed)."""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import cyclopts
import orjson
from cyclopts.exceptions import CycloptsError

from dotfiles_py.logging import configure_logging, get_logger
from plugins_update.core import (
    RC_ERROR,
    RC_NEEDS_REVIEW,
    RC_OK,
    format_summary,
    load_tracked,
    run_update,
)

DEFAULT_TRACKED = (
    Path(__file__).resolve().parents[2] / "assets" / "plugins-tracked.json"
)
_NOTIFY_TEXT = "Plugin hook change needs review: run `mise run plugins:update`."


def _log_path() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(os.path.expanduser(xdg)) if xdg else Path.home() / ".local" / "state"
    return base / "plugins-update" / "plugins-update.log.jsonl"


def _is_mac_host() -> bool:
    env = os.environ.get("MISE_ENV", "")
    in_container = (
        Path("/.dockerenv").exists() or os.environ.get("container") == "docker"  # noqa: SIM112 (docker sets lowercase)
    )
    return (
        platform.system() == "Darwin"
        and "vm" not in env.split(",")
        and not in_container
    )


def _notify(message: str) -> None:
    script = f'display notification {orjson.dumps(message).decode()} with title "dotfiles-v2 plugins"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        get_logger("plugins_update").warning("notify_failed", error=str(exc))


def _build_app() -> cyclopts.App:
    app = cyclopts.App(
        name="plugins-update",
        help="Keep tracked Codex/Claude plugins on their latest upstream release.",
        version_flags=(),
        default_command=None,
    )

    @app.default
    def update_cmd(
        *,
        apply: Annotated[bool, cyclopts.Parameter(name="--apply", negative="")] = False,
        json_out: Annotated[
            bool, cyclopts.Parameter(name="--json", negative="")
        ] = False,
        plugin: Annotated[str | None, cyclopts.Parameter(name="--plugin")] = None,
        scheduled: Annotated[
            bool, cyclopts.Parameter(name="--scheduled", negative="")
        ] = False,
        tracked: Annotated[Path | None, cyclopts.Parameter(name="--tracked")] = None,
        approve: Annotated[str | None, cyclopts.Parameter(name="--approve")] = None,
    ) -> int:
        """Compare installed plugin versions with upstream releases and update when safe."""
        configure_logging(
            jsonl_path=_log_path() if apply else None,
            level=logging.WARNING if json_out else logging.INFO,
            file_level=logging.INFO,
            logger_name="plugins_update",
        )
        if scheduled and not _is_mac_host():
            sys.stdout.write("plugins:update --scheduled: skipped (not the Mac host)\n")
            return RC_OK
        if approve is not None and (plugin is None or scheduled):
            sys.stderr.write(
                "plugins-update: --approve requires --plugin and cannot be scheduled\n"
            )
            return RC_ERROR
        resp = run_update(
            load_tracked(tracked or DEFAULT_TRACKED),
            apply=apply or approve is not None,
            approve=approve,
            only=plugin,
        )
        if json_out:
            sys.stdout.write(
                orjson.dumps(resp.model_dump(mode="json")).decode("utf-8") + "\n"
            )
        else:
            sys.stdout.write(format_summary(resp))
        if resp.rc == RC_NEEDS_REVIEW and scheduled:
            _notify(_NOTIFY_TEXT)
            return RC_OK  # keep the daemon healthy; the notification and log carry the signal
        if resp.rc == RC_ERROR and scheduled:
            reason = next(
                (e.message for r in resp.results for e in r.errors), "see the log"
            )
            _notify(f"Plugin update failed: {(reason or 'see the log')[:120]}")
        return resp.rc

    return app


def main(argv: Sequence[str] | None = None) -> int:
    app = _build_app()
    try:
        result = app(
            list(argv) if argv is not None else sys.argv[1:], exit_on_error=False
        )
    except CycloptsError as exc:
        sys.stderr.write(f"plugins-update: {exc}\n")
        return 2
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"plugins-update: {exc}\n")
        return 1
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
