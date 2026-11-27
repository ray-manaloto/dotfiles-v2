"""Read-only inventory of pc's bootstrap resources and runtime guards."""

import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Source: cpp-standard python/pctools/src/pctools/data/pc-kernel.json.
KERNEL = {
    "kernel.kptr_restrict": "0",
    "kernel.sysrq": "16",
    "kernel.printk": "0\t4\t1\t4",
    "kernel.core_pattern": "|/usr/lib/systemd/systemd-coredump %P %u %g %s %t %c %h %d %F %I",
    "fs.protected_regular": "1",
}
FENCE = {
    "iptables": (
        "-A INPUT -i br-gha -m conntrack --ctstate NEW -j DROP",
        "-A FORWARD -i br-gha -o tailscale0 -j DROP",
    ),
    "ip6tables": (
        "-A INPUT -i br-gha ! -p ipv6-icmp -m conntrack --ctstate NEW -j DROP",
    ),
}
# Match the names in config.pc.toml. Keep an independent baseline so removing
# a declaration itself still fails the doctor.
PC_BOOTSTRAP_REQUIRED = {
    "file:/etc/systemd/system/gha-slot@.service",
    "file:/etc/systemd/system/gha-fence.service",
    "file:/etc/gha-runner/fence.rules",
    "file:/etc/gha-runner/fence6.rules",
    "file:/etc/ufw/before.rules",
    "file:/etc/ufw/after.rules",
    "file:/etc/systemd/system/acu-trial.service",
    "file:/etc/modules-load.d/nct6775.conf",
    "file:/etc/systemd/system/h1-pump-full.service",
    "file:/etc/systemd/system/h1-radfan-curve.service",
    "file:/etc/systemd/system/hw-heartbeat.service",
    "file:/etc/systemd/system/hw-heartbeat.timer",
    "file:/etc/systemd/system/hw-watch.service",
    "file:/etc/systemd/system/hw-watch.timer",
    "file:/usr/local/sbin/gha-slot",
    "file:/usr/local/sbin/h1-pump-full",
    "file:/usr/local/sbin/h1-radfan-curve",
    "file:/usr/local/sbin/hw-watch",
    "file:~/.local/bin/agent-secrets",
    "file:/etc/cdi/nvidia.yaml",
    "service:gha-fence.service",
    "service:ufw",
    "service:gha-slot@glaze",
    "service:gha-slot@cpp-utilities",
    "service:gha-slot@dotfiles",
    "service:gha-slot@hft-challenges-prototype",
    "service:gha-slot@knowledge-base-v2",
    "service:acu-trial.service",
    "service:h1-pump-full.service",
    "service:h1-radfan-curve.service",
    "service:hw-heartbeat.timer",
    "service:hw-watch.timer",
    "service:nvidia-cdi-refresh.path",
    "service:nvidia-cdi-refresh.service",
    "user-service:agent-secrets",
}
PC_BOOTSTRAP_PACKAGES = {
    "package:pacman:lm_sensors",
    "package:pacman:nvme-cli",
    "package:pacman:zsh",
    "package:pacman:nvidia-container-toolkit",
}
# Mirrors manifest claude.plugins in unix/home/.config/mise/assets/reviewer-plugins.json. This script runs on pc
# from stdin and cannot read the manifest; tests/test_pc_host.py asserts the two stay equal.
REVIEWER_CLAUDE_PLUGINS = (
    "astral@astral-sh",
    "clangd-lsp@claude-plugins-official",
    "code-modernization@claude-plugins-official",
    "coderabbit@claude-plugins-official",
    "codex@openai-codex",
    "egm@egm",
    "eli5@claude-community",
    "fleet-agents@ray-manaloto-skills",
    "graphify@graphify",
    "greptile@claude-plugins-official",
    "html-plan@claude-community",
    "hyperresearch@hyperresearch",
    "last30days@last30days-skill",
    "mattpocock-skills@mattpocock",
    "next-steps@claude-community",
    "planning-with-files@planning-with-files",
    "plugin-dev@claude-plugins-official",
    "repowise@repowise",
    "skills@ray-manaloto-skills",
    "tokeneconomizer@tokeneconomizer",
    "typescript-native-lsp@dotfiles",
)
MISE = "/usr/local/bin/mise"
UPDATE = re.compile(r"\S+ \S+ -> \S+(?: \[(?:-?\d+[dhm]){1,2}\])?")


