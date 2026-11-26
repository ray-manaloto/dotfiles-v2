"""push_leak_scan.py: which commits the pre-push betterleaks step scans."""

from __future__ import annotations

import pytest
from tasks_py_loader import load_module

scan = load_module("push_leak_scan")

Z40 = "0" * 40
A, B, C, D = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)


@pytest.fixture(autouse=True)
def every_commit_known(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "known_commits", lambda oids: {o for o in oids if o != D})


def line(local: str, remote: str, ref: str = "refs/heads/x") -> str:
    return f"{ref} {local} {ref} {remote}"


def test_every_pushed_ref_is_a_tip() -> None:
    stdin = "\n".join([line(A, Z40), line(B, C, "refs/heads/y")])
    assert scan.push_log_opts(stdin, "origin") == [A, B, "--not", C, "--remotes=origin"]


def test_update_of_known_ref_is_bounded_by_its_old_oid_only() -> None:
    assert scan.push_log_opts(line(A, C), "origin") == [A, "--not", C]


def test_delete_only_push_publishes_nothing() -> None:
    assert scan.push_log_opts(line(Z40, A), "origin") is None


def test_delete_is_skipped_but_other_refs_are_scanned() -> None:
    stdin = "\n".join([line(Z40, C), line(A, Z40, "refs/heads/y")])
    assert scan.push_log_opts(stdin, "origin") == [A, "--not", "--remotes=origin"]


def test_unfetched_remote_oid_widens_instead_of_failing() -> None:
    assert scan.push_log_opts(line(A, D), "origin") == [A, "--not", "--remotes=origin"]


def test_sha256_zero_oid_is_a_delete() -> None:
    assert scan.push_log_opts(line("0" * 64, A), "origin") is None


def test_malformed_hook_line_fails_closed() -> None:
    with pytest.raises(ValueError):
        scan.push_log_opts("refs/heads/x " + A, "origin")


def test_fallback_range_and_delete() -> None:
    assert scan.range_log_opts("origin/main", "HEAD") == ["origin/main..HEAD"]
    assert scan.range_log_opts("origin/main", Z40) == ["--all", "--not", "--remotes"]
    with pytest.raises(ValueError):
        scan.range_log_opts("", "")
