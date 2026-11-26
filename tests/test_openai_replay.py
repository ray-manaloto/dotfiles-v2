"""Exercise replay changes with local fixtures and fake providers."""

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def replay_modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / ".github/openai-review"))
    monkeypatch.syspath_prepend(str(ROOT / "tasks/openai/replay"))
    return [
        importlib.import_module(name) for name in ("inputs", "grade", "batch", "spend")
    ]


@pytest.mark.parametrize("private", [False, True])
def test_collect_persists_oversized_decline(
    tmp_path, monkeypatch, replay_modules, private
):
    inputs, *_ = replay_modules
    review = inputs.review
    repo = inputs.REPOS[0]
    endpoint = f"repos/{repo}/pulls/123"
    pr = {
        "merged": True,
        "base": {
            "repo": {"full_name": repo, "default_branch": "main", "private": private},
            "ref": "main",
            "sha": "a" * 40,
        },
        "head": {"sha": "b" * 40},
        "merge_commit_sha": "c" * 40,
        "merged_at": "2026-11-01T00:00:00Z",
        "changed_files": 1,
    }
    files = [
        {
            "filename": "script.py",
            "additions": 1,
            "deletions": 0,
            "patch": "@@ -0,0 +1 @@\n+" + "a" * review.MAX_PATCH_BYTES,
        }
    ]
    monkeypatch.setattr(
        inputs,
        "github",
        {
            endpoint: pr,
            f"{endpoint}/files?per_page={review.FILES_PER_PAGE}": files,
        }.__getitem__,
    )

    if private:
        with pytest.raises(RuntimeError, match="--allow-private"):
            inputs.collect_one(tmp_path, repo, 123, private=private)
        assert not list(tmp_path.glob("pr-*.json"))

    row = inputs.collect_one(
        tmp_path, repo, 123, private=private, allow_private=private
    )

    assert "data" not in row
    assert row["decline"] == "No reviewable files remain after patch checks."
    assert row["skipped"] == {"oversized": 1}
    assert (
        inputs.collect_one(tmp_path, repo, 123, private=private, allow_private=private)
        == row
    )
    if private:
        with pytest.raises(RuntimeError, match="--allow-private"):
            inputs.collect_one(tmp_path, repo, 123, private=private)


def test_missing_line_has_no_patch_excerpt(replay_modules):
    _, grade, *_ = replay_modules
    assert grade.patch_excerpt("", 99) == "[Cited line absent from the supplied patch.]"
    patch = "@@ -0,0 +1 @@\n+some code"
    assert (
        grade.patch_excerpt(patch, 99) == "[Cited line absent from the supplied patch.]"
    )
    assert grade.patch_excerpt(patch, 1) == patch


def test_decisions_retry_respects_spend_cap(tmp_path, monkeypatch, replay_modules):
    _, grade, _, spend = replay_modules
    body = {"input": "fixture"}
    cost = spend.upper_cost(spend.token_bound(body))
    dispatches = []

    def respond(*_args, **_kwargs):
        dispatches.append(None)
        if len(dispatches) == 1:
            raise grade.review.ReviewError("Fake transport failure.")
        return {
            "usage": {
                "input_tokens": 12,
                "input_tokens_details": {"cached_tokens": None},
            }
        }

    monkeypatch.setattr(grade.review, "request_json", respond)
    monkeypatch.setattr(spend, "CAP", 2 * cost - 1)
    with pytest.raises(RuntimeError, match="Cached Decisions"):
        grade.decisions(tmp_path, None, "finding", body)
    with pytest.raises(grade.review.ReviewError):
        grade.decisions(tmp_path, "placeholder", "finding", body)
    with pytest.raises(RuntimeError, match="--retry-reserved"):
        grade.decisions(tmp_path, "placeholder", "finding", body)
    with pytest.raises(RuntimeError, match="spend ceiling"):
        grade.decisions(tmp_path, "placeholder", "finding", body, retry_reserved=True)
    assert len(dispatches) == 1

    monkeypatch.setattr(spend, "CAP", 2 * cost)
    result = grade.decisions(
        tmp_path, "placeholder", "finding", body, retry_reserved=True
    )
    assert grade.decisions(tmp_path, None, "finding", body) == result
    assert len(dispatches) == 2
    assert sorted(map(spend.charge, spend.totals(tmp_path))) == [2, cost]


def test_decisions_distinguishes_payloads_and_preserves_old_charges(
    tmp_path, monkeypatch, replay_modules
):
    inputs, grade, _, spend = replay_modules
    calls = []
    body = {"model": "fixture", "input": "evidence"}
    changed = {**body, "input": "new evidence"}

    def respond(_url, _key, *, body):
        calls.append(body)
        return {"usage": {"input_tokens": 12}, "fixture": body["input"]}

    monkeypatch.setattr(grade.review, "request_json", respond)
    result = grade.decisions(tmp_path, "placeholder", "finding", body)
    assert (
        grade.decisions(tmp_path, None, "finding", dict(reversed(list(body.items()))))
        == result
    )
    fingerprint = inputs.payload_fingerprint(body)
    path = tmp_path / f"decision-finding-{fingerprint}.json"
    original = path.read_bytes()

    for key in (None, "placeholder"):
        with pytest.raises(grade.PayloadMismatch, match="payload changed"):
            grade.decisions(tmp_path, key, "finding", changed)
    assert calls == [body]

    replacement = grade.decisions(
        tmp_path, "placeholder", "finding", changed, retry_reserved=True
    )
    assert replacement != result
    assert grade.decisions(tmp_path, None, "finding", changed) == replacement
    assert path.read_bytes() == original
    assert inputs.read_json(path)["fingerprint"] == fingerprint
    assert calls == [body, changed]
    assert list(map(spend.charge, spend.totals(tmp_path))) == [2, 2]


