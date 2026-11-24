#!/usr/bin/env -S python3 -I -B
"""Sync Antigravity CLI (agy) global planning-with-files skill from active Codex installation."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import importlib
import json
import os
import plistlib
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

DEFAULT_TRUSTED_SOURCE = "https://github.com/OthmanAdi/planning-with-files.git"
PLUGIN_KEY = "planning-with-files@planning-with-files"
MARKETPLACE_KEY = "planning-with-files"
SKILL_NAME = "planning-with-files"
HOOK_NAME = "agy-pwf-sync"
LAUNCHAGENT_LABEL = "dev.agy.pwf-sync"
DEFAULT_HOOK_SCRIPT_EXPR = '"$HOME/.gemini/config/hooks/agy-pwf-sync.py"'
AGY_SKILL_BODY_CAP_BYTES = 24000
TAIL_OVERLAP_LINES = 15
MAX_EXPORT_FILES = 500
MAX_EXPORT_BYTES = 20 * 1024 * 1024
SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
FORBIDDEN_PATH_PARTS_LOWER = frozenset(
    {".git", ".gitmodules", ".gitconfig", ".claude-plugin", "hooks.json"}
)
ALLOWED_GIT_MODES = frozenset({"100644", "100755"})
HOOK_EXPECTED_ERRORS = (
    OSError,
    UnicodeDecodeError,
    ValueError,
    KeyError,
    TypeError,
    RuntimeError,
    json.JSONDecodeError,
    subprocess.SubprocessError,
)
REQUIRED_REL_FILES = (
    "SKILL.md",
    "scripts/resolve-plan-dir.sh",
    "scripts/attest-plan.sh",
    "scripts/init-session.sh",
    "scripts/check-complete.sh",
    "templates/task_plan.md",
    "templates/findings.md",
    "templates/progress.md",
)
EXECUTABLE_REL_FILES = (
    "scripts/init-session.sh",
    "scripts/check-complete.sh",
)


class SyncBlockedError(RuntimeError):
    """Fail-closed sync error with stable classification code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def utc_now_iso() -> str:
    return datetime.now(timezone(timedelta(0))).strftime("%Y-%m-%dT%H:%M:%SZ")


class _MiniTomlParser:
    """Minimal stdlib TOML parser fallback for Python 3.9/3.10 without `tomllib`."""

    @staticmethod
    def _strip_comment(line: str) -> str:
        in_single = False
        in_double = False
        escaped = False
        for idx, ch in enumerate(line):
            if escaped:
                escaped = False
                continue
            if in_double:
                if ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_double = False
                continue
            if in_single:
                if ch == "'":
                    in_single = False
                continue
            if ch == '"':
                in_double = True
            elif ch == "'":
                in_single = True
            elif ch == "#":
                return line[:idx]
        return line

    @staticmethod
    def _parse_key_segment(raw: str, pos: int) -> tuple[str, int]:
        n = len(raw)
        while pos < n and raw[pos] in " \t":
            pos += 1
        if pos >= n:
            raise ValueError("Empty TOML key segment")
        if raw[pos] == '"':
            pos += 1
            out: list[str] = []
            while pos < n:
                ch = raw[pos]
                if ch == "\\":
                    pos += 1
                    if pos >= n:
                        raise ValueError("Unterminated escape in quoted key")
                    esc = raw[pos]
                    out.append(
                        {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}.get(
                            esc, esc
                        )
                    )
                elif ch == '"':
                    return "".join(out), pos + 1
                else:
                    out.append(ch)
                pos += 1
            raise ValueError("Unterminated double-quoted key")
        if raw[pos] == "'":
            end = raw.find("'", pos + 1)
            if end == -1:
                raise ValueError("Unterminated single-quoted key")
            return raw[pos + 1 : end], end + 1
        start = pos
        while pos < n and re.match(r"[A-Za-z0-9_-]", raw[pos]):
            pos += 1
        if pos == start:
            raise ValueError(f"Invalid TOML bare key at {raw[pos:]!r}")
        return raw[start:pos], pos

    @classmethod
    def _parse_dotted_key(cls, raw: str) -> list[str]:
        parts: list[str] = []
        pos = 0
        n = len(raw)
        while True:
            seg, pos = cls._parse_key_segment(raw, pos)
            parts.append(seg)
            while pos < n and raw[pos] in " \t":
                pos += 1
            if pos >= n:
                return parts
            if raw[pos] != ".":
                raise ValueError(f"Unexpected trailing characters in key {raw!r}")
            pos += 1

    @classmethod
    def _split_top_level(cls, raw: str) -> list[str]:
        items: list[str] = []
        cur: list[str] = []
        depth = 0
        in_single = False
        in_double = False
        escaped = False
        for ch in raw:
            if escaped:
                cur.append(ch)
                escaped = False
                continue
            if in_double:
                cur.append(ch)
                if ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_double = False
                continue
            if in_single:
                cur.append(ch)
                if ch == "'":
                    in_single = False
                continue
            if ch == '"':
                in_double = True
            elif ch == "'":
                in_single = True
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth < 0:
                    raise ValueError(f"Unbalanced closing delimiter in {raw!r}")
            elif ch == "," and depth == 0:
                piece = "".join(cur).strip()
                if piece:
                    items.append(piece)
                cur = []
                continue
            cur.append(ch)
        if depth != 0 or in_single or in_double:
            raise ValueError(f"Unterminated container or string in {raw!r}")
        tail = "".join(cur).strip()
        if tail:
            items.append(tail)
        return items

    @classmethod
    def _parse_value(cls, raw: str) -> Any:
        val = raw.strip()
        if not val:
            raise ValueError("Empty TOML value")
        if val.startswith('"""') and val.endswith('"""') and len(val) >= 6:
            inner = val[3:-3]
            if inner.startswith("\n"):
                inner = inner[1:]
            return inner
        if val.startswith("'''") and val.endswith("'''") and len(val) >= 6:
            inner = val[3:-3]
            if inner.startswith("\n"):
                inner = inner[1:]
            return inner
        if val.startswith('"') and val.endswith('"'):
            return json.loads(val)
        if val.startswith("'") and val.endswith("'"):
            if "'" in val[1:-1]:
                raise ValueError(f"Invalid literal string: {val!r}")
            return val[1:-1]
        if val == "true":
            return True
        if val == "false":
            return False
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            if not inner:
                return []
            return [cls._parse_value(item) for item in cls._split_top_level(inner)]
        if val.startswith("{") and val.endswith("}"):
            inner = val[1:-1].strip()
            tbl: dict[str, Any] = {}
            if not inner:
                return tbl
            for entry in cls._split_top_level(inner):
                eq_idx = cls._find_assignment_eq(entry)
                if eq_idx < 0:
                    raise ValueError(f"Invalid inline table entry: {entry!r}")
                k_parts = cls._parse_dotted_key(entry[:eq_idx].strip())
                v_obj = cls._parse_value(entry[eq_idx + 1 :])
                cur_tbl = tbl
                for part in k_parts[:-1]:
                    nxt = cur_tbl.setdefault(part, {})
                    if not isinstance(nxt, dict):
                        raise ValueError(
                            f"Key conflict in inline table for {k_parts!r}"
                        )
                    cur_tbl = nxt
                cur_tbl[k_parts[-1]] = v_obj
            return tbl
        num_clean = val.replace("_", "")
        if re.fullmatch(r"[+-]?(?:0|[1-9][0-9]*)", num_clean):
            return int(num_clean, 10)
        if re.fullmatch(
            r"[+-]?(?:[0-9]+\.[0-9]+|[0-9]+(?:e[+-]?[0-9]+|\.[0-9]+e[+-]?[0-9]+))",
            num_clean,
            re.IGNORECASE,
        ):
            return float(num_clean)
        raise ValueError(f"Unsupported TOML value: {val!r}")

    @staticmethod
    def _find_assignment_eq(line: str) -> int:
        in_single = False
        in_double = False
        escaped = False
        for idx, ch in enumerate(line):
            if escaped:
                escaped = False
                continue
            if in_double:
                if ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_double = False
                continue
            if in_single:
                if ch == "'":
                    in_single = False
                continue
            if ch == '"':
                in_double = True
            elif ch == "'":
                in_single = True
            elif ch == "=":
                return idx
        return -1

    @staticmethod
    def _unbalanced_delimiters(text: str) -> bool:
        depth = 0
        in_single = False
        in_double = False
        escaped = False
        for ch in text:
            if escaped:
                escaped = False
                continue
            if in_double:
                if ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_double = False
                continue
            if in_single:
                if ch == "'":
                    in_single = False
                continue
            if ch == '"':
                in_double = True
            elif ch == "'":
                in_single = True
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
        return depth > 0

    @classmethod
    def loads(cls, text: str) -> dict[str, Any]:
        root: dict[str, Any] = {}
        current: dict[str, Any] = root
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            stripped = cls._strip_comment(lines[i]).strip()
            i += 1
            if not stripped:
                continue
            if stripped.startswith("[[") and stripped.endswith("]]"):
                parts = cls._parse_dotted_key(stripped[2:-2].strip())
                target = root
                for part in parts[:-1]:
                    nxt = target.setdefault(part, {})
                    if isinstance(nxt, list):
                        nxt = nxt[-1]
                    if not isinstance(nxt, dict):
                        raise ValueError(f"TOML table conflict at {parts!r}")
                    target = nxt
                arr = target.setdefault(parts[-1], [])
                if not isinstance(arr, list):
                    raise ValueError(f"TOML array-table conflict at {parts!r}")
                elem: dict[str, Any] = {}
                arr.append(elem)
                current = elem
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                parts = cls._parse_dotted_key(stripped[1:-1].strip())
                target = root
                for part in parts:
                    nxt = target.setdefault(part, {})
                    if isinstance(nxt, list):
                        nxt = nxt[-1]
                    if not isinstance(nxt, dict):
                        raise ValueError(f"TOML table conflict at {parts!r}")
                    target = nxt
                current = target
                continue
            eq_idx = cls._find_assignment_eq(stripped)
            if eq_idx < 0:
                raise ValueError(f"Invalid TOML line: {stripped!r}")
            key_raw = stripped[:eq_idx].strip()
            val_raw = stripped[eq_idx + 1 :].strip()
            while cls._unbalanced_delimiters(val_raw) and i < len(lines):
                val_raw += "\n" + cls._strip_comment(lines[i]).strip()
                i += 1
            key_parts = cls._parse_dotted_key(key_raw)
            val_obj = cls._parse_value(val_raw)
            target = current
            for part in key_parts[:-1]:
                nxt = target.setdefault(part, {})
                if not isinstance(nxt, dict):
                    raise ValueError(f"TOML dotted-key conflict at {key_parts!r}")
                target = nxt
            target[key_parts[-1]] = val_obj
        return root


