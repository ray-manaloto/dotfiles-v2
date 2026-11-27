"""pc:trash (the sanctioned remote soft-delete) and the dcg repro fixtures from #116."""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import IO
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
    for folder in PROTECTED_DIRS:
        (home / folder).mkdir(parents=True, exist_ok=True)
    (home / ".ssh/id_ed25519").write_text("k")
    (home / ".local/bin/mise").write_text("m")
    (home / ".bashrc").write_text("b")
    return home.resolve()


PROTECTED_DIRS = (
    ".ssh",
    ".gnupg",
    ".config/fnox",
    ".config/mise/conf.d",
    ".config/dcg",
    ".local/bin",
    ".local/share/mise/installs",
    "dev/dotfiles-v2/unix",
)


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
    (home / "dev/a b").write_text("y")
    spaced = pc_trash.trash("~/dev/a b", home, NOW_NS)
    info = (home / f".local/share/Trash/info/{spaced.name}.trashinfo").read_text()
    assert f"Path={quote(str(home))}/dev/a%20b\n" in info


def test_trash_moves_symlink_not_target(pc_trash: ModuleType, home: Path) -> None:
    (home / "dev/link").symlink_to(home / ".ssh")
    dest = pc_trash.trash("~/dev/link", home, NOW_NS)
    assert dest.is_symlink() and (home / ".ssh/id_ed25519").exists()


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


@pytest.mark.parametrize(
    "raw",
    [
        # Top-level entries: `pc:trash dev` would take the whole checkout tree.
        "dev",
        "~/dev",
        "~/.bashrc",
        "~/.ssh",
        "~/.config",
        "~/.local",
        # Protected paths, what is inside them, and what contains them.
        "~/.ssh/id_ed25519",
        "~/.gnupg",
        "~/.config/fnox",
        "~/.config/mise",
        "~/.config/mise/conf.d",
        "~/.config/dcg",
        "~/.local/bin",
        "~/.local/bin/mise",
        "~/.local/share",
        "~/.local/share/mise",
        "~/.local/share/mise/installs",
        "~/.local/share/Trash",
        "~/.local/share/Trash/files/old",
        "~/dev/dotfiles-v2",
        "dev/dotfiles-v2/unix",
    ],
)
def test_trash_refuses_protected(pc_trash: ModuleType, home: Path, raw: str) -> None:
    before = sorted(home.rglob("*"))
    with pytest.raises(ValueError, match=r"protected|top-level"):
        pc_trash.trash(raw, home, NOW_NS)
    assert sorted(home.rglob("*")) == before


def test_trash_refuses_symlinked_protected_target(
    pc_trash: ModuleType, home: Path
) -> None:
    # ~/.config/mise links into a checkout under another name; trashing the checkout breaks it.
    (home / "dev/other/mise").mkdir(parents=True)
    (home / ".config/mise/conf.d").rmdir()
    (home / ".config/mise").rmdir()
    (home / ".config/mise").symlink_to(home / "dev/other/mise")
    for raw in ("~/dev/other", "~/dev/other/mise"):
        with pytest.raises(ValueError, match=re.escape("protected ~/.config/mise")):
            pc_trash.trash(raw, home, NOW_NS)
    assert (home / "dev/other/mise").is_dir()


@pytest.mark.parametrize(
    ("code", "raised"), [(errno.EXDEV, ValueError), (errno.EACCES, PermissionError)]
)
def test_trash_rename_failure_cleans_up(
    pc_trash: ModuleType,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    raised: type[Exception],
) -> None:
    def fail(src: Path, dest: Path) -> None:
        raise OSError(code, os.strerror(code), str(src))

    monkeypatch.setattr(pc_trash, "_renameat2", lambda: None)
    monkeypatch.setattr(pc_trash.os, "rename", fail)
    with pytest.raises(raised):
        pc_trash.trash("~/dev/tmp/probe", home, NOW_NS)
    assert (home / "dev/tmp/probe/out.txt").exists()
    assert not list((home / ".local/share/Trash/info").iterdir())


