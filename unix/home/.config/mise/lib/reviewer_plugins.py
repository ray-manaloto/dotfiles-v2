"""Declarative user-scope harness plugins and existing reviewer integration checks.

Stdlib only. The manifest (assets/reviewer-plugins.json) is the single source of truth for:
  sync   - idempotently add marketplaces / install plugins via each harness CLI (user scope),
           and apply the Repowise rewrite guard.
  check  - report drift: missing plugins, Claude plugin MCPs not Connected, unguarded repos.
  guard  - ensure every Repowise-indexed repo turns off the auto-approved Bash rewrite hook.

OpenCode entries live directly in ~/.config/opencode/opencode.json (a managed dotfile); the
manifest lists them for repo and live checks.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

MISE_DIR = Path(__file__).resolve().parent.parent
MANIFEST = MISE_DIR / "assets" / "reviewer-plugins.json"
GUARD_LINE = "Disables the auto-approved Bash rewrite (dotfiles-v2 repowise guard)."
GUARD_YAML = f"# {GUARD_LINE}\ndistill:\n  commands:\n    enabled: false\n"


def load_manifest(path: Path = MANIFEST) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


PLATFORMS = {
    "os": {"macos", "linux"},
    "arch": {"amd64", "arm64"},
    "env": {"host", "vm", "container"},
}


def gate_errors(spec: dict) -> list[str]:
    """Reject unknown gated items, dimensions, and values before syncing."""
    gates = spec.get("platforms", {})
    if not isinstance(gates, dict):
        return ["platforms must map declared plugin IDs to platform selectors"]
    errors = []
    for name, gate in gates.items():
        if (
            name not in spec.get("plugins", ())
            or not isinstance(gate, dict)
            or not gate
        ):
            errors.append(f"invalid platform gate for {name}")
            continue
        for key, values in gate.items():
            if (
                key not in PLATFORMS
                or not isinstance(values, list)
                or not values
                or any(
                    not isinstance(v, str) or v not in PLATFORMS[key] for v in values
                )
            ):
                errors.append(f"invalid {key} selector for {name}")
    return errors


def _platform(home: Path) -> dict[str, str]:
    system = {"darwin": "macos", "linux": "linux"}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    env = "host"
    # Docker runs in this repo set the lowercase container variable.
    in_container = os.environ.get("container") == "docker"  # noqa: SIM112
    if in_container or Path("/.dockerenv").is_file():
        env = "container"
    elif (home / ".config/dotfiles-v2/vm-env.sh").is_file():
        env = "vm"
    return {"os": system, "arch": arch, "env": env}


def _applies(spec: dict, name: str, host: dict[str, str]) -> bool:
    return all(
        host[key] in values
        for key, values in spec.get("platforms", {}).get(name, {}).items()
    )


def _marketplaces(spec: dict, host: dict[str, str]) -> dict[str, str]:
    """Gate a marketplace when every declared plugin using it is gated out."""
    marketplaces = {}
    for name, source in spec.get("marketplaces", {}).items():
        ids = [pid for pid in spec.get("plugins", []) if pid.partition("@")[2] == name]
        if not ids or any(_applies(spec, pid, host) for pid in ids):
            marketplaces[name] = source
    return marketplaces


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
    except (OSError, subprocess.TimeoutExpired) as exc:  # fmt: skip
        return 1, str(exc)
    if proc.returncode != 0:
        return proc.returncode, (proc.stderr or proc.stdout).strip()
    return proc.returncode, proc.stdout


FAIL = "FAIL: "


def _result(label: str, rc: int, out: str) -> str:
    """One sync log line. Any non-zero rc (incl. negative: killed by a signal) is prefixed FAIL."""
    if rc == 0:
        return f"{label}: rc=0"
    tail = out.strip().splitlines()[-1][:200] if out.strip() else ""
    return f"{FAIL}{label}: rc={rc} {tail}".rstrip()


def sync_failed(log: list[str]) -> bool:
    return any(ln.startswith((FAIL, "MANUAL")) for ln in log)


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
    path = shutil.which(name)
    if path and _dead_mise_shim(path, name):
        return None
    return path


def _dead_mise_shim(path: str, name: str) -> bool:
    """A mise shim whose tool is no longer installed (e.g. left by another mise config).

    Shims are symlinks to the mise binary. `mise which` resolves without auto-installing.
    """
    target = os.path.realpath(path)
    if os.path.basename(target) != "mise" or name == "mise":
        return False
    rc, _ = _run([target, "which", name], timeout=30)
    return rc != 0


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


# Total time all MCP status retries in one check() may spend (doctor timeout is 420s).
MCP_RETRY_BUDGET_S = 60.0


def claude_mcp_status_settled(
    claude: str,
    name: str,
    attempts: int = 3,
    delay: float = 5.0,
    sleep: Callable[[float], None] | None = None,
    deadline: float | None = None,
) -> str | None:
    """Re-read a non-Connected status so a brief reconnect ("Needs authentication") never reads as a sign-in task.

    Retries stop at `deadline` (a `time.monotonic()` value) so slow lookups across several
    servers stay inside the doctor timeout.
    """
    sleep = sleep or time.sleep
    status = claude_mcp_status(claude, name)
    for _ in range(attempts - 1):
        # None/"" = unregistered or `claude mcp get` failed/timed out: not transient, and
        # retrying a hung CLI would blow the doctor timeout. Only a reported status is re-read.
        if not status or is_connected(status):
            break
        if deadline is not None and time.monotonic() + delay >= deadline:
            break
        sleep(delay)
        status = claude_mcp_status(claude, name)
    return status


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
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError):  # fmt: skip
        return None


def _guarded_fallback(text: str) -> bool:
    """Stdlib approximation when the repowise CLI is absent: any indent, quoted booleans, comments."""
    off = r"[\"']?(false|off|no|0)[\"']?\s*(#.*)?$"  # repowise parse_flag FALSY
    perm_off = (
        r"[\"']?(off|deny|disable|disabled|none|false)[\"']?\s*(#.*)?$"  # _OFF_VALUES
    )
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
                or re.fullmatch(r"permission:\s*" + perm_off, key, re.I)
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
    return [home / "dev", home / ".codex" / "worktrees"]


# --- sync / check -------------------------------------------------------------------------


def sync(manifest: dict, home: Path, *, plugins_only: bool = False) -> list[str]:
    log = [
        f"{FAIL}{harness}: {err}"
        for harness, spec in manifest.items()
        for err in gate_errors(spec)
    ]
    if log:
        return log
    host = _platform(home)
    claude = _bin("claude", home)
    if claude and "claude" in manifest:
        spec = manifest["claude"]
        plugins, mps = claude_state(claude)
        for name, src in _marketplaces(spec, host).items():
            if name not in mps:
                rc, out = _run([claude, "plugin", "marketplace", "add", src])
                log.append(_result(f"claude marketplace add {src}", rc, out))
        for pid in spec.get("plugins", []):
            if pid not in plugins and _applies(spec, pid, host):
                rc, out = _run([claude, "plugin", "install", pid])
                log.append(_result(f"claude plugin install {pid}", rc, out))
    codex = _bin("codex", home)
    if codex and "codex" in manifest:
        spec = manifest["codex"]
        installed, mps = codex_state(codex)
        for name, src in _marketplaces(spec, host).items():
            if name not in mps:
                rc, out = _run([codex, "plugin", "marketplace", "add", src])
                log.append(_result(f"codex marketplace add {src}", rc, out))
        for pid in spec.get("plugins", []):
            if pid not in installed and _applies(spec, pid, host):
                rc, out = _run([codex, "plugin", "add", pid])
                log.append(_result(f"codex plugin add {pid}", rc, out))
    agy = _bin("agy", home)
    if agy and "agy" in manifest:
        have = agy_state(agy)
        spec = manifest["agy"]
        for name, target in spec.get("plugins", {}).items():
            if name not in have and _applies(spec, name, host):
                rc, out = _run([agy, "plugin", "install", target])
                log.append(_result(f"agy plugin install {target}", rc, out))
    if plugins_only:
        return log
    repowise = _bin("repowise", home)
    if repowise and repowise_telemetry_enabled(home):
        # Persistent opt-out: Codex starts MCP servers with a cleared env, so DO_NOT_TRACK never reaches them.
        rc, out = _run(
            [repowise, "telemetry", "disable"],
        )
        log.append(_result("repowise telemetry disable", rc, out))
    for repo in repowise_repos(default_roots(home)):
        try:
            log.append(apply_guard(repo))
        except OSError as exc:
            log.append(f"{FAIL}repowise guard {repo}: {exc}")
    return log


def repowise_telemetry_enabled(home: Path) -> bool:
    """True unless repowise's persistent consent (~/.repowise/platform.json) is off.

    Read the file directly: `repowise telemetry status` is itself instrumented, and Codex starts MCP
    servers with a cleared env, so only the persisted opt-out covers every harness.
    """
    try:
        state = json.loads(
            (home / ".repowise" / "platform.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):  # fmt: skip
        return True
    return not (isinstance(state, dict) and state.get("telemetry_enabled") is False)


def check(
    manifest: dict,
    home: Path,
    *,
    require_connected: bool = True,
    plugins_only: bool = False,
) -> list[str]:
    errors = [
        f"{harness}: {err}"
        for harness, spec in manifest.items()
        for err in gate_errors(spec)
    ]
    if errors:
        return errors
    host = _platform(home)
    claude = _bin("claude", home)
    if claude and "claude" in manifest:
        spec = manifest["claude"]
        plugins, mps = claude_state(claude)
        errors += [
            f"claude marketplace missing: {name}"
            for name in _marketplaces(spec, host)
            if name not in mps
        ]
        errors += [
            f"claude plugin missing/disabled: {p}"
            for p in spec.get("plugins", [])
            if p not in plugins and _applies(spec, p, host)
        ]
        if require_connected and not plugins_only:
            retry_deadline = time.monotonic() + MCP_RETRY_BUDGET_S
            for name in spec.get("connected_mcps", []):
                plugin = name.partition(":")[2].partition(":")[0]
                if not any(
                    p.partition("@")[0] == plugin and _applies(spec, p, host)
                    for p in spec.get("plugins", [])
                ):
                    continue
                status = claude_mcp_status_settled(
                    claude, name, deadline=retry_deadline
                )
                if not is_connected(status):
                    errors.append(
                        f"claude MCP {name} not Connected (status: {status or 'missing'})"
                    )
    codex = _bin("codex", home)
    if codex and "codex" in manifest:
        spec = manifest["codex"]
        installed, mps = codex_state(codex)
        errors += [
            f"codex marketplace missing: {name}"
            for name in _marketplaces(spec, host)
            if name not in mps
        ]
        errors += [
            f"codex plugin missing/disabled: {p}"
            for p in spec.get("plugins", [])
            if p not in installed and _applies(spec, p, host)
        ]
    agy = _bin("agy", home)
    if agy and "agy" in manifest:
        spec = manifest["agy"]
        have = agy_state(agy)
        errors += [
            f"agy plugin missing/disabled: {n}"
            for n in spec.get("plugins", {})
            if n not in have and _applies(spec, n, host)
        ]
    if _bin("opencode", home) and "opencode" in manifest:
        spec = manifest["opencode"]
        config_dir = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        try:
            config = json.loads(
                (config_dir / "opencode/opencode.json").read_text(encoding="utf-8")
            )
            if not isinstance(config, dict):
                raise ValueError
        except (OSError, ValueError):  # fmt: skip
            errors.append(
                "opencode: cannot read user opencode.json (run mise dot apply)"
            )
        else:
            plugins = config.get("plugins")
            plugins = plugins if isinstance(plugins, list) else []
            errors += [
                f"opencode plugin missing: {name} (run mise dot apply)"
                for name in spec.get("plugins", [])
                if name not in plugins and _applies(spec, name, host)
            ]
            mcp = config.get("mcp") or {}
            servers = mcp.get("servers") if isinstance(mcp, dict) else {}
            servers = servers if isinstance(servers, dict) else {}
            errors += [
                f"opencode MCP missing/disabled: {name} (run mise dot apply)"
                for name in spec.get("mcp_servers", [])
                if not isinstance(servers.get(name), dict)
                or servers[name].get("disabled") is True
            ]
    if plugins_only:
        return errors
    repowise = _bin("repowise", home)
    if repowise and repowise_telemetry_enabled(home):
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
    sub.add_parser("sync").add_argument(
        "--plugins-only",
        action="store_true",
        help="skip Repowise telemetry and project writes",
    )
    sub.add_parser("check").add_argument(
        "--plugins-only",
        action="store_true",
        help="check installs without Claude OAuth or Repowise host checks",
    )
    g = sub.add_parser("guard")
    g.add_argument("repos", nargs="*", type=Path)
    g.add_argument(
        "--under", type=Path, help="also guard every indexed repo below this folder"
    )
    args = ap.parse_args(argv)
    home = Path.home()
    if args.cmd == "guard":
        repos = [p.resolve() for p in args.repos]
        if args.under:
            under = args.under.resolve()
            repos += [under, *repowise_repos([under])]
        repos = sorted(set(repos)) or repowise_repos(default_roots(home))
        out = [apply_guard(r) for r in repos if (r / ".repowise").is_dir()]
        _emit(out, "repowise: no indexed repos")
        return 1 if any(o.startswith("MANUAL") for o in out) else 0
    manifest = load_manifest()
    if args.cmd == "sync":
        log = sync(manifest, home, plugins_only=args.plugins_only)
        _emit(log, "reviewer-plugins: up to date")
        return 1 if sync_failed(log) else 0
    errs = check(manifest, home, plugins_only=args.plugins_only)
    _emit(errs, "reviewer-plugins: applicable checks passed")
    return 1 if errs else 0


def _emit(lines: list[str], empty: str) -> None:
    sys.stdout.write(("\n".join(lines) or empty) + "\n")


if __name__ == "__main__":
    sys.exit(main())
