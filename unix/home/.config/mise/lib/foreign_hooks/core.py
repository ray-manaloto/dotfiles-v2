"""Scan and remove foreign agent hooks across Claude, Codex, agy, and opencode.

Stdlib-only. Hits are reported as ``<file>:<json path>`` (``<file>:<line>`` for
TOML, the entry path for opencode plugin files/dirs).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

BACKUP_ROOT_REL = Path("dev") / "tmp" / "foreign-hook-removal"


def load_markers(asset: Path) -> list[str]:
    data = json.loads(asset.read_text(encoding="utf-8"))
    markers = data["markers"]
    if not isinstance(markers, list) or not markers:
        raise ValueError("markers must be a non-empty list")
    if not all(isinstance(m, str) and m for m in markers):
        raise ValueError("markers must be non-empty strings")
    return list(markers)


def _matches(text: object, markers: Sequence[str]) -> bool:
    return isinstance(text, str) and any(m in text for m in markers)


def _values_match(value: Any, markers: Sequence[str]) -> bool:
    if isinstance(value, list):
        return any(_matches(v, markers) for v in value)
    return _matches(value, markers)


def _entry_matches(entry: Any, markers: Sequence[str]) -> bool:
    return isinstance(entry, dict) and any(
        _values_match(entry.get(f), markers)
        for f in ("command", "args", "url", "prompt")
    )


def _body_matches(body: Any, markers: Sequence[str]) -> bool:
    """agy body match: only `command`/`args` string values, at any depth."""
    if isinstance(body, dict):
        return any(
            _values_match(v, markers)
            if k in ("command", "args")
            else _body_matches(v, markers)
            for k, v in body.items()
        )
    if isinstance(body, list):
        return any(_body_matches(v, markers) for v in body)
    return False


def _load_json(path: Path) -> tuple[Any, str | None]:
    data, err, _raw = _load_json_raw(path)
    return data, err


def _load_json_raw(path: Path) -> tuple[Any, str | None, bytes]:
    try:
        raw = path.read_bytes()
        return json.loads(raw.decode("utf-8")), None, raw
    except FileNotFoundError:
        return None, None, b""
    except (OSError, ValueError) as exc:
        return None, f"{path}: cannot parse JSON ({exc})", b""


def _hook_events(data: Any) -> dict[str, Any]:
    hooks = data.get("hooks") if isinstance(data, dict) else None
    return hooks if isinstance(hooks, dict) else {}


def _scan_event_hooks(path: Path, data: Any, markers: Sequence[str]) -> list[str]:
    hits: list[str] = []
    for event, groups in _hook_events(data).items():
        if not isinstance(groups, list):
            continue
        for gi, group in enumerate(groups):
            base = f"hooks.{event}[{gi}]"
            if _entry_matches(group, markers):
                hits.append(f"{path}:{base}.command")
            inner = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(inner, list):
                continue
            for hi, hook in enumerate(inner):
                if _entry_matches(hook, markers):
                    hits.append(f"{path}:{base}.hooks[{hi}].command")
    return hits


def _agy_key_matches(key: str, body: Any, markers: Sequence[str]) -> bool:
    return _matches(key, markers) or _body_matches(body, markers)


def _scan_agy(path: Path, data: Any, markers: Sequence[str]) -> list[str]:
    if not isinstance(data, dict):
        return []
    return [
        f"{path}:{key}"
        for key, body in data.items()
        if _agy_key_matches(key, body, markers)
    ]


def _scan_toml(path: Path, markers: Sequence[str]) -> tuple[list[str], list[str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [f"{path}: cannot read ({exc})"]
    hits = [
        f"{path}:{n}"
        for n, line in enumerate(text.splitlines(), 1)
        if _matches(line, markers)
    ]
    return hits, []


def opencode_matches(home: Path, markers: Sequence[str]) -> list[Path]:
    base = home / ".config" / "opencode"
    found: list[Path] = []
    for directory in (base, base / "plugins"):
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        found.extend(e for e in entries if any(e.name.startswith(m) for m in markers))
    return found


def scan(home: Path, markers: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return (hits, errors) for all harness locations under ``home``."""
    hits: list[str] = []
    errors: list[str] = []

    for path in (home / ".claude" / "settings.json", home / ".codex" / "hooks.json"):
        data, err = _load_json(path)
        if err:
            errors.append(err)
        elif data is not None:
            hits.extend(_scan_event_hooks(path, data, markers))

    agy = home / ".gemini" / "config" / "hooks.json"
    data, err = _load_json(agy)
    if err:
        errors.append(err)
    elif data is not None:
        hits.extend(_scan_agy(agy, data, markers))

    toml_hits, toml_errs = _scan_toml(home / ".codex" / "config.toml", markers)
    hits.extend(toml_hits)
    errors.extend(toml_errs)

    hits.extend(str(p) for p in opencode_matches(home, markers))
    return hits, errors


