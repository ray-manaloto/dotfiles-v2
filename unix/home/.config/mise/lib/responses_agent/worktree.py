"""Exact-file tools. The model supplies data, never executable arguments."""

from __future__ import annotations

import asyncio
import os
import re
import stat
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager, suppress
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO
from uuid import uuid4

MAX_FILE_BYTES = 16_384
MAX_LINT_BYTES = 4096
LINT_SECONDS = 10
CHILD_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "NO_COLOR": "1"}
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_RESERVED_FILES = {"mise.toml", "hk.pkl", ".gitattributes", ".gitmodules"}
# The only executable target code is the reviewed hk-config #6 suite and its module.
_REVIEW_TEST_FILES = {
    "review.py": "e09b0d9f316dbfc204e284f9c9cdbebe0d1688072fb4fc3e01f5984c6a459ed2",
    "test_review.py": "9635c15248f27cc14645787ee70cda906dbc7e861f7273451c66e410ba7cf725",
}


class ScopeError(ValueError):
    """The requested operation exceeds the local tool policy."""


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), "-c", "core.fsmonitor=false", *args],
        env=CHILD_ENV,
        capture_output=True,
        timeout=5,
        check=False,
    )
    if result.returncode:
        raise ScopeError("A required Git inspection failed.")
    return result.stdout


def check_worktree(root: Path) -> str:
    details = (
        git(
            root,
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
            "HEAD",
        )
        .decode()
        .splitlines()
    )
    if len(details) != 4:
        raise ScopeError("Could not identify the Git worktree and commit.")
    toplevel, git_dir, common_dir, commit = details
    if Path(toplevel) != root or Path(git_dir).resolve() == Path(common_dir).resolve():
        raise ScopeError("Select the root of a disposable linked Git worktree.")
    if git(root, "rev-parse", "--abbrev-ref", "HEAD").strip() != b"HEAD":
        raise ScopeError("The disposable worktree must have a detached HEAD.")
    if git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ):
        raise ScopeError("The disposable worktree must be clean at the start.")
    return commit


def bounded_text(stream: BinaryIO, limit: int) -> str:
    data = stream.read(limit + 1)
    if len(data) > limit or b"\0" in data:
        raise ScopeError("Expected bounded UTF-8 text without NUL bytes.")
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise ScopeError("Expected UTF-8 text.") from None


class Worktree:
    def __init__(
        self,
        root: Path,
        root_fd: int,
        commit: str,
        files: list[str],
        zizmor: Path | None,
    ) -> None:
        self.root = root
        self.root_fd = root_fd
        self.commit = commit
        self.files = frozenset(files)
        self.zizmor = zizmor
        self.results: list[str] = []
        for path in files:
            if any(
                not re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_.-]*", part)
                or part.casefold() in {".", "..", ".git"}
                for part in path.split("/")
            ):
                raise ScopeError("Approve canonical, worktree-relative file paths.")
            if Path(path).name.casefold() in _RESERVED_FILES:
                raise ScopeError("Repository control files cannot be approved.")
            if not git(root, "ls-files", "--", path):
                raise ScopeError("Only tracked files can be approved.")
            self.read(path)

    def patch(self) -> bytes:
        if git(self.root, "rev-parse", "HEAD").decode().strip() != self.commit:
            raise ScopeError("The target commit changed during the run.")
        return git(
            self.root,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--no-color",
            "--binary",
            self.commit,
            "--",
            *sorted(self.files),
        )

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
        return await self._check(
            f"zizmor ({path})",
            str(self.zizmor),
            "--offline",
            "--no-config",
            "--pedantic",
            "--strict-collection",
            "--",
            "-",
            source=source,
        )

    async def test_reviewer(self) -> str:
        # Copy only verified Git blobs. Do not import from the model's editable tree.
        with TemporaryDirectory(prefix="responses-review-tests-") as directory:
            for name, expected in _REVIEW_TEST_FILES.items():
                data = git(
                    self.root,
                    "show",
                    "--no-textconv",
                    f"{self.commit}:.github/openai-review/{name}",
                )
                if sha256(data).hexdigest() != expected:
                    raise ScopeError(
                        "This worktree does not contain the approved reviewer suite."
                    )
                (Path(directory) / name).write_bytes(data)
            return await self._check(
                "reviewer unittest",
                sys.executable,
                "-I",
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                directory,
                "-p",
                "test_review.py",
            )

    async def _check(self, label: str, *argv: str, source: bytes = b"") -> str:
        try:
            result = await self._check_output(*argv, source=source)
        except TimeoutError:
            result = "Stopped after exceeding the command's time limit."
        except asyncio.CancelledError:
            self.results.append(f"{label}: Cancelled before a result was available.")
            raise
        except OSError:
            result = "Could not start or read the command."
        output = f"{label}: {result}"
        self.results.append(output)
        return output

    @staticmethod
    async def _check_output(*argv: str, source: bytes) -> str:
        async with asyncio.timeout(LINT_SECONDS):
            process = await asyncio.create_subprocess_exec(
                *argv,
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
                    return f"exit {code}\n{error.partial.decode('utf-8', errors='replace')}"
                return "Stopped after exceeding the command's output limit."
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
    root = root.resolve(strict=True)
    commit = check_worktree(root)
    if zizmor is not None:
        zizmor = zizmor.resolve(strict=True)
        if zizmor.is_relative_to(root) or not zizmor.is_file():
            raise ScopeError("Use the managed zizmor executable outside the worktree.")
    fd = os.open(root, _DIR_FLAGS)
    try:
        yield Worktree(root, fd, commit, files, zizmor)
    finally:
        os.close(fd)
