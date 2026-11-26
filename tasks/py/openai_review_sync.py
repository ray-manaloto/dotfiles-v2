"""Copy the canonical OpenAI review files or check KB main over read-only HTTPS."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

KB_REPO = "ray-manaloto/knowledge-base-v2"
PIPELINE = Path(".github/openai-review")
SHARED_FILES = (
    "autofix-prompt.md",
    "autofix.py",
    "findings.schema.json",
    "repo.schema.json",
    "repo_config.py",
    "review.py",
    "test_autofix.py",
    "test_review.py",
    "test_triage.py",
    "triage.py",
)


class UpstreamUnavailable(RuntimeError):
    """The remote revision could not be checked."""


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL
    ).strip()


def sync(source: Path, target: Path) -> None:
    target = target.resolve()
    root = Path(git(target, "rev-parse", "--show-toplevel"))
    git_dir = git(target, "rev-parse", "--absolute-git-dir")
    common = git(target, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if target != root or git_dir == common:
        raise ValueError("Pass the root of a linked KB worktree.")
    url = git(target, "remote", "get-url", "origin").rstrip("/").removesuffix(".git")
    if url not in {
        f"https://github.com/{KB_REPO}",
        f"git@github.com:{KB_REPO}",
        f"ssh://git@github.com/{KB_REPO}",
    }:
        raise ValueError("The target's origin must be ray-manaloto/knowledge-base-v2.")
    directory = target / PIPELINE
    if not directory.is_dir() or directory.resolve() != directory:
        raise ValueError("The target pipeline directory must exist without symlinks.")
    changed = []
    for name in SHARED_FILES:
        path = directory / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"Unsafe target: {name}.")
        data = (source / name).read_bytes()
        if git(target, "diff", "--cached", "--name-only", "--", str(path)):
            staged = subprocess.run(
                ["git", "-C", str(target), "show", f":{PIPELINE / name}"],
                capture_output=True,
                check=False,
            )
            if staged.returncode or staged.stdout != data:
                raise ValueError(f"Target has a different staged version: {name}.")
        if path.exists() and path.read_bytes() == data:
            continue
        if git(
            target, "status", "--porcelain", "--untracked-files=all", "--", str(path)
        ):
            raise ValueError(f"Target has an uncommitted change: {name}.")
        if path.exists() and path.stat().st_nlink != 1:
            raise ValueError(f"Unsafe hard-linked target: {name}.")
        changed.append((path, data))
    for path, data in changed:
        path.write_bytes(data)
    for name in SHARED_FILES:
        if (directory / name).read_bytes() != (source / name).read_bytes():
            raise ValueError(f"Copy verification failed: {name}.")
    sys.stdout.write(f"KB worktree matches all {len(SHARED_FILES)} shared files.\n")


def gh(endpoint: str, *, missing_file: bool = False) -> dict | None:
    try:
        result = subprocess.run(
            ["gh", "api", "--hostname", "github.com", "--method", "GET", endpoint],
            capture_output=True,
            timeout=15,
            check=False,
            env={**os.environ, "GH_PROMPT_DISABLED": "1"},
        )
    except FileNotFoundError as exc:
        raise UpstreamUnavailable("gh is unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpstreamUnavailable("GitHub API request timed out") from exc
    if not result.returncode:
        return json.loads(result.stdout)
    error = result.stderr.decode("utf-8", errors="replace").lower()
    status = re.search(r"\(http (\d{3})\)", error)
    code = status[1] if status else ""
    if code == "404" and missing_file:
        return None
    if code in {"401", "403", "404", "429"} or code.startswith("5"):
        raise UpstreamUnavailable(f"GitHub API unavailable (HTTP {code})")
    if result.returncode == 4 or "gh auth login" in error:
        raise UpstreamUnavailable("gh is unauthenticated for github.com")
    if any(
        text in error
        for text in (
            "error connecting to",
            "no such host",
            "connection refused",
            "connection reset",
            "network is unreachable",
            "network is down",
            "no route to host",
            "i/o timeout",
        )
    ):
        raise UpstreamUnavailable("cannot connect to the GitHub API")
    raise ValueError(f"GitHub API read failed (HTTP {code or 'unknown'}).")


def check(source: Path) -> int:
    if os.environ.get("MISE_OFFLINE", "").lower() in {"1", "true", "yes", "on"}:
        raise UpstreamUnavailable("MISE_OFFLINE is enabled")
    root = f"repos/{KB_REPO}"
    commit = gh(f"{root}/commits/main")
    if commit is None or not re.fullmatch(r"[0-9a-f]{40}", commit["sha"]):
        raise ValueError("GitHub returned an invalid KB main revision.")
    revision = commit["sha"]
    differences = []
    for name in SHARED_FILES:
        result = gh(
            f"{root}/contents/{PIPELINE}/{name}?ref={revision}", missing_file=True
        )
        if result is None:
            differences.append(f"{name}: missing from KB")
            continue
        if result["type"] != "file" or result["encoding"] != "base64":
            raise ValueError(f"Unexpected GitHub file response: {name}.")
        data = base64.b64decode(result["content"].replace("\n", ""), validate=True)
        if len(data) != result["size"]:
            raise ValueError(f"Incomplete GitHub file response: {name}.")
        if data != (source / name).read_bytes():
            differences.append(f"{name}: differs from KB")
    if differences:
        sys.stderr.write(f"KB main {revision}:\n" + "\n".join(differences) + "\n")
        return 1
    sys.stdout.write(
        f"KB main {revision} matches all {len(SHARED_FILES)} shared files.\n"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kb_worktree", type=Path, nargs="?")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[2] / PIPELINE
    try:
        if args.kb_worktree is None:
            return check(source)
        sync(source, args.kb_worktree)
    except UpstreamUnavailable as exc:
        sys.stdout.write(f"skipped: {exc}\n")
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
    ) as exc:
        sys.stderr.write(f"KB sync: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
