"""Tests for `plugins:update` orchestration, the strict tree gate, and the drift cache."""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from dotfiles_py.generated import (
    PluginsTracked,
    PluginsUpdateResponse,
    PluginsUpdateStatus,
)
from plugins_update import drift, surface
from plugins_update.core import run_update

SHA = "a" * 40
CLONE_HEAD = "b" * 40
TRACKED = PluginsTracked.model_validate(
    {
        "$schema": "x",
        "plugins": [{"plugin": "pwf", "marketplace": "mkt", "repo": "o/pwf"}],
    }
)


def _write(root: Path, rel: str, text: str | bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")


def _plugin(
    root: Path,
    *,
    version: str,
    hook_note: str = "same",
    extra: dict[str, str | bytes] | None = None,
    hook_arg: str = "scripts/stop.sh",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _write(
        root,
        ".claude-plugin/plugin.json",
        json.dumps({"name": "pwf", "version": version, "description": version}),
    )
    _write(
        root,
        ".claude-plugin/marketplace.json",
        json.dumps({"plugins": [{"name": "pwf", "source": "./", "version": version}]}),
    )
    hook = {
        "type": "command",
        "command": "sh",
        "args": ["${CLAUDE_PLUGIN_ROOT}/" + hook_arg],
    }
    _write(
        root, "hooks/hooks.json", json.dumps({"hooks": {"Stop": [{"hooks": [hook]}]}})
    )
    _write(root, "scripts/stop.sh", f"echo {hook_note}\n")
    _write(root, "README.md", f"readme {version}\n")
    _write(root, "CHANGELOG.md", f"changelog for v{version}\n")
    for rel, text in (extra or {}).items():
        _write(root, rel, text)
    return root


def _tarball(src: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(src, arcname="o-pwf-abc123")
    return buf.getvalue()


class FakeRunner:
    def __init__(
        self,
        *,
        codex: str | None = None,
        claude: str | None = None,
        install_path: Path | None = None,
        clone: Path | None = None,
        after_path: Path | None = None,
        codex_src: Path | None = None,
        codex_cache: Path | None = None,
        tamper_after: bool = False,
        codex_update: tuple[int, dict] | None = None,
        gh_fail: bool = False,
        tag: str = "v3.29.1",
    ) -> None:
        self.codex = codex
        self.claude = claude
        self.install_path = install_path
        self.clone = clone
        self.after_path = after_path
        self.codex_src = codex_src
        self.codex_cache = codex_cache
        self.tamper_after = tamper_after
        self.updated = False
        self.codex_update = codex_update or (0, {"errors": [], "rc": 0})
        self.gh_fail = gh_fail
        self.tag = tag
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        a = list(argv)
        self.calls.append(a)

        def ok(out: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(a, rc, out, "")

        if a[0] == "gh":
            if self.gh_fail:
                raise drift.LookupFailure("gh not found on PATH", "ERR_TOOL_MISSING")
            return ok(self.tag + "\n" if "releases/latest" in a[2] else SHA + "\n")
        if a[0] == "git":
            return ok(CLONE_HEAD + "\n" if "rev-parse" in a else "")
        if a[:4] == ["claude", "plugin", "marketplace", "list"]:
            return ok(json.dumps([{"name": "mkt", "installLocation": str(self.clone)}]))
        if a[:3] == ["codex", "plugin", "list"]:
            rows = (
                [{"pluginId": "pwf@mkt", "version": self.codex}] if self.codex else []
            )
            return ok(json.dumps({"installed": rows}))
        if a[:3] == ["claude", "plugin", "list"]:
            path = self.after_path if self.updated else self.install_path
            rows = (
                [
                    {
                        "id": "pwf@mkt",
                        "scope": "user",
                        "version": self.claude,
                        "installPath": str(path),
                    },
                    {"id": "pwf@mkt", "scope": "project", "version": "0.0.1"},
                ]
                if self.claude
                else []
            )
            return ok(json.dumps(rows))
        if a[:3] == ["claude", "plugin", "update"]:
            assert self.clone is not None and self.after_path is not None
            shutil.copytree(self.clone, self.after_path, dirs_exist_ok=True)
            if self.tamper_after:
                _write(self.after_path, "scripts/stop.sh", "evil\n")
            self.updated = True
            return ok()
        if a[:3] == ["mise", "run", "-q"] and a[3] == "codex:plugin-update":
            rc, payload = self.codex_update
            if "--apply" in a and self.codex_src and self.codex_cache:
                dest = self.codex_cache / "3.29.1"
                shutil.copytree(self.codex_src, dest, dirs_exist_ok=True)
                _write(dest, ".codex-marketplace-install.json", "{}")
                if self.tamper_after:
                    _write(dest, "scripts/stop.sh", "evil\n")
                self.codex = "3.29.1"
            return ok(json.dumps(payload), rc)
        return ok()

    def claude_updates(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["claude", "plugin", "update"]]

    def claude_disables(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["claude", "plugin", "disable"]]

    def codex_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["mise", "run", "-q"]]


def _run(
    harness: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    apply: bool = True,
    approve: str | None = None,
    base_extra: dict[str, str | bytes] | None = None,
    extra: dict[str, str | bytes] | None = None,
    mutate: Callable[[Path], None] | None = None,
    tamper_after: bool = False,
    tag: str = "v3.29.1",
    **new_kw: Any,
) -> tuple[PluginsUpdateResponse, FakeRunner]:
    """Run one update for *harness* (`claude` or `codex`) against fabricated trees."""
    old = _plugin(tmp_path / "old", version="3.29.0", extra=base_extra)
    new = _plugin(
        tmp_path / "new",
        version="3.29.1",
        extra={**(base_extra or {}), **(extra or {})},
        **new_kw,
    )
    if mutate:
        mutate(new)
    if harness == "claude":
        _write(old, ".in_use/123", "pid")  # runtime dir at the installed root
        runner = FakeRunner(
            claude="3.29.0",
            install_path=old,
            clone=new,
            after_path=tmp_path / "after",
            tamper_after=tamper_after,
            tag=tag,
        )
        fetcher = None
    else:
        home = tmp_path / "codexhome"
        cache = home / "plugins" / "cache" / "mkt" / "pwf"
        shutil.copytree(old, cache / "3.29.0")
        _write(cache / "3.29.0", ".codex-marketplace-install.json", "{}")
        _write(cache / "3.29.0", "scripts/__pycache__/s.cpython-314.pyc", b"\0")
        monkeypatch.setenv("CODEX_HOME", str(home))
        runner = FakeRunner(
            codex="3.29.0",
            codex_src=new,
            codex_cache=cache,
            tamper_after=tamper_after,
            tag=tag,
        )
        data = _tarball(new)
        fetcher = lambda _repo, _ref: data  # noqa: E731
    kwargs: dict[str, Any] = {"apply": apply, "approve": approve, "runner": runner}
    if fetcher:
        kwargs["fetcher"] = fetcher
    return run_update(TRACKED, **kwargs), runner


def _updated(harness: str, runner: FakeRunner) -> bool:
    if harness == "claude":
        return bool(runner.claude_updates())
    return any("--apply" in c for c in runner.codex_calls())


HARNESSES = ["claude", "codex"]


@pytest.mark.parametrize("harness", HARNESSES)
def test_pure_version_bump_auto_applies(
    harness: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp, r = _run(harness, tmp_path, monkeypatch)
    assert resp.rc == 0, resp.results[0].notes
    assert resp.results[0].status is PluginsUpdateStatus.UPDATED
    assert _updated(harness, r)
    if harness == "claude":
        assert resp.results[0].clone_head_sha == CLONE_HEAD
        assert r.claude_updates() == [
            ["claude", "plugin", "update", "pwf@mkt", "--scope", "user"]
        ]
        marketplace = ["claude", "plugin", "marketplace", "update", "mkt"]
        assert r.calls.index(marketplace) < r.calls.index(r.claude_updates()[0])
    else:
        calls = r.codex_calls()
        assert SHA in calls[0] and "v3.29.1" not in calls[0]
        assert "--trust-hook-diff" not in calls[0]


@pytest.mark.parametrize("harness", HARNESSES)
def test_unreferenced_doc_change_auto_applies(
    harness: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp, r = _run(
        harness,
        tmp_path,
        monkeypatch,
        base_extra={"docs/guide.md": "old docs\n"},
        extra={"docs/guide.md": "new docs\n", "media/logo.png": b"png"},
    )
    assert resp.rc == 0, resp.results[0].notes
    assert _updated(harness, r)


@pytest.mark.parametrize("harness", HARNESSES)
def test_dry_run_never_installs(
    harness: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp, r = _run(harness, tmp_path, monkeypatch, apply=False)
    assert resp.rc == 0
    assert resp.results[0].status is PluginsUpdateStatus.UPDATE_AVAILABLE
    assert not _updated(harness, r)


def _chmod_x(root: Path) -> None:
    (root / "scripts" / "stop.sh").chmod(0o755)


def _symlink(root: Path) -> None:
    (root / "scripts" / "link.sh").symlink_to("stop.sh")


BYPASSES: dict[str, dict[str, Any]] = {
    "script_only": {"extra": {"scripts/x.py": "print('hi')\n"}},
    "hook_script_change": {"hook_note": "changed"},
    "hooks_json_args_path": {"hook_arg": "scripts/other.sh"},
    "docs_shell_script": {"extra": {"docs/x.sh": "echo hi\n"}},
    "license_named_script": {"extra": {"scripts/LICENSE.sh": "echo hi\n"}},
    "png_named_script": {"extra": {"scripts/x.png": "echo hi\n"}},
    "node_modules": {"extra": {"node_modules/a/i.js": "x\n"}},
    "pycache_source": {"extra": {"scripts/__pycache__/a.py": "x\n"}},
    "nested_in_use": {"extra": {"s/.in_use/r.sh": "x\n"}},
    "stray_pyc": {"extra": {"scripts/m.pyc": b"\0\1"}},
    "reference_markdown": {"extra": {"skills/x/reference.md": "do things\n"}},
    "agent_body": {
        "base_extra": {"agents/a.md": "---\nname: a\n---\nbody one\n"},
        "extra": {"agents/a.md": "---\nname: a\n---\nbody two\n"},
    },
    "skill_body": {
        "base_extra": {"skills/s/SKILL.md": "---\nname: s\n---\nstep one\n"},
        "extra": {"skills/s/SKILL.md": "---\nname: s\n---\nstep two\n"},
    },
    "block_scalar_version_line": {
        "base_extra": {
            "skills/s/SKILL.md": "---\nname: s\ndescription: |\n  version: a\n---\n"
        },
        "extra": {
            "skills/s/SKILL.md": "---\nname: s\ndescription: |\n  version: b\n---\n"
        },
    },
    "bom_file": {
        "base_extra": {"skills/s/SKILL.md": "---\nname: s\n---\nbody\n"},
        "extra": {"skills/s/SKILL.md": "﻿---\nname: s\n---\nbody\n"},
    },
    "chmod_x": {"mutate": _chmod_x},
    "added_symlink": {"mutate": _symlink},
    "referenced_doc": {
        "base_extra": {
            "scripts/ref.sh": "cat docs/guide.md\n",
            "docs/guide.md": "one\n",
        },
        "extra": {"docs/guide.md": "two\n"},
    },
    "new_root_in_use": {"extra": {".in_use/x.sh": "echo hi\n"}},
    "doc_glob_in_script": {
        "base_extra": {
            "scripts/g.sh": "for f in docs/*.md; do cat $f; done\n",
            "docs/guide.md": "one\n",
        },
        "extra": {"docs/guide.md": "two\n"},
    },
    "media_glob_in_script": {
        "base_extra": {"scripts/g.sh": "cat media/*\n", "media/a.png": b"1"},
        "extra": {"media/a.png": b"2"},
    },
    "manifest_other_key": {"extra": {"package.json": '{"scripts": {"x": "evil"}}'}},
}


@pytest.mark.parametrize("harness", HARNESSES)
@pytest.mark.parametrize("name", sorted(BYPASSES))
def test_bypass_attempts_need_review_and_never_install(
    harness: str, name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp, r = _run(harness, tmp_path, monkeypatch, **BYPASSES[name])
    assert resp.rc == 3, (name, resp.results[0].notes[:2])
    res = resp.results[0]
    assert res.status is PluginsUpdateStatus.NEEDS_REVIEW
    assert "plugins:update --plugin pwf" in (res.review_command or "")
    assert not _updated(harness, r)


def _digest(resp: PluginsUpdateResponse) -> str:
    match = re.search(r"--approve ([0-9a-f]{64})", resp.results[0].review_command or "")
    assert match
    return match.group(1)


@pytest.mark.parametrize("harness", HARNESSES)
def test_approve_applies_only_the_exact_reviewed_diff(
    harness: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changed: dict[str, Any] = {"extra": {"scripts/x.py": "print('hi')\n"}}
    first, _ = _run(harness, tmp_path / "a", monkeypatch, **changed)
    digest = _digest(first)

    bad, r_bad = _run(harness, tmp_path / "b", monkeypatch, approve="0" * 64, **changed)
    assert bad.rc == 3
    assert any("does not match" in n for n in bad.results[0].notes)
    assert not _updated(harness, r_bad)

    other: dict[str, Any] = {"extra": {"scripts/x.py": "print('different')\n"}}
    wrong, r_wrong = _run(harness, tmp_path / "c", monkeypatch, approve=digest, **other)
    assert wrong.rc == 3
    assert not _updated(harness, r_wrong)

    good, r_good = _run(harness, tmp_path / "d", monkeypatch, approve=digest, **changed)
    assert good.rc == 0, good.results[0].notes
    assert _updated(harness, r_good)
    if harness == "codex":
        assert "--trust-hook-diff" in r_good.codex_calls()[0]


def test_codex_own_hook_gate_refusal_maps_to_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "h"
    old = _plugin(tmp_path / "old", version="3.29.0")
    new = _plugin(tmp_path / "new", version="3.29.1")
    shutil.copytree(old, home / "plugins" / "cache" / "mkt" / "pwf" / "3.29.0")
    monkeypatch.setenv("CODEX_HOME", str(home))
    r = FakeRunner(
        codex="3.29.0",
        codex_update=(1, {"errors": [{"code": "ERR_HOOK_SURFACE_CHANGED"}]}),
    )
    data = _tarball(new)
    resp = run_update(TRACKED, apply=True, runner=r, fetcher=lambda _a, _b: data)
    assert resp.rc == 3


def test_claude_post_install_mismatch_disables_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp, r = _run("claude", tmp_path, monkeypatch, tamper_after=True)
    assert resp.rc == 1
    assert resp.results[0].status is PluginsUpdateStatus.ERROR
    assert any(
        e.code.value == "ERR_POST_INSTALL_MISMATCH" for e in resp.results[0].errors
    )
    assert r.claude_disables() == [
        ["claude", "plugin", "disable", "pwf@mkt", "--scope", "user"]
    ]


def test_codex_post_install_mismatch_is_loud_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp, _r = _run("codex", tmp_path, monkeypatch, tamper_after=True)
    assert resp.rc == 1
    err = resp.results[0].errors[0]
    assert err.code.value == "ERR_POST_INSTALL_MISMATCH"
    assert "INSTALLED CODEX TREE DIFFERS" in (err.message or "")


def test_claude_clone_behind_release_is_only_a_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp, _r = _run("claude", tmp_path, monkeypatch, apply=False)
    assert resp.rc == 0
    assert not any("not at or after" in n for n in resp.results[0].notes)
    behind = FakeRunner.__call__

    def patched(
        self: FakeRunner, argv: Sequence[str]
    ) -> subprocess.CompletedProcess[str]:
        if "merge-base" in argv:
            return subprocess.CompletedProcess(list(argv), 1, "", "")
        return behind(self, argv)

    monkeypatch.setattr(FakeRunner, "__call__", patched)
    resp, _ = _run("claude", tmp_path / "again", monkeypatch, apply=False)
    assert resp.rc == 0
    assert any("not at or after" in n for n in resp.results[0].notes)


def test_up_to_date_makes_no_changes(tmp_path: Path) -> None:
    inst = _plugin(tmp_path / "inst", version="3.29.1")
    r = FakeRunner(codex="3.29.1", claude="3.29.1", install_path=inst)
    resp = run_update(TRACKED, apply=True, runner=r)
    assert resp.rc == 0
    assert resp.results[0].status is PluginsUpdateStatus.UP_TO_DATE
    assert not r.claude_updates()
    assert not r.codex_calls()


def test_degraded_inputs_fail_closed(tmp_path: Path) -> None:
    inst = _plugin(tmp_path / "inst", version="3.29.1")
    gh_missing = FakeRunner(
        codex="3.29.1", claude="3.29.1", install_path=inst, gh_fail=True
    )
    resp = run_update(TRACKED, apply=True, runner=gh_missing)
    assert resp.rc == 1
    assert resp.results[0].status is PluginsUpdateStatus.ERROR

    not_installed = FakeRunner()
    assert run_update(TRACKED, apply=False, runner=not_installed).rc == 1

    unknown = FakeRunner(codex="3.29.1", claude="3.29.1", install_path=inst)
    assert run_update(TRACKED, apply=False, only="nope", runner=unknown).rc == 1


def test_codex_staging_failure_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "h"
    _plugin(home / "plugins" / "cache" / "mkt" / "pwf" / "3.29.0", version="3.29.0")
    monkeypatch.setenv("CODEX_HOME", str(home))
    r = FakeRunner(codex="3.29.0")

    def boom(repo: str, ref: str) -> bytes:
        raise drift.LookupFailure("download failed", "ERR_GITHUB_LOOKUP_FAILED")

    resp = run_update(TRACKED, apply=True, runner=r, fetcher=boom)
    assert resp.rc == 1
    assert not r.codex_calls()


def test_tarball_caps_reject_oversized_archives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugins_update.core as core

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for i in range(3):
            info = tarfile.TarInfo(f"top/f{i}")
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
    monkeypatch.setattr(core, "MAX_TAR_MEMBERS", 2)
    with pytest.raises(drift.LookupFailure):
        core._extract_release(buf.getvalue(), Path(tempfile.mkdtemp()))


def test_surface_primitives(tmp_path: Path) -> None:
    root = tmp_path / "t"
    _write(root, "a.txt", "v1.0.0 and 1.0.0\n")
    _write(root, ".git/config", "x")
    _write(root, ".in_use/1", "x")
    _write(root, "sub/.in_use/1", "x")
    tree = surface.read_tree(root)
    assert set(tree) == {"a.txt", ".in_use/1", "sub/.in_use/1"}
    assert set(surface.read_tree(root, {".in_use"})) == {"a.txt", "sub/.in_use/1"}
    bumped = surface.substitute_version(tree, "1.0.0", "2.0.0")
    assert bumped["a.txt"][2] == b"v2.0.0 and 2.0.0\n"
    assert surface.strict_diff(tree, tree) == []
    assert surface.is_doc_path("docs/a/b.md") and not surface.is_doc_path("docs/a.sh")
    assert surface.is_doc_path("README.md") and not surface.is_doc_path("sub/README.md")
    assert surface.is_doc_path("media/x.PNG") and not surface.is_doc_path(
        "scripts/x.png"
    )


def test_cached_latest_uses_ttl(tmp_path: Path) -> None:
    calls: list[int] = []

    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        return subprocess.CompletedProcess(list(argv), 0, "v1.2.3\n", "")

    cache = tmp_path / "c" / "drift.json"
    assert (
        drift.cached_latest("o/r", "v", cache_file=cache, now=1000.0, runner=runner)
        == "1.2.3"
    )
    assert (
        drift.cached_latest("o/r", "v", cache_file=cache, now=2000.0, runner=runner)
        == "1.2.3"
    )
    assert len(calls) == 1
    drift.cached_latest(
        "o/r", "v", cache_file=cache, now=1000.0 + 7 * 3600, runner=runner
    )
    assert len(calls) == 2


def test_version_compare() -> None:
    assert drift.version_lt("3.29.0", "3.29.1")
    assert not drift.version_lt("3.29.1", "3.29.1")
    assert not drift.version_lt("3.30", "3.29.9")


@pytest.mark.parametrize("harness", HARNESSES)
@pytest.mark.parametrize(
    "tag",
    ['v3.30.0","mcpServers":{"x":{"command":"evil"}}', "v3.30.0-rc1", "vx.y", "v3"],
)
def test_hostile_or_prerelease_tags_fail_closed(
    harness: str, tag: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resp, r = _run(harness, tmp_path, monkeypatch, tag=tag)
    assert resp.rc == 1
    assert resp.results[0].status is PluginsUpdateStatus.ERROR
    assert not _updated(harness, r)
    assert not any("commits" in " ".join(c) for c in r.calls if c[0] == "gh")


def test_hostile_installed_version_fails_closed(tmp_path: Path) -> None:
    inst = _plugin(tmp_path / "inst", version="3.29.0")
    r = FakeRunner(claude='3.29.0","x":"y', install_path=inst)
    resp = run_update(TRACKED, apply=True, runner=r)
    assert resp.rc == 1
    assert not r.claude_updates()


def test_digest_is_bound_to_harness_and_plugin_id() -> None:
    import plugins_update.core as core

    old: surface.Tree = {"a.sh": ("file", 0o644, b"1")}
    new: surface.Tree = {"a.sh": ("file", 0o644, b"2")}

    def digest(label: str, plugin_id: str) -> str:
        _s, _n, cmd = core._verdict(
            label, "pwf", plugin_id, old, new, "1.0", "2.0", "p", None
        )
        match = re.search(r"--approve ([0-9a-f]{64})", cmd or "")
        assert match
        return match.group(1)

    claude = digest("Claude", "pwf@mkt")
    assert claude != digest("Codex", "pwf@mkt")
    assert claude != digest("Claude", "pwf@other")
    state, _notes, _cmd = core._verdict(
        "Codex", "pwf", "pwf@mkt", old, new, "1.0", "2.0", "p", claude
    )
    assert state == "review"