def test_batch_late_retry_meters_only_successful_reservation(
    tmp_path, monkeypatch, replay_modules
):
    _, _, batch, spend = replay_modules
    requests = [{"custom_id": "fixture"}]
    replies = iter([b"{}", b'{"id":"batch_success"}'])
    monkeypatch.setattr(batch, "upload", lambda *_: "file-fixture")
    monkeypatch.setattr(batch, "api", lambda *_args, **_kwargs: next(replies))
    with pytest.raises(RuntimeError, match="invalid replay identifier"):
        batch.submit(tmp_path, "placeholder", requests, {"batch": 100})
    with pytest.raises(RuntimeError, match="--retry-reserved"):
        batch.submit(tmp_path, "placeholder", requests, {"batch": 100})
    assert (
        batch.submit(
            tmp_path, "placeholder", requests, {"batch": 100}, retry_reserved=True
        )
        == "batch_success"
    )
    spend.meter(
        batch.reservation_path(tmp_path),
        [{"input_tokens": 10, "output_tokens": 1}],
        batch=True,
    )

    assert sorted(map(spend.charge, spend.totals(tmp_path))) == [15, 100]


def test_regrade_rebuilds_summary_from_receipt(tmp_path, monkeypatch, replay_modules):
    inputs, *_ = replay_modules
    independent = importlib.import_module("independent")
    rows = [
        {
            "id": "0-123-finding",
            "scores": (0.91, 0.0),
            "luna": "incorrect",
            "finding": {"claim": "fixture claim"},
            "prior": [],
        }
    ]
    sent = []

    def respond(url, _key, *, body):
        sent.append(body)
        assert url == independent.review.OPENAI_ENDPOINT
        assert body["model"] == "gpt-6-sol"
        assert body["store"] is False
        assert "incorrect" not in body["input"][1]["content"]
        return {
            "model": "gpt-6-sol",
            "status": "completed",
            "usage": {"input_tokens": 12, "output_tokens": 3},
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": '{"verdict":"defect"}'}
                    ],
                }
            ],
        }

    monkeypatch.setattr(independent.review, "request_json", respond)
    with pytest.raises(RuntimeError, match="Cached independent response"):
        independent.regrade(tmp_path, None, rows)
    assert not sent
    independent.regrade(tmp_path, "placeholder", rows)
    path = tmp_path / "independent-summary.json"
    first = path.read_bytes()
    path.unlink()

    independent.regrade(tmp_path, None, rows)

    assert len(sent) == 1
    assert path.read_bytes() == first
    assert inputs.read_json(path)["rows"][0]["verdict"] == "defect"

    reporting = importlib.import_module("reporting")
    summary = inputs.read_json(path)
    assert summary["thresholds"] == {"proposed": [0.85, 0.0], "baseline": [0.85, 1.0]}
    graded = [("defect", 0.91, 0.0, True)]
    original_report = reporting.independent_section(summary, graded)
    with monkeypatch.context() as context:
        context.setattr(independent, "PROBABILITY", 0.95)
        context.setattr(independent.triage, "MIN_PROBABILITY", 0.95)
        independent.regrade(tmp_path, None, rows)
        assert path.read_bytes() == first
        assert reporting.independent_section(summary, graded) == original_report

    original_path = tmp_path / "independent-0-123-finding.json"
    original_receipt = original_path.read_bytes()
    inputs.write_json(
        tmp_path / "evidence-0-123-finding.json", {"merged_file_excerpt": "new context"}
    )
    for key in (None, "placeholder"):
        with pytest.raises(RuntimeError, match="No matching independent receipt"):
            independent.regrade(tmp_path, key, rows)
    assert len(sent) == 1
    independent.regrade(tmp_path, "placeholder", rows, retry_reserved=True)
    independent.regrade(tmp_path, None, rows)
    assert len(sent) == 2
    assert original_path.read_bytes() == original_receipt
    replacement = inputs.read_json(tmp_path / "independent-0-123-finding-retry-1.json")
    original = inputs.read_json(original_path)
    assert replacement["fingerprint"] != original["fingerprint"]
    assert (
        inputs.read_json(path)["estimate"]
        == original["reserved"] + replacement["reserved"]
    )


