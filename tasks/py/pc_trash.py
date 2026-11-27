"""Soft-delete one path on this host by moving it into the freedesktop Trash.

`mise run pc:trash <path>` streams this file to the host as `python3 -I -B - <path>` (#116).
`~/` and relative paths resolve against $HOME, and only paths inside $HOME are accepted.
The item lands in ~/.local/share/Trash/files/<name>.<timestamp> next to a matching
info/<name>.<timestamp>.trashinfo, so desktop trash tools can list and restore it.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

NS = 1_000_000_000


def stamp(ns: int) -> str:
    """Return local time `YYYYmmddTHHMMSS.NNNNNNNNN±HHMM` for an epoch in nanoseconds."""
    moment = datetime.fromtimestamp(ns // NS).astimezone()
    return f"{moment:%Y%m%dT%H%M%S}.{ns % NS:09d}{moment:%z}"


def resolve(raw: str, home: Path) -> Path:
    """Absolute path for *raw*; the last component stays unresolved so a symlink is trashed itself."""
    if not raw.strip():
        raise ValueError("empty path")
    if raw == "~" or raw.startswith("~/"):
        raw = str(home) + raw[1:]
    elif raw.startswith("~"):
        raise ValueError(f"{raw}: ~user paths are not supported")
    path = Path(os.path.normpath(home / raw))
    if path == Path(path.anchor):
        raise ValueError(f"{path}: refusing to trash the filesystem root")
    return path.parent.resolve(strict=True) / path.name


def reserve(files: Path, info: Path, base: str) -> tuple[str, int]:
    """Claim a free Trash name by creating its `.trashinfo` exclusively (freedesktop Trash spec)."""
    for n in range(100):
        name = base if n == 0 else f"{base}.{n}"
        if os.path.lexists(files / name):
            continue
        try:
            fd = os.open(
                info / f"{name}.trashinfo", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            continue
        return name, fd
    raise ValueError(f"no free Trash name for {base}")


def trash(raw: str, home: Path, now_ns: int) -> Path:
    """Move *raw* into the home Trash and return its new location."""
    home = home.resolve(strict=True)
    src = resolve(raw, home)
    if not os.path.lexists(src):
        raise ValueError(f"{src}: no such file or directory")
    if src == home or not src.is_relative_to(home):
        raise ValueError(f"{src}: only paths inside {home} can be trashed")
    trash_dir = home / ".local/share/Trash"
    files, info = trash_dir / "files", trash_dir / "info"
    if trash_dir.resolve().is_relative_to(src):
        raise ValueError(f"{src}: contains the Trash itself")
    if src.is_relative_to(trash_dir.resolve()):
        raise ValueError(f"{src}: already in the Trash")
    for folder in (files, info):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    name, fd = reserve(files, info, f"{src.name}.{stamp(now_ns)}")
    record = info / f"{name}.trashinfo"
    deleted = datetime.fromtimestamp(now_ns // NS).strftime("%Y-%m-%dT%H:%M:%S")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"[Trash Info]\nPath={quote(str(src))}\nDeletionDate={deleted}\n")
    dest = files / name
    try:
        os.rename(src, dest)
    except OSError:
        record.unlink(missing_ok=True)
        raise
    return dest


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: python3 -I -B - <path> <pc_trash.py\n")
        return 2
    try:
        dest = trash(argv[0], Path.home(), time.time_ns())
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"pc:trash: {exc}\n")
        return 1
    sys.stdout.write(f"pc:trash: moved {argv[0]} to {dest}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
