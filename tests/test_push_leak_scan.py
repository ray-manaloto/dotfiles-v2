"""push_leak_scan.py: which commits the pre-push betterleaks step scans."""

from __future__ import annotations

import subprocess

import pytest
from tasks_py_loader import load_module

scan = load_module("push_leak_scan")

Z40 = "0" * 40
A, B, C, D, E = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40)


@pytest.fixture(autouse=True)
def fake_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """D is never fetched locally; the server holds C (fetched) and D (not fetched)."""
    monkeypatch.setattr(scan, "known_commits", lambda oids: {o for o in oids if o != D})
    monkeypatch.setattr(scan, "server_oids", lambda remote: [C, D])


def line(local: str, remote: str, ref: str = "refs/heads/x") -> str:
    return f"{ref} {local} {ref} {remote}"


def test_every_pushed_ref_is_a_tip_and_new_refs_use_server_oids() -> None:
    stdin = "\n".join([line(A, Z40), line(B, E, "refs/heads/y")])
    assert scan.push_log_opts(stdin, "url") == [A, B, "--not", E, C]


def test_update_of_known_ref_is_bounded_by_its_old_oid_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scan, "server_oids", lambda remote: pytest.fail("no ls-remote"))
    assert scan.push_log_opts(line(A, C), "url") == [A, "--not", C]


def test_delete_only_push_publishes_nothing() -> None:
    assert scan.push_log_opts(line(Z40, A), "url") is None


def test_delete_is_skipped_but_other_refs_are_scanned() -> None:
    stdin = "\n".join([line(Z40, E), line(A, Z40, "refs/heads/y")])
    assert scan.push_log_opts(stdin, "url") == [A, "--not", C]


def test_unfetched_remote_oid_falls_back_to_server_oids() -> None:
    assert scan.push_log_opts(line(A, D), "url") == [A, "--not", C]


def test_server_listing_failure_blocks_the_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(remote: str) -> list[str]:
        raise ValueError("git ls-remote url failed")

    monkeypatch.setattr(scan, "server_oids", boom)
    with pytest.raises(ValueError):
        scan.push_log_opts(line(A, Z40), "url")


def test_sha256_zero_oid_is_a_delete() -> None:
    assert scan.push_log_opts(line("0" * 64, A), "url") is None


def test_malformed_hook_line_fails_closed() -> None:
    with pytest.raises(ValueError):
        scan.push_log_opts("refs/heads/x " + A, "url")


def test_hook_stdin_read_error_blocks_the_push(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class BrokenInput:
        def read(self) -> str:
            raise OSError("input unavailable")

    monkeypatch.setattr(scan.sys, "stdin", BrokenInput())
    assert scan.main(["origin url"]) == 2
    assert "input unavailable" in capsys.readouterr().err


def test_large_revision_set_uses_stdin_then_bounded_scans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commits = [f"{i:040x}" for i in range(1, 1002)]
    opts = [*commits, "--not", C]
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        stdout = "\n".join(commits) if command[0] == "git" else ""
        return subprocess.CompletedProcess(command, 0, stdout)

    monkeypatch.setattr(scan, "MAX_LOG_OPTS_BYTES", 1)
    monkeypatch.setattr(scan.subprocess, "run", run)
    assert scan.scan_with_betterleaks(opts) == 0
    command, kwargs = calls[0]
    assert command == ["git", "rev-list", "--stdin"]
    assert kwargs["input"] == "\n".join(opts) + "\n"
    assert kwargs["check"] is True
    assert [cmd[-1] for cmd, _ in calls[1:]] == [
        "--log-opts=--no-walk " + " ".join(commits[:1000]),
        "--log-opts=--no-walk " + commits[-1],
    ]


def test_fallback_range_and_delete() -> None:
    assert scan.range_log_opts("origin/main", "HEAD") == ["origin/main..HEAD"]
    assert scan.range_log_opts("origin/main", Z40) == ["--all", "--not", "--remotes"]
    assert scan.range_log_opts(Z40, "HEAD") == ["HEAD", "--not", "--remotes"]
    with pytest.raises(ValueError):
        scan.range_log_opts("", "")


PASSWORD = "not" + "-a-real-one"
URL = "https://user:" + PASSWORD + "@example.invalid/r.git"


def test_ls_remote_error_never_echoes_url_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Failed:
        returncode = 128
        stdout = ""
        stderr = f"fatal: unable to access '{URL}/'"

    monkeypatch.undo()  # drop the fake server_oids so the real one runs
    monkeypatch.setattr(scan.subprocess, "run", lambda *a, **k: Failed())
    with pytest.raises(ValueError) as err:
        scan.server_oids(URL)
    assert PASSWORD not in str(err.value)
