import json
import os
import subprocess
from pathlib import Path

import pytest

LAND = Path(__file__).resolve().parents[1] / "tasks/pr/land"


@pytest.mark.parametrize("ahead", [True, False], ids=["ahead", "in-sync"])
def test_pr_land_requires_main_to_match_origin(tmp_path, ahead):
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
    git("-C", str(origin), "commit", "-q", "--allow-empty", "-m", "Merged PR")
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
    mise.write_text("#!/bin/sh\nprintf 'mise %s\\n' \"$*\"\n", encoding="utf-8")
    for stub in (gh, mise):
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
