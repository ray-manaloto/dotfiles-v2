"""Stdlib-only lookups shared by `plugins:update` and the warn-only `plugin-drift` doctor check."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

_DIGITS_RE = re.compile(r"\d+")
CACHE_TTL_SECONDS = 6 * 60 * 60

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class LookupFailure(Exception):
    """A degraded lookup (tool missing, command failed, unparsable output)."""

    def __init__(self, message: str, code: str = "ERR_UNKNOWN") -> None:
        super().__init__(message)
        self.code = code


def run_cmd(
    argv: Sequence[str], timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    """Run *argv* without a shell; a missing binary raises `LookupFailure`."""
    if shutil.which(argv[0]) is None:
        raise LookupFailure(f"{argv[0]} not found on PATH", "ERR_TOOL_MISSING")
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LookupFailure(f"{argv[0]} failed: {exc}", "ERR_UNKNOWN") from exc


def parse_version(text: str) -> tuple[int, ...] | None:
    nums = _DIGITS_RE.findall(text.split("-", 1)[0].split("+", 1)[0])
    return tuple(int(n) for n in nums) if nums else None


def version_lt(installed: str, latest: str) -> bool:
    """True when *installed* is strictly older than *latest*; raises on unparsable input."""
    a, b = parse_version(installed), parse_version(latest)
    if a is None or b is None:
        raise LookupFailure(f"cannot compare versions {installed!r} vs {latest!r}")
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) < b + (0,) * (width - len(b))


_VERSION_RE = re.compile(r"\d+(\.\d+){1,3}")


def validate_version(text: str, what: str) -> str:
    """Accept only plain dotted numeric versions; anything else fails closed."""
    if not _VERSION_RE.fullmatch(text):
        raise LookupFailure(
            f"{what} version {text[:60]!r} is not a plain dotted number; refusing",
            "ERR_INVALID_INPUT",
        )
    return text


def latest_release(
    repo: str, tag_prefix: str = "v", runner: Runner = run_cmd
) -> tuple[str, str, str]:
    """Return `(tag, commit_sha, version)` for the latest GitHub release of *repo*."""
    proc = runner(["gh", "api", f"repos/{repo}/releases/latest", "--jq", ".tag_name"])
    tag = proc.stdout.strip()
    if proc.returncode != 0 or not tag:
        raise LookupFailure(
            f"gh release lookup failed for {repo}: {proc.stderr.strip()[:200]}",
            "ERR_GITHUB_LOOKUP_FAILED",
        )
    version = validate_version(
        tag[len(tag_prefix) :] if tag_prefix and tag.startswith(tag_prefix) else tag,
        "release tag",
    )
    proc = runner(["gh", "api", f"repos/{repo}/commits/{tag}", "--jq", ".sha"])
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise LookupFailure(
            f"gh could not resolve tag {tag} of {repo} to a commit",
            "ERR_GITHUB_LOOKUP_FAILED",
        )
    return tag, sha, version


def codex_installed_version(
    plugin: str, marketplace: str, runner: Runner = run_cmd
) -> str | None:
    """Installed Codex version of `plugin@marketplace`, or None when not installed."""
    proc = runner(["codex", "plugin", "list", "-m", marketplace, "--json"])
    if proc.returncode != 0:
        raise LookupFailure(
            f"codex plugin list failed: {proc.stderr.strip()[:200]}",
            "ERR_CODEX_LIST_FAILED",
        )
    try:
        data = json.loads(proc.stdout)
        rows = data["installed"]
    except (ValueError, KeyError, TypeError) as exc:
        raise LookupFailure(
            f"codex plugin list emitted unparsable JSON: {exc}", "ERR_CODEX_LIST_FAILED"
        ) from exc
    want = f"{plugin}@{marketplace}"
    for row in rows:
        if isinstance(row, Mapping) and row.get("pluginId") == want:
            ver = row.get("version")
            return str(ver) if ver else None
    return None


def claude_user_install(
    plugin: str, marketplace: str, runner: Runner = run_cmd
) -> tuple[str, str] | None:
    """`(version, installPath)` of the Claude user-scope install, or None when absent."""
    proc = runner(["claude", "plugin", "list", "--json"])
    if proc.returncode != 0:
        raise LookupFailure(
            f"claude plugin list failed: {proc.stderr.strip()[:200]}",
            "ERR_CLAUDE_LIST_FAILED",
        )
    try:
        rows = json.loads(proc.stdout)
        if not isinstance(rows, list):
            raise TypeError("expected a JSON list")
    except (ValueError, TypeError) as exc:
        raise LookupFailure(
            f"claude plugin list emitted unparsable JSON: {exc}",
            "ERR_CLAUDE_LIST_FAILED",
        ) from exc
    want = f"{plugin}@{marketplace}"
    for row in rows:
        if (
            isinstance(row, Mapping)
            and row.get("id") == want
            and row.get("scope") == "user"
        ):
            return str(row.get("version") or ""), str(row.get("installPath") or "")
    return None


def drift_cache_path(cache_home: Path | None = None) -> Path:
    base = cache_home if cache_home is not None else Path.home() / ".cache"
    return base / "dotfiles-v2" / "plugin-drift.json"


def cached_latest(
    repo: str,
    tag_prefix: str,
    *,
    cache_file: Path,
    now: float | None = None,
    runner: Runner = run_cmd,
) -> str:
    """Latest release version for *repo*, cached on disk for six hours."""
    stamp = time.time() if now is None else now
    cache: dict[str, dict[str, object]] = {}
    try:
        loaded = json.loads(cache_file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            cache = loaded
    except OSError, ValueError:
        cache = {}
    hit = cache.get(repo)
    if isinstance(hit, dict):
        hit_version = hit.get("version")
        hit_at = hit.get("at")
        if (
            isinstance(hit_version, str)
            and isinstance(hit_at, (int, float))
            and 0 <= stamp - float(hit_at) < CACHE_TTL_SECONDS
        ):
            return hit_version
    proc = runner(["gh", "api", f"repos/{repo}/releases/latest", "--jq", ".tag_name"])
    tag = proc.stdout.strip()
    if proc.returncode != 0 or not tag:
        raise LookupFailure(
            f"gh release lookup failed for {repo}", "ERR_GITHUB_LOOKUP_FAILED"
        )
    version = (
        tag[len(tag_prefix) :] if tag_prefix and tag.startswith(tag_prefix) else tag
    )
    cache[repo] = {"version": version, "at": stamp}
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass
    return version
