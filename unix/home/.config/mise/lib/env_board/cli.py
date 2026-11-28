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
from env_board.balance import BalanceScheduler, BalanceTicker

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


FIRST_FRAME_WAIT_S = 2.0


def wait_for(poller: Poller, timeout_s: float) -> None:
    """Block until ``poller`` has reported once (value or error) or the timeout passes."""
    deadline = time.monotonic() + timeout_s
    while (
        time.monotonic() < deadline and poller.updated is None and poller.error is None
    ):
        time.sleep(0.05)


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


def frame(
    pollers: dict[str, Poller],
    window_days: int,
    interval_s: int,
    scheduler: BalanceScheduler | None = None,
) -> str:
    try:
        return _frame(pollers, window_days, interval_s, scheduler)
    except Exception as exc:
        return f"env:board render error: {type(exc).__name__}\n"


def _frame(
    pollers: dict[str, Poller],
    window_days: int,
    interval_s: int,
    scheduler: BalanceScheduler | None,
) -> str:
    errors = [p.error for p in pollers.values() if p.error]
    pending = [n for n, p in pollers.items() if p.updated is None and p.error is None]
    return board.render(
        now=datetime.now(UTC),
        quota_rows=pollers["quota"].value,
        quota_age_s=pollers["quota"].age(),
        usage=pollers["usage"].value,
        ledger=pollers["ledger"].value,
        window_days=window_days,
        errors=errors,
        interval_s=interval_s,
        balance=None if scheduler is None else scheduler.view(),
        pending=pending,
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
    scheduler = BalanceScheduler()
    for poller in pollers.values():
        poller.start()
    if once:
        # One synchronous step: calls api:balance only if the cache is stale.
        scheduler.tick()
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and any(
            p.updated is None and p.error is None for p in pollers.values()
        ):
            time.sleep(0.2)
        sys.stdout.write(frame(pollers, window_days, interval, scheduler))
        return 0
    ticker = BalanceTicker(scheduler)
    ticker.start()
    try:
        # Give the fast local reads (the ledger) a moment so the first frame is real data.
        wait_for(pollers["ledger"], FIRST_FRAME_WAIT_S)
        while True:
            sys.stdout.write(CLEAR + frame(pollers, window_days, interval, scheduler))
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        ticker.stop()
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    return app(argv, result_action="return_value")