def run(
    *args: str, accepted: tuple[int, ...] = (0,), timeout: int = 20, **env: str
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ | env,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"{args[0]}: {type(error).__name__}") from error
    if result.returncode not in accepted:
        raise ValueError(f"{' '.join(args[:3])} exited {result.returncode}")
    return result


def bootstrap(check: str) -> str:
    flag, drift_exit = (
        ("--missing", 1) if check == "status" else ("--detailed-exitcode", 2)
    )
    result = run(
        "/usr/local/bin/mise",
        "bootstrap",
        check,
        "--json",
        flag,
        accepted=(0, drift_exit),
    )
    report = json.loads(result.stdout)
    if check == "status":
        entries = report["files"] + report["services"]
    else:
        entries = report["resources"]
    required = PC_BOOTSTRAP_REQUIRED
    if check == "plan":
        required = required | PC_BOOTSTRAP_PACKAGES
    home = f":{Path.home()}/"
    resources = {}
    for entry in entries:
        resource = entry["id"]
        key = f"{resource['kind']}:{resource['name']}".replace(home, ":~/")
        if (
            "pc" in (entry.get("origin") or {}).get("environment", [])
            or key in PC_BOOTSTRAP_PACKAGES
        ):
            resources[key] = entry["action"]
    if check == "status":
        for service in report["user_services"]:
            if service["name"] == "agent-secrets":
                resources["user-service:agent-secrets"] = service["action"]
    missing = required - resources.keys()
    if missing:
        raise ValueError("missing pc declarations: " + ", ".join(sorted(missing)))
    drift = [
        f"{name} ({action})" for name, action in resources.items() if action != "noop"
    ]
    if drift:
        raise ValueError("pc resources differ: " + ", ".join(drift))
    return f"{len(resources)} pc resources match"


def dead_shims() -> str:
    """Shims whose tool is gone: `mise which` fails for them (only suspects are probed one by one)."""
    data = Path(os.environ.get("MISE_DATA_DIR") or Path.home() / ".local/share/mise")
    try:
        shims = sorted(p.name for p in (data / "shims").iterdir())
    except OSError as error:
        raise ValueError(f"cannot list {data}/shims") from error
    # One call resolves every live tool's bin dir; a shim with a file there is alive.
    live: set[str] = set()
    for line in run(MISE, "bin-paths", timeout=60).stdout.splitlines():
        try:
            live.update(entry.name for entry in Path(line.strip()).iterdir())
        except OSError:
            continue
    dead, inactive = [], []
    for name in shims:
        if name in live or name == "mise":
            continue
        # accepted=(0, 1): read mise's own verdict instead of only its exit code.
        out = run(MISE, "which", name, accepted=(0, 1), timeout=15)
        if out.returncode == 0:
            continue
        # "is a mise bin however it is not currently active": the tool is installed but not in
        # the active config (e.g. hub:up's pinned ollama, or an older python). Not broken.
        (inactive if "not currently active" in out.stderr else dead).append(name)
    note = (
        f" ({len(inactive)} installed but inactive: {', '.join(inactive[:3])})"
        if inactive
        else ""
    )
    if dead:
        first = ", ".join(dead[:5]) + (", ..." if len(dead) > 5 else "")
        raise ValueError(
            f"{len(dead)} of {len(shims)} mise shims are dead (no installed tool provides them): "
            f"{first}; run `mise reshim`{note}"
        )
    return f"no dead mise shims among {len(shims)}{note}"


