"""Declarative reviewer-bot plugins (Graphify, Greptile, CodeRabbit, Repowise) across harnesses.

Stdlib only. The manifest (assets/reviewer-plugins.json) is the single source of truth for:
  sync   - idempotently add marketplaces / install plugins via each harness CLI (user scope),
           retire superseded direct MCPs, and apply the Repowise rewrite guard.
  check  - report drift: missing plugins, Claude plugin MCPs not Connected, unguarded repos.
  guard  - ensure every Repowise-indexed repo turns off the auto-approved Bash rewrite hook.

OpenCode entries live directly in ~/.config/opencode/opencode.json (a managed dotfile); the
manifest lists them so the repo-scope check can assert they stay declared.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

MISE_DIR = Path(__file__).resolve().parent.parent
MANIFEST = MISE_DIR / "assets" / "reviewer-plugins.json"
GUARD_LINE = "Disables the auto-approved Bash rewrite (dotfiles-v2 repowise guard)."
GUARD_YAML = f"# {GUARD_LINE}\ndistill:\n  commands:\n    enabled: false\n"


def load_manifest(path: Path = MANIFEST) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _run(argv: list[str], timeout: int = 180) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, proc.stdout


def _json(argv: list[str]) -> object:
    rc, out = _run(argv)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _bin(name: str, home: Path) -> str | None:
    if name == "claude":
        local = home / ".local" / "bin" / "claude"
        if os.access(local, os.X_OK):
            return str(local)
    return shutil.which(name)


# --- live state readers -------------------------------------------------------------------


def claude_state(claude: str) -> tuple[set[str], set[str]]:
    """(installed+enabled user plugin ids, marketplace names)."""
    rows = _json([claude, "plugin", "list", "--json"])
    plugins = {
        str(r.get("id"))
        for r in (rows if isinstance(rows, list) else [])
        if isinstance(r, dict)
        and r.get("enabled") is not False
        and r.get("scope") in (None, "user")
    }
    mps = _json([claude, "plugin", "marketplace", "list", "--json"])
    names = {
        str(m.get("name"))
        for m in (mps if isinstance(mps, list) else [])
        if isinstance(m, dict)
    }
    return plugins, names


def claude_mcp_status(claude: str, name: str) -> str | None:
    """Status text from `claude mcp get` (e.g. '✔ Connected'), or None if not registered."""
    rc, out = _run([claude, "mcp", "get", name], timeout=60)
    if rc != 0:
        return None
    m = re.search(r"^\s*Status:\s*(.+?)\s*$", out, re.M)
    return m.group(1) if m else ""


def is_connected(status: str | None) -> bool:
    return bool(status) and re.fullmatch(r"(?:✔\s*)?Connected", str(status)) is not None


def codex_state(codex: str) -> tuple[set[str], set[str]]:
    data = _json([codex, "plugin", "marketplace", "list", "--json"])
    mps = data.get("marketplaces", []) if isinstance(data, dict) else []
    names = {str(m.get("name")) for m in mps if isinstance(m, dict)}
    installed: set[str] = set()
    for mp in names:
        data = _json([codex, "plugin", "list", "-m", mp, "--json"])
        for p in (data.get("installed") or []) if isinstance(data, dict) else []:
            if (
                isinstance(p, dict)
                and p.get("installed") is not False
                and p.get("enabled") is not False
            ):
                installed.add(
                    str(
                        p.get("pluginId")
                        or f"{p.get('name')}@{p.get('marketplaceName')}"
                    )
                )
    return installed, names


def agy_state(agy: str) -> set[str]:
    rows = _json([agy, "plugin", "list", "--json"])
    return {
        str(r.get("name"))
        for r in (rows if isinstance(rows, list) else [])
        if isinstance(r, dict) and r.get("enabled") is not False
    }


# --- repowise guard -----------------------------------------------------------------------


def repowise_repos(roots: list[Path]) -> list[Path]:
    """Repos (depth <= 3 under each root) that contain a .repowise/ index directory."""
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for depth in ("*", "*/*", "*/*/*"):
            for d in root.glob(f"{depth}/.repowise"):
                if d.is_dir():
                    found.append(d.parent)
    return sorted(set(found))


def guarded(repo: Path) -> bool:
    """True when .repowise/config.yaml disables distill command rewriting.

    Mirrors repowise's own _load_commands_config: distill.enabled false, distill.commands.enabled
    false, or distill.commands.permission off all disable the rewrite.
    """
    cfg = repo / ".repowise" / "config.yaml"
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        return False
    if GUARD_LINE in text:
        return True
    block = re.search(r"^distill:\s*\n((?:[ \t]+.*\n?|\s*\n)*)", text, re.M)
    if not block:
        return False
    body = block.group(1)
    return bool(
        re.search(r"^  enabled:\s*(false|off|no)\b", body, re.M)
        or re.search(
            r"^  commands:\s*\n(?:    .*\n)*?    (enabled:\s*(false|off|no)|permission:\s*(off|false))\b",
            body,
            re.M,
        )
    )


def apply_guard(repo: Path) -> str:
    cfg = repo / ".repowise" / "config.yaml"
    if guarded(repo):
        return f"ok: {repo}"
    if cfg.exists():
        text = cfg.read_text(encoding="utf-8")
        if re.search(r"^distill:", text, re.M):
            return f"MANUAL: {cfg} has a distill: block; set distill.commands.enabled: false"
        sep = "" if text.endswith("\n") or not text else "\n"
        cfg.write_text(text + sep + GUARD_YAML, encoding="utf-8")
    else:
        cfg.write_text(GUARD_YAML, encoding="utf-8")
    return f"guarded: {repo}"


def default_roots(home: Path) -> list[Path]:
    return [home / "dev"]


# --- sync / check -------------------------------------------------------------------------


def sync(manifest: dict, home: Path) -> list[str]:
    log: list[str] = []
    claude = _bin("claude", home)
    if claude and "claude" in manifest:
        spec = manifest["claude"]
        plugins, mps = claude_state(claude)
        for name, src in spec.get("marketplaces", {}).items():
            if name not in mps:
                rc, _ = _run([claude, "plugin", "marketplace", "add", src])
                log.append(f"claude marketplace add {src}: rc={rc}")
        for pid in spec.get("plugins", []):
            if pid not in plugins:
                rc, _ = _run([claude, "plugin", "install", pid])
                log.append(f"claude plugin install {pid}: rc={rc}")
        for name in spec.get("retired_mcps", []):
            if claude_mcp_status(claude, name) is not None:
                rc, _ = _run([claude, "mcp", "remove", "--scope", "user", name])
                log.append(f"claude mcp remove {name}: rc={rc}")
    codex = _bin("codex", home)
    if codex and "codex" in manifest:
        spec = manifest["codex"]
        installed, mps = codex_state(codex)
        for name, src in spec.get("marketplaces", {}).items():
            if name not in mps:
                rc, _ = _run([codex, "plugin", "marketplace", "add", src])
                log.append(f"codex marketplace add {src}: rc={rc}")
        for pid in spec.get("plugins", []):
            if pid not in installed:
                rc, _ = _run([codex, "plugin", "add", pid])
                log.append(f"codex plugin add {pid}: rc={rc}")
    agy = _bin("agy", home)
    if agy and "agy" in manifest:
        have = agy_state(agy)
        for name, target in manifest["agy"].get("plugins", {}).items():
            if name not in have:
                rc, _ = _run([agy, "plugin", "install", target])
                log.append(f"agy plugin install {target}: rc={rc}")
    log.extend(apply_guard(r) for r in repowise_repos(default_roots(home)))
    return log


def check(manifest: dict, home: Path, *, require_connected: bool = True) -> list[str]:
    errors: list[str] = []
    claude = _bin("claude", home)
    if claude and "claude" in manifest:
        spec = manifest["claude"]
        plugins, _ = claude_state(claude)
        errors += [
            f"claude plugin missing/disabled: {p}"
            for p in spec.get("plugins", [])
            if p not in plugins
        ]
        for name in spec.get("retired_mcps", []):
            if claude_mcp_status(claude, name) is not None:
                errors.append(f"claude retired MCP still registered: {name}")
        if require_connected:
            for name in spec.get("connected_mcps", []):
                status = claude_mcp_status(claude, name)
                if not is_connected(status):
                    errors.append(
                        f"claude MCP {name} not Connected (status: {status or 'missing'})"
                    )
    codex = _bin("codex", home)
    if codex and "codex" in manifest:
        installed, _ = codex_state(codex)
        errors += [
            f"codex plugin missing: {p}"
            for p in manifest["codex"].get("plugins", [])
            if p not in installed
        ]
    agy = _bin("agy", home)
    if agy and "agy" in manifest:
        have = agy_state(agy)
        errors += [
            f"agy plugin missing: {n}"
            for n in manifest["agy"].get("plugins", {})
            if n not in have
        ]
    errors += [
        f"repowise rewrite not disabled: {r}/.repowise/config.yaml (run `mise run repowise:guard`)"
        for r in repowise_repos(default_roots(home))
        if not guarded(r)
    ]
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="reviewer_plugins")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync")
    sub.add_parser("check")
    g = sub.add_parser("guard")
    g.add_argument("repos", nargs="*", type=Path)
    args = ap.parse_args(argv)
    home = Path.home()
    if args.cmd == "guard":
        repos = [p.resolve() for p in args.repos] or repowise_repos(default_roots(home))
        out = [apply_guard(r) for r in repos if (r / ".repowise").is_dir()]
        _emit(out, "repowise: no indexed repos")
        return 1 if any(o.startswith("MANUAL") for o in out) else 0
    manifest = load_manifest()
    if args.cmd == "sync":
        _emit(sync(manifest, home), "reviewer-plugins: up to date")
        return 0
    errs = check(manifest, home)
    _emit(errs, "reviewer-plugins: all declared plugins installed and Connected")
    return 1 if errs else 0


def _emit(lines: list[str], empty: str) -> None:
    sys.stdout.write(("\n".join(lines) or empty) + "\n")


if __name__ == "__main__":
    sys.exit(main())
