"""API-credit balance lane for the harness quota board.

``api:balance`` opens a background Chrome tab (3-8 s per call), so this module
keeps a disk cache, a single-flight lock, and live settings written by the
Herdr ``env-board-controls`` mod. Collection is injected (``runner``,
``availability``, ``clock``) so tests never spawn anything. Runtime state lives
under ``${XDG_STATE_HOME:-~/.local/state}/env-board/`` and is not a
mise-managed dotfile. Shapes are declared in ``schemas/env_board.schema.json``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from dotfiles_py.generated.env_board_schema import (
    EnvBoardBalanceCache,
    EnvBoardBalanceReading,
    EnvBoardSettings,
)

MIN_INTERVAL_MIN = 5
MAX_INTERVAL_MIN = 240
DEFAULT_INTERVAL_MIN = 10
REFRESH_MIN_GAP_S = 60
REFRESH_FUTURE_SLACK_S = 120
TASK_TIMEOUT_S = 90
TERM_GRACE_S = 20
TICK_S = 2.0
TASK_FILE = Path("tasks") / "api" / "balance"
ALT_TASK_FILE = Path("unix/home/.config/mise/tasks/api/balance")
UNAVAILABLE_TASK = "unavailable: api:balance task not installed"
UNAVAILABLE_BROWSE = "unavailable: browse CLI not installed"

DEFAULT_SETTINGS = EnvBoardSettings(
    balance_interval_min=DEFAULT_INTERVAL_MIN,
    balance_paused=False,
    refresh_requested_at=None,
)
EMPTY_CACHE = EnvBoardBalanceCache(read=None, last_attempt_at=None, last_error=None)

Runner = Callable[[Path], "tuple[EnvBoardBalanceReading | None, str | None]"]


def state_dir(env: Mapping[str, str] | None = None) -> Path:
    """``${XDG_STATE_HOME:-~/.local/state}/env-board``."""
    source = os.environ if env is None else env
    base = source.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "env-board"


def default_repo_root() -> Path:
    """Repo checkout containing this library (``lib`` is six levels below it)."""
    return Path(__file__).resolve().parents[6]


def ensure_private_dir(path: Path) -> bool:
    """Create/validate the state dir: not a symlink, owned by us; chmod 0700."""
    if path.is_symlink():
        return False
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode):
        return False
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)
    return True


def parse_iso(value: object) -> datetime | None:
    """Parse an ISO instant (``Z`` ok) to an aware datetime; None if invalid."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def clamp_interval(value: int) -> int:
    return max(MIN_INTERVAL_MIN, min(MAX_INTERVAL_MIN, value))


def load_settings(
    path: Path, last_good: EnvBoardSettings = DEFAULT_SETTINGS
) -> EnvBoardSettings:
    """Read live settings; every bad or missing field keeps its last good value.

    Integers outside 5..240 are clamped; non-integers, non-booleans, and
    unparsable instants fall back field by field.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return last_good
    if not isinstance(raw, dict):
        return last_good
    interval = last_good.balance_interval_min
    value = raw.get("balance_interval_min")
    if isinstance(value, int) and not isinstance(value, bool):
        interval = clamp_interval(value)
    paused = last_good.balance_paused
    if isinstance(raw.get("balance_paused"), bool):
        paused = raw["balance_paused"]
    requested = last_good.refresh_requested_at
    if "refresh_requested_at" in raw:
        field = raw["refresh_requested_at"]
        if field is None:
            requested = None
        elif parse_iso(field) is not None:
            requested = field
    return EnvBoardSettings(
        balance_interval_min=interval,
        balance_paused=paused,
        refresh_requested_at=requested,
    )


def load_cache(path: Path) -> EnvBoardBalanceCache:
    """Read the balance cache; absent or corrupt files read as empty."""
    try:
        return EnvBoardBalanceCache.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except OSError, ValueError, ValidationError:
        return EMPTY_CACHE


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically with mode 0600 (dir 0700)."""
    if not ensure_private_dir(path.parent):
        raise OSError("refusing unsafe state directory")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_cache(path: Path, cache: EnvBoardBalanceCache) -> None:
    atomic_write_text(path, cache.model_dump_json(indent=2) + "\n")


def check_available(repo_root: Path) -> str | None:
    """None when the task and ``browse`` exist, else the unavailable reason."""
    if (
        not (repo_root / TASK_FILE).is_file()
        and not (repo_root / ALT_TASK_FILE).is_file()
    ):
        return UNAVAILABLE_TASK
    if shutil.which("browse") is None:
        return UNAVAILABLE_BROWSE
    return None


def parse_task_output(
    stdout: str,
) -> tuple[EnvBoardBalanceReading | None, str | None]:
    """Parse ``api:balance --json`` stdout regardless of exit code."""
    payload: Any = None
    candidates = [stdout.strip()]
    candidates += [ln for ln in reversed(stdout.splitlines()) if ln.strip()][:1]
    for text in candidates:
        try:
            payload = json.loads(text)
            break
        except ValueError:
            continue
    if not isinstance(payload, dict):
        return None, "api:balance returned no JSON"
    if payload.get("ok") is not True:
        return None, str(payload.get("error") or "api:balance reported failure")[:300]
    body = {k: v for k, v in payload.items() if k != "ok"}
    try:
        return EnvBoardBalanceReading.model_validate(body), None
    except ValidationError:
        return None, "api:balance JSON shape changed"


