"""Exercise the real bootstrap:skills script with offline stand-ins."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MISE_STUB = """\
case "$*" in
    "-C $HOME skills ls --json") printf '[]\\n' ;;
    'skills sync --global --prune') exit 0 ;;
    *) printf 'unexpected mise invocation: %s\\n' "$*" >&2; exit 1 ;;
esac
"""
AGENTSVIEW_STUB = """\
printf '%s\\0' "$@" >> "$HOME/agentsview.argv"
skill="$HOME/.agents/skills/agentsview-finding-history"
mkdir -p "$skill"
printf '%s\\n' 'generated-by: agentsview' > "$skill/SKILL.md"
"""
WRAPPER_STUB = """\
printf 'called\\n' > "$HOME/wrapper-called"
exit 99
"""
EXPECTED_ARGV = b"skills\x00install\x00--harness\x00agents\x00--force\x00"


def _stub(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\nset -eu\n{body}", encoding="utf-8")
    path.chmod(0o700)


def _check_case(
    task: Path, home: Path, *, with_agentsview: bool, with_wrapper: bool
) -> None:
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True)
    # The child sees only local utilities and the stand-ins, even on a host
    # with real AgentsView, mise, or marketplace clients installed.
    for name in ("bash", "dirname", "grep", "mkdir", "readlink", "rm"):
        tool = shutil.which(name, path=os.defpath)
        if tool is None:
            raise RuntimeError(f"required system utility {name} is missing")
        (bin_dir / name).symlink_to(tool)
    (bin_dir / "python3").symlink_to(sys.executable)
    _stub(bin_dir / "mise", MISE_STUB)
    if with_agentsview:
        _stub(bin_dir / "agentsview", AGENTSVIEW_STUB)

    search_path = [bin_dir]
    if with_wrapper:
        wrapper_dir = home / "command-wrappers" / "bin"
        wrapper_dir.mkdir(parents=True)
        _stub(wrapper_dir / "agentsview", WRAPPER_STUB)
        search_path.insert(0, wrapper_dir)

    result = subprocess.run(
        [str(task)],
        cwd=home,
        env={
            "HOME": str(home),
            "PATH": os.pathsep.join(map(str, search_path)),
            "MISE_OFFLINE": "1",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if (home / "wrapper-called").exists():
        raise RuntimeError(
            "invoked the mise wrapper with no ~/.local/bin/agentsview target"
        )
    if result.returncode:
        raise RuntimeError(
            f"bootstrap:skills exited {result.returncode}: {result.stderr.strip()}"
        )

    argv_file = home / "agentsview.argv"
    if with_agentsview:
        actual = argv_file.read_bytes() if argv_file.exists() else None
        if actual != EXPECTED_ARGV:
            raise RuntimeError(
                f"expected AgentsView argv {EXPECTED_ARGV!r}, got {actual!r}"
            )
        if not (home / ".agents/skills/agentsview-finding-history/SKILL.md").is_file():
            raise RuntimeError("AgentsView skill was not installed in the scratch home")
    else:
        warned = any(
            line.startswith("warn") and "agentsview" in line and "skip" in line
            for line in result.stderr.lower().splitlines()
        )
        if argv_file.exists() or not warned:
            raise RuntimeError(
                f"expected a warning about skipping AgentsView: {result.stderr.strip()}"
            )


def check_bootstrap_skills(task: Path) -> int:
    cases = (
        ("AgentsView present", True, False),
        ("AgentsView missing", False, False),
        ("dangling mise wrapper before AgentsView", True, True),
        ("dangling mise wrapper only", False, True),
    )
    with tempfile.TemporaryDirectory(prefix="doctor-bootstrap-skills-") as tmp:
        for index, (label, with_agentsview, with_wrapper) in enumerate(cases):
            try:
                _check_case(
                    task,
                    Path(tmp) / str(index),
                    with_agentsview=with_agentsview,
                    with_wrapper=with_wrapper,
                )
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                sys.stderr.write(f"FAIL bootstrap:skills ({label}): {exc}\n")
                return 1
            sys.stdout.write(f"PASS bootstrap:skills ({label})\n")
    return 0


if __name__ == "__main__":
    repo = Path(__file__).resolve().parents[2]
    raise SystemExit(
        check_bootstrap_skills(repo / "unix/home/.config/mise/tasks/bootstrap/skills")
    )