def reviewer_plugins() -> str:
    """Declared Claude reviewer plugins are installed and enabled (the Mac-only live check, via the host CLI)."""
    claude = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    try:
        rows = json.loads(run(claude, "plugin", "list", "--json", timeout=60).stdout)
    except (ValueError, TypeError) as error:
        raise ValueError(f"claude plugin list --json failed: {error}") from error
    have = {
        str(row.get("id"))
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict)
        and row.get("enabled") is not False
        and row.get("scope") in (None, "user")
    }
    missing = [p for p in REVIEWER_CLAUDE_PLUGINS if p not in have]
    if missing:
        raise ValueError(
            f"{len(missing)} claude plugins missing/disabled: {', '.join(missing)}; "
            "run `mise run bootstrap:reviewer-plugins`"
        )
    return f"{len(REVIEWER_CLAUDE_PLUGINS)} claude reviewer plugins installed"


def kernel_watch() -> str:
    values = dict(
        line.split(" = ", 1) for line in run("sysctl", *KERNEL).stdout.splitlines()
    )
    changed = [key for key, expected in KERNEL.items() if values.get(key) != expected]
    if changed:
        raise ValueError("kernel settings differ: " + ", ".join(changed))
    return f"{len(KERNEL)} kernel settings match"


def privileged() -> str:
    ids = run("docker", "ps", "--no-trunc", "--quiet").stdout.split()
    if ids:
        values = run(
            "docker",
            "inspect",
            "--type",
            "container",
            "--format",
            "{{.HostConfig.Privileged}}",
            *ids,
        )
        flags = values.stdout.splitlines()
        if len(flags) != len(ids) or set(flags) - {"true", "false"}:
            raise ValueError(
                "could not determine the --privileged flag for every running container"
            )
        if "true" in flags:
            raise ValueError(
                f"{flags.count('true')} running containers use --privileged"
            )
    return f"{len(ids)} running containers checked for --privileged"


def gpu_cdi() -> str:
    run(
        "pacman",
        "-Q",
        "nvidia-container-toolkit",
        "libnvidia-container",
        "nvidia-utils",
    )
    run(
        "systemctl",
        "is-enabled",
        "--quiet",
        "nvidia-cdi-refresh.path",
    )
    run("systemctl", "is-active", "--quiet", "nvidia-cdi-refresh.path")
    show = (
        "systemctl",
        "show",
        "--property=LoadState,UnitFileState,ActiveState,Result",
        "nvidia-cdi-refresh.service",
    )
    values = dict(line.split("=", 1) for line in run(*show).stdout.splitlines())
    if values.get("ActiveState") == "activating":
        time.sleep(2)
        values = dict(line.split("=", 1) for line in run(*show).stdout.splitlines())
    state = values.pop("ActiveState", None)
    if state not in {"inactive", "activating"} or values != {
        "LoadState": "loaded",
        "UnitFileState": "enabled",
        "Result": "success",
    }:
        raise ValueError("CDI refresh service is outside its successful oneshot state")
    if (
        os.path.lexists("/etc/cdi/nvidia.yaml")
        or not Path("/run/cdi/nvidia.yaml").is_file()
    ):
        raise ValueError("expected the NVIDIA CDI spec in /run/cdi only")
    devices = run("nvidia-ctk", "cdi", "list").stdout.splitlines()
    if devices.count("nvidia.com/gpu=all") != 1:
        raise ValueError("nvidia.com/gpu=all does not resolve exactly once")
    return "refresh units and generated device spec match"


def cooling() -> str:
    for unit in ("h1-pump-full.service", "h1-radfan-curve.service"):
        run("systemctl", "is-active", "--quiet", unit)

    try:
        chips = [
            name.parent
            for name in Path("/sys/class/hwmon").glob("hwmon*/name")
            if name.read_text().strip() == "nct6798"
        ]
        if len(chips) != 1:
            raise ValueError("expected one nct6798 cooling controller")
        pump = int((chips[0] / "fan2_input").read_text())
        fan = int((chips[0] / "fan5_input").read_text())
    except OSError as error:
        raise ValueError("could not read cooling speeds from hwmon") from error

    if pump < 1500 or fan < 400:
        raise ValueError(
            f"cooling speeds below threshold: pump {pump} RPM, fan {fan} RPM"
        )
    return f"both units active, pump {pump} RPM, radiator fan {fan} RPM"


