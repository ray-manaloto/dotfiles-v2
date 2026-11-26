"""Exact-file tools. The model supplies data, never executable arguments."""

from __future__ import annotations

import asyncio
import os
import re
import stat
import subprocess
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

MAX_FILE_BYTES = 16_384
MAX_LINT_BYTES = 4096
LINT_SECONDS = 10
CHILD_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "NO_COLOR": "1"}
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class ScopeError(ValueError):
    """The requested operation exceeds the local tool policy."""


def check_worktree(root: Path) -> Path:
    root = root.resolve(strict=True)
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        env=CHILD_ENV,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode or Path(result.stdout.rstrip("\n")) != root:
        raise ScopeError("Select the root of a Git worktree.")
    return root


def bounded_text(stream: BinaryIO, limit: int) -> str:
    data = stream.read(limit + 1)
    if len(data) > limit or b"\0" in data:
        raise ScopeError("Expected bounded UTF-8 text without NUL bytes.")
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise ScopeError("Expected UTF-8 text.") from None


class Worktree:
    def __init__(self, root_fd: int, files: list[str], zizmor: Path | None) -> None:
        self.root_fd = root_fd
        self.files = frozenset(files)
        self.zizmor = zizmor
        for path in files:
            if any(
                not re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_.-]*", part)
                or part.casefold() in {".", "..", ".git"}
                for part in path.split("/")
            ):
                raise ScopeError("Approve canonical, worktree-relative file paths.")
            self.read(path)

    @contextmanager
    def _parent(self, path: str) -> Generator[tuple[int, str]]:
        if path not in self.files:
            raise ScopeError("File is outside the approved list.")
        *dirs, name = path.split("/")
        fd = os.dup(self.root_fd)
        try:
            for dirname in dirs:
                child_fd = os.open(dirname, _DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child_fd
            yield fd, name
        finally:
            os.close(fd)

    @staticmethod
    def _read(fd: int, name: str) -> tuple[str, os.stat_result]:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(name, flags, dir_fd=fd), "rb") as stream:
            details = os.fstat(stream.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                raise ScopeError("Only singly linked regular files are allowed.")
            return bounded_text(stream, MAX_FILE_BYTES), details

    def read(self, path: str) -> str:
        with self._parent(path) as (fd, name):
            return self._read(fd, name)[0]

    def replace(self, path: str, old: str, new: str) -> str:
        with self._parent(path) as (fd, name):
            text, original = self._read(fd, name)
            # Look again at offset +1 so overlapping matches are ambiguous too.
            start = text.find(old)
            if not old or start < 0 or text.find(old, start + 1) >= 0:
                raise ScopeError("The old text must appear exactly once.")
            updated = (text[:start] + new + text[start + len(old) :]).encode("utf-8")
            if len(updated) > MAX_FILE_BYTES or b"\0" in updated:
                raise ScopeError("The edit exceeds the text file policy.")
            if old == new:
                return "No change."

            temp = f".responses-edit-{uuid4().hex}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            handle = os.open(temp, flags, 0o600, dir_fd=fd)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(updated)
                    os.fchmod(stream.fileno(), original.st_mode & 0o777)
                current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (
                    current.st_dev,
                    current.st_ino,
                    current.st_ctime_ns,
                ) != (original.st_dev, original.st_ino, original.st_ctime_ns):
                    raise ScopeError("The file changed during the edit. Read it again.")
                os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temp, dir_fd=fd)
        return "Updated."

    async def lint(self, path: str) -> str:
        if not re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", path):
            raise ScopeError("Lint accepts an approved GitHub Actions workflow.")
        source = self.read(path).encode("utf-8")
        if self.zizmor is None:
            raise ScopeError("The managed zizmor executable is unavailable.")
        async with asyncio.timeout(LINT_SECONDS):
            process = await asyncio.create_subprocess_exec(
                str(self.zizmor),
                "--offline",
                "--no-config",
                "--pedantic",
                "--strict-collection",
                "--",
                "-",
                cwd="/",
                env=CHILD_ENV,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert process.stdin is not None and process.stdout is not None
            try:
                process.stdin.write(source)
                process.stdin.close()
                try:
                    await process.stdout.readexactly(MAX_LINT_BYTES + 1)
                except asyncio.IncompleteReadError as error:
                    code = await process.wait()
                    return f"zizmor exit {code}\n{error.partial.decode('utf-8', errors='replace')}"
                return "Lint stopped after exceeding its output limit."
            finally:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                    # Discard bounded chunks while draining after termination.
                    while await process.stdout.read(MAX_LINT_BYTES):
                        pass
                    await process.wait()


@contextmanager
def open_worktree(
    root: Path, files: list[str], zizmor: Path | None
) -> Generator[Worktree]:
    root = check_worktree(root)
    if zizmor is not None:
        zizmor = zizmor.resolve(strict=True)
        if zizmor.is_relative_to(root) or not zizmor.is_file():
            raise ScopeError("Use the managed zizmor executable outside the worktree.")
    fd = os.open(root, _DIR_FLAGS)
    try:
        yield Worktree(fd, files, zizmor)
    finally:
        os.close(fd)
