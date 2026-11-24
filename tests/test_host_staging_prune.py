"""Unit tests for inactive orphaned mise staging dir detection and pruning."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tasks_py_loader import load_module


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _setup_mise_fixture(tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "home"
    data_dir = home / ".local" / "share" / "mise"
    state_dir = home / ".local" / "state" / "mise"
    installs_dir = data_dir / "installs"
    installs_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    skills_dir = home / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / ".mise-skills.json").write_text(
        json.dumps({"links": {}}), encoding="utf-8"
    )

    # 1. Active hashed staging dir targeted by tool/version symlink (`age/1.3.2 -> ../age-7newbu5w`)
    active_hashed = installs_dir / "age-7newbu5w"
    active_hashed.mkdir()
    (active_hashed / ".mise-install.toml").write_text("", encoding="utf-8")
    age_dir = installs_dir / "age"
    age_dir.mkdir()
    (age_dir / ".mise.backend.toml").write_text("", encoding="utf-8")
    (age_dir / "1.3.2").symlink_to("../age-7newbu5w")

    # 2. Orphaned unlinked dir (even with internal sibling symlink inside itself)
    orphan_dir = installs_dir / "aube-5gqpfdro"
    orphan_dir.mkdir()
    (orphan_dir / ".mise-install.toml").write_text("", encoding="utf-8")
    (orphan_dir / "aube").write_text("#!/bin/sh\n", encoding="utf-8")
    (orphan_dir / "aubr").symlink_to("aube")

    # 3. Unlinked dir referenced by a live running process
    proc_dir = installs_dir / "agentsview-esuqwxrf"
    proc_dir.mkdir()
    (proc_dir / ".mise-install.toml").write_text("", encoding="utf-8")

    # 4. Unlinked dir referenced only by `mise ls --installed --json`
    ls_ref_dir = installs_dir / "hk-bvxuhybc"
    ls_ref_dir.mkdir()
    (ls_ref_dir / ".mise-install.toml").write_text("", encoding="utf-8")

    # 5. Backend dir whose name happens to end in 8 base32 chars (`hadolint`) with `.mise.backend.toml`
    backend_dir = installs_dir / "aqua-hadolint-hadolint"
    backend_dir.mkdir()
    (backend_dir / ".mise.backend.toml").write_text("", encoding="utf-8")
    hadolint_hashed = installs_dir / "hadolint-ejkosnzv"
    hadolint_hashed.mkdir()
    (backend_dir / "2.15.1").symlink_to("../hadolint-ejkosnzv")

    return {
        "home": home,
        "data_dir": data_dir,
        "state_dir": state_dir,
        "installs_dir": installs_dir,
        "active_hashed": active_hashed,
        "orphan_dir": orphan_dir,
        "proc_dir": proc_dir,
        "ls_ref_dir": ls_ref_dir,
        "backend_dir": backend_dir,
        "hadolint_hashed": hadolint_hashed,
    }


def _fake_ls_payload(fx: dict[str, Path]) -> dict[str, object]:
    return {
        "age": [
            {
                "version": "1.3.2",
                "install_path": str(fx["installs_dir"] / "age" / "1.3.2"),
                "symlinked_to": str(fx["installs_dir"] / "age" / "1.3.2"),
                "installed": True,
                "active": True,
            }
        ],
        "hk": [
            {
                "version": "2.13.0",
                "install_path": str(fx["ls_ref_dir"]),
                "symlinked_to": None,
                "installed": True,
                "active": True,
            }
        ],
    }


def test_find_orphaned_staging_dirs_and_host_hygiene(
    tmp_path: Path, monkeypatch
) -> None:
    doc_mod = load_module("doctor_checks")
    fx = _setup_mise_fixture(tmp_path)
    ls_payload = json.dumps(_fake_ls_payload(fx))
    ps_stdout = f"/bin/zsh -i\n{os.path.realpath(fx['proc_dir'])}/agentsview serve\n"

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("HOME", str(fx["home"]))
    monkeypatch.setenv("MISE_DATA_DIR", str(fx["data_dir"]))
    monkeypatch.setenv("MISE_STATE_DIR", str(fx["state_dir"]))

    def fake_run(cmd, **_kw):
        if "ls" in cmd and "--installed" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=ls_payload, stderr="")
        if cmd[:2] == ["ps", "-axo"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=ps_stdout, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def fake_check_output(cmd, **_kw):
        if "skills" in cmd:
            return "[]"
        if "bin-paths" in cmd:
            return ""
        return ""

    monkeypatch.setattr(doc_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(doc_mod.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(doc_mod.shutil, "which", lambda _cmd: None)

    orphans, probe_errs = doc_mod.find_orphaned_mise_staging_dirs(
        fx["installs_dir"], fx["home"]
    )
    assert probe_errs == []
    assert orphans == [fx["orphan_dir"]]

    hygiene_errs, _ = doc_mod.check_host_hygiene(_repo_root())
    staging_errs = [e for e in hygiene_errs if "orphaned-mise-staging-dir:" in e]
    assert len(staging_errs) == 1
    assert str(fx["orphan_dir"]) in staging_errs[0]
    for protected in (
        fx["active_hashed"],
        fx["proc_dir"],
        fx["ls_ref_dir"],
        fx["backend_dir"],
        fx["hadolint_hashed"],
    ):
        assert not any(str(protected) in e for e in hygiene_errs)


def test_find_orphaned_staging_dirs_fails_closed_on_probe_errors(
    tmp_path: Path, monkeypatch
) -> None:
    doc_mod = load_module("doctor_checks")
    fx = _setup_mise_fixture(tmp_path)

    # Bad JSON / list payload from mise ls must fail closed with no orphans returned.
    monkeypatch.setattr(
        doc_mod.subprocess,
        "run",
        lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr=""),
    )
    orphans, probe_errs = doc_mod.find_orphaned_mise_staging_dirs(
        fx["installs_dir"], fx["home"]
    )
    assert orphans == []
    assert any("staging-dir-check-failed:" in e for e in probe_errs)

    # Failed ps probe must also fail closed.
    ls_payload = json.dumps(_fake_ls_payload(fx))

    def fake_run_ps_fail(cmd, **_kw):
        if "ls" in cmd and "--installed" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=ls_payload, stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ps denied")

    monkeypatch.setattr(doc_mod.subprocess, "run", fake_run_ps_fail)
    orphans2, probe_errs2 = doc_mod.find_orphaned_mise_staging_dirs(
        fx["installs_dir"], fx["home"]
    )
    assert orphans2 == []
    assert any("ps -axo args=" in e for e in probe_errs2)


def test_host_prune_dry_run_apply_and_fail_closed(tmp_path: Path) -> None:
    fx = _setup_mise_fixture(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ls_json_file = tmp_path / "ls.json"
    ls_json_file.write_text(json.dumps(_fake_ls_payload(fx)), encoding="utf-8")

    _write_exec(
        bin_dir / "mise",
        f"""#!/bin/sh
