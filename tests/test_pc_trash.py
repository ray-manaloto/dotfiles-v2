"""pc:trash (the sanctioned remote soft-delete) and the dcg repro fixtures from #116."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "tasks/pc/trash"
SCRIPT = ROOT / "tasks/py/pc_trash.py"
REPRO = ROOT / "tasks/py/dcg_repro.py"
FIXTURES = ROOT / "tests/fixtures/dcg"
NOW_NS = 1_795_000_000_123_456_789
STAMP = re.compile(r"\.\d{8}T\d{6}\.123456789[+-]\d{4}$")


@pytest.fixture
def pc_trash() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pc_trash", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "dev/tmp/probe").mkdir(parents=True)
    (home / "dev/tmp/probe/out.txt").write_text("x")
    (home / ".local/share/Trash/files/old").mkdir(parents=True)
    return home.resolve()


@pytest.mark.parametrize(
    "raw", ["~/dev/tmp/probe", "dev/tmp/probe", "~/dev/../dev/tmp/probe"]
)
def test_trash_moves_with_stamp_and_info(
    pc_trash: ModuleType, home: Path, raw: str
) -> None:
    dest = pc_trash.trash(raw, home, NOW_NS)
    assert dest.parent == home / ".local/share/Trash/files"
    assert dest.name.startswith("probe.") and STAMP.search(dest.name)
    assert (dest / "out.txt").read_text() == "x"
    assert not (home / "dev/tmp/probe").exists()
    info = (home / f".local/share/Trash/info/{dest.name}.trashinfo").read_text()
    lines = info.splitlines()
    assert lines[:2] == ["[Trash Info]", f"Path={quote(str(home / 'dev/tmp/probe'))}"]
    assert re.fullmatch(r"DeletionDate=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", lines[2])


def test_trash_absolute_path_with_space_and_collision(
    pc_trash: ModuleType, home: Path
) -> None:
    first = pc_trash.trash(str(home / "dev/tmp/probe"), home, NOW_NS)
    (home / "dev/tmp/probe").mkdir()
    second = pc_trash.trash("~/dev/tmp/probe", home, NOW_NS)
    assert second.name == f"{first.name}.1" and first.is_dir() and second.is_dir()
    (home / "a b").write_text("y")
    spaced = pc_trash.trash("~/a b", home, NOW_NS)
    info = (home / f".local/share/Trash/info/{spaced.name}.trashinfo").read_text()
    assert f"Path={quote(str(home))}/a%20b\n" in info


def test_trash_moves_symlink_not_target(pc_trash: ModuleType, home: Path) -> None:
    (home / "link").symlink_to(home / "dev/tmp/probe")
    dest = pc_trash.trash("~/link", home, NOW_NS)
    assert dest.is_symlink() and (home / "dev/tmp/probe/out.txt").exists()


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " ",
        "~",
        "/",
        "~root/x",
        "../outside",
        "~/dev/../../outside",
        "~/nope",
        "~/nope/deeper",
        "~/.local",
        "~/.local/share/Trash",
        "~/.local/share/Trash/files/old",
    ],
)
def test_trash_refuses(pc_trash: ModuleType, home: Path, raw: str) -> None:
    (home.parent / "outside").mkdir()
    before = sorted(p.relative_to(home.parent) for p in home.parent.rglob("*"))
    with pytest.raises((ValueError, OSError)):
        pc_trash.trash(raw, home, NOW_NS)
    assert sorted(p.relative_to(home.parent) for p in home.parent.rglob("*")) == before


def test_script_runs_as_streamed_stdin(home: Path) -> None:
    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", "-B", "-", *args],
            input=SCRIPT.read_text(),
            text=True,
            capture_output=True,
            env={"HOME": str(home), "PATH": os.defpath},
            check=False,
        )

    ok = run("~/dev/tmp/probe")
    assert ok.returncode == 0 and "pc:trash: moved ~/dev/tmp/probe to " in ok.stdout
    bad = run("~")
    assert bad.returncode == 1 and bad.stderr.startswith("pc:trash: ")
    assert run().returncode == 2


def test_task_is_executable_in_git() -> None:
    mode = subprocess.check_output(
        ["git", "-C", ROOT, "ls-files", "-s", TASK], text=True
    )
    assert mode.startswith("100755 ")


@pytest.fixture
def stub_env(tmp_path: Path) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$PC_CALLS"\ncat > "$PC_STDIN"\n')
    ssh.chmod(0o755)
    return {
        **os.environ,
        "HOME": str(tmp_path),
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "PC_CALLS": str(tmp_path / "calls"),
        "PC_STDIN": str(tmp_path / "stdin"),
    }


def _task(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # The same command the shebang runs, so a lost working-tree exec bit cannot mask a failure.
    return subprocess.run(
        ["usage", "exec", "bash", str(TASK), *args],
        text=True,
        capture_output=True,
        env=env,
    )


def test_task_usage_and_dry_run(stub_env: dict[str, str]) -> None:
    subprocess.run(["usage", "lint", "-W", "-q", TASK], check=True)
    local = f"{stub_env['HOME']}/dev/tmp/probe"
    for args, host in [([], "pc"), (["--host", "fixture"], "fixture")]:
        out = _task("--dry-run", *args, local, env=stub_env)
        assert out.returncode == 0, out.stderr
        assert f"pc:trash dry-run ({host}): move ~/dev/tmp/probe into" in out.stdout
    assert _task("--dry-run", env=stub_env).returncode != 0
    assert not Path(stub_env["PC_CALLS"]).exists()


def test_task_streams_script_with_quoted_path(stub_env: dict[str, str]) -> None:
    out = _task("--host", "fixture", "~/dev/tmp/a b", env=stub_env)
    assert out.returncode == 0, out.stderr
    calls = Path(stub_env["PC_CALLS"]).read_text().splitlines()
    assert len(calls) == 1
    assert calls[0].startswith("-o BatchMode=yes -- fixture /usr/bin/python3 -I -B - ")
    # ssh hands the remote command to the login shell, which must see one literal word.
    assert shlex.split(calls[0])[-1] == "~/dev/tmp/a b"
    assert Path(stub_env["PC_STDIN"]).read_text() == SCRIPT.read_text()


def test_dcg_fixtures_cover_both_repros() -> None:
    cases = {p.stem: json.loads(p.read_text()) for p in FIXTURES.glob("*.json")}
    assert {"ssh-inline-trash-mv", "quoted-heredoc-data"} <= cases.keys()
    for case in cases.values():
        assert case["issue"] == 116 and case["expect"] in ("allow", "block")
        assert isinstance(case["command"], str) and case["command"]
    assert cases["ssh-inline-trash-mv"]["command"].startswith("ssh pc 'mv ")
    assert cases["quoted-heredoc-data"]["command"].startswith(
        "cat >> task_plan.md <<'EOF'\n"
    )


def _repro(tmp_path: Path, fake: str | None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    if fake is not None:
        dcg = tmp_path / "dcg"
        dcg.write_text("#!/bin/sh\n" + fake)
        dcg.chmod(0o755)
        env["DCG_BIN"] = str(dcg)
    return subprocess.run(
        [sys.executable, "-I", "-B", str(REPRO)],
        text=True,
        capture_output=True,
        env=env,
    )


def test_dcg_repro_reports_blocked_fixture(tmp_path: Path) -> None:
    deny = json.dumps(
        {
            "hookSpecificOutput": {
                "permissionDecision": "deny",
                "permissionDecisionReason": "core.filesystem:mv-sensitive-source-root-home\nmore",
            }
        }
    )
    out = _repro(
        tmp_path, f"in=$(cat)\ncase $in in *'ssh pc'*) printf '%s\\n' '{deny}';; esac\n"
    )
    assert out.returncode == 1, out.stderr
    lines = dict(line.split(maxsplit=1) for line in out.stdout.splitlines())
    assert lines["REPRO"].startswith(
        "ssh-inline-trash-mv: block (expected allow) [core.filesystem:mv-sensitive-source-root-home]"
    )
    assert out.stdout.count("OK ") == len(list(FIXTURES.glob("*.json"))) - 1


def test_dcg_repro_passes_when_fixed_and_fails_closed(tmp_path: Path) -> None:
    fixed = _repro(tmp_path, "cat >/dev/null\n")
    assert fixed.returncode == 0 and "REPRO" not in fixed.stdout
    broken = _repro(tmp_path, "echo 'mise ERROR dcg is not installed' >&2\nexit 1\n")
    assert broken.returncode == 2 and "dcg exited 1" in broken.stderr
    assert _repro(tmp_path, None).returncode == 2
