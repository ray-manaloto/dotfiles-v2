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


# Runs inside repowise's own interpreter so the decision uses repowise's exact config reader
# (_load_commands_config: same YAML parsing and on/off flag semantics as the live hook).
_REPOWISE_HELPER = """
import json, pathlib, sys
import yaml
from repowise.cli.rewrite_hook import _load_commands_config
mode, repo, header = sys.argv[1], sys.argv[2], sys.argv[3]
if mode == "set":
    cfg = pathlib.Path(repo, ".repowise", "config.yaml")
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        sys.exit(3)
    distill = data.get("distill") if isinstance(data.get("distill"), dict) else {}
    commands = distill.get("commands") if isinstance(distill.get("commands"), dict) else {}
    commands["enabled"] = False
    distill["commands"] = commands
    data["distill"] = distill
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(header + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
enabled, _perm, _fam = _load_commands_config(repo)
sys.stdout.write(json.dumps({"rewrite_enabled": bool(enabled)}))
"""


def repowise_python(home: Path | None = None) -> str | None:
    """The interpreter of the installed repowise CLI (has yaml + repowise importable)."""
    exe = _bin("repowise", home or Path.home())
    if not exe:
        return None
    py = Path(os.path.realpath(exe)).parent / "python"
    return str(py) if os.access(py, os.X_OK) else None


def _helper(py: str, mode: str, repo: Path) -> bool | None:
    try:
        proc = subprocess.run(
            [py, "-I", "-c", _REPOWISE_HELPER, mode, str(repo), f"# {GUARD_LINE}\n"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return (
            not json.loads(proc.stdout)["rewrite_enabled"]
            if proc.returncode == 0
            else None
        )
    except OSError, subprocess.TimeoutExpired, ValueError, KeyError:
        return None


def _guarded_fallback(text: str) -> bool:
    """Stdlib approximation when the repowise CLI is absent: any indent, quoted booleans, comments."""
    off = r"[\"']?(false|off|no|0|deny|disable|disabled|none)[\"']?\s*(#.*)?$"
    lines = [ln.rstrip("\r") for ln in text.splitlines()]
    in_distill = in_commands = False
    distill_indent = commands_indent = -1
    for ln in lines:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        key = ln.strip()
        if indent == 0:
            in_distill = key.startswith("distill:")
            in_commands = False
            if in_distill and re.search(r"enabled:\s*" + off, key, re.I):
                return True  # flow style: distill: {enabled: false}
            if in_distill and re.search(
                r"commands:\s*\{[^}]*enabled:\s*(false|off|no)", key, re.I
            ):
                return True
            continue
        if not in_distill:
            continue
        if distill_indent < 0:
            distill_indent = indent
        if indent == distill_indent:
            in_commands = key.startswith("commands:")
            commands_indent = -1
            if re.fullmatch(r"enabled:\s*" + off, key, re.I):
                return True
            continue
        if in_commands:
            if commands_indent < 0:
                commands_indent = indent
            if indent == commands_indent and (
                re.fullmatch(r"enabled:\s*" + off, key, re.I)
                or re.fullmatch(r"permission:\s*" + off, key, re.I)
            ):
                return True
    return False


def guarded(repo: Path, py: str | None = None) -> bool:
    """True when repowise will NOT rewrite Bash commands in this repo."""
    py = py or repowise_python()
    if py:
        verdict = _helper(py, "check", repo)
        if verdict is not None:
            return verdict
    try:
        return _guarded_fallback(
            (repo / ".repowise" / "config.yaml").read_text(encoding="utf-8")
        )
    except OSError:
        return False


def apply_guard(repo: Path, py: str | None = None) -> str:
    cfg = repo / ".repowise" / "config.yaml"
    py = py or repowise_python()
    if guarded(repo, py):
        return f"ok: {repo}"
    if py and _helper(py, "set", repo) is True:
        return f"guarded: {repo}"
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    if re.search(r"^distill\s*:", text, re.M) or text.strip() in ("{}", "---", "..."):
        return f"MANUAL: {cfg}: set distill.commands.enabled: false"
    sep = "" if text.endswith("\n") or not text else "\n"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text + sep + GUARD_YAML, encoding="utf-8")
    return (
        f"guarded: {repo}"
        if guarded(repo, py)
        else f"MANUAL: {cfg}: guard not recognised"
    )


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
    repowise = _bin("repowise", home)
    if repowise and repowise_telemetry_enabled(repowise):
        # Persistent opt-out: Codex starts MCP servers with a cleared env, so DO_NOT_TRACK never reaches them.
        rc, _ = _run([repowise, "telemetry", "disable"])
        log.append(f"repowise telemetry disable: rc={rc}")
    log.extend(apply_guard(r) for r in repowise_repos(default_roots(home)))
    return log


def repowise_telemetry_enabled(repowise: str) -> bool:
    """True unless `repowise telemetry status` reports the persistent opt-out (env vars ignored)."""
    env = {k: v for k, v in os.environ.items() if k not in _TELEMETRY_ENV}
    try:
        out = subprocess.run(
            [repowise, "telemetry", "status"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=env,
        ).stdout
    except OSError, subprocess.TimeoutExpired:
        return True
    return re.search(r"^Telemetry:\s*disabled", out, re.M) is None


_TELEMETRY_ENV = ("DO_NOT_TRACK", "REPOWISE_TELEMETRY_DISABLED", "REPOWISE_OFFLINE")


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
    repowise = _bin("repowise", home)
    if repowise and repowise_telemetry_enabled(repowise):
        errors.append("repowise telemetry enabled (run `repowise telemetry disable`)")
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
        log = sync(manifest, home)
        _emit(log, "reviewer-plugins: up to date")
        failed = [
            ln for ln in log if re.search(r"rc=[1-9]", ln) or ln.startswith("MANUAL")
        ]
        return 1 if failed else 0
    errs = check(manifest, home)
    _emit(errs, "reviewer-plugins: all declared plugins installed and Connected")
    return 1 if errs else 0


def _emit(lines: list[str], empty: str) -> None:
    sys.stdout.write(("\n".join(lines) or empty) + "\n")


if __name__ == "__main__":
    sys.exit(main())
