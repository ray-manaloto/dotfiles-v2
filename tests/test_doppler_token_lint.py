"""doppler-token-lint: no tracked file stores the Doppler token in the system keyring (#91)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINT = ROOT / "tasks/py/doppler_token_lint.py"
# Split so this test file never matches the lint itself.
CMD = "doppler configure " + "set"


def run(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-B", str(LINT), *map(str, paths)],
        capture_output=True,
        text=True,
        check=False,
    )


def write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_flags_keyring_token_invocations(tmp_path: Path) -> None:
    bad = write(
        tmp_path,
        "setup.sh",
        f'{CMD} token "$tok" --scope ~\n'
        f"{CMD} token=$tok project=x\n"
        f"  ssh pc '{CMD}  token -' </dev/null\n"
        f'subprocess.run(["sh", "-c", "{CMD} token x"])\n',
    )
    proc = run(bad)
    assert proc.returncode == 1
    assert proc.stderr.count("doppler-keyring-token") == 4


def test_other_doppler_commands_and_comments_pass(tmp_path: Path) -> None:
    good = write(
        tmp_path,
        "ok.sh",
        "doppler configure unset token --scope ~\n"
        "doppler configure get token --plain\n"
        f"{CMD} project=x config=dev\n"
        f"# never {CMD} token; use a 0600 file\n"
        f"// {CMD} token is forbidden\n",
    )
    proc = run(good)
    assert proc.returncode == 0, proc.stderr


def test_allow_marker_needs_a_reason(tmp_path: Path) -> None:
    ok = write(
        tmp_path,
        "ok.sh",
        f"# doppler-token: ok throwaway VM, keyring wiped on teardown\n{CMD} token x\n"
        f"{CMD} token y  # doppler-token: ok same-line reason\n",
    )
    assert run(ok).returncode == 0
    no_reason = write(tmp_path, "no_reason.sh", f"# doppler-token: ok\n{CMD} token x\n")
    assert run(no_reason).returncode == 1


def test_skips_binary_and_missing_files(tmp_path: Path) -> None:
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"\0" + f"{CMD} token x\n".encode())
    assert run(blob, tmp_path / "missing.sh", tmp_path).returncode == 0


def test_tracked_files_are_clean() -> None:
    files = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    proc = run(*(ROOT / f for f in files if f))
    assert proc.returncode == 0, proc.stderr
