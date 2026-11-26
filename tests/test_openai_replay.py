"""Exercise replay changes with local fixtures and fake providers."""

import importlib
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


def test_collect_persists_oversized_decline(tmp_path, monkeypatch, replay_modules):
    inputs, *_ = replay_modules
    review = inputs.review
    repo = inputs.REPOS[0]
    endpoint = f"repos/{repo}/pulls/123"
    pr = {
        "merged": True,
        "base": {
            "repo": {"full_name": repo, "default_branch": "main"},
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

    row = inputs.collect_one(tmp_path, repo, 123)

    assert "data" not in row
    assert row["decline"] == "No reviewable files remain after patch checks."
    assert row["skipped"] == {"oversized": 1}
    assert inputs.collect_one(tmp_path, repo, 123) == row


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