def stop_group(proc: subprocess.Popen[str]) -> None:
    """SIGTERM the task's process group, wait, then SIGKILL so its trap runs."""
    for sig, wait in ((signal.SIGTERM, TERM_GRACE_S), (signal.SIGKILL, None)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError, PermissionError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def run_balance_task(
    repo_root: Path,
    timeout_s: float = TASK_TIMEOUT_S,
    popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
) -> tuple[EnvBoardBalanceReading | None, str | None]:
    """Run ``mise run -q api:balance --json``; (reading, None) or (None, error)."""
    try:
        proc = popen(
            ["mise", "run", "-q", "api:balance", "--json"],
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return None, f"could not start api:balance: {type(exc).__name__}"
    try:
        stdout, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        stop_group(proc)
        with contextlib.suppress(subprocess.TimeoutExpired, ValueError):
            proc.communicate(timeout=1)
        return None, f"api:balance timed out after {int(timeout_s)}s"
    return parse_task_output(stdout or "")


class BalanceScheduler:
    """Short-tick scheduler: re-reads settings every tick, calls the task rarely."""

    def __init__(
        self,
        *,
        state: Path | None = None,
        repo_root: Path | None = None,
        runner: Runner = run_balance_task,
        availability: Callable[[Path], str | None] = check_available,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.state = state or state_dir()
        self.repo_root = repo_root or default_repo_root()
        self.runner = runner
        self.availability = availability
        self.clock = clock
        self.settings = DEFAULT_SETTINGS
        self.unavailable: str | None = None
        self.running = False
        self._lock = threading.Lock()

    @property
    def settings_path(self) -> Path:
        return self.state / "settings.json"

    @property
    def cache_path(self) -> Path:
        return self.state / "balance-cache.json"

    @property
    def lock_path(self) -> Path:
        return self.state / "balance.lock"

    def _reference(self, cache: EnvBoardBalanceCache) -> datetime | None:
        points = [
            parse_iso(cache.read.read_at) if cache.read else None,
            parse_iso(cache.last_attempt_at),
        ]
        known = [p for p in points if p is not None]
        return max(known) if known else None

    def _valid_request(self, now: datetime) -> datetime | None:
        """The refresh request, unless absent, unparsable, or dated in the future."""
        requested = parse_iso(self.settings.refresh_requested_at)
        if requested is None:
            return None
        if (requested - now).total_seconds() > REFRESH_FUTURE_SLACK_S:
            return None
        return requested

    def _request_is_fresh(self, cache: EnvBoardBalanceCache, now: datetime) -> bool:
        """True when the request is newer than both the last attempt and last served one."""
        requested = self._valid_request(now)
        if requested is None:
            return False
        for seen in (
            parse_iso(cache.last_attempt_at),
            parse_iso(cache.last_served_request),
        ):
            if seen is not None and requested <= seen:
                return False
        return True

    def _due(self, cache: EnvBoardBalanceCache, now: datetime) -> bool:
        attempt = parse_iso(cache.last_attempt_at)
        if self._request_is_fresh(cache, now):
            gap_ok = (
                attempt is None or (now - attempt).total_seconds() >= REFRESH_MIN_GAP_S
            )
            if gap_ok:
                return True
        if self.settings.balance_paused:
            return False
        ref = self._reference(cache)
        interval = timedelta(minutes=self.settings.balance_interval_min)
        return ref is None or now - ref >= interval

    def tick(self) -> bool:
        """One scheduler step; True when the task was attempted."""
        self.settings = load_settings(self.settings_path, self.settings)
        self.unavailable = self.availability(self.repo_root)
        if self.unavailable is not None:
            return False
        now = self.clock()
        if not self._due(load_cache(self.cache_path), now):
            return False
        if not self._lock.acquire(blocking=False):
            return False
        try:
            return self._attempt()
        finally:
            self._lock.release()

    def _attempt(self) -> bool:
        if not ensure_private_dir(self.state):
            return False
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            cache = load_cache(self.cache_path)
            if not self._due(cache, self.clock()):
                return False
            save_cache(
                self.cache_path,
                cache.model_copy(update={"last_attempt_at": iso(self.clock())}),
            )
            self.running = True
            try:
                reading, error = self.runner(self.repo_root)
            except Exception as exc:
                reading, error = None, f"api:balance failed: {type(exc).__name__}"
            finally:
                self.running = False
            served = cache.last_served_request
            if self._valid_request(self.clock()) is not None:
                served = self.settings.refresh_requested_at
            save_cache(
                self.cache_path,
                EnvBoardBalanceCache(
                    read=reading or cache.read,
                    last_attempt_at=iso(self.clock()),
                    last_error=None if reading else (error or "unknown error"),
                    last_served_request=served,
                ),
            )
            return True
        finally:
            os.close(fd)

    def view(self) -> dict[str, Any]:
        """Plain snapshot for the pure renderer (cheap: one small JSON read)."""
        cache = load_cache(self.cache_path)
        ref = self._reference(cache)
        due = (
            None
            if ref is None
            else ref + timedelta(minutes=self.settings.balance_interval_min)
        )
        return {
            "unavailable": self.unavailable,
            "reading": cache.read.model_dump(mode="json") if cache.read else None,
            "error": cache.last_error,
            "interval_min": self.settings.balance_interval_min,
            "paused": self.settings.balance_paused,
            "next_due": None if due is None else iso(due),
            "running": self.running,
        }


class BalanceTicker:
    """Daemon thread driving ``BalanceScheduler.tick`` every ``period_s``."""

    def __init__(self, scheduler: BalanceScheduler, period_s: float = TICK_S) -> None:
        self.scheduler = scheduler
        self.period_s = period_s
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.scheduler.tick()
            except Exception as exc:
                # A scheduler bug must never kill the board; the next tick retries.
                self.last_error = type(exc).__name__
            self._stop.wait(self.period_s)