def load_tomllib() -> Any:
    for mod_name in ("tomllib", "tomli"):
        try:
            return importlib.import_module(mod_name)
        except ModuleNotFoundError:
            continue
    return _MiniTomlParser


def positive_keep_releases(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid integer for --keep-releases: {raw!r}"
        ) from exc
    if value < 2:
        raise argparse.ArgumentTypeError("--keep-releases must be >= 2")
    return value


def validate_credential_free_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned:
        raise SyncBlockedError(
            "blocked_invalid_marketplace_url",
            "Marketplace URL must be a plain http(s) git URL without query or fragment",
        )
    try:
        parsed = urlsplit(cleaned)
    except ValueError as exc:
        raise SyncBlockedError(
            "blocked_invalid_marketplace_url",
            "Marketplace URL must be a plain http(s) git URL without query or fragment",
        ) from exc

    if (
        parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
    ):
        raise SyncBlockedError(
            "blocked_credential_in_url",
            "Marketplace URL must not contain embedded credentials",
        )
    if (
        parsed.scheme.lower() not in ("http", "https")
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
    ):
        raise SyncBlockedError(
            "blocked_invalid_marketplace_url",
            "Marketplace URL must be a plain http(s) git URL without query or fragment",
        )
    return cleaned


def normalize_git_url(url: str) -> str:
    cleaned = validate_credential_free_url(url)
    parsed = urlsplit(cleaned)
    path = parsed.path.rstrip("/")
    path = re.sub(r"\.git\Z", "", path, flags=re.IGNORECASE).rstrip("/")
    host = parsed.hostname.lower() if parsed.hostname else ""
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    return f"{parsed.scheme.lower()}://{netloc}{path}"


def semver_sort_key(
    version_str: str,
) -> tuple[tuple[int, ...], int, tuple[tuple[int, int | str], ...]]:
    no_build = version_str.split("+", 1)[0].strip().lstrip("vV")
    if "-" in no_build:
        core_str, pre_str = no_build.split("-", 1)
        is_stable = 0
        pre_parts: tuple[tuple[int, int | str], ...] = tuple(
            (0, int(tok)) if tok.isdigit() else (1, tok)
            for tok in pre_str.split(".")
            if tok
        )
    else:
        core_str = no_build
        is_stable = 1
        pre_parts = ()

    nums = [int(part) for part in re.findall(r"\d+", core_str)]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums), is_stable, pre_parts


def atomic_write_text(target: Path, content: str, mode: int = 0o644) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    tmp_path = Path(tmp_name)
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
        replaced = True
    finally:
        if not replaced:
            tmp_path.unlink(missing_ok=True)


