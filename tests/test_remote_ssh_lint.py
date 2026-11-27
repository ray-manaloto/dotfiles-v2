"""remote-ssh-lint: flags remote-shell hazards in ssh calls, passes the safe shapes (#103)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

LINT = Path(__file__).resolve().parents[1] / "tasks" / "py" / "remote_ssh_lint.py"

BAD = """#!/usr/bin/env bash
ssh -o BatchMode=yes "${host}" "tar -xf - && rm -f '${d}'/._*"
ssh "${host}" env K="${v}" /bin/bash -euo pipefail <<'R'
true
R
ssh -o BatchMode=yes \\
\t"${host}" "ls /etc/*.conf"
ssh "${host}" 'ls /tmp/y?'
"""

GOOD = """#!/usr/bin/env bash
ssh -o BatchMode=yes "${host}" "find '${d}' -maxdepth 1 -name '._*' -type f"
printf -v cmd 'env K=%q /bin/bash -euo pipefail' "${v}"
ssh -o BatchMode=yes "${host}" "${cmd}" <<'R'
rm -f /tmp/x*
R
ssh "${host}" "true; echo rc=\\$?"
x="$(ssh "${host}" 'tailscale ip -4 | head -n1')"
"""


def run(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    f = tmp_path / "task"
    f.write_text(body, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-I", "-B", str(LINT), str(f)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_flags_each_hazard(tmp_path: Path) -> None:
    p = run(tmp_path, BAD)
    assert p.returncode == 1
    lines = p.stderr.strip().splitlines()
    assert [ln.split(": ", 2)[1] for ln in lines] == [
        "ssh-glob",
        "ssh-env",
        "ssh-glob",
        "ssh-glob",
    ]
    assert [ln.split(":")[1] for ln in lines] == ["2", "3", "6", "8"]


def test_safe_shapes_pass(tmp_path: Path) -> None:
    p = run(tmp_path, GOOD)
    assert p.returncode == 0, p.stderr
    assert "1 shell task(s) clean" in p.stdout


def test_repo_tasks_are_clean() -> None:
    root = LINT.parents[2]
    files = sorted(
        str(p)
        for d in ("tasks/hub", "tasks/pc")
        for p in (root / d).iterdir()
        if p.is_file()
    )
    p = subprocess.run(
        [sys.executable, "-I", "-B", str(LINT), *files],
        capture_output=True,
        text=True,
        check=False,
    )
    assert p.returncode == 0, p.stderr