def _prune_event_hooks(data: Any, markers: Sequence[str]) -> int:
    """Remove matching hook entries in place; drop emptied groups/events."""
    removed = 0
    events = _hook_events(data)
    for event in list(events):
        groups = events[event]
        if not isinstance(groups, list):
            continue
        kept: list[Any] = []
        for group in groups:
            if _entry_matches(group, markers):
                removed += 1
                continue
            inner = group.get("hooks") if isinstance(group, dict) else None
            if isinstance(inner, list):
                new_inner = [h for h in inner if not _entry_matches(h, markers)]
                dropped = len(inner) - len(new_inner)
                removed += dropped
                if dropped:
                    if not new_inner:
                        continue
                    group["hooks"] = new_inner
            kept.append(group)
        if kept:
            events[event] = kept
        else:
            del events[event]
    if removed and isinstance(data, dict) and data.get("hooks") == {}:
        del data["hooks"]
    return removed


def _prune_agy(data: Any, markers: Sequence[str]) -> int:
    if not isinstance(data, dict):
        return 0
    doomed = [k for k, body in data.items() if _agy_key_matches(k, body, markers)]
    for key in doomed:
        del data[key]
    return len(doomed)


def _git_checkout_root(target: Path) -> Path | None:
    for parent in target.parents:
        if (parent / ".git").exists():
            return parent
    return None


def _atomic_write_bytes(target: Path, payload: bytes, expect: bytes) -> None:
    """Write ``payload`` to the resolved ``target`` atomically, aborting on a lost update."""
    mode = target.stat().st_mode & 0o7777
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.chmod(tmp_name, mode)
        if target.read_bytes() != expect:
            raise RuntimeError(f"{target} changed during the run; retry")
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _unique(dest_dir: Path, name: str) -> Path:
    cand = dest_dir / name
    n = 1
    while cand.exists() or cand.is_symlink():
        cand = dest_dir / f"{name}.{n}"
        n += 1
    return cand


def trash_dir(home: Path, platform: str | None = None) -> Path:
    if (platform or sys.platform) == "darwin":
        return home / ".Trash"
    return home / ".local" / "share" / "Trash" / "files"


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _remove_json(
    path: Path,
    tag: str,
    pruner: Callable[[Any, Sequence[str]], int],
    markers: Sequence[str],
    backup_dir: Path,
    apply: bool,
    actions: list[str],
    errors: list[str],
) -> None:
    data, err, raw = _load_json_raw(path)
    if err:
        errors.append(err)
        return
    if data is None:
        return
    count = pruner(data, markers)
    if not count:
        return
    target = path.resolve()
    repo = _git_checkout_root(target)
    if repo is not None:
        actions.append(
            f"MANUAL: {path} resolves to {target} inside git checkout {repo}; "
            "remove the foreign entries in that repo file instead"
        )
        return
    noun = "entry" if count == 1 else "entries"
    verb = "removed" if apply else "would remove"
    if not apply:
        actions.append(f"{verb} {count} foreign hook {noun} from {target}")
        return
    try:
        _mkdir_private(backup_dir)
        backup = backup_dir / f"{tag}-{path.name}"
        backup.write_bytes(raw)
        backup.chmod(0o600)
        payload = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()
        _atomic_write_bytes(target, payload, raw)
    except (OSError, RuntimeError) as exc:
        errors.append(f"{path}: {exc}")
        return
    actions.append(f"backup: {backup}")
    actions.append(f"{verb} {count} foreign hook {noun} from {target}")


def remove(
    home: Path,
    markers: Sequence[str],
    *,
    apply: bool,
    platform: str | None = None,
) -> tuple[list[str], list[str]]:
    """Dry-run by default. Return (actions, errors); writes only when ``apply``."""
    actions: list[str] = []
    errors: list[str] = []
    stamp = f"{time.time_ns()}-{os.getpid()}"
    backup_dir = home / BACKUP_ROOT_REL / stamp

    targets: tuple[tuple[Path, str, Callable[[Any, Sequence[str]], int]], ...] = (
        (home / ".claude" / "settings.json", "claude", _prune_event_hooks),
        (home / ".codex" / "hooks.json", "codex", _prune_event_hooks),
        (home / ".gemini" / "config" / "hooks.json", "agy", _prune_agy),
    )
    for path, tag, pruner in targets:
        _remove_json(path, tag, pruner, markers, backup_dir, apply, actions, errors)

    toml_hits, _ = _scan_toml(home / ".codex" / "config.toml", markers)
    actions.extend(
        f"MANUAL: edit {hit} (config.toml is not rewritten automatically)"
        for hit in toml_hits
    )

    trash = trash_dir(home, platform)
    for entry in opencode_matches(home, markers):
        if not apply:
            actions.append(f"would move {entry} -> {trash}/")
            continue
        try:
            if not trash.exists():
                trash.mkdir(parents=True, mode=0o700)
            dest = _unique(trash, entry.name)
            shutil.move(str(entry), str(dest))
        except OSError as exc:
            errors.append(f"{entry}: {exc}")
            continue
        actions.append(f"moved {entry} -> {dest}")
    return actions, errors