def pending_updates() -> str:
    summaries = []
    with tempfile.TemporaryDirectory(prefix="pc-doctor-") as directory:
        for source, args, empty_exit in (
            ("official", ("checkupdates",), 2),
            ("AUR", ("yay", "-Qua"), 1),
        ):
            try:
                result = run(
                    *args,
                    accepted=(0, empty_exit),
                    timeout=45,
                    CHECKUPDATES_DB=f"{directory}/checkupdates",
                    XDG_CACHE_HOME=directory,
                )
                lines = result.stdout.splitlines()
                if result.returncode == empty_exit:
                    if lines or result.stderr.strip():
                        raise ValueError("unexpected output from empty result")
                elif not lines or any(not UPDATE.fullmatch(line) for line in lines):
                    raise ValueError("unexpected update listing")
                summaries.append(f"{source}: {len(lines)} waiting")
                for line in lines:
                    sys.stdout.write(f"      {source} {line}\n")
            except ValueError as error:
                summaries.append(f"{source}: unknown ({error})")
    summary = ", ".join(summaries)
    if "unknown" in summary:
        raise ValueError(summary)
    return summary


def fence_drops() -> str:
    mismatches = []
    for program, rules in FENCE.items():
        listed = run("sudo", "-n", program, "-t", "mangle", "-S").stdout.splitlines()
        if not any(line.startswith("-P INPUT ") for line in listed):
            raise ValueError(f"{program} did not list the mangle INPUT chain")
        mismatches.extend(
            f"{program} {rule}" for rule in rules if listed.count(rule) != 1
        )
    if mismatches:
        raise ValueError("missing or duplicate drops: " + ", ".join(mismatches))
    return "each of the 3 fence drops is present once"


def apt_cache_route() -> str:
    destination = run(
        "docker",
        "inspect",
        "--type",
        "container",
        "--format",
        '{{with index .NetworkSettings.Networks "apt-cache"}}{{.IPAddress}}{{end}}',
        "acu-trial",
    ).stdout.strip()
    ipaddress.IPv4Address(destination)
    rules = run("sudo", "-n", "ufw", "show", "added").stdout.splitlines()
    expected_tail = [
        "to",
        destination,
        "port",
        "3142",
        "proto",
        "tcp",
        "comment",
        "apt-cache",
    ]
    routes = [
        shlex.split(line)
        for line in rules
        if line.startswith("ufw route ") and "apt-cache" in line
    ]
    if len(routes) != 1:
        raise ValueError("expected exactly one apt-cache route")
    route = routes[0]
    if (
        route[:7] != ["ufw", "route", "allow", "in", "on", "tailscale0", "from"]
        or route[8:] != expected_tail
    ):
        raise ValueError(
            "apt-cache route differs from the running cache destination or policy"
        )
    source = ipaddress.IPv4Address(route[7])
    if source not in ipaddress.IPv4Network("100.64.0.0/10"):
        raise ValueError("apt-cache route source is outside the tailnet address range")
    return "TCP 3142 route is limited to one tailnet source and the running cache"


def main() -> int:
    os.environ.update(
        MISE_AUTO_INSTALL="0",
        MISE_NOT_FOUND_AUTO_INSTALL="0",
        MISE_ENV="linux,pc",
        MISE_SYSTEM_CONFIG_DIR="/nonexistent",
        MISE_YES="1",
    )
    checks = (
        ("bootstrap-status", lambda: bootstrap("status")),
        ("bootstrap-plan", lambda: bootstrap("plan")),
        ("dead-mise-shims", dead_shims),
        ("reviewer-plugins", reviewer_plugins),
        ("kernel-watch", kernel_watch),
        ("docker-privileged-flag", privileged),
        ("gpu-cdi", gpu_cdi),
        ("cooling", cooling),
        ("pending-updates", pending_updates),
        ("fence-drops", fence_drops),
        ("apt-cache-route", apt_cache_route),
    )
    ok = True
    for name, check in checks:
        try:
            message = check()
            sys.stdout.write(f"PASS  {name}: {message}\n")
        except (ValueError, KeyError, TypeError) as error:
            ok = False
            sys.stdout.write(f"FAIL  {name}: {error}\n")
        sys.stdout.flush()
    return int(not ok)


if __name__ == "__main__":
    raise SystemExit(main())