def atomic_symlink(link_path: Path, target_text: str) -> None:
    if link_path.exists() and not link_path.is_symlink() and link_path.is_dir():
        raise SyncBlockedError(
            "blocked_invalid_current_link",
            f"Refusing to replace real directory {link_path} with symlink",
        )
    link_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_link = (
        link_path.parent / f".{link_path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    )
    created = False
    try:
        os.symlink(target_text, tmp_link)
        created = True
        os.replace(tmp_link, link_path)
        created = False
    finally:
        if created:
            tmp_link.unlink(missing_ok=True)


@contextmanager
def sync_lock(
    lock_path: Path, timeout_s: float = 5.0, *, shared: bool = False
) -> Generator[None, None, None]:
    if shared and not lock_path.exists():
        yield
        return

    try:
        if shared:
            fd = os.open(lock_path, os.O_RDONLY)
        else:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        if shared and exc.errno == errno.ENOENT:
            yield
            return
        raise SyncBlockedError(
            "blocked_lock_error", f"Failed to open lock file {lock_path}: {exc}"
        ) from exc

    lock_mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    acquired = False
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(fd, lock_mode | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                pass
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise SyncBlockedError(
                        "blocked_lock_error",
                        f"Failed to lock {lock_path}: {exc}",
                    ) from exc
            if time.monotonic() >= deadline:
                raise SyncBlockedError(
                    "blocked_lock_timeout",
                    f"Timed out waiting for sync lock {lock_path}",
                )
            time.sleep(0.05)
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def clean_git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    return env


def run_git(
    repo_dir: Path,
    *args: str,
    binary: bool = False,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[Any]:
    cmd = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "protocol.file.allow=never",
        "-C",
        str(repo_dir),
        *args,
    ]
    try:
        if input_bytes is None:
            return subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=not binary,
                encoding=None if binary else "utf-8",
                errors=None if binary else "replace",
                env=clean_git_env(),
                timeout=30,
                check=False,
            )
        with tempfile.TemporaryFile() as stdin_tmp:
            stdin_tmp.write(input_bytes)
            stdin_tmp.flush()
            stdin_tmp.seek(0)
            return subprocess.run(
                cmd,
                stdin=stdin_tmp,
                capture_output=True,
                text=not binary,
                encoding=None if binary else "utf-8",
                errors=None if binary else "replace",
                env=clean_git_env(),
                timeout=30,
                check=False,
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise SyncBlockedError(
            "blocked_git_exec_failed",
            f"git invocation failed in {repo_dir}: {exc}",
        ) from exc


def git_blob_hash(data: bytes, hex_len: int = 40) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    if hex_len == 64:
        return hashlib.sha256(header + data).hexdigest()
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


def validate_export_rel_path(rel_posix: str) -> PurePosixPath:
    if not rel_posix or "\x00" in rel_posix or "\\" in rel_posix:
        raise SyncBlockedError(
            "blocked_forbidden_export_path",
            f"Invalid export path: {rel_posix!r}",
        )
    posix = PurePosixPath(rel_posix)
    if (
        posix.is_absolute()
        or ".." in posix.parts
        or any(part.lower() in FORBIDDEN_PATH_PARTS_LOWER for part in posix.parts)
    ):
        raise SyncBlockedError(
            "blocked_forbidden_export_path",
            f"Forbidden path component in skill export: {rel_posix!r}",
        )
    return posix


def read_codex_active_config(
    codex_home: Path, trusted_source: str
) -> tuple[str, str | None]:
    tomllib_mod = load_tomllib()
    config_path = codex_home / "config.toml"
    if not config_path.is_file():
        raise SyncBlockedError(
            "blocked_missing_codex_config",
            f"Missing Codex config at {config_path}",
        )
    try:
        config = tomllib_mod.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise SyncBlockedError(
            "blocked_invalid_codex_config",
            f"Failed to parse {config_path}: {exc}",
        ) from exc

    plugins = config.get("plugins")
    plugin_entry = plugins.get(PLUGIN_KEY) if isinstance(plugins, dict) else None
    if not isinstance(plugin_entry, dict) or plugin_entry.get("enabled") is not True:
        raise SyncBlockedError(
            "blocked_plugin_disabled_or_missing",
            f"Codex plugin {PLUGIN_KEY!r} is missing or disabled in {config_path}",
        )

    marketplaces = config.get("marketplaces")
    mp_entry = (
        marketplaces.get(MARKETPLACE_KEY) if isinstance(marketplaces, dict) else None
    )
    if not isinstance(mp_entry, dict):
        raise SyncBlockedError(
            "blocked_missing_marketplace",
            f"Codex marketplace {MARKETPLACE_KEY!r} missing from {config_path}",
        )

    source_type = mp_entry.get("source_type")
    source_url = mp_entry.get("source")
    if (
        source_type != "git"
        or not isinstance(source_url, str)
        or not source_url.strip()
    ):
        raise SyncBlockedError(
            "blocked_unsupported_marketplace_source",
            f"Codex marketplace {MARKETPLACE_KEY!r} must use git source_type",
        )

    validated_trusted = validate_credential_free_url(trusted_source)
    validated_source = validate_credential_free_url(source_url)
    if normalize_git_url(validated_source) != normalize_git_url(validated_trusted):
        raise SyncBlockedError(
            "blocked_untrusted_marketplace",
            f"Marketplace source {validated_source!r} does not match trusted source {validated_trusted!r}",
        )

    cleaned_ref: str | None = None
    if "ref" in mp_entry:
        pinned_ref = mp_entry.get("ref")
        if not isinstance(pinned_ref, str) or not pinned_ref.strip():
            raise SyncBlockedError(
                "blocked_invalid_marketplace_ref",
                "Marketplace ref must be a non-empty string when present",
            )
        cleaned_ref = pinned_ref.strip()
    return validated_source, cleaned_ref


def _resolve_clean_skill_paths(
    cache_dir: Path, cache_resolved: Path, raw_skills_rel: str
) -> tuple[str, str] | None:
    if "\\" in raw_skills_rel:
        return None
    skills_posix = PurePosixPath(raw_skills_rel)
    if skills_posix.is_absolute() or ".." in skills_posix.parts:
        return None
    if any(part.lower() in FORBIDDEN_PATH_PARTS_LOWER for part in skills_posix.parts):
        return None

    cursor = cache_dir
    for part in skills_posix.parts:
        if part == ".":
            continue
        cursor = cursor / part
        if cursor.is_symlink():
            return None
    if not cursor.is_dir():
        return None

    try:
        skills_dir = cursor.resolve()
    except (OSError, RuntimeError):
        return None
    if not skills_dir.is_relative_to(cache_resolved):
        return None

    raw_skill_pkg = skills_dir / SKILL_NAME
    if raw_skill_pkg.is_symlink() or not raw_skill_pkg.is_dir():
        return None
    try:
        skill_pkg_dir = raw_skill_pkg.resolve()
    except (OSError, RuntimeError):
        return None
    if not skill_pkg_dir.is_relative_to(cache_resolved):
        return None

    raw_skill_md = skill_pkg_dir / "SKILL.md"
    if raw_skill_md.is_symlink() or not raw_skill_md.is_file():
        return None

    return (
        skills_dir.relative_to(cache_resolved).as_posix(),
        skill_pkg_dir.relative_to(cache_resolved).as_posix(),
    )


def select_active_codex_cache(
    codex_home: Path,
    expected_source: str,
    pinned_ref: str | None,
) -> dict[str, Any]:
    cache_base = codex_home / "plugins" / "cache" / MARKETPLACE_KEY / SKILL_NAME
    if cache_base.is_symlink() or not cache_base.is_dir():
        raise SyncBlockedError(
            "blocked_missing_cache",
            f"Codex plugin cache directory not found or invalid at {cache_base}",
        )

    norm_expected = normalize_git_url(expected_source)
    candidates: list[
        tuple[
            tuple[tuple[int, ...], int, tuple[tuple[int, int | str], ...]],
            int,
            int,
            dict[str, Any],
        ]
    ] = []
    rejection_reasons: list[str] = []

    for child in sorted(cache_base.iterdir()):
        if child.name.startswith("."):
            continue
        if child.is_symlink():
            rejection_reasons.append(f"{child.name}: cache entry must not be a symlink")
            continue
        if not child.is_dir():
            continue
        receipt_path = child / ".codex-marketplace-install.json"
        plugin_dir = child / ".codex-plugin"
        manifest_path = plugin_dir / "plugin.json"
        if (
            receipt_path.is_symlink()
            or plugin_dir.is_symlink()
            or manifest_path.is_symlink()
        ):
            rejection_reasons.append(
                f"{child.name}: receipt or .codex-plugin/plugin.json must not be a symlink"
            )
            continue
        if not receipt_path.is_file() or not manifest_path.is_file():
            rejection_reasons.append(f"{child.name}: missing receipt or plugin.json")
            continue

        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            rejection_reasons.append(f"{child.name}: invalid JSON ({exc})")
            continue

        if not isinstance(receipt, dict) or not isinstance(manifest, dict):
            rejection_reasons.append(f"{child.name}: receipt/manifest not JSON objects")
            continue

        if receipt.get("source_type") != "git":
            rejection_reasons.append(f"{child.name}: receipt source_type != git")
            continue
        receipt_source = receipt.get("source")
        if not isinstance(receipt_source, str):
            rejection_reasons.append(f"{child.name}: receipt source missing")
            continue
        try:
            validated_receipt_source = validate_credential_free_url(receipt_source)
        except SyncBlockedError as exc:
            rejection_reasons.append(f"{child.name}: {exc.code}")
            continue
        if normalize_git_url(validated_receipt_source) != norm_expected:
            rejection_reasons.append(
                f"{child.name}: receipt source mismatch ({validated_receipt_source!r})"
            )
            continue

        raw_revision = receipt.get("revision")
        if not isinstance(raw_revision, str) or not SHA_RE.fullmatch(
            raw_revision.lower()
        ):
            rejection_reasons.append(f"{child.name}: invalid revision {raw_revision!r}")
            continue
        revision = raw_revision.lower()

        ref_name = receipt.get("ref_name")
        if pinned_ref is not None:
            pinned_lower = pinned_ref.lower()
            if SHA_RE.fullmatch(pinned_lower):
                if revision != pinned_lower:
                    rejection_reasons.append(
                        f"{child.name}: receipt revision {revision} != pinned SHA {pinned_ref}"
                    )
                    continue
            elif ref_name != pinned_ref:
                rejection_reasons.append(
                    f"{child.name}: receipt ref_name={ref_name!r} != pinned ref {pinned_ref!r}"
                )
                continue

        if manifest.get("name") != SKILL_NAME:
            rejection_reasons.append(f"{child.name}: manifest name != {SKILL_NAME!r}")
            continue
        version = manifest.get("version")
        if not isinstance(version, str) or not version.strip():
            rejection_reasons.append(f"{child.name}: manifest missing version")
            continue

        skills_field = manifest.get("skills")
        raw_skills_candidates: list[str]
        if skills_field is None:
            raw_skills_candidates = ["./skills/"]
        elif isinstance(skills_field, str):
            if not skills_field.strip():
                rejection_reasons.append(f"{child.name}: empty manifest skills path")
                continue
            raw_skills_candidates = [skills_field.strip()]
        elif isinstance(skills_field, list):
            if not skills_field or any(
                not isinstance(item, str) or not item.strip() for item in skills_field
            ):
                rejection_reasons.append(f"{child.name}: invalid manifest skills list")
                continue
            raw_skills_candidates = [item.strip() for item in skills_field]
        else:
            rejection_reasons.append(
                f"{child.name}: manifest skills must be string or list"
            )
            continue

        cache_resolved = child.resolve()
        resolved_paths: tuple[str, str] | None = None
        for raw_rel in raw_skills_candidates:
            resolved_paths = _resolve_clean_skill_paths(child, cache_resolved, raw_rel)
            if resolved_paths is not None:
                break
        if resolved_paths is None:
            rejection_reasons.append(
                f"{child.name}: missing, symlinked, or escaping {SKILL_NAME}/SKILL.md"
            )
            continue

        skills_rel_posix, skill_pkg_rel_posix = resolved_paths
        is_local = 1 if child.name == "local" else 0
        sv_key = semver_sort_key(version.strip())
        mtime_ns = receipt_path.stat().st_mtime_ns
        candidates.append(
            (
                sv_key,
                is_local,
                mtime_ns,
                {
                    "cache_dir": cache_resolved,
                    "dirname": child.name,
                    "version": version.strip(),
                    "revision": revision,
                    "ref_name": ref_name if isinstance(ref_name, str) else None,
                    "source_url": validated_receipt_source,
                    "skills_rel": skills_rel_posix,
                    "skill_pkg_rel": skill_pkg_rel_posix,
                },
            )
        )

    if not candidates:
        detail = (
            "; ".join(rejection_reasons)
            if rejection_reasons
            else "no version directories found"
        )
        raise SyncBlockedError(
            "blocked_incomplete_or_mismatched_cache",
            f"No valid active Codex {SKILL_NAME} cache in {cache_base}: {detail}",
        )

    candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    top_sv, top_local, _, top_payload = candidates[0]
    for cand_sv, cand_local, _, cand_payload in candidates[1:]:
        if (
            cand_sv == top_sv
            and cand_local == top_local
            and cand_payload["revision"] != top_payload["revision"]
        ):
            raise SyncBlockedError(
                "blocked_ambiguous_codex_cache",
                f"Multiple distinct revisions tie at {top_payload['version']}: "
                f"{top_payload['dirname']} ({top_payload['revision'][:12]}) vs "
                f"{cand_payload['dirname']} ({cand_payload['revision'][:12]})",
            )
    return top_payload


def verify_git_repo_provenance(repo_dir: Path, trusted_source: str) -> None:
    git_entry = repo_dir / ".git"
    if repo_dir.is_symlink() or git_entry.is_symlink() or not git_entry.is_dir():
        raise SyncBlockedError(
            "blocked_unverifiable_source",
            f"Invalid git repository metadata in {repo_dir}",
        )

    origin_proc = run_git(repo_dir, "remote", "get-url", "origin")
    if origin_proc.returncode != 0 or not origin_proc.stdout.strip():
        raise SyncBlockedError(
            "blocked_missing_git_origin",
            f"Git repository {repo_dir} missing origin remote",
        )
    try:
        origin_url = validate_credential_free_url(origin_proc.stdout.strip())
    except SyncBlockedError as exc:
        raise SyncBlockedError(
            exc.code,
            f"Git repository {repo_dir} origin rejected ({exc.code})",
        ) from exc

    if normalize_git_url(origin_url) != normalize_git_url(trusted_source):
        raise SyncBlockedError(
            "blocked_untrusted_git_origin",
            f"Git repository {repo_dir} origin {origin_url!r} != trusted source {trusted_source!r}",
        )


def list_git_tree_entries(
    git_repo: Path,
    revision: str,
    scoped_paths: tuple[str, ...],
) -> dict[str, tuple[str, str]]:
    commit_check = run_git(git_repo, "rev-parse", "--verify", f"{revision}^{{commit}}")
    if commit_check.returncode != 0:
        raise SyncBlockedError(
            "blocked_missing_git_commit",
            f"Commit {revision} not found in {git_repo}",
        )

    ls_proc = run_git(
        git_repo, "ls-tree", "-r", "-z", revision, "--", *scoped_paths, binary=True
    )
    if ls_proc.returncode != 0:
        raise SyncBlockedError(
            "blocked_git_ls_tree_failed",
            f"Failed to list git tree for {revision}: {ls_proc.stderr.decode('utf-8', 'replace')}",
        )

    entries: dict[str, tuple[str, str]] = {}
    for entry in ls_proc.stdout.split(b"\x00"):
        if not entry:
            continue
        if b"\t" not in entry:
            raise SyncBlockedError(
                "blocked_git_ls_tree_failed",
                f"Malformed ls-tree entry in {revision}",
            )
        meta, raw_rel = entry.split(b"\t", 1)
        parts = meta.decode("utf-8", "replace").split()
        if len(parts) < 3:
            raise SyncBlockedError(
                "blocked_git_ls_tree_failed",
                f"Malformed ls-tree metadata in {revision}",
            )
        mode, obj_type, oid = parts[0], parts[1], parts[2]
        rel = raw_rel.decode("utf-8", "strict")
        if obj_type != "blob" or mode not in ALLOWED_GIT_MODES:
            raise SyncBlockedError(
                "blocked_unsupported_git_object",
                f"Unsupported git object {mode} {obj_type} at {rel}",
            )
        if any(
            part.lower() in FORBIDDEN_PATH_PARTS_LOWER
            for part in PurePosixPath(rel).parts
        ):
            raise SyncBlockedError(
                "blocked_forbidden_export_path",
                f"Forbidden path component in skill tree: {rel!r}",
            )
        entries[rel] = (mode, oid)
    return entries


def verify_cache_matches_git_object(
    cache_dir: Path,
    git_repo: Path,
    revision: str,
    scoped_paths: tuple[str, ...],
) -> None:
    expected_entries = list_git_tree_entries(git_repo, revision, scoped_paths)

    actual_files: dict[str, tuple[Path, os.stat_result]] = {}
    for rel_root in scoped_paths:
        rel_posix = PurePosixPath(rel_root)
        cursor = cache_dir
        for part in rel_posix.parts:
            if part == ".":
                continue
            cursor = cursor / part
            if cursor.is_symlink():
                raise SyncBlockedError(
                    "blocked_symlink_in_cache",
                    f"Unexpected symlink in cache path: {cursor}",
                )

        target = cursor
        try:
            target_st = target.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SyncBlockedError(
                "blocked_cache_stat_failed",
                f"Cannot stat cache path {target}: {exc}",
            ) from exc

        if stat.S_ISLNK(target_st.st_mode):
            raise SyncBlockedError(
                "blocked_symlink_in_cache", f"Unexpected symlink in cache: {target}"
            )
        if stat.S_ISREG(target_st.st_mode):
            actual_files[rel_root] = (target, target_st)
        elif stat.S_ISDIR(target_st.st_mode):
            for walk_root, dirnames, filenames in os.walk(target, followlinks=False):
                walk_path = Path(walk_root)
                dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
                for dname in dirnames:
                    dpath = walk_path / dname
                    if dpath.is_symlink():
                        raise SyncBlockedError(
                            "blocked_symlink_in_cache",
                            f"Unexpected symlink dir: {dpath}",
                        )
                for fname in sorted(filenames):
                    if fname == ".DS_Store" or fname.endswith(".pyc"):
                        continue
                    fpath = walk_path / fname
                    st = fpath.lstat()
                    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                        raise SyncBlockedError(
                            "blocked_symlink_in_cache",
                            f"Unexpected non-regular file or symlink in cache: {fpath}",
                        )
                    rel = fpath.relative_to(cache_dir).as_posix()
                    actual_files[rel] = (fpath, st)
        else:
            raise SyncBlockedError(
                "blocked_symlink_in_cache",
                f"Unsupported special file in cache: {target}",
            )

    extra = sorted(set(actual_files) - set(expected_entries))
    missing = sorted(set(expected_entries) - set(actual_files))
    if extra or missing:
        raise SyncBlockedError(
            "blocked_unreviewed_cache_bytes",
            f"Cache tree mismatch vs {revision}: missing={missing[:5]}, extra={extra[:5]}",
        )

    for rel, (expected_mode, oid) in expected_entries.items():
        fpath, st = actual_files[rel]
        actual_exec = bool(stat.S_IMODE(st.st_mode) & stat.S_IXUSR)
        expected_exec = expected_mode == "100755"
        if actual_exec != expected_exec:
            raise SyncBlockedError(
                "blocked_cache_mode_mismatch",
                f"Cache file mode mismatch at {rel} (expected {expected_mode})",
            )
        actual_bytes = fpath.read_bytes()
        if git_blob_hash(actual_bytes, len(oid)) != oid:
            raise SyncBlockedError(
                "blocked_modified_cache_bytes",
                f"Uncommitted byte modification detected in cache at {rel}",
            )


def verify_active_cache_clean(
    cache_dir: Path,
    codex_home: Path,
    revision: str,
    skill_pkg_rel: str,
    trusted_source: str,
) -> Path:
    scoped_paths = (skill_pkg_rel, ".codex-plugin/plugin.json")
    git_entry = cache_dir / ".git"
    if git_entry.exists() or git_entry.is_symlink():
        verify_git_repo_provenance(cache_dir, trusted_source)
        head_proc = run_git(cache_dir, "rev-parse", "HEAD")
        if head_proc.returncode != 0 or head_proc.stdout.strip().lower() != revision:
            raise SyncBlockedError(
                "blocked_head_revision_mismatch",
                f"Cache git HEAD {head_proc.stdout.strip()!r} != receipt revision {revision!r}",
            )
        diff_proc = run_git(cache_dir, "diff", "--quiet", "HEAD", "--", *scoped_paths)
        if diff_proc.returncode != 0:
            raise SyncBlockedError(
                "blocked_dirty_working_tree",
                f"Uncommitted modifications detected in {cache_dir} under {scoped_paths}",
            )
        status_proc = run_git(
            cache_dir,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            *scoped_paths,
        )
        if status_proc.returncode != 0 or status_proc.stdout.strip():
            raise SyncBlockedError(
                "blocked_untracked_or_dirty_files",
                f"Untracked or dirty files in {cache_dir}: {status_proc.stdout.strip()!r}",
            )
        verify_cache_matches_git_object(cache_dir, cache_dir, revision, scoped_paths)
        return cache_dir

    mp_dir = codex_home / ".tmp" / "marketplaces" / MARKETPLACE_KEY
    verify_git_repo_provenance(mp_dir, trusted_source)
    verify_cache_matches_git_object(cache_dir, mp_dir, revision, scoped_paths)
    return mp_dir


def export_committed_tree_batch(
    git_repo: Path,
    revision: str,
    skill_pkg_rel: str,
    staging_dir: Path,
) -> None:
    scoped_paths = (skill_pkg_rel, ".codex-plugin/plugin.json")
    entries = list_git_tree_entries(git_repo, revision, scoped_paths)

    lic_proc = run_git(
        git_repo, "ls-tree", "-z", revision, "--", "LICENSE", binary=True
    )
    if lic_proc.returncode == 0 and lic_proc.stdout:
        for lic_entry in lic_proc.stdout.split(b"\x00"):
            if not lic_entry or b"\t" not in lic_entry:
                continue
            meta, raw_rel = lic_entry.split(b"\t", 1)
            parts = meta.decode("utf-8", "replace").split()
            if (
                len(parts) >= 3
                and parts[1] == "blob"
                and parts[0] in ALLOWED_GIT_MODES
                and raw_rel.decode("utf-8", "strict") == "LICENSE"
            ):
                entries["LICENSE"] = (parts[0], parts[2])

    if not entries:
        raise SyncBlockedError(
            "blocked_incomplete_skill_files",
            f"No exportable files found in {revision}",
        )
    if len(entries) > MAX_EXPORT_FILES:
        raise SyncBlockedError(
            "blocked_export_too_many_files",
            f"Export file count {len(entries)} exceeds limit {MAX_EXPORT_FILES}",
        )

    seen_lower: set[str] = set()
    ordered_items: list[tuple[str, PurePosixPath, str, str]] = []
    for rel in sorted(entries):
        posix = validate_export_rel_path(rel)
        folded = rel.lower()
        if folded in seen_lower:
            raise SyncBlockedError(
                "blocked_case_collision_in_export",
                f"Case-insensitive collision in export tree: {rel!r}",
            )
        seen_lower.add(folded)
        mode, oid = entries[rel]
        ordered_items.append((rel, posix, mode, oid))

    batch_input = "\n".join(item[3] for item in ordered_items).encode("ascii") + b"\n"
    batch_proc = run_git(
        git_repo, "cat-file", "--batch", binary=True, input_bytes=batch_input
    )
    if batch_proc.returncode != 0:
        raise SyncBlockedError(
            "blocked_git_blob_read",
            f"git cat-file --batch failed for {revision}: {batch_proc.stderr.decode('utf-8', 'replace')}",
        )

    raw_stream = batch_proc.stdout
    pos = 0
    total_bytes = 0
    staging_resolved = staging_dir.resolve()
    for rel, posix, mode, expected_oid in ordered_items:
        nl = raw_stream.find(b"\n", pos)
        if nl == -1:
            raise SyncBlockedError(
                "blocked_git_blob_read", f"Truncated cat-file header for {rel}"
            )
        header_parts = raw_stream[pos:nl].decode("ascii", "replace").split(" ")
        if len(header_parts) != 3 or header_parts[1] != "blob":
            raise SyncBlockedError(
                "blocked_git_blob_read",
                f"Unexpected cat-file header for {rel}: {header_parts}",
            )
        got_oid, _, size_str = header_parts
        try:
            size = int(size_str)
        except ValueError as exc:
            raise SyncBlockedError(
                "blocked_git_blob_read", f"Invalid blob size for {rel}: {size_str!r}"
            ) from exc
        if got_oid != expected_oid or size < 0:
            raise SyncBlockedError(
                "blocked_git_blob_read",
                f"OID/size mismatch reading committed blob for {rel}",
            )
        total_bytes += size
        if total_bytes > MAX_EXPORT_BYTES:
            raise SyncBlockedError(
                "blocked_export_size_limit",
                f"Total exported bytes exceeds limit {MAX_EXPORT_BYTES}",
            )

        blob_start = nl + 1
        blob_end = blob_start + size
        if (
            len(raw_stream) < blob_end + 1
            or raw_stream[blob_end : blob_end + 1] != b"\n"
        ):
            raise SyncBlockedError(
                "blocked_git_blob_read", f"Truncated cat-file payload for {rel}"
            )
        blob_data = raw_stream[blob_start:blob_end]
        pos = blob_end + 1

        if git_blob_hash(blob_data, len(expected_oid)) != expected_oid:
            raise SyncBlockedError(
                "blocked_git_blob_read", f"Blob hash verification failed for {rel}"
            )

        dest = (staging_resolved / Path(*posix.parts)).resolve()
        if not dest.is_relative_to(staging_resolved):
            raise SyncBlockedError(
                "blocked_path_escape",
                f"Export path escapes staging root: {rel!r}",
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as handle:
            handle.write(blob_data)
        os.chmod(dest, 0o755 if mode == "100755" else 0o644)


def split_lf_lines(text: str) -> list[str]:
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def parse_skill_frontmatter(raw_bytes: bytes) -> tuple[dict[str, str], int, int]:
    bom_len = 3 if raw_bytes.startswith(b"\xef\xbb\xbf") else 0
    try:
        text = raw_bytes[bom_len:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SyncBlockedError(
            "blocked_invalid_skill_encoding", f"SKILL.md is not UTF-8: {exc}"
        ) from exc

    lines = split_lf_lines(text)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise SyncBlockedError(
            "blocked_missing_skill_frontmatter",
            "SKILL.md missing opening --- frontmatter",
        )

    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].rstrip("\r\n") == "---":
            end_idx = idx
            break
    if end_idx is None:
        raise SyncBlockedError(
            "blocked_unclosed_skill_frontmatter",
            "SKILL.md missing closing --- frontmatter",
        )

    fm_lines = lines[1:end_idx]
    body_start_line = end_idx + 2
    body_offset = bom_len + sum(
        len(line.encode("utf-8")) for line in lines[: end_idx + 1]
    )

    fields: dict[str, str] = {}
    current_section: str | None = None
    metadata_indent: int | None = None
    multiline_key: str | None = None
    multiline_parts: list[str] = []

    def flush_multiline() -> None:
        nonlocal multiline_key, multiline_parts
        if multiline_key is not None:
            joined = " ".join(part for part in multiline_parts if part)
            if joined:
                fields[multiline_key] = joined
            multiline_key = None
            multiline_parts = []

    for raw_line in fm_lines:
        stripped_end = raw_line.rstrip("\r\n")
        if not stripped_end.strip():
            if multiline_key is not None:
                continue
            continue
        if stripped_end.lstrip().startswith("#"):
            continue
        if stripped_end[0].isspace():
            if multiline_key is not None:
                multiline_parts.append(stripped_end.strip())
                continue
            if current_section == "metadata":
                indent = len(stripped_end) - len(stripped_end.lstrip(" \t"))
                if metadata_indent is None:
                    metadata_indent = indent
                if indent == metadata_indent:
                    match = re.match(r"^\s+version:\s*(.+?)\s*$", stripped_end)
                    if match:
                        fields["metadata.version"] = (
                            match.group(1).strip().strip('"').strip("'")
                        )
            elif current_section == "description" and not re.match(
                r"^\s*(?:-|[\w.-]+\s*:)", stripped_end
            ):
                multiline_key = "description"
                multiline_parts = [stripped_end.strip()]
            continue

        flush_multiline()
        metadata_indent = None
        if ":" in stripped_end:
            key, val = stripped_end.split(":", 1)
            cleaned_key = key.strip()
            val_stripped = val.strip()
            if val_stripped in (">", ">-", ">+", "|", "|-", "|+"):
                current_section = None
                multiline_key = cleaned_key
                multiline_parts = []
                fields[cleaned_key] = ""
            else:
                cleaned_val = val_stripped.strip('"').strip("'")
                fields[cleaned_key] = cleaned_val
                current_section = cleaned_key if cleaned_val == "" else None
        else:
            current_section = None

    flush_multiline()
    return fields, body_offset, body_start_line


def compute_cutoff_metadata(raw_bytes: bytes, body_offset: int) -> dict[str, Any]:
    total_bytes = len(raw_bytes)
    body_bytes = max(0, total_bytes - body_offset)
    if body_bytes <= AGY_SKILL_BODY_CAP_BYTES:
        return {
            "agy_skill_body_cap_bytes": AGY_SKILL_BODY_CAP_BYTES,
            "skill_md_total_bytes": total_bytes,
            "skill_md_body_bytes": body_bytes,
            "truncated": False,
            "cutoff_full_line": None,
            "tail_start_line": None,
            "truncated_headings": [],
        }

    body_text = raw_bytes[body_offset:].decode("utf-8", errors="replace")
    body_lines = split_lf_lines(body_text)
    body_start_line = raw_bytes[:body_offset].count(b"\n") + 1

    cum_bytes = 0
    cutoff_full_line: int | None = None
    fence_char = ""
    fence_len = 0
    truncated_headings: list[str] = []

    for offset_idx, line in enumerate(body_lines):
        line_no = body_start_line + offset_idx
        cum_bytes += len(line.encode("utf-8"))
        if cutoff_full_line is None and cum_bytes > AGY_SKILL_BODY_CAP_BYTES:
            cutoff_full_line = line_no

        no_eol = line.rstrip("\r\n")
        if not fence_char:
            open_match = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", no_eol)
            if open_match:
                marker = open_match.group(1)
                fence_char = marker[0]
                fence_len = len(marker)
            elif (
                cutoff_full_line is not None
                and line_no >= cutoff_full_line
                and re.match(r"^[ \t]{0,3}#{1,6}(?:[ \t]+|$)", no_eol)
            ):
                truncated_headings.append(no_eol.strip())
        else:
            close_pat = rf"^[ \t]{{0,3}}{re.escape(fence_char)}{{{fence_len},}}[ \t]*$"
            if re.match(close_pat, no_eol):
                fence_char = ""
                fence_len = 0

    if cutoff_full_line is None:
        cutoff_full_line = body_start_line
    tail_start_line = max(1, cutoff_full_line - TAIL_OVERLAP_LINES)
    return {
        "agy_skill_body_cap_bytes": AGY_SKILL_BODY_CAP_BYTES,
        "skill_md_total_bytes": total_bytes,
        "skill_md_body_bytes": body_bytes,
        "truncated": True,
        "cutoff_full_line": cutoff_full_line,
        "tail_start_line": tail_start_line,
        "truncated_headings": truncated_headings,
    }


def compute_tree_sha256(root_dir: Path) -> str:
    hasher = hashlib.sha256()
    root_resolved = root_dir.resolve()
    entries: list[Path] = []
    for walk_root, dirnames, filenames in os.walk(root_resolved, followlinks=False):
        walk_path = Path(walk_root)
        dirnames.sort()
        for dname in dirnames:
            dpath = walk_path / dname
            if dpath.is_symlink():
                raise SyncBlockedError(
                    "blocked_symlink_in_release", f"Symlink in release tree: {dpath}"
                )
        for fname in sorted(filenames):
            fpath = walk_path / fname
            st = fpath.lstat()
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                raise SyncBlockedError(
                    "blocked_symlink_in_release",
                    f"Non-regular file or symlink in release tree: {fpath}",
                )
            if walk_path == root_resolved and fname == ".agy-pwf-release.json":
                continue
            entries.append(fpath)

    entries.sort(key=lambda p: p.relative_to(root_resolved).as_posix())
    for fpath in entries:
        rel = fpath.relative_to(root_resolved).as_posix()
        mode = stat.S_IMODE(fpath.lstat().st_mode)
        fhash = hashlib.sha256(fpath.read_bytes()).hexdigest()
        hasher.update(f"{mode:04o} {rel} {fhash}\n".encode())
    return hasher.hexdigest()


def validate_staged_release(
    staging_dir: Path,
    skill_pkg_rel: str,
    expected_version: str,
) -> tuple[dict[str, Any], str]:
    skill_dir = staging_dir / skill_pkg_rel
    if not skill_dir.is_dir():
        raise SyncBlockedError(
            "blocked_missing_skill_dir", f"Staged skill dir missing: {skill_dir}"
        )

    for walk_root, dirnames, filenames in os.walk(staging_dir, followlinks=False):
        wp = Path(walk_root)
        for dname in dirnames:
            dp = wp / dname
            if dname.lower() in FORBIDDEN_PATH_PARTS_LOWER or dp.is_symlink():
                raise SyncBlockedError(
                    "blocked_hygiene_violation", f"Disallowed directory/symlink: {dp}"
                )
        for fname in filenames:
            fp = wp / fname
            st = fp.lstat()
            if (
                fname.lower() in FORBIDDEN_PATH_PARTS_LOWER
                or stat.S_ISLNK(st.st_mode)
                or not stat.S_ISREG(st.st_mode)
            ):
                raise SyncBlockedError(
                    "blocked_hygiene_violation", f"Disallowed file/symlink: {fp}"
                )

    for rel in REQUIRED_REL_FILES:
        fpath = skill_dir / rel
        if fpath.is_symlink() or not fpath.is_file() or fpath.stat().st_size == 0:
            raise SyncBlockedError(
                "blocked_incomplete_skill_files",
                f"Missing or empty required file: {fpath}",
            )

    for rel in EXECUTABLE_REL_FILES:
        fpath = skill_dir / rel
        if not (fpath.lstat().st_mode & stat.S_IXUSR):
            raise SyncBlockedError(
                "blocked_non_executable_script",
                f"Required script not owner-executable: {fpath}",
            )

    raw_skill = (skill_dir / "SKILL.md").read_bytes()
    fm, body_offset, _ = parse_skill_frontmatter(raw_skill)
    if fm.get("name") != SKILL_NAME:
        raise SyncBlockedError(
            "blocked_skill_name_mismatch",
            f"SKILL.md frontmatter name {fm.get('name')!r} != {SKILL_NAME!r}",
        )
    if not fm.get("description"):
        raise SyncBlockedError(
            "blocked_skill_missing_description",
            "SKILL.md frontmatter missing description",
        )
    fm_ver = fm.get("metadata.version") or fm.get("version")
    if fm_ver != expected_version:
        raise SyncBlockedError(
            "blocked_skill_version_mismatch",
            f"SKILL.md version {fm_ver!r} != manifest version {expected_version!r}",
        )

    cutoff = compute_cutoff_metadata(raw_skill, body_offset)
    tree_sha256 = compute_tree_sha256(staging_dir)
    return cutoff, tree_sha256


def read_skills_manifest(skills_json_path: Path) -> dict[str, Any]:
    if skills_json_path.is_symlink():
        raise SyncBlockedError(
            "blocked_symlink_skills_json",
            f"Refusing to manage symlinked {skills_json_path}",
        )
    if not skills_json_path.exists():
        return {}
    try:
        data = json.loads(skills_json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncBlockedError(
            "blocked_invalid_skills_json",
            f"Existing {skills_json_path} is unreadable or invalid JSON: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise SyncBlockedError(
            "blocked_invalid_skills_json",
            f"Existing {skills_json_path} must be a JSON object",
        )
    return data


def _is_managed_skills_path(raw_path: str, releases_resolved: Path) -> bool:
    if not (raw_path.startswith(("/", "~/")) or raw_path == "~"):
        return False
    try:
        resolved_item = Path(raw_path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return resolved_item == releases_resolved or resolved_item.is_relative_to(
        releases_resolved
    )


def build_updated_skills_manifest(
    existing: dict[str, Any],
    releases_root: Path,
    desired_skills_path: Path,
) -> tuple[dict[str, Any], bool]:
    desired_entry = {
        "path": str(desired_skills_path.resolve()),
        "include_only": [SKILL_NAME],
    }
    releases_resolved = releases_root.resolve()

    old_entries = existing.get("entries")
    if old_entries is None:
        new_entries = [desired_entry]
    elif isinstance(old_entries, list):
        new_entries = []
        inserted = False
        for item in old_entries:
            if (
                isinstance(item, dict)
                and isinstance(item.get("path"), str)
                and _is_managed_skills_path(item["path"], releases_resolved)
            ):
                if not inserted:
                    new_entries.append(desired_entry)
                    inserted = True
                continue
            new_entries.append(item)
        if not inserted:
            new_entries.append(desired_entry)
    else:
        raise SyncBlockedError(
            "blocked_invalid_skills_json", "skills.json 'entries' must be a list"
        )

    updated = dict(existing)
    updated["entries"] = new_entries

    already_exact = isinstance(old_entries, list) and old_entries == updated["entries"]
    return updated, not already_exact


def is_release_verified(
    release_dir: Path,
    expected_revision: str,
    expected_version: str,
    expected_skills_rel: str,
    expected_skill_pkg_rel: str,
    expected_source: str,
) -> dict[str, Any] | None:
    meta_path = release_dir / ".agy-pwf-release.json"
    if (
        release_dir.is_symlink()
        or meta_path.is_symlink()
        or not release_dir.is_dir()
        or not meta_path.is_file()
    ):
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict):
        return None
    if (
        meta.get("revision") != expected_revision
        or meta.get("version") != expected_version
        or meta.get("skills_rel") != expected_skills_rel
        or meta.get("skill_pkg_rel") != expected_skill_pkg_rel
    ):
        return None

    source_url = meta.get("source_url")
    if not isinstance(source_url, str):
        return None
    try:
        if normalize_git_url(source_url) != normalize_git_url(expected_source):
            return None
    except SyncBlockedError:
        return None

    cutoff = meta.get("cutoff")
    if not isinstance(cutoff, dict) or not isinstance(cutoff.get("truncated"), bool):
        return None
    if cutoff.get("truncated") is True:
        tail_line = cutoff.get("tail_start_line")
        if (
            not isinstance(tail_line, int)
            or isinstance(tail_line, bool)
            or tail_line < 1
        ):
            return None

    recorded_sha = meta.get("tree_sha256")
    if not isinstance(recorded_sha, str):
        return None
    try:
        release_resolved = release_dir.resolve()
        skills_dir = (release_resolved / expected_skills_rel).resolve()
        if not skills_dir.is_relative_to(release_resolved):
            return None
        skill_md = release_resolved / expected_skill_pkg_rel / "SKILL.md"
        if skill_md.is_symlink() or not skill_md.is_file():
            return None
        actual_sha = compute_tree_sha256(release_dir)
    except (SyncBlockedError, OSError, UnicodeDecodeError, RuntimeError):
        return None
    if actual_sha != recorded_sha:
        return None
    return meta


def load_state(state_path: Path) -> dict[str, Any]:
    if state_path.is_symlink():
        raise SyncBlockedError(
            "blocked_symlink_state_json",
            f"Refusing to manage symlinked {state_path}",
        )
    if not state_path.exists():
        return {"history": []}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncBlockedError(
            "blocked_invalid_state_json",
            f"Existing {state_path} is unreadable or invalid JSON: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise SyncBlockedError(
            "blocked_invalid_state_json",
            f"Existing {state_path} must be a JSON object",
        )
    history = data.get("history")
    if history is not None and not isinstance(history, list):
        raise SyncBlockedError(
            "blocked_invalid_state_json",
            f"Existing {state_path} has non-list 'history'",
        )
    return data


def read_current_revision_on_disk(
    current_link: Path, releases_root: Path
) -> str | None:
    if not current_link.is_symlink():
        return None
    try:
        resolved = current_link.resolve()
        releases_resolved = releases_root.resolve()
    except (OSError, RuntimeError):
        return None
    if (
        resolved.parent == releases_resolved
        and SHA_RE.fullmatch(resolved.name)
        and resolved.is_dir()
    ):
        return resolved.name
    return None


def prune_releases(
    releases_root: Path,
    state: dict[str, Any],
    keep_releases: int,
    extra_keep_revision: str | None = None,
) -> list[str]:
    if releases_root.is_symlink() or not releases_root.is_dir():
        return []

    on_disk = {
        child.name
        for child in releases_root.iterdir()
        if not child.is_symlink() and child.is_dir() and SHA_RE.fullmatch(child.name)
    }

    keep_set: set[str] = set()
    for rev in (
        state.get("active_revision"),
        state.get("previous_revision"),
        extra_keep_revision,
    ):
        if isinstance(rev, str) and SHA_RE.fullmatch(rev) and rev in on_disk:
            keep_set.add(rev)

    history = state.get("history")
    if isinstance(history, list):
        for entry in reversed(history):
            if len(keep_set) >= keep_releases:
                break
            if isinstance(entry, dict) and isinstance(entry.get("revision"), str):
                rev = entry["revision"]
                if SHA_RE.fullmatch(rev) and rev in on_disk:
                    keep_set.add(rev)

    removed: list[str] = []
    now = time.time()
    for child in sorted(releases_root.iterdir()):
        if child.is_symlink():
            continue
        if child.name.startswith(".staging-") or child.name.startswith(".replaced-"):
            if child.is_dir() and (now - child.stat().st_mtime) > 3600:
                shutil.rmtree(child, ignore_errors=True)
            continue
        if not child.is_dir() or not SHA_RE.fullmatch(child.name):
            continue
        if child.name not in keep_set:
            shutil.rmtree(child)
            removed.append(child.name)
    return removed


def _escape_double_quoted_sh(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )


def build_hook_command(
    hook_script: Path | None = None,
    *,
    extra_args: list[str] | None = None,
) -> str:
    """Build portable /bin/sh wrapper selecting Python 3.9+ and running --hook."""
    py_lookup = (
        'PY=""; '
        'for _py in "$HOME/.local/share/mise/shims/python3" '
        '"/opt/homebrew/bin/python3" "/usr/bin/python3" python3; do '
        'if "$_py" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >/dev/null 2>&1; then PY="$_py"; break; fi; '
        "done; "
        'if [ -z "$PY" ]; then '
        'echo "{\\"injectSteps\\":[{\\"systemMessage\\":{\\"systemMessage\\":\\"[agy-pwf-sync] blocked: python3 >= 3.9 not found\\"}}]}"; '
        "exit 0; "
        "fi; "
    )
    if hook_script is None:
        script_part = DEFAULT_HOOK_SCRIPT_EXPR
    else:
        script_part = (
            f'"{_escape_double_quoted_sh(str(hook_script.expanduser().resolve()))}"'
        )
    args_parts = [f'"{_escape_double_quoted_sh(arg)}"' for arg in (extra_args or [])]
    suffix = (" " + " ".join(args_parts)) if args_parts else ""
    inner = f'{py_lookup}exec "$PY" -I -B {script_part}{suffix} --hook'
    return f"/bin/sh -c {shlex.quote(inner)}"


def build_hook_entry(command: str, timeout: int = 10) -> dict[str, Any]:
    return {
        "SessionStart": [
            {
                "type": "command",
                "command": command,
                "timeout": timeout,
            }
        ]
    }


def register_hook(
    agy_config_dir: Path, command: str, timeout: int = 10
) -> dict[str, Any]:
    hooks_path = agy_config_dir / "hooks.json"
    if hooks_path.is_symlink():
        raise SyncBlockedError(
            "blocked_symlink_hooks_json",
            f"Refusing to overwrite symlinked hooks config {hooks_path}",
        )
    if hooks_path.exists():
        try:
            existing = json.loads(hooks_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SyncBlockedError(
                "blocked_invalid_hooks_json",
                f"Existing {hooks_path} is unreadable or invalid JSON: {exc}",
            ) from exc
        if not isinstance(existing, dict):
            raise SyncBlockedError(
                "blocked_invalid_hooks_json",
                f"Existing {hooks_path} must be a JSON object",
            )
    else:
        existing = {}

    desired_entry = build_hook_entry(command, timeout=timeout)
    changed = existing.get(HOOK_NAME) != desired_entry
    updated = dict(existing)
    updated[HOOK_NAME] = desired_entry
    if changed:
        atomic_write_text(hooks_path, json.dumps(updated, indent=2) + "\n")
    return {
        "status": "registered" if changed else "noop_already_registered",
        "hooks_json": str(hooks_path),
        "hook_name": HOOK_NAME,
        "entry": desired_entry,
    }


def resolve_python_with_tomllib(home_dir: Path) -> Path:
    candidates = [
        home_dir / ".local/share/mise/shims/python3",
        Path("/opt/homebrew/bin/python3"),
        Path("/usr/bin/python3"),
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            probe = subprocess.run(
                [
                    str(candidate),
                    "-I",
                    "-c",
                    "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=5,
                check=False,
            )
            if probe.returncode == 0:
                return candidate
    raise SyncBlockedError(
        "blocked_missing_python3",
        "No Python 3.9+ interpreter found for LaunchAgent",
    )


def build_launchagent_plist(
    home_dir: Path,
    codex_home: Path,
    agy_config_dir: Path,
    state_dir: Path,
    hook_script: Path,
    trusted_source: str,
    keep_releases: int,
) -> dict[str, Any]:
    py_exe = resolve_python_with_tomllib(home_dir)
    path_env = ":".join(
        [
            str(home_dir / ".local/share/mise/shims"),
            str(home_dir / ".local/bin"),
            "/opt/homebrew/bin",
            "/usr/bin",
            "/bin",
        ]
    )
    return {
        "Label": LAUNCHAGENT_LABEL,
        "ProgramArguments": [
            str(py_exe),
            "-I",
            "-B",
            str(hook_script.expanduser().resolve()),
            "--codex-home",
            str(codex_home),
            "--agy-config-dir",
            str(agy_config_dir),
            "--state-dir",
            str(state_dir),
            "--trusted-source",
            trusted_source,
            "--keep-releases",
            str(keep_releases),
            "--json",
        ],
        "RunAtLoad": True,
        "ThrottleInterval": 2,
        "WatchPaths": [
            str(codex_home / "config.toml"),
            str(codex_home / "plugins" / "cache" / MARKETPLACE_KEY / SKILL_NAME),
        ],
        "EnvironmentVariables": {"PATH": path_env},
        "StandardOutPath": str(state_dir / "launchd.out.log"),
        "StandardErrorPath": str(state_dir / "launchd.err.log"),
    }


def install_launchagent(
    plist_path: Path,
    codex_home: Path,
    agy_config_dir: Path,
    state_dir: Path,
    hook_script: Path,
    trusted_source: str,
    keep_releases: int,
) -> dict[str, Any]:
    resolved_plist = plist_path.expanduser()
    if resolved_plist.is_symlink():
        raise SyncBlockedError(
            "blocked_symlink_plist",
            f"Refusing to overwrite symlinked LaunchAgent plist {resolved_plist}",
        )
    state_dir.mkdir(parents=True, exist_ok=True)
    plist_data = build_launchagent_plist(
        home_dir=Path.home(),
        codex_home=codex_home,
        agy_config_dir=agy_config_dir,
        state_dir=state_dir,
        hook_script=hook_script,
        trusted_source=validate_credential_free_url(trusted_source),
        keep_releases=keep_releases,
    )
    plist_text = plistlib.dumps(plist_data).decode("utf-8").rstrip("\n") + "\n"
    atomic_write_text(resolved_plist, plist_text, mode=0o644)
    return {
        "status": "installed",
        "label": LAUNCHAGENT_LABEL,
        "plist_path": str(resolved_plist),
        "program_arguments": plist_data["ProgramArguments"],
        "watch_paths": plist_data["WatchPaths"],
    }


def _check_current_symlink_matches(
    current_link: Path, target_release_dir: Path
) -> bool:
    if (
        current_link.exists()
        and not current_link.is_symlink()
        and current_link.is_dir()
    ):
        raise SyncBlockedError(
            "blocked_invalid_current_link",
            f"Refusing to treat real directory {current_link} as release symlink",
        )
    if not current_link.is_symlink():
        return False
    try:
        return current_link.resolve() == target_release_dir.resolve()
    except (OSError, RuntimeError):
        return False


def perform_sync(args: argparse.Namespace) -> dict[str, Any]:
    codex_home = Path(args.codex_home).expanduser().resolve()
    agy_config_dir = Path(args.agy_config_dir).expanduser().resolve()
    state_dir = Path(args.state_dir).expanduser().resolve()
    releases_root = state_dir / "releases"
    current_link = state_dir / "current"
    state_path = state_dir / "state.json"
    skills_json_path = agy_config_dir / "skills.json"
    keep_releases = int(args.keep_releases)
    lock_path = state_dir / "sync.lock"

    if args.check:
        with sync_lock(lock_path, shared=True):
            expected_source, pinned_ref = read_codex_active_config(
                codex_home, args.trusted_source
            )
            active_cache = select_active_codex_cache(
                codex_home, expected_source, pinned_ref
            )
            revision = active_cache["revision"]
            version = active_cache["version"]
            skills_rel = active_cache["skills_rel"]
            skill_pkg_rel = active_cache["skill_pkg_rel"]
            target_release_dir = releases_root / revision
            desired_skills_path = target_release_dir / skills_rel

            verify_active_cache_clean(
                active_cache["cache_dir"],
                codex_home,
                revision,
                skill_pkg_rel,
                expected_source,
            )
            meta = is_release_verified(
                target_release_dir,
                revision,
                version,
                skills_rel,
                skill_pkg_rel,
                expected_source,
            )
            existing_skills = read_skills_manifest(skills_json_path)
            _, needs_manifest_update = build_updated_skills_manifest(
                existing_skills, releases_root, desired_skills_path
            )
            current_ok = _check_current_symlink_matches(
                current_link, target_release_dir
            )
            synced = meta is not None and not needs_manifest_update and current_ok
            return {
                "status": "ok" if synced else "drift",
                "synced": synced,
                "revision": revision,
                "version": version,
                "release_dir": str(target_release_dir),
                "skills_json": str(skills_json_path),
                "current_ok": current_ok,
                "manifest_ok": not needs_manifest_update,
                "cutoff": meta.get("cutoff") if meta else None,
            }

    with sync_lock(lock_path, shared=False):
        expected_source, pinned_ref = read_codex_active_config(
            codex_home, args.trusted_source
        )
        active_cache = select_active_codex_cache(
            codex_home, expected_source, pinned_ref
        )
        revision = active_cache["revision"]
        version = active_cache["version"]
        skills_rel = active_cache["skills_rel"]
        skill_pkg_rel = active_cache["skill_pkg_rel"]
        target_release_dir = releases_root / revision
        desired_skills_path = target_release_dir / skills_rel

        releases_root.mkdir(parents=True, exist_ok=True)
        verified_git_repo = verify_active_cache_clean(
            active_cache["cache_dir"],
            codex_home,
            revision,
            skill_pkg_rel,
            expected_source,
        )
        existing_skills = read_skills_manifest(skills_json_path)
        updated_skills, needs_manifest_update = build_updated_skills_manifest(
            existing_skills, releases_root, desired_skills_path
        )
        current_ok = _check_current_symlink_matches(current_link, target_release_dir)
        prior_on_disk = read_current_revision_on_disk(current_link, releases_root)
        state = load_state(state_path)
        existing_meta = is_release_verified(
            target_release_dir,
            revision,
            version,
            skills_rel,
            skill_pkg_rel,
            expected_source,
        )

        if existing_meta is not None and not needs_manifest_update and current_ok:
            return {
                "status": "noop_up_to_date",
                "revision": revision,
                "version": version,
                "release_dir": str(target_release_dir),
                "skills_json": str(skills_json_path),
                "tree_sha256": existing_meta["tree_sha256"],
                "cutoff": existing_meta["cutoff"],
            }

        if existing_meta is None:
            staging_dir = (
                releases_root
                / f".staging-{revision}-{os.getpid()}-{secrets.token_hex(4)}"
            )
            staging_dir.mkdir(parents=True, exist_ok=False)
            promoted = False
            try:
                export_committed_tree_batch(
                    verified_git_repo,
                    revision,
                    skill_pkg_rel,
                    staging_dir,
                )
                cutoff, tree_sha256 = validate_staged_release(
                    staging_dir, skill_pkg_rel, version
                )
                release_meta = {
                    "schema_version": 1,
                    "synced_at": utc_now_iso(),
                    "skill_name": SKILL_NAME,
                    "version": version,
                    "revision": revision,
                    "ref_name": active_cache["ref_name"],
                    "source_url": validate_credential_free_url(
                        active_cache["source_url"]
                    ),
                    "codex_cache_dir": str(active_cache["cache_dir"]),
                    "verified_git_repo": str(verified_git_repo),
                    "skills_rel": skills_rel,
                    "skill_pkg_rel": skill_pkg_rel,
                    "tree_sha256": tree_sha256,
                    "cutoff": cutoff,
                }
                atomic_write_text(
                    staging_dir / ".agy-pwf-release.json",
                    json.dumps(release_meta, indent=2) + "\n",
                )
                backup_dir: Path | None = None
                if target_release_dir.is_symlink() or target_release_dir.is_file():
                    target_release_dir.unlink()
                elif target_release_dir.is_dir():
                    backup_dir = (
                        releases_root
                        / f".replaced-{revision}-{os.getpid()}-{secrets.token_hex(4)}"
                    )
                    os.replace(target_release_dir, backup_dir)
                try:
                    os.replace(staging_dir, target_release_dir)
                    promoted = True
                except OSError:
                    if (
                        backup_dir is not None
                        and backup_dir.exists()
                        and not target_release_dir.exists()
                    ):
                        os.replace(backup_dir, target_release_dir)
                    raise
                if backup_dir is not None and backup_dir.exists():
                    shutil.rmtree(backup_dir, ignore_errors=True)
                existing_meta = release_meta
            finally:
                if not promoted and staging_dir.exists():
                    shutil.rmtree(staging_dir, ignore_errors=True)

        if not current_ok:
            atomic_symlink(current_link, f"releases/{revision}")
        if needs_manifest_update:
            atomic_write_text(
                skills_json_path,
                json.dumps(updated_skills, indent=2) + "\n",
            )

        prev_active = state.get("active_revision")
        if isinstance(prev_active, str) and prev_active != revision:
            state["previous_revision"] = prev_active
        elif prior_on_disk is not None and prior_on_disk != revision:
            state["previous_revision"] = prior_on_disk
        state["active_revision"] = revision
        state["version"] = version
        state["last_sync"] = utc_now_iso()
        raw_history = state.get("history")
        history_entries: list[Any] = (
            raw_history if isinstance(raw_history, list) else []
        )
        history = [
            item
            for item in history_entries
            if isinstance(item, dict) and item.get("revision") != revision
        ]
        history.append(
            {
                "revision": revision,
                "version": version,
                "activated_at": state["last_sync"],
                "tree_sha256": existing_meta["tree_sha256"],
            }
        )
        max_history = max(keep_releases * 4, 20)
        if len(history) > max_history:
            history = history[-max_history:]
        state["history"] = history
        atomic_write_text(state_path, json.dumps(state, indent=2) + "\n")
        pruned = prune_releases(releases_root, state, keep_releases, prior_on_disk)

        return {
            "status": "synced",
            "revision": revision,
            "previous_revision": state.get("previous_revision"),
            "version": version,
            "release_dir": str(target_release_dir),
            "skills_json": str(skills_json_path),
            "tree_sha256": existing_meta["tree_sha256"],
            "cutoff": existing_meta["cutoff"],
            "pruned_revisions": pruned,
        }


def read_hook_conversation_id() -> str | None:
    try:
        if sys.stdin.isatty():
            return None
        raw_bytes = sys.stdin.buffer.read(65536)
    except OSError:
        return None
    if not raw_bytes.strip():
        return None
    raw = raw_bytes.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    cid = payload.get("conversationId")
    if not isinstance(cid, str):
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", cid).strip()
    return cleaned[:128] if cleaned else None


def write_hook_audit(state_dir: Path, record: dict[str, Any]) -> None:
    try:
        atomic_write_text(
            state_dir / "last-hook-run.json",
            json.dumps(record, indent=2) + "\n",
        )
    except OSError:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely sync Antigravity CLI global planning-with-files skill from active Codex release."
    )
    parser.add_argument(
        "--codex-home", default="~/.codex", help="Path to Codex home directory"
    )
    parser.add_argument(
        "--agy-config-dir",
        default="~/.gemini/config",
        help="Path to Antigravity config dir",
    )
    parser.add_argument(
        "--state-dir",
        default="~/.local/share/agy-pwf",
        help="Path to immutable release store",
    )
    parser.add_argument(
        "--trusted-source",
        default=DEFAULT_TRUSTED_SOURCE,
        help="Trusted git remote URL",
    )
    parser.add_argument(
        "--keep-releases",
        type=positive_keep_releases,
        default=5,
        help="Retained release count (minimum 2)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit structured JSON output"
    )
    parser.add_argument(
        "--hook-script",
        default=None,
        help="Explicit script path for --register-hook or --install-launchagent",
    )
    parser.add_argument(
        "--hook-pass-paths",
        action="store_true",
        help="Embed --codex-home/--agy-config-dir/--state-dir into registered hook command",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--check", action="store_true", help="Read-only check; exit 2 on drift"
    )
    mode_group.add_argument(
        "--hook", action="store_true", help="Antigravity SessionStart hook mode"
    )
    mode_group.add_argument(
        "--register-hook",
        action="store_true",
        help="Register agy-pwf-sync SessionStart hook in <agy-config-dir>/hooks.json",
    )
    mode_group.add_argument(
        "--install-launchagent",
        default=None,
        metavar="PLIST_PATH",
        help="Write dev.agy.pwf-sync LaunchAgent plist atomically",
    )
    mode_group.add_argument(
        "--self-check",
        action="store_true",
        help="Verify stdlib Python 3.9+ TOML fallback, config parsing, and hook command builder",
    )
    return parser


def _run_self_check() -> list[str]:
    checks: list[str] = []
    sample = (
        "# Comment\n"
        'model = "gpt-5"\n'
        'flags = ["a", "b"]\n'
        'inline = { k = "v", n = 42 }\n'
        '[plugins."planning-with-files@planning-with-files"]\n'
        "enabled = true\n"
        "[marketplaces.planning-with-files]\n"
        'source_type = "git"\n'
        f'source = "{DEFAULT_TRUSTED_SOURCE}"\n'
        'ref = "0123456789abcdef0123456789abcdef01234567"\n'
    )
    parsed = _MiniTomlParser.loads(sample)
    assert (
        parsed["plugins"]["planning-with-files@planning-with-files"]["enabled"] is True
    )
    assert parsed["marketplaces"]["planning-with-files"]["source_type"] == "git"
    assert parsed["flags"] == ["a", "b"]
    assert parsed["inline"] == {"k": "v", "n": 42}
    checks.append("mini_toml_parser")

    with tempfile.TemporaryDirectory() as tmp:
        codex_home = Path(tmp)
        (codex_home / "config.toml").write_text(sample, encoding="utf-8")
        src, ref = read_codex_active_config(codex_home, DEFAULT_TRUSTED_SOURCE)
        assert src == DEFAULT_TRUSTED_SOURCE
        assert ref == "0123456789abcdef0123456789abcdef01234567"
    checks.append("read_codex_active_config")

    cmd = build_hook_command()
    assert "/usr/bin/python3" in cmd and "tomllib" not in cmd
    checks.append("build_hook_command_py39_portable")
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.self_check:
        checks = _run_self_check()
        sys.stdout.write(json.dumps({"status": "ok", "checks": checks}) + "\n")
        return 0

    if args.register_hook:
        agy_config_dir = Path(args.agy_config_dir).expanduser().resolve()
        codex_home = Path(args.codex_home).expanduser().resolve()
        state_dir = Path(args.state_dir).expanduser().resolve()
        keep_releases = int(args.keep_releases)
        hook_script = Path(args.hook_script) if args.hook_script else None
        extra_args = (
            [
                "--codex-home",
                str(codex_home),
                "--agy-config-dir",
                str(agy_config_dir),
                "--state-dir",
                str(state_dir),
                "--trusted-source",
                args.trusted_source,
                "--keep-releases",
                str(keep_releases),
            ]
            if args.hook_pass_paths
            else None
        )
        try:
            command = build_hook_command(hook_script, extra_args=extra_args)
            res = register_hook(agy_config_dir, command)
        except SyncBlockedError as exc:
            payload = {"status": "blocked", "code": exc.code, "error": exc.message}
            if args.json:
                sys.stdout.write(json.dumps(payload, indent=2) + "\n")
            else:
                sys.stderr.write(f"agy-pwf-sync: {exc.code}: {exc.message}\n")
            return 1
        if args.json:
            sys.stdout.write(json.dumps(res, indent=2) + "\n")
        else:
            sys.stdout.write(f"agy-pwf-sync: {res['status']} ({res['hooks_json']})\n")
        return 0

    if args.install_launchagent:
        codex_home = Path(args.codex_home).expanduser().resolve()
        agy_config_dir = Path(args.agy_config_dir).expanduser().resolve()
        state_dir = Path(args.state_dir).expanduser().resolve()
        keep_releases = int(args.keep_releases)
        hook_script = (
            Path(args.hook_script) if args.hook_script else Path(__file__).resolve()
        )
        try:
            res = install_launchagent(
                plist_path=Path(args.install_launchagent),
                codex_home=codex_home,
                agy_config_dir=agy_config_dir,
                state_dir=state_dir,
                hook_script=hook_script,
                trusted_source=args.trusted_source,
                keep_releases=keep_releases,
            )
        except SyncBlockedError as exc:
            payload = {"status": "blocked", "code": exc.code, "error": exc.message}
            if args.json:
                sys.stdout.write(json.dumps(payload, indent=2) + "\n")
            else:
                sys.stderr.write(f"agy-pwf-sync: {exc.code}: {exc.message}\n")
            return 1
        if args.json:
            sys.stdout.write(json.dumps(res, indent=2) + "\n")
        else:
            sys.stdout.write(f"agy-pwf-sync: {res['status']} ({res['plist_path']})\n")
        return 0

    if args.hook:
        state_dir = Path(args.state_dir).expanduser().resolve()
        conv_id = read_hook_conversation_id()
        try:
            result = perform_sync(args)
            write_hook_audit(
                state_dir,
                {
                    "fired_at": utc_now_iso(),
                    "conversation_id": conv_id,
                    "status": result["status"],
                    "revision": result["revision"],
                    "version": result["version"],
                    "code": None,
                },
            )
            if result["status"] == "synced":
                cutoff = result["cutoff"]
                tail_note = (
                    f"; cutoff tail_start_line={cutoff['tail_start_line']}"
                    if isinstance(cutoff, dict)
                    and cutoff.get("truncated")
                    and cutoff.get("tail_start_line")
                    else ""
                )
                notice = (
                    f"[agy-pwf-sync] synced {SKILL_NAME} to {result['version']} "
                    f"({result['revision'][:12]}); active release: {result['release_dir']}"
                    f"{tail_note}"
                )
                sys.stdout.write(
                    json.dumps(
                        {"injectSteps": [{"systemMessage": {"systemMessage": notice}}]}
                    )
                    + "\n"
                )
            else:
                sys.stdout.write("{}\n")
        except SyncBlockedError as exc:
            write_hook_audit(
                state_dir,
                {
                    "fired_at": utc_now_iso(),
                    "conversation_id": conv_id,
                    "status": "blocked",
                    "revision": None,
                    "version": None,
                    "code": exc.code,
                },
            )
            notice = f"[agy-pwf-sync] blocked ({exc.code}): {exc.message}"
            sys.stdout.write(
                json.dumps(
                    {"injectSteps": [{"systemMessage": {"systemMessage": notice}}]}
                )
                + "\n"
            )
        except HOOK_EXPECTED_ERRORS as exc:
            write_hook_audit(
                state_dir,
                {
                    "fired_at": utc_now_iso(),
                    "conversation_id": conv_id,
                    "status": "error",
                    "revision": None,
                    "version": None,
                    "code": type(exc).__name__,
                },
            )
            notice = f"[agy-pwf-sync] error ({type(exc).__name__}): {exc}"
            sys.stdout.write(
                json.dumps(
                    {"injectSteps": [{"systemMessage": {"systemMessage": notice}}]}
                )
                + "\n"
            )
        return 0

    try:
        result = perform_sync(args)
    except SyncBlockedError as exc:
        payload = {"status": "blocked", "code": exc.code, "error": exc.message}
        if args.json:
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        else:
            sys.stderr.write(f"agy-pwf-sync: {exc.code}: {exc.message}\n")
        return 1

    if args.json:
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
    else:
        sys.stdout.write(
            f"agy-pwf-sync: {result['status']} ({result['version']} @ {result['revision'][:12]})\n"
        )

    if args.check and not result.get("synced", False):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
