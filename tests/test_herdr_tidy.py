import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "herdr_tidy", Path(__file__).resolve().parents[1] / "tasks/py/herdr_tidy.py"
)
assert _SPEC is not None and _SPEC.loader is not None
herdr_tidy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(herdr_tidy)


HOME = Path.home()
GONE = HOME / "__herdr_tidy_gone__"


def term(
    address, *, agent="claude-code", status="done", cwd=str(GONE / "wt"), tab="lane"
):
    return {
        "address": address,
        "agent": agent,
        "status": status,
        "cwd": cwd,
        "where": f"space / {tab}",
    }


def exists(path):
    """Fake filesystem: everything except paths under GONE exists."""
    return not str(path).startswith(str(GONE))


@pytest.mark.parametrize(
    ("t", "closed"),
    [
        (term("a"), True),
        (term("b", status="idle", agent="antigravity"), False),  # idle peer, sweep mode
        (term("i", status="blocked"), False),  # waiting on a prompt
        (term("r", cwd="/home/dev/wt"), False),  # remote machine path
        (term("rel", cwd="wt"), False),  # relative cwd
        (term("c", status="working"), False),  # mid-turn agent
        (term("d", agent=None), False),  # plain shell
        (term("e", agent="zsh"), False),
        (term("f", cwd=str(HOME)), False),  # folder still exists
        (term("g", tab="credits-claude"), False),  # --keep
        (term("me"), False),  # this terminal
        (term("h", cwd=""), False),
    ],
    ids=[
        "done",
        "idle-agy",
        "blocked",
        "remote",
        "relative",
        "working",
        "shell",
        "zsh",
        "live-cwd",
        "kept",
        "self",
        "no-cwd",
    ],
)
def test_plan_rules(t, closed):
    todo = herdr_tidy.plan(
        [t], keep=frozenset({"credits-claude"}), me="me", exists=exists
    )
    assert [x["address"] for x, _ in todo] == ([t["address"]] if closed else [])


def test_plan_cwd_scopes_to_the_landed_worktree(tmp_path):
    wt = tmp_path / "wt-a"
    link = tmp_path / "link"
    link.symlink_to(wt, target_is_directory=True)
    terms = [
        term("in", cwd=str(wt)),
        term("sub", cwd=str(wt / "sub"), status="idle"),
        term("via-link", cwd=str(link / "x")),  # same folder through a symlink
        term("busy", cwd=str(wt), status="working"),
        term("sibling", cwd=str(tmp_path / "wt-ab")),  # same prefix, different folder
    ]
    todo = herdr_tidy.plan(terms, cwd=wt, exists=exists)
    assert [x["address"] for x, _ in todo] == ["in", "sub", "via-link"]


def fake_hc(terms, rm_rc=0):
    calls = []

    def run(argv):
        calls.append(" ".join(argv[1:]))
        if argv[1] == "ls":  # the real shape: an object with terminals and machines
            out = {"terminals": terms, "machines": [], "hidden_finished": 0}
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(out), stderr=""
            )
        return subprocess.CompletedProcess(
            argv, rm_rc if argv[1] == "rm" else 0, stdout="", stderr="nope"
        )

    return run, calls


def test_main_keep_and_self(monkeypatch):
    monkeypatch.setenv("HC_TERMINAL_ID", "me")
    run, calls = fake_hc([term("me"), term("k", tab="credits-claude"), term("a")])
    assert herdr_tidy.main(["--apply", "--keep", "credits-claude,x"], run=run) == 0
    assert calls == ["ls --json", "kill a", "rm a"]


def test_main_dry_run_closes_nothing(capsys, monkeypatch):
    monkeypatch.delenv("HC_TERMINAL_ID", raising=False)
    run, calls = fake_hc([term("a")])
    assert herdr_tidy.main([], run=run) == 0
    assert calls == ["ls --json"]
    assert "would close lane (a)" in capsys.readouterr().out


def test_main_apply_and_failure(capsys, monkeypatch):
    monkeypatch.delenv("HC_TERMINAL_ID", raising=False)
    run, calls = fake_hc([term("a")])
    assert herdr_tidy.main(["--apply"], run=run) == 0
    assert calls == ["ls --json", "kill a", "rm a"]
    run, _ = fake_hc([term("a")], rm_rc=1)
    assert herdr_tidy.main(["--apply"], run=run) == 1
    assert "FAILED lane (a)" in capsys.readouterr().err


def test_main_hc_ls_failure_exits_2():
    def run(argv):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="core down")

    assert herdr_tidy.main([], run=run) == 2