def test_regrade_retry_keeps_ambiguous_attempt_charged(
    tmp_path, monkeypatch, replay_modules
):
    inputs, *_ = replay_modules
    independent = importlib.import_module("independent")
    rows = [
        {
            "id": "0-123-finding",
            "scores": (0.91, 0.0),
            "luna": "incorrect",
            "finding": {"claim": "fixture claim"},
            "prior": [],
        }
    ]
    replies = iter(
        (None, {"model": "gpt-6-sol", "status": "incomplete", "usage": None})
    )

    def respond(*_args, **_kwargs):
        reply = next(replies)
        if reply is None:
            raise independent.review.ReviewError("Fake transport failure.")
        return reply

    monkeypatch.setattr(independent.review, "request_json", respond)
    with pytest.raises(independent.review.ReviewError):
        independent.regrade(tmp_path, "placeholder", rows)
    first = tmp_path / "independent-0-123-finding.json"
    reservation = inputs.read_json(first)
    cost = reservation["reserved"]
    with pytest.raises(RuntimeError, match="--retry-reserved"):
        independent.regrade(tmp_path, "placeholder", rows)
    monkeypatch.setattr(independent, "CAP", 2 * cost - 1)
    with pytest.raises(RuntimeError, match="added"):
        independent.regrade(tmp_path, "placeholder", rows, retry_reserved=True)

    monkeypatch.setattr(independent, "CAP", 2 * cost)
    independent.regrade(tmp_path, "placeholder", rows, retry_reserved=True)
    independent.regrade(tmp_path, None, rows)
    summary = inputs.read_json(tmp_path / "independent-summary.json")

    assert inputs.read_json(first) == reservation
    assert summary["estimate"] == 2 * cost
    assert summary["unreconciled"] == cost


@pytest.fixture
def replay_cli(tmp_path, monkeypatch, replay_modules):
    _, _, batch, _ = replay_modules
    run = importlib.import_module("run")
    plan = tmp_path / ".planning" / "fixture"
    plan.mkdir(parents=True)
    (plan / "task_plan.md").touch()
    monkeypatch.setenv("PLAN_ID", plan.name)
    monkeypatch.setattr(run, "ROOT", tmp_path)
    monkeypatch.setattr(run, "configure_logging", lambda **_: None)
    monkeypatch.setattr(run.os, "umask", lambda _: None)

    def stop_after_privacy_check(*_args):
        raise ValueError("Validation reached request preparation.")

    monkeypatch.setattr(batch, "prepare", stop_after_privacy_check)
    return run


@pytest.mark.parametrize("phase", ["submit", "evidence", "finish", "regrade"])
@pytest.mark.parametrize("private", [True, None], ids=["private", "unknown"])
def test_live_phases_check_fresh_visibility(
    monkeypatch, replay_modules, replay_cli, phase, private
):
    inputs, *_ = replay_modules
    repo = inputs.REPOS[0]
    reads = []

    def github(endpoint):
        reads.append(endpoint)
        return {"full_name": repo, "private": private}

    monkeypatch.setattr(inputs, "github", github)
    monkeypatch.setattr(
        replay_cli, "collect", lambda *_, **__: [{"repo": repo, "private": False}]
    )
    monkeypatch.setattr(sys, "argv", ["run.py", phase])
    with pytest.raises(RuntimeError, match="--allow-private"):
        replay_cli.main()

    monkeypatch.setattr(sys, "argv", ["run.py", phase, "--allow-private"])
    with pytest.raises(ValueError, match="Validation reached request preparation"):
        replay_cli.main()
    assert reads == [f"repos/{repo}", f"repos/{repo}"]


@pytest.mark.parametrize("phase", ["finish", "regrade"])
@pytest.mark.parametrize("row", [{"private": True}, {}], ids=["private", "unknown"])
def test_cache_only_checks_saved_visibility(
    monkeypatch, replay_modules, replay_cli, phase, row
):
    inputs, *_ = replay_modules
    monkeypatch.setattr(
        inputs, "github", lambda *_: pytest.fail("cache-only attempted a GitHub read")
    )
    monkeypatch.setattr(replay_cli, "collect", lambda *_, **__: [row])
    argv = ["run.py", phase, "--cached-only"]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(RuntimeError, match="--allow-private"):
        replay_cli.main()

    monkeypatch.setattr(sys, "argv", [*argv, "--allow-private"])
    with pytest.raises(ValueError, match="Validation reached request preparation"):
        replay_cli.main()


def test_prepare_rechecks_visibility_before_using_cached_inputs(
    tmp_path, monkeypatch, replay_modules
):
    inputs, *_ = replay_modules
    repo = inputs.REPOS[0]
    inputs.write_json(tmp_path / "inventory.json", [[repo, 123]])
    path = tmp_path / "pr-0-123.json"
    inputs.write_json(path, {"private": False})
    monkeypatch.setattr(
        inputs,
        "github",
        {
            f"repos/{name}": {"full_name": name, "private": name == repo}
            for name in inputs.REPOS
        }.__getitem__,
    )
    with pytest.raises(RuntimeError, match="--allow-private"):
        inputs.collect(tmp_path)
    assert inputs.read_json(path) == {"private": False}
    assert inputs.collect(tmp_path, allow_private=True) == [{"private": True}]
    assert inputs.read_json(path) == {"private": True}
