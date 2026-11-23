#!/usr/bin/env python3
"""Antigravity SessionStart scanner and manifest generator for 24 KB SKILL.md truncation."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

AGY_SKILL_BODY_CAP_BYTES = 24000
NEAR_CAP_BYTES = 22000
TAIL_CONTEXT_LINES = 15

FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)
HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")
FM_NAME_RE = re.compile(r"(?m)^name:\s*['\"]?([^'\"\n#]+?)['\"]?\s*$")


def _home() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home()))


def _manifest_path() -> Path:
    xdg_data = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_data).expanduser() if xdg_data else _home() / ".local" / "share"
    return base / "agy-skill-cutoff" / "manifest.json"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _iter_skill_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    direct = root / "SKILL.md"
    found: list[Path] = []
    if direct.is_file():
        found.append(direct)
    try:
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if child.name.startswith(".") or not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if skill_md.is_file():
                found.append(skill_md)
    except OSError:
        return found
    return found


def _parse_frontmatter_name(text: str, fallback: str) -> str:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return fallback
    name_m = FM_NAME_RE.search(m.group(0))
    return name_m.group(1).strip() if name_m else fallback


def _discover_skill_files() -> list[Path]:
    gemini_dir = _home() / ".gemini"
    cfg_dir = gemini_dir / "config"
    skills_cfg = _load_json(cfg_dir / "skills.json")
    cfg = _load_json(cfg_dir / "config.json")
    raw_plugins = cfg.get("plugins")
    plugins_cfg: dict[str, Any] = raw_plugins if isinstance(raw_plugins, dict) else {}

    excludes: set[str] = set()
    raw_excludes = skills_cfg.get("exclude")
    if isinstance(raw_excludes, list):
        excludes = {str(x) for x in raw_excludes if isinstance(x, str)}

    candidates: list[Path] = []
    raw_entries = skills_cfg.get("entries")
    if isinstance(raw_entries, list):
        for entry in raw_entries:
            if not isinstance(entry, dict) or not entry.get("path"):
                continue
            root = Path(str(entry["path"])).expanduser()
            inc = entry.get("include_only")
            allow = (
                {str(x) for x in inc if isinstance(x, str)}
                if isinstance(inc, list)
                else None
            )
            for skill_md in _iter_skill_files(root):
                if allow is not None and skill_md.parent.name not in allow:
                    continue
                candidates.append(skill_md)

    for extra_root in (
        cfg_dir / "skills",
        gemini_dir / "skills",
        gemini_dir / "antigravity-cli" / "builtin" / "skills",
    ):
        candidates.extend(_iter_skill_files(extra_root))

    plugins_dir = cfg_dir / "plugins"
    if plugins_dir.is_dir():
        for plugin_dir in sorted(plugins_dir.iterdir(), key=lambda p: p.name):
            if plugin_dir.name.startswith(".") or not plugin_dir.is_dir():
                continue
            plugin_meta = plugins_cfg.get(plugin_dir.name)
            if isinstance(plugin_meta, dict) and plugin_meta.get("enabled") is False:
                continue
            candidates.extend(_iter_skill_files(plugin_dir / "skills"))

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_file() or resolved in seen:
            continue
        if (
            path.parent.name in excludes
            or str(path) in excludes
            or str(resolved) in excludes
        ):
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _analyze_skill(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    name = _parse_frontmatter_name(text, path.parent.name)
    m = FRONTMATTER_RE.match(text)
    if m:
        fm_lines = len(m.group(0).splitlines())
        body = text[m.end() :]
    else:
        fm_lines = 0
        body = text

    body_bytes = len(body.encode("utf-8"))
    total_bytes = len(raw)
    acc = 0
    cutoff_line: int | None = None
    headings: list[str] = []

    for idx, line in enumerate(body.splitlines(keepends=True), start=1):
        full_line = fm_lines + idx
        acc += len(line.encode("utf-8"))
        if cutoff_line is None and acc > AGY_SKILL_BODY_CAP_BYTES:
            cutoff_line = full_line
        if cutoff_line is not None:
            hm = HEADING_RE.match(line)
            if hm:
                headings.append(f"L{full_line}: {hm.group(1)} {hm.group(2)}")

    truncated = cutoff_line is not None
    tail_start_line = (
        max(1, cutoff_line - TAIL_CONTEXT_LINES) if cutoff_line is not None else None
    )
    managed_by = (
        "agy-pwf-sync"
        if name == "planning-with-files" or "/agy-pwf/" in str(path)
        else "agy-skill-cutoff"
    )
    return {
        "name": name,
        "path": str(path),
        "managed_by": managed_by,
        "body_bytes": body_bytes,
        "total_bytes": total_bytes,
        "truncated": truncated,
        "cutoff_line": cutoff_line,
        "tail_start_line": tail_start_line,
        "truncated_headings": headings,
    }


def _scan() -> tuple[Path, dict[str, Any]]:
    records: list[dict[str, Any]] = []
    unreadable: list[dict[str, str]] = []
    for path in _discover_skill_files():
        try:
            records.append(_analyze_skill(path))
        except OSError as exc:
            unreadable.append({"path": str(path), "error": str(exc)})
    records.sort(key=lambda r: (r["name"], r["path"]))
    unreadable.sort(key=lambda r: r["path"])
    truncated = [r for r in records if r["truncated"]]
    near_cap = [
        r for r in records if not r["truncated"] and r["body_bytes"] >= NEAR_CAP_BYTES
    ]
    manifest_path = _manifest_path()
    payload: dict[str, Any] = {
        "status": "ok",
        "cap_bytes": AGY_SKILL_BODY_CAP_BYTES,
        "near_cap_bytes": NEAR_CAP_BYTES,
        "tail_context_lines": TAIL_CONTEXT_LINES,
        "scanned_count": len(records),
        "unreadable_count": len(unreadable),
        "manifest_path": str(manifest_path),
        "truncated": truncated,
        "near_cap": near_cap,
        "unreadable": unreadable,
    }
    return manifest_path, payload


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=".manifest-", suffix=".json", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hook", action="store_true", help="Emit SessionStart hook JSON."
    )
    parser.add_argument("--check", action="store_true", help="Check active AGY skills.")
    parser.add_argument(
        "--json", action="store_true", help="Print JSON payload in --check mode."
    )
    parser.add_argument(
        "--no-write", action="store_true", help="Do not write manifest file."
    )
    args = parser.parse_args()

    manifest_path, payload = _scan()
    if not args.no_write:
        _write_manifest(manifest_path, payload)

    if args.hook:
        non_pwf = [
            item
            for item in payload["truncated"]
            if item.get("managed_by") != "agy-pwf-sync"
        ]
        if not non_pwf:
            sys.stdout.write("{}\n")
            return 0
        summary = "; ".join(
            f"{item['name']} (StartLine: {item['tail_start_line']}, path: {item['path']})"
            for item in non_pwf
        )
        msg = (
            f"[agy-skill-cutoff] Truncated AGY skills (>{AGY_SKILL_BODY_CAP_BYTES:,} B body): "
            f"{summary}. See ~/.local/share/agy-skill-cutoff/manifest.json and skill-cutoff-agy.md."
        )
        sys.stdout.write(
            json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": msg}}]})
            + "\n"
        )
        return 0

    if args.json or args.check:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
