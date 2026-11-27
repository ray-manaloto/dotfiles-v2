"""tasks/py/py39_compat.py: files run by /usr/bin/python3 (3.9 on macOS) must use 3.9 syntax (#112)."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tasks/py/py39_compat.py"


def _run(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-B", str(SCRIPT), *map(str, paths)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "source",
    [
        "try:\n    pass\nexcept OSError, ValueError:\n    pass\n",
        "match 1:\n    case 1:\n        pass\n",
    ],
)
def test_rejects_post_39_syntax(tmp_path: Path, source: str) -> None:
    f = tmp_path / "hook.py"
    f.write_text(source)
    proc = _run(f)
    assert proc.returncode == 1
    assert "not Python 3.9 syntax" in proc.stderr


def test_accepts_39_syntax_and_reports_count(tmp_path: Path) -> None:
    f = tmp_path / "hook.py"
    f.write_text("try:\n    pass\nexcept (OSError, ValueError):\n    pass\n")
    proc = _run(f)
    assert proc.returncode == 0, proc.stderr
    assert "1 file(s) clean" in proc.stdout


def test_real_targets_pass_and_globs_match_ruff_override() -> None:
    hk = (ROOT / "hk.pkl").read_text()
    block = hk[hk.index('["py39-compat"]') :]
    block = block[: block.index("check =")]
    globs = re.findall(r'"([^"]+)"', block.split("List(", 1)[1])
    ruff = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["ruff"]
    assert set(ruff["per-file-target-version"]) == set(globs)
    assert set(ruff["per-file-target-version"].values()) == {"py39"}
    files = sorted({p for g in globs for p in ROOT.glob(g)})
    assert files
    proc = _run(*files)
    assert proc.returncode == 0, proc.stderr


def test_pc_ollama_pin_matches_hub_up() -> None:
    """config.pc.toml declares ollama so prune keeps it (#117); hub:up links that same version."""
    pc = tomllib.loads((ROOT / "unix/home/.config/mise/config.pc.toml").read_text())
    hub = re.search(r'^ollama_ver="([^"]+)"', (ROOT / "tasks/hub/up").read_text(), re.M)
    assert hub and pc["tools"]["ollama"] == hub.group(1)
    lock = tomllib.loads((ROOT / "unix/home/.config/mise/mise.pc.lock").read_text())
    assert lock["tools"]["ollama"][0]["version"] == hub.group(1)
