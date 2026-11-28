import json
import os
import subprocess
from pathlib import Path

import pytest

LAND = Path(__file__).resolve().parents[1] / "tasks/pr/land"


@pytest.mark.parametrize(
    ("ahead", "doctor_fails"),
    [(True, False), (False, False), (False, True)],
    ids=["ahead", "in-sync", "doctor-fails"],
)
def test_pr_land_requires_main_to_match_origin(tmp_path, ahead, doctor_fails):
    origin = tmp_path / "origin"
    main = tmp_path / "main"
    feature = tmp_path / "feature"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        XDG_STATE_HOME=str(tmp_path / "state"),
        PLAN_ID="fix-land-test",
        usage_pr="1",
    )
    env.pop("usage_skip_learn_check", None)
    # #131: the caller's shell may export a stale checkout venv; the post-land doctor must not see it.
    env["VIRTUAL_ENV"] = str(tmp_path / "caller" / ".venv")

    def git(*args):
        return subprocess.check_output(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.com",
                *args,
            ],
            cwd=tmp_path,
            env=env,
            text=True,
        ).strip()

    git("init", "-q", "-b", "main", str(origin))
    # The merged main still lists a shipped path as Remaining: pr:land surfaces the drift.
    (origin / "tasks/py").mkdir(parents=True)
    (origin / "tasks/env").mkdir(parents=True)
    (origin / "tasks/py/doctor_checks.py").write_bytes(
        (LAND.parents[1] / "py/doctor_checks.py").read_bytes()
    )
    (origin / "tasks/env/quota").write_text("#!/bin/sh\n", encoding="utf-8")
    (origin / "ROADMAP.md").write_text(
        "- [ ] Work. **Remaining:** `tasks/env/quota`.\n", encoding="utf-8"
    )
    git("-C", str(origin), "add", "ROADMAP.md", "tasks")
    git("-C", str(origin), "commit", "-q", "-m", "Merged PR")
    merged = git("-C", str(origin), "rev-parse", "HEAD")
    git("clone", "-q", str(origin), str(main))
    git("-C", str(main), "worktree", "add", "-q", "-b", "wt/fixture", str(feature))
    if ahead:
        for message in ("First local commit", "Second local commit"):
            git("-C", str(main), "commit", "-q", "--allow-empty", "-m", message)
    local_head = git("-C", str(main), "rev-parse", "HEAD")
    local_commits = git("-C", str(main), "log", "--oneline", "origin/main..main")

    reviews = tmp_path / "state" / "agy-learn" / "reviews.jsonl"
    reviews.parent.mkdir(parents=True)
    reviews.write_text(
        json.dumps({"plan_id": env["PLAN_ID"], "outcome": "no_new_proposals"}) + "\n",
        encoding="utf-8",
    )
    gh = bin_dir / "gh"
    gh.write_text(
        f"#!/bin/sh\nprintf '%s\\n' 'MERGED wt/fixture main {merged} {merged} 1970-01-01T00:00:00Z'\n",
        encoding="utf-8",
    )
    mise = bin_dir / "mise"
    doctor_rc = 1 if doctor_fails else 0
    mise.write_text(
        '#!/bin/sh\nif [ "$1 $2" = "doctor project" ] && [ -n "$VIRTUAL_ENV" ]; then\n'
        '  echo "FAIL venv-leak: $VIRTUAL_ENV"; echo "1 failed"; exit 1\nfi\n'
        'if [ "$1 $2" = "doctor project" ]; then\n'
        f'  echo "FAIL fake-check: boom"; echo "1 failed"; exit {doctor_rc}\nfi\n'
        "printf 'mise %s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    # Step 4b runs main's copy of herdr_tidy.py against a fake hc: only the finished tab in the
    # landed worktree is closed, never the working one.
    tidy = main / "tasks" / "py" / "herdr_tidy.py"
    tidy.parent.mkdir(parents=True, exist_ok=True)
    tidy.write_bytes((LAND.parents[1] / "py" / "herdr_tidy.py").read_bytes())
    hc_log = tmp_path / "hc.log"
    tabs = [
        {
            "address": "t1",
            "agent": "claude-code",
            "status": "done",
            "cwd": str(feature),
            "where": "s / lane",
        },
        {
            "address": "t2",
            "agent": "codex",
            "status": "working",
            "cwd": str(feature),
            "where": "s / busy",
        },
    ]
    hc = bin_dir / "hc"
    hc.write_text(
        f"#!/bin/sh\necho \"$*\" >> '{hc_log}'\n"
        f"[ \"$1\" = ls ] && printf '%s\\n' '{json.dumps(tabs)}'\nexit 0\n",
        encoding="utf-8",
    )
    env.pop("HC_TERMINAL_ID", None)
    for stub in (gh, mise, hc):
        stub.chmod(0o755)

    result = subprocess.run(
        ["bash", str(LAND)],
        cwd=feature,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert git("-C", str(main), "rev-parse", "HEAD") == local_head
    assert git("-C", str(main), "rev-parse", "origin/main") == merged
    if ahead:
        assert result.returncode == 3, result.stdout + result.stderr
        assert "REFUSED" in result.stderr
        assert local_commits and local_commits in result.stderr
        assert "worktree:rm" not in result.stdout
        assert git("-C", str(feature), "rev-parse", "HEAD") == merged
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "mise run -q worktree:rm fixture" in result.stdout
        assert "live doctor on main" in result.stdout or doctor_fails
        assert hc_log.read_text(encoding="utf-8").splitlines() == [
            "ls --json",
            "kill t1",
            "rm t1",
        ]
        assert "herdr:tidy: closed lane (t1)" in result.stdout
        # Warn-only roadmap drift on the merged main is printed, never fatal.
        assert (
            "pr:land: WARNING: roadmap-drift: ROADMAP.md:1 " in result.stderr
            and "`tasks/env/quota`" in result.stderr
        ), result.stderr
        if doctor_fails:
            # The land already happened: the doctor failure warns loudly but keeps exit 0.
            assert "WARNING: live 'mise doctor project' on main failed" in result.stderr
            assert "FAIL fake-check: boom" in result.stderr


# The fake mise treats main's .venv as fresh while uv.lock + pyproject.toml match the stamp its last
# `mise deps uv` wrote, mirroring `mise deps uv --explain` (exit 1 when stale).
FAKE_MISE = """#!/bin/sh
echo "$* venv=${VIRTUAL_ENV:-} root=${MISE_PROJECT_ROOT:-}" >> '{log}'
if [ "$1 $2" = "deps uv" ]; then
  cur="$(cat uv.lock pyproject.toml)"
  if [ "$3" = "--explain" ]; then [ "$cur" = "$(cat '{stamp}' 2>/dev/null)" ]; exit $?; fi
  if [ -n "$FAKE_SYNC_FAIL" ]; then echo "uv sync failed: boom"; exit 2; fi
  printf '%s' "$cur" > '{stamp}'; exit 0
fi
if [ "$1 $2" = "doctor project" ]; then echo "0 failed"; fi
exit 0
"""


@pytest.mark.parametrize(
    ("changed", "stamp", "sync_fails", "expect"),
    [
        ("uv.lock", "v1\nv1", False, "synced"),
        ("pyproject.toml", "v1\nv1", False, "synced"),
        ("README.md", "v1\nv1", False, "fresh"),
        # uv.lock reached main earlier (an owner pull, an older land): still resynced.
        ("README.md", "old\nv1", False, "synced"),
        ("uv.lock", "v1\nv1", True, "warned"),
    ],
    ids=["uv-lock", "pyproject", "unrelated", "stale-from-earlier-pull", "sync-fails"],
)
def test_pr_land_syncs_stale_venv(tmp_path, changed, stamp, sync_fails, expect):
    origin, main, feature, bin_dir = (
        tmp_path / n for n in ("origin", "main", "feature", "bin")
    )
    bin_dir.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env['PATH']}",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        XDG_STATE_HOME=str(tmp_path / "state"),
        PLAN_ID="deps-uv-test",
        usage_pr="1",
    )
    for key in ("usage_skip_learn_check", "FAKE_SYNC_FAIL"):
        env.pop(key, None)

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.com", *args],
            cwd=tmp_path,
            env=env,
            text=True,
        ).strip()

    git("init", "-q", "-b", "main", str(origin))
    for name in ("uv.lock", "pyproject.toml", "README.md"):
        (origin / name).write_text("v1\n", encoding="utf-8")
    git("-C", str(origin), "add", ".")
    git("-C", str(origin), "commit", "-q", "-m", "Base")
    git("clone", "-q", str(origin), str(main))
    (origin / changed).write_text("v2\n", encoding="utf-8")
    git("-C", str(origin), "commit", "-q", "-am", "Merged PR")
    merged = git("-C", str(origin), "rev-parse", "HEAD")
    git("-C", str(main), "fetch", "-q", "origin")
    git(
        "-C",
        str(main),
        "worktree",
        "add",
        "-q",
        "-b",
        "wt/fixture",
        str(feature),
        "origin/main",
    )

    reviews = tmp_path / "state" / "agy-learn" / "reviews.jsonl"
    reviews.parent.mkdir(parents=True)
    reviews.write_text(
        json.dumps({"plan_id": env["PLAN_ID"], "outcome": "no_new_proposals"}) + "\n",
        encoding="utf-8",
    )
    log, stamp_file = tmp_path / "mise.log", tmp_path / "venv.stamp"
    stamp_file.write_text(stamp, encoding="utf-8")
    stubs = {
        "gh": f"#!/bin/sh\nprintf '%s\\n' 'MERGED wt/fixture main {merged} {merged} 1970-01-01T00:00:00Z'\n",
        "mise": FAKE_MISE.replace("{log}", str(log)).replace(
            "{stamp}", str(stamp_file)
        ),
        "hc": "#!/bin/sh\nexit 0\n",
    }
    for name, body in stubs.items():
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    # A stale caller venv and the worktree's mise project root must not reach the sync.
    env["VIRTUAL_ENV"] = str(tmp_path / "caller" / ".venv")
    env["MISE_PROJECT_ROOT"] = str(feature)
    if sync_fails:
        env["FAKE_SYNC_FAIL"] = "1"

    result = subprocess.run(
        ["bash", str(LAND)],
        cwd=feature,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert git("-C", str(main), "rev-parse", "HEAD") == merged
    calls = log.read_text(encoding="utf-8").splitlines()
    clean = " venv= root="
    assert calls[0] == "deps uv --explain" + clean, calls
    synced_line = "uv.lock or pyproject.toml changed; synced main's .venv"
    assert ("deps uv" + clean in calls) is (expect != "fresh"), calls
    assert (synced_line in result.stdout) is (expect == "synced"), out
    if expect == "synced":
        assert (
            stamp_file.read_text(encoding="utf-8")
            == (main / "uv.lock").read_text(encoding="utf-8").strip()
            + "\n"
            + (main / "pyproject.toml").read_text(encoding="utf-8").strip()
        )
    if expect == "warned":
        assert (
            "WARNING: could not verify or sync main's .venv; 'mise deps uv' failed (rc 2)"
            in out
        ), out
        assert "uv sync failed: boom" in out, out
        assert "removed worktree fixture" in result.stdout, (
            out
        )  # the land still completes
    # The sync precedes the post-land doctor, so the doctor sees the synced venv.
    doctor = [c for c in calls if c.startswith("doctor project")]
    assert doctor, calls
    assert calls.index("deps uv --explain" + clean) < calls.index(doctor[0]), calls
