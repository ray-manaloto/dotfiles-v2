"""Read-only inventory of pc's bootstrap resources and runtime guards."""

import ipaddress
import json
import os
import re
import shlex
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
    "file:/usr/local/sbin/gha-slot",
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
    "service:nvidia-cdi-refresh.path",
    "service:nvidia-cdi-refresh.service",
    "user-service:agent-secrets",
}
PC_BOOTSTRAP_PACKAGES = {
    "package:pacman:zsh",
    "package:pacman:nvidia-container-toolkit",
}
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
        MISE_ENV="linux,pc",
        MISE_SYSTEM_CONFIG_DIR="/nonexistent",
        MISE_YES="1",
    )
    checks = (
        ("bootstrap-status", lambda: bootstrap("status")),
        ("bootstrap-plan", lambda: bootstrap("plan")),
        ("kernel-watch", kernel_watch),
        ("docker-privileged-flag", privileged),
        ("gpu-cdi", gpu_cdi),
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
