"""Refreshing terminal board for AI harness quotas, usage, and API credit."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cyclopts

from env_board import board

DEFAULT_LEDGER_ROOT = Path.home() / "dev" / "tmp"
CLEAR = "\x1b[H\x1b[J"


def collect_quota() -> list[dict[str, Any]]:
    """Run the zero-token harness quota probe (same meters as env:quota)."""
    from env_quota.cli import collect
    from env_smoke.runner import parse_harnesses

    response = collect(parse_harnesses(None))
    return board.parse_quota(response.model_dump(mode="json"))


def collect_usage(window_days: int) -> dict[str, dict[str, Any]]:
    """Read local transcript usage via the AgentsView CLI (no network)."""
    import json

    proc = subprocess.run(
        [
            "agentsview",
            "report",
            "usage",
            "daily",
            "--since",
            f"{window_days}d",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        stdin=subprocess.DEVNULL,
    )
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    return board.parse_usage(json.loads(proc.stdout), today)


def collect_ledger(root: Path) -> dict[str, Any] | None:
    """Sum the newest API-credit ledger under ``root``."""
    path = board.find_ledger(root)
    if path is None:
        return None
    with path.open(encoding="utf-8") as handle:
        return board.parse_ledger(handle)


class Poller:
    """Background refresher so a slow probe never freezes the clock."""

    def __init__(self, name: str, fn: Callable[[], Any], every_s: float) -> None:
        self.name = name
        self.fn = fn
        self.every_s = every_s
        self.value: Any = None
        self.updated: float | None = None
        self.error: str | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                self.value = self.fn()
                self.error = None
                self.updated = time.monotonic()
            except Exception as exc:
                self.error = f"{self.name}: {type(exc).__name__}"
            time.sleep(self.every_s)

    def age(self) -> int | None:
        return None if self.updated is None else int(time.monotonic() - self.updated)


def frame(pollers: dict[str, Poller], window_days: int, interval_s: int) -> str:
    errors = [p.error for p in pollers.values() if p.error]
    return board.render(
        now=datetime.now(UTC),
        quota_rows=pollers["quota"].value,
        quota_age_s=pollers["quota"].age(),
        usage=pollers["usage"].value,
        ledger=pollers["ledger"].value,
        window_days=window_days,
        errors=errors,
        interval_s=interval_s,
    )


app = cyclopts.App(name="env-board", version_flags=())


@app.default
def run(
    *,
    interval: int = 10,
    window_days: int = 7,
    once: bool = False,
    ledger_root: Path = DEFAULT_LEDGER_ROOT,
) -> int:
    """Render the board every ``interval`` seconds (or once with --once)."""
    pollers = {
        "quota": Poller("quota", collect_quota, 60),
        "usage": Poller("usage", lambda: collect_usage(window_days), 30),
        "ledger": Poller("ledger", lambda: collect_ledger(ledger_root), 10),
    }
    for poller in pollers.values():
        poller.start()
    if once:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and any(
            p.updated is None and p.error is None for p in pollers.values()
        ):
            time.sleep(0.2)
        sys.stdout.write(frame(pollers, window_days, interval))
        return 0
    try:
        while True:
            sys.stdout.write(CLEAR + frame(pollers, window_days, interval))
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    return app(argv, result_action="return_value")
