"""Soft-delete one path on this host by moving it into the freedesktop Trash.

`mise run pc:trash <path>` streams this file to the host as `python3 -I -B - <path>` (#116).
`~/` and relative paths resolve against $HOME, and only paths at least two levels inside
$HOME are accepted. dcg sees only `mise run pc:trash ...`, so this script is the guard: it
refuses the paths `custom.mise_protect` and AGENTS.md mark critical (PROTECTED), anything
inside them, and anything that contains them.
The item lands in ~/.local/share/Trash/files/<name>.<timestamp> next to a matching
info/<name>.<timestamp>.trashinfo, so desktop trash tools can list and restore it.
The move is a rename, so a path on another filesystem than the Trash is refused (EXDEV).
"""

from __future__ import annotations

import errno
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

NS = 1_000_000_000
# Relative to $HOME: credentials, the mise toolchain, the dcg policy and the dotfiles checkout.
PROTECTED = (
    ".ssh",
    ".gnupg",
    ".config/fnox",
    ".config/mise",
    ".config/dcg",
    ".local/bin",
    ".local/share/mise",
    ".local/share/Trash",
    "dev/dotfiles-v2",
)


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


def refuse_protected(src: Path, home: Path) -> None:
    """Raise unless *src* is a scratch path: deep inside *home* and clear of PROTECTED."""
    if src == home or not src.is_relative_to(home):
        raise ValueError(f"{src}: only paths inside {home} can be trashed")
    if src.parent == home:
        raise ValueError(f"{src}: top-level entries of {home} cannot be trashed")
    for rel in PROTECTED:
        lexical = home / rel
        # Also the target, so trashing the checkout behind a ~/.config/mise symlink is refused.
        for guarded in dict.fromkeys((lexical, lexical.resolve())):
            if src.is_relative_to(guarded):
                raise ValueError(f"{src}: inside protected ~/{rel}")
            if guarded.is_relative_to(src):
                raise ValueError(f"{src}: contains protected ~/{rel}")


def trash(raw: str, home: Path, now_ns: int) -> Path:
    """Move *raw* into the home Trash and return its new location."""
    home = home.resolve(strict=True)
    src = resolve(raw, home)
    if not os.path.lexists(src):
        raise ValueError(f"{src}: no such file or directory")
    refuse_protected(src, home)
    trash_dir = home / ".local/share/Trash"
    files, info = trash_dir / "files", trash_dir / "info"
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
    except OSError as exc:
        record.unlink(missing_ok=True)
        if exc.errno == errno.EXDEV:
            raise ValueError(f"{src}: on another filesystem than {trash_dir}") from exc
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