case " $* " in
  *" ls --installed --json "*)
    cat "{ls_json_file}"
    ;;
  *" plugins ls "*)
    exit 0
    ;;
  *" doctor --json "*)
    printf '{{}}'
    ;;
  *)
    exit 0
    ;;
esac
""",
    )
    _write_exec(
        bin_dir / "ps",
        f"""#!/bin/sh
if [ "${{FAIL_PS:-0}}" = "1" ]; then
  exit 1
fi
printf '%s\\n' '/bin/zsh -i' '{fx["proc_dir"]}/agentsview serve'
""",
    )
    # Keep python3 accessible while overriding mise and ps first on PATH.
    sys_py_dir = str(Path(shutil.which("python3") or sys.executable).parent)
    base_env = {
        "PATH": f"{bin_dir}:{sys_py_dir}:/usr/bin:/bin",
        "HOME": str(fx["home"]),
        "MISE_DATA_DIR": str(fx["data_dir"]),
        "MISE_STATE_DIR": str(fx["state_dir"]),
    }
    prune_script = (
        _repo_root() / "unix" / "home" / ".config" / "mise" / "tasks" / "host" / "prune"
    )

    dry = subprocess.run(
        ["bash", str(prune_script)],
        env={**base_env, "usage_apply": "false"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry.returncode == 0, dry.stderr
    assert f"WOULD REMOVE orphaned staging dir: {fx['orphan_dir']}" in dry.stdout
    assert fx["orphan_dir"].is_dir()

    # Fail closed when ps fails even under --apply.
    fail_ps = subprocess.run(
        ["bash", str(prune_script)],
        env={**base_env, "usage_apply": "true", "FAIL_PS": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert fail_ps.returncode == 0, fail_ps.stderr
    assert "SKIP  staging-dir prune:" in fail_ps.stdout
    assert fx["orphan_dir"].is_dir()

    applied = subprocess.run(
        ["bash", str(prune_script)],
        env={**base_env, "usage_apply": "true"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode == 0, applied.stderr
    assert f"REMOVED orphaned staging dir: {fx['orphan_dir']}" in applied.stdout
    assert not fx["orphan_dir"].exists()
    for kept in (
        fx["active_hashed"],
        fx["proc_dir"],
        fx["ls_ref_dir"],
        fx["backend_dir"],
        fx["hadolint_hashed"],
    ):
        assert kept.is_dir()