def test_trashinfo_write_failure_leaves_no_partial_file(
    pc_trash: ModuleType, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.fdopen

    class Failing:
        def __init__(self, fh: IO[str]) -> None:
            self.fh = fh

        def __enter__(self) -> Failing:
            return self

        def __exit__(self, *exc: object) -> None:
            self.fh.close()

        def write(self, _text: str) -> None:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(pc_trash.os, "fdopen", lambda *a, **k: Failing(real(*a, **k)))
    with pytest.raises(OSError):
        pc_trash.trash("~/dev/tmp/probe", home, NOW_NS)
    assert (home / "dev/tmp/probe/out.txt").exists()
    assert not list((home / ".local/share/Trash/info").iterdir())


def test_collision_never_overwrites_existing_destination(
    pc_trash: ModuleType, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = home / f".local/share/Trash/files/probe.{pc_trash.stamp(NOW_NS)}"
    taken.mkdir()
    (taken / "keep").write_text("k")
    real = os.path.lexists
    calls = iter([False])
    # The first existence check is blind (a racing writer), so only the move can notice.
    monkeypatch.setattr(
        pc_trash.os.path, "lexists", lambda p: next(calls, None) or real(p)
    )
    dest = pc_trash.trash("~/dev/tmp/probe", home, NOW_NS)
    assert dest.name == f"{taken.name}.1"
    assert (taken / "keep").read_text() == "k" and not (taken / "out.txt").exists()
    assert (dest / "out.txt").read_text() == "x"
    infos = {p.name for p in (home / ".local/share/Trash/info").iterdir()}
    assert infos == {f"{dest.name}.trashinfo"}


@pytest.mark.parametrize("bad", ["a\nb", "a\tb", "a\x7fb", "a\x01b"])
def test_task_rejects_control_characters_before_ssh(
    stub_env: dict[str, str], bad: str
) -> None:
    out = _task("--host", "fixture", f"~/dev/tmp/{bad}", env=stub_env)
    assert out.returncode == 1 and "control characters" in out.stderr
    assert not Path(stub_env["PC_CALLS"]).exists()


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
    heredocs = {"quoted-heredoc-find-delete", "quoted-heredoc-find-exec-rm"}
    assert {"ssh-inline-trash-mv", *heredocs} <= cases.keys()
    for case in cases.values():
        assert case["issue"] == 116 and case["expect"] in ("allow", "block")
        assert isinstance(case["command"], str) and case["command"]
    assert cases["ssh-inline-trash-mv"]["command"].startswith("ssh pc 'mv ")
    for stem in heredocs:
        command = cases[stem]["command"]
        assert command.startswith("cat >> task_plan.md <<'EOF'\n")
        # One trigger per fixture, so a block names exactly one cause.
        assert command.count("find ") == 1 and command.count("\n") == 2
        control = cases[stem.replace("quoted-", "bash-")]
        assert control["expect"] == "block" and control["command"].startswith(
            "bash <<'EOF'\n"
        )


def _repro(
    tmp_path: Path, fake: str | None, *args: str
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    if fake is not None:
        dcg = tmp_path / "dcg"
        dcg.write_text("#!/bin/sh\n" + fake)
        dcg.chmod(0o755)
        env["DCG_BIN"] = str(dcg)
    return subprocess.run(
        [sys.executable, "-I", "-B", str(REPRO), *args],
        text=True,
        capture_output=True,
        env=env,
    )


# dcg 0.17.2's deny shape: a banner first, the rule ID on a later `Rule:` line.
DENY = json.dumps(
    {
        "hookSpecificOutput": {
            "permissionDecision": "deny",
            "permissionDecisionReason": "BLOCKED by dcg\n\nReason: x\n\n"
            "Rule: core.filesystem:mv-sensitive-source-root-home\n",
        }
    }
)
# Blocks the ssh repro and the bash-heredoc controls; allows everything else.
FAKE_DCG = (
    f"in=$(cat)\ncase $in in *'ssh pc'*|*'bash <<'*) printf '%s\\n' '{DENY}';; esac\n"
)


def _statuses(stdout: str) -> dict[str, str]:
    rows = (line.split(maxsplit=2) for line in stdout.splitlines() if line[:1] != " ")
    return {stem.rstrip(":"): f"{status} {rest}" for status, stem, rest in rows}


def test_dcg_repro_reports_blocked_fixture(tmp_path: Path) -> None:
    out = _repro(tmp_path, FAKE_DCG)
    assert out.returncode == 1, out.stderr
    rows = _statuses(out.stdout)
    assert rows["ssh-inline-trash-mv"] == (
        "REPRO block (expected allow) [core.filesystem:mv-sensitive-source-root-home]"
    )
    assert rows["bash-heredoc-find-delete"].startswith("OK block (expected block)")
    assert rows["quoted-heredoc-find-delete"] == "OK allow (expected allow)"
    assert len(rows) == len(list(FIXTURES.glob("*.json")))
    assert [s for s, row in rows.items() if row.startswith("REPRO")] == [
        "ssh-inline-trash-mv"
    ]
    assert "| BLOCKED by dcg" not in out.stdout
    verbose = _repro(tmp_path, FAKE_DCG, "--verbose")
    assert (
        "       | Rule: core.filesystem:mv-sensitive-source-root-home\n"
        in verbose.stdout
    )
    assert _repro(tmp_path, FAKE_DCG, "--bogus").returncode == 2


def test_dcg_repro_passes_when_fixed_and_fails_closed(tmp_path: Path) -> None:
    fixed = _repro(tmp_path, "cat >/dev/null\n")
    # An allow-everything dcg fixes the repro but breaks the bash-heredoc controls.
    assert fixed.returncode == 1
    assert {s for s, row in _statuses(fixed.stdout).items() if "REPRO" in row} == {
        "bash-heredoc-find-delete",
        "bash-heredoc-find-exec-rm",
    }
    healed = _repro(tmp_path, FAKE_DCG.replace("*'ssh pc'*|", ""))
    assert healed.returncode == 0 and "REPRO" not in healed.stdout, healed.stdout
    broken = _repro(tmp_path, "echo 'mise ERROR dcg is not installed' >&2\nexit 1\n")
    assert broken.returncode == 2 and "dcg exited 1" in broken.stderr
    assert _repro(tmp_path, None).returncode == 2
