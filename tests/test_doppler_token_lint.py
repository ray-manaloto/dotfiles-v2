"""doppler-token-lint: no tracked file stores the Doppler token in the system keyring (#91)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

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


def materialize(body: str) -> str:
    return body.replace("DPL", "doppler")


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


# Bodies below spell the binary `DPL` so this file stays clean; `materialize` swaps it in.
@pytest.mark.parametrize(
    ("body", "line"),
    [
        pytest.param(
            'subprocess.run(["DPL", "configure", "set", "token", tok])\n',
            1,
            id="argv-list",
        ),
        pytest.param(
            'cmd = [\n    "DPL",\n    # keyring\n    "configure",\n    "set",\n    "token",\n    tok,\n]\n',
            1,
            id="argv-list-one-per-line",
        ),
        pytest.param(
            'true\nDPL configure set \\\n  token "$tok" --scope ~\n',
            2,
            id="continuation",
        ),
        pytest.param(
            "DPL \\\n  configure \\\n  set \\\n  token x\n",
            1,
            id="continuation-every-word",
        ),
        pytest.param(
            "DPL --no-check-version configure set token x\n",
            1,
            id="flag-before-configure",
        ),
        pytest.param(
            "DPL configure --scope / set token x\n", 1, id="flag-value-before-set"
        ),
        pytest.param(
            "DPL configure set --scope=/ project=p token=x\n", 1, id="token-after-pairs"
        ),
        pytest.param(
            "/usr/local/bin/DPL configure set token x\n", 1, id="absolute-path"
        ),
        pytest.param(
            '    check =\n      #"DPL configure set token x"#\n', 2, id="pkl-raw-string"
        ),
        pytest.param(
            '      ##"DPL configure set token x"##\n', 1, id="pkl-raw-string-2"
        ),
        pytest.param("cd /tmp; DPL configure set token x\n", 1, id="after-terminator"),
        pytest.param(
            'echo "a # b"; DPL configure set token x\n', 1, id="hash-in-double-quotes"
        ),
        pytest.param(
            "echo 'a // b' && DPL configure set token x\n",
            1,
            id="slashes-in-single-quotes",
        ),
        pytest.param(
            'echo "a \\" # b"; DPL configure set token x\n',
            1,
            id="escaped-quote-then-hash",
        ),
    ],
)
def test_flags_evasion_forms(tmp_path: Path, body: str, line: int) -> None:
    proc = run(write(tmp_path, "bad.txt", materialize(body)))
    assert proc.returncode == 1, body
    assert proc.stderr.count("doppler-keyring-token") == 1, proc.stderr
    assert f"bad.txt:{line}:" in proc.stderr


def test_other_doppler_commands_and_comments_pass(tmp_path: Path) -> None:
    good = write(
        tmp_path,
        "ok.sh",
        materialize(
            "DPL configure unset token --scope ~\n"
            "DPL configure get token --plain\n"
            "DPL configure set project=x config=dev\n"
            "DPL configure set project=x  # token stays in the file\n"
            "DPL configure set project=x; echo token\n"
            "DPL configure set project x token\n"
            "DPL run -- ./configure set token\n"
            "# never DPL configure set token; use a 0600 file\n"
            "#!/bin/sh DPL configure set token\n"
            "// DPL configure set token is forbidden\n"
            "x = 1  // DPL configure set token\n"
        ),
    )
    proc = run(good)
    assert proc.returncode == 0, proc.stderr


def test_allow_marker(tmp_path: Path) -> None:
    ok = write(
        tmp_path,
        "ok.sh",
        materialize(
            "# doppler-token: ok throwaway VM, keyring wiped on teardown\nDPL configure set token x\n"
            "DPL configure set token y  # doppler-token: ok same-line reason\n"
            "DPL configure set \\\n  token z  # doppler-token: ok on the continuation\n"
        ),
    )
    assert run(ok).returncode == 0, run(ok).stderr


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            "# doppler-token: ok\nDPL configure set token x\n", id="no-reason"
        ),
        pytest.param(
            "<!-- doppler-token: ok -->\nDPL configure set token x\n",
            id="html-terminator",
        ),
        pytest.param(
            "DPL configure set token x  /* doppler-token: ok */\n", id="c-terminator"
        ),
        pytest.param(
            "DPL configure set token x  # doppler-token: ok reviewed\nDPL configure set token y\n",
            id="same-line-marker-does-not-bleed",
        ),
    ],
)
def test_allow_marker_rejected(tmp_path: Path, body: str) -> None:
    proc = run(write(tmp_path, "bad.sh", materialize(body)))
    assert proc.returncode == 1
    assert proc.stderr.count("doppler-keyring-token") == 1, proc.stderr


def test_skips_binary_and_missing_files(tmp_path: Path) -> None:
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"\0" + f"{CMD} token x\n".encode())
    assert run(blob, tmp_path / "missing.sh", tmp_path).returncode == 0


def test_invalid_utf8_is_scanned(tmp_path: Path) -> None:
    bad = tmp_path / "latin1.sh"
    bad.write_bytes(b"# calf\xe9\n" + f"{CMD} token x\n".encode())
    proc = run(bad)
    assert proc.returncode == 1, proc.stderr
    assert "latin1.sh:2:" in proc.stderr


def test_tracked_files_are_clean() -> None:
    files = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    proc = run(*(ROOT / f for f in files if f))
    assert proc.returncode == 0, proc.stderr
