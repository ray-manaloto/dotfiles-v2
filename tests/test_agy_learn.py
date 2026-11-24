"""Tests for schema-first `agy_learn` ledger, 5-tier escalation, heal checks, and hook fixes."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import orjson
import pytest
from pydantic import ValidationError

from agy_learn import (
    DECLINED_BUFFER_NS,
    PROPOSAL_MD_MAX_BYTES,
    mark_proposal,
    propose_item,
    read_proposals,
    read_reviews,
    record_review,
    render_active_proposal_md,
    run_heal_check,
)
from agy_learn.cli import main as agy_learn_cli_main
from dotfiles_py import NANOSECOND_TIMESTAMP_RE
from dotfiles_py.generated import (
    EnforcementTier,
    InteractiveQuestionsHookState,
    LearnErrorCode,
    LearnProposal,
    LearnReturnCode,
    ProposalStatus,
    ProvenanceRef,
    RetentionClass,
    ReviewVerdict,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "unix" / "home" / ".gemini" / "config" / "hooks"
HOOKS_JSON = REPO_ROOT / "unix" / "home" / ".gemini" / "config" / "hooks.json"


def _load_hook(name: str) -> Any:
    path = HOOKS_DIR / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generated_agy_learn_models_and_hook_state_strict() -> None:
    state = InteractiveQuestionsHookState(
        last_user_idx=2,
        last_blocked_step_idx=5,
        blocks_in_turn=1,
        pre_denied_fps=["2:abc"],
    )
    assert state.blocks_in_turn == 1

    with pytest.raises(ValidationError):
        InteractiveQuestionsHookState.model_validate(
            {
                "last_user_idx": 1,
                "last_blocked_step_idx": -1,
                "blocks_in_turn": 0,
                "pre_denied_fps": [],
                "extra": True,
            }
        )

    with pytest.raises(ValidationError):
        LearnProposal.model_validate(
            {
                "proposal_id": "p1",
                "dedup_key": "k1",
                "title": "t",
                "summary": "s",
                "target_path": "AGENTS.md",
                "tier": "rule",
                "recommended_tier": "rule",
                "retention_class": "safety-invariant",
                "status": "pending",
                "occurrences": 1,
                "provenance": {"transcript_step": "L1"},
                "timestamp": "invalid-ts",
                "timestamp_unix_ns": 1,
            }
        )


def test_record_review_requires_provenance_and_accepts_zero_survivors(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "agy-learn"

    missing_prov = record_review(
        plan_key="/tmp/plan/task_plan.md",
        plan_sha256="abc123",
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=0,
        survivor_count=0,
        provenance=ProvenanceRef(),
        state_dir=state_dir,
    )
    assert missing_prov.rc == 1
    assert missing_prov.errors[0].code is LearnErrorCode.ERR_MISSING_PROVENANCE

    zero_ok = record_review(
        plan_key="/tmp/plan/task_plan.md",
        plan_sha256="abc123",
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=1,
        survivor_count=0,
        provenance=ProvenanceRef(transcript_step="transcript.jsonl#L42"),
        state_dir=state_dir,
    )
    assert zero_ok.rc == 0
    assert zero_ok.review is not None
    assert zero_ok.review.survivor_count == 0
    assert NANOSECOND_TIMESTAMP_RE.fullmatch(zero_ok.timestamp)
    assert zero_ok.returns[0].code is LearnReturnCode.RET_NO_NEW_PROPOSALS

    rows, errs = read_reviews(state_dir)
    assert errs == []
    assert len(rows) == 1
    assert rows[0].outcome is ReviewVerdict.NO_NEW_PROPOSALS


def test_declined_30_day_buffer_and_recurrence_auto_escalation(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "agy-learn"

    # Missing provenance fails closed
    bad = propose_item(
        dedup_key="skill:uv-offline",
        title="Use uv --offline",
        summary="Pass --offline to uv in tests",
        target_path="unix/home/.agents/skills/mise-bootstrap-ops/SKILL.md",
        tier=EnforcementTier.SKILL,
        retention_class=RetentionClass.WORKFLOW_SKILL,
        provenance=ProvenanceRef(),
        state_dir=state_dir,
    )
    assert bad.rc == 1
    assert bad.errors[0].code is LearnErrorCode.ERR_MISSING_PROVENANCE

    # First occurrence at Tier 4 (skill)
    p1 = propose_item(
        dedup_key="skill:uv-offline",
        title="Use uv --offline",
        summary="Pass --offline to uv in tests",
        target_path="unix/home/.agents/skills/mise-bootstrap-ops/SKILL.md",
        tier=EnforcementTier.SKILL,
        retention_class=RetentionClass.WORKFLOW_SKILL,
        provenance=ProvenanceRef(plan_error_ref="task_plan.md#error-1"),
        state_dir=state_dir,
    )
    assert p1.rc == 0
    assert p1.proposal is not None
    pid = p1.proposal.proposal_id
    assert p1.proposal.recommended_tier is EnforcementTier.SKILL

    # Decline -> sets 30-day declined_until_unix_ns
    declined = mark_proposal(
        proposal_id=pid,
        status=ProposalStatus.DECLINED,
        state_dir=state_dir,
    )
    assert declined.rc == 0
    assert declined.proposal is not None
    assert declined.proposal.declined_until_unix_ns is not None
    assert (
        declined.proposal.declined_until_unix_ns - declined.proposal.timestamp_unix_ns
        == DECLINED_BUFFER_NS
    )

    # Identical provenance re-proposal within 30 days is blocked by declined buffer
    blocked = propose_item(
        dedup_key="skill:uv-offline",
        title="Use uv --offline",
        summary="Pass --offline to uv in tests",
        target_path="unix/home/.agents/skills/mise-bootstrap-ops/SKILL.md",
        tier=EnforcementTier.SKILL,
        retention_class=RetentionClass.WORKFLOW_SKILL,
        provenance=ProvenanceRef(plan_error_ref="task_plan.md#error-1"),
        state_dir=state_dir,
    )
    assert blocked.rc == 1
    assert blocked.errors[0].code is LearnErrorCode.ERR_DECLINED_ACTIVE

    # New occurrence (occurrences == 2) overrides 30-day buffer and escalates to Tier 1-3!
    escalated = propose_item(
        dedup_key="skill:uv-offline",
        title="Use uv --offline",
        summary="Repeated across 2 tasks; enforce deterministically",
        target_path="unix/home/.gemini/config/hooks/post-task-learn.py",
        tier=EnforcementTier.RULE,
        retention_class=RetentionClass.SAFETY_INVARIANT,
        provenance=ProvenanceRef(plan_error_ref="task_plan2.md#error-1"),
        state_dir=state_dir,
    )
    assert escalated.rc == 0
    assert escalated.proposal is not None
    assert escalated.proposal.occurrences == 2
    assert escalated.proposal.status is ProposalStatus.PENDING
    assert escalated.proposal.recommended_tier is EnforcementTier.AGY_HOOK
    assert any(r.code is LearnReturnCode.RET_ESCALATED for r in escalated.returns)


def test_heal_check_read_only_detection_and_thin_proposal_render(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "agy-learn"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    # Empty state check does not create state_dir (strictly read-only)
    empty_report = run_heal_check(state_dir=state_dir, repo_root=repo_dir)
    assert empty_report.healthy is True
    assert empty_report.read_only is True
    assert not state_dir.exists()

    # Create target file already containing proposed snippet
    target_skill = repo_dir / "SKILL.md"
    target_skill.write_text(
        "# Skill\n\nAlways run mise lock --global && chmod 0644 mise.lock cleanly.\n",
        encoding="utf-8",
    )

    propose_item(
        dedup_key="already:lock-chmod",
        title="Lock chmod 0644",
        summary="Ensure 0644 permissions on mise.lock",
        target_path="SKILL.md",
        tier=EnforcementTier.SKILL,
        retention_class=RetentionClass.WORKFLOW_SKILL,
        provenance=ProvenanceRef(transcript_step="t.jsonl#L5"),
        proposed_diff="Always run mise lock --global && chmod 0644 mise.lock cleanly.",
        state_dir=state_dir,
    )
    propose_item(
        dedup_key="note:temp",
        title="One-time note",
        summary="Transient note should prune from active render",
        target_path="AGENTS.md",
        tier=EnforcementTier.RULE,
        retention_class=RetentionClass.ONE_TIME_NOTE,
        provenance=ProvenanceRef(transcript_step="t.jsonl#L8"),
        state_dir=state_dir,
    )
    # Add many pending items with long diffs to test 8 KB budget cap
    for idx in range(25):
        propose_item(
            dedup_key=f"hk:rule-{idx}",
            title=f"Gate rule {idx}",
            summary=f"Enforce invariant {idx} deterministically.",
            target_path="hk.pkl",
            tier=EnforcementTier.HK_PKL,
            retention_class=RetentionClass.SAFETY_INVARIANT,
            provenance=ProvenanceRef(transcript_step=f"t.jsonl#L{20 + idx}"),
            proposed_diff=("x" * 500) + f"_{idx}",
            state_dir=state_dir,
        )

    # Record owing_learn review
    record_review(
        plan_key=str(repo_dir / ".planning" / "p1" / "task_plan.md"),
        plan_sha256="deadbeef",
        outcome=ReviewVerdict.OWING_LEARN,
        error_count=2,
        survivor_count=0,
        provenance=ProvenanceRef(plan_error_ref="p1#Errors Encountered"),
        state_dir=state_dir,
    )

    # Oversized learning_proposal.md triggers bloat finding
    out_md = tmp_path / "learning_proposal.md"
    out_md.write_text("B" * (PROPOSAL_MD_MAX_BYTES + 100), encoding="utf-8")

    before_proposals = (state_dir / "proposals.jsonl").read_bytes()
    report = run_heal_check(
        state_dir=state_dir, repo_root=repo_dir, proposal_md_path=out_md
    )
    after_proposals = (state_dir / "proposals.jsonl").read_bytes()
    assert before_proposals == after_proposals  # strictly read-only

    assert report.healthy is False
    assert report.proposal_md_within_budget is False
    assert len(report.owing_plans) == 1
    assert len(report.already_applied_ids) == 1

    # render-proposal produces thin <= 8 KB active-only file pruning already_applied & one-time-note
    rendered = render_active_proposal_md(
        out_path=out_md, state_dir=state_dir, repo_root=repo_dir
    )
    assert rendered.rc == 0
    assert rendered.rendered_bytes is not None
    assert rendered.rendered_bytes <= PROPOSAL_MD_MAX_BYTES
    md_text = out_md.read_text(encoding="utf-8")
    assert "already:lock-chmod" not in md_text
    assert "One-time note" not in md_text
    assert any(r.code is LearnReturnCode.RET_AUTO_PRUNED for r in rendered.returns)

    # Also test CLI round-trip
    rc = agy_learn_cli_main(
        [
            "heal",
            "--check",
            "--json",
            "--state-dir",
            str(state_dir),
            "--repo",
            str(repo_dir),
            "--proposal-md",
            str(out_md),
        ]
    )
    assert rc == 1  # owing_learn still active until cleared

    rows, _ = read_proposals(state_dir)
    assert len(rows) == 27


def test_hooks_python39_compatibility_and_cross_session_worktree_rm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Run every edited hook's --self-check under macOS /usr/bin/python3 (3.9.6) when present
    py39 = Path("/usr/bin/python3")
    if py39.is_file():
        for hook_name in (
            "post-task-learn.py",
            "agy-pwf-guard.py",
            "agy-hc-send-guard.py",
            "agy-interactive-questions.py",
        ):
            proc = subprocess.run(
                [
                    str(py39),
                    "-I",
                    "-B",
                    str(HOOKS_DIR / hook_name),
                    "--self-check",
                    "--check-hooks-json",
                    str(HOOKS_JSON),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            parsed = orjson.loads(proc.stdout)
            assert parsed["status"] == "ok", hook_name

    # Verify cross-session worktree:rm against shared $XDG_STATE_HOME/agy-learn/reviews.jsonl
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    pwf_guard = _load_hook("agy-pwf-guard.py")

    main_repo = tmp_path / "repo"
    (main_repo / ".git").mkdir(parents=True)
    wt_dir = tmp_path / "repo-feat"
    pdir = wt_dir / ".planning" / "2026-11-23-feat"
    pdir.mkdir(parents=True)
    plan_file = pdir / "task_plan.md"
    plan_file.write_text(
        "# Task Plan: #12 Feat\n\n### Phase 1: Do\n**Status:** complete\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(plan_file.read_bytes()).hexdigest()

    deny_res = pwf_guard._run_hook(
        {
            "cwd": str(main_repo),
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "mise run worktree:rm feat"},
            },
        }
    )
    assert deny_res["decision"] == "deny"

    rec_resp = record_review(
        plan_key=str(plan_file.resolve()),
        plan_sha256=digest,
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=0,
        survivor_count=0,
        provenance=ProvenanceRef(transcript_step="t.jsonl#L10"),
    )
    assert rec_resp.rc == 0

    allow_res = pwf_guard._run_hook(
        {
            "cwd": str(main_repo),
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "mise run worktree:rm feat"},
            },
        }
    )
    assert allow_res == {"decision": "allow"}
    assert os.environ["XDG_STATE_HOME"] == str(xdg)
    assert json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["post-task-learn"][
        "SessionStart"
    ]
