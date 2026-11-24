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
from agy_learn.ledger import apply_proposals, migrate_reviews
from dotfiles_py import NANOSECOND_TIMESTAMP_RE
from dotfiles_py.generated import (
    EnforcementTier,
    HealFindingKind,
    HealSeverity,
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

    zero_clean = record_review(
        plan_key="/tmp/plan/task_plan.md",
        plan_sha256="abc123",
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=0,
        survivor_count=0,
        provenance=ProvenanceRef(transcript_step="transcript.jsonl#L42"),
        state_dir=state_dir,
    )
    assert zero_clean.rc == 0
    assert zero_clean.review is not None
    assert zero_clean.review.survivor_count == 0
    assert NANOSECOND_TIMESTAMP_RE.fullmatch(zero_clean.timestamp)
    assert zero_clean.returns[0].code is LearnReturnCode.RET_NO_NEW_PROPOSALS

    zero_with_err_justified = record_review(
        plan_key="/tmp/plan/task_plan.md",
        plan_sha256="abc123",
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=1,
        survivor_count=0,
        provenance=ProvenanceRef(
            transcript_step="transcript.jsonl#L42",
            plan_error_ref="Errors Encountered row 1: transient timeout",
        ),
        notes="Row 1 transient timeout was already handled by existing retry backoff; zero new rules needed.",
        state_dir=state_dir,
    )
    assert zero_with_err_justified.rc == 0
    assert zero_with_err_justified.review is not None
    assert zero_with_err_justified.review.error_count == 1
    assert zero_with_err_justified.review.survivor_count == 0

    rows, errs = read_reviews(state_dir)
    assert errs == []
    assert len(rows) == 2
    assert rows[0].outcome is ReviewVerdict.NO_NEW_PROPOSALS


def test_record_review_canonicalizes_plan_key_and_rejects_bare_slugs(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "agy-learn"
    repo_dir = tmp_path / "repo-wt"
    plan_file = repo_dir / ".planning" / "2026-11-23-feat" / "task_plan.md"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text("# Task Plan: #17 Feat\n", encoding="utf-8")
    resolved_plan = str(plan_file.resolve())

    # 1. Bare non-path slug without --plan-path fails closed
    bare_err = record_review(
        plan_key="2026-11-23-feat",
        plan_sha256="sha1",
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=0,
        survivor_count=0,
        provenance=ProvenanceRef(transcript_step="t.jsonl#L1"),
        state_dir=state_dir,
    )
    assert bare_err.rc == 1
    assert bare_err.errors[0].code is LearnErrorCode.ERR_INVALID_INPUT

    # 2. Bare slug via CLI without --plan-path also returns rc=1 ERR_INVALID_INPUT
    cli_rc = agy_learn_cli_main(
        [
            "record-review",
            "--plan-key",
            "2026-11-23-feat",
            "--plan-sha256",
            "sha1",
            "--outcome",
            "no_new_proposals",
            "--transcript-step",
            "t.jsonl#L1",
            "--state-dir",
            str(state_dir),
        ]
    )
    assert cli_rc == 1

    # 3. Bare matching slug or relative path canonicalizes against --plan-path
    from_bare_with_path = record_review(
        plan_key="2026-11-23-feat",
        plan_sha256="sha1",
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=0,
        survivor_count=0,
        provenance=ProvenanceRef(
            transcript_step="t.jsonl#L2", plan_path=str(plan_file)
        ),
        state_dir=state_dir,
    )
    assert from_bare_with_path.rc == 0
    assert from_bare_with_path.review is not None
    assert from_bare_with_path.review.plan_key == resolved_plan

    from_rel_with_path = record_review(
        plan_key=".planning/2026-11-23-feat/task_plan.md",
        plan_sha256="sha1",
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=0,
        survivor_count=0,
        provenance=ProvenanceRef(
            transcript_step="t.jsonl#L3", plan_path=str(plan_file)
        ),
        state_dir=state_dir,
    )
    assert from_rel_with_path.rc == 0
    assert from_rel_with_path.review is not None
    assert from_rel_with_path.review.plan_key == resolved_plan

    # 4. Omitted --plan-key in CLI with --plan-path derives canonical key
    cli_from_path_rc = agy_learn_cli_main(
        [
            "record-review",
            "--plan-sha256",
            "sha1",
            "--outcome",
            "no_new_proposals",
            "--transcript-step",
            "t.jsonl#L4",
            "--plan-path",
            str(plan_file),
            "--state-dir",
            str(state_dir),
        ]
    )
    assert cli_from_path_rc == 0

    # 5. Mismatched --plan-key vs --plan-path fails closed
    mismatch = record_review(
        plan_key="other-slug",
        plan_sha256="sha1",
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=0,
        survivor_count=0,
        provenance=ProvenanceRef(
            transcript_step="t.jsonl#L5", plan_path=str(plan_file)
        ),
        state_dir=state_dir,
    )
    assert mismatch.rc == 1
    assert mismatch.errors[0].code is LearnErrorCode.ERR_INVALID_INPUT

    path_mismatch = record_review(
        plan_key=str(repo_dir / ".planning" / "other" / "task_plan.md"),
        plan_sha256="sha1",
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=0,
        survivor_count=0,
        provenance=ProvenanceRef(
            transcript_step="t.jsonl#L6", plan_path=str(plan_file)
        ),
        state_dir=state_dir,
    )
    assert path_mismatch.rc == 1
    assert path_mismatch.errors[0].code is LearnErrorCode.ERR_INVALID_INPUT

    # 6. Repo-relative .planning/<PLAN_ID>/task_plan.md and legacy task_plan.md stay valid
    rel_only = record_review(
        plan_key=".planning/2026-11-23-feat/task_plan.md",
        plan_sha256="sha1",
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=0,
        survivor_count=0,
        provenance=ProvenanceRef(transcript_step="t.jsonl#L7"),
        state_dir=state_dir,
    )
    assert rel_only.rc == 0
    assert rel_only.review is not None
    assert rel_only.review.plan_key == ".planning/2026-11-23-feat/task_plan.md"

    rel_with_root = record_review(
        plan_key=".planning/2026-11-23-feat/task_plan.md",
        plan_sha256="sha1",
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=0,
        survivor_count=0,
        provenance=ProvenanceRef(transcript_step="t.jsonl#L8"),
        repo_root=str(repo_dir),
        state_dir=state_dir,
    )
    assert rel_with_root.rc == 0
    assert rel_with_root.review is not None
    assert rel_with_root.review.plan_key == resolved_plan

    # 7. Directory-without-trailing-slash and '..' traversal rejected on plan_key and plan_path
    for bad_key, bad_plan_path in (
        (".planning/2026-11-23-feat", None),
        ("../wt/.planning/2026-11-23-feat/task_plan.md", None),
        ("", str(repo_dir / ".planning" / "2026-11-23-feat")),
        (
            str(repo_dir / ".planning" / "2026-11-23-feat" / "task_plan.md"),
            str(repo_dir / ".planning" / "2026-11-23-feat"),
        ),
        (
            "2026-11-23-feat",
            str(
                repo_dir
                / ".planning"
                / "2026-11-23-feat"
                / ".."
                / "2026-11-23-feat"
                / "task_plan.md"
            ),
        ),
    ):
        bad_dir_or_dotdot = record_review(
            plan_key=bad_key,
            plan_sha256="sha1",
            outcome=ReviewVerdict.NO_NEW_PROPOSALS,
            error_count=0,
            survivor_count=0,
            provenance=ProvenanceRef(
                transcript_step="t.jsonl#L9", plan_path=bad_plan_path
            ),
            state_dir=state_dir,
        )
        assert bad_dir_or_dotdot.rc == 1
        assert bad_dir_or_dotdot.errors[0].code is LearnErrorCode.ERR_INVALID_INPUT


def test_anti_rubber_stamp_gate_rejects_unjustified_zero_survivor_reviews(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "agy-learn"
    plan_key = str((tmp_path / ".planning" / "p1" / "task_plan.md").resolve())

    # Missing notes & plan_error_ref
    missing_both = record_review(
        plan_key=plan_key,
        plan_sha256="sha2",
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=2,
        survivor_count=0,
        provenance=ProvenanceRef(transcript_step="t.jsonl#L10"),
        state_dir=state_dir,
    )
    assert missing_both.rc == 1
    assert missing_both.errors[0].code is LearnErrorCode.ERR_INVALID_INPUT

    # Short notes (< 40 chars) even with plan_error_ref
    short_notes = record_review(
        plan_key=plan_key,
        plan_sha256="sha2",
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=1,
        survivor_count=0,
        provenance=ProvenanceRef(
            transcript_step="t.jsonl#L11",
            plan_error_ref="Errors Encountered row 1",
        ),
        notes="Issue #13 canonical path review.",
        state_dir=state_dir,
    )
    assert short_notes.rc == 1
    assert short_notes.errors[0].code is LearnErrorCode.ERR_INVALID_INPUT

    # Boilerplate notes >= 40 chars (including dash padding & hyphen/slash tokens) rejected
    for bad_note in (
        "post-task /learn: no reusable customization updates needed.",
        "Post-task /learn: no reusable updates needed.",
        "no new proposals --- --- --- none needed",
        "-" * 50,
        "no new proposals needed; no new proposals needed; none",
    ):
        boilerplate = record_review(
            plan_key=plan_key,
            plan_sha256="sha2",
            outcome=ReviewVerdict.NO_NEW_PROPOSALS,
            error_count=1,
            survivor_count=0,
            provenance=ProvenanceRef(
                transcript_step="t.jsonl#L12",
                plan_error_ref="Errors Encountered row 1",
            ),
            notes=bad_note,
            state_dir=state_dir,
        )
        assert boilerplate.rc == 1, bad_note
        assert boilerplate.errors[0].code is LearnErrorCode.ERR_INVALID_INPUT

    # Placeholder / trivial plan_error_ref variants rejected
    for bad_ref in ("(none)", "no error", "N/A - none", "x", "_"):
        bad_err_ref = record_review(
            plan_key=plan_key,
            plan_sha256="sha2",
            outcome=ReviewVerdict.NO_NEW_PROPOSALS,
            error_count=1,
            survivor_count=0,
            provenance=ProvenanceRef(
                transcript_step="t.jsonl#L12b",
                plan_error_ref=bad_ref,
            ),
            notes="Row 1 already enforced in hk.pkl ruff and ty static checks.",
            state_dir=state_dir,
        )
        assert bad_err_ref.rc == 1, bad_ref
        assert bad_err_ref.errors[0].code is LearnErrorCode.ERR_INVALID_INPUT

    # Substantive notes >= 40 chars without plan_error_ref rejected
    no_err_ref = record_review(
        plan_key=plan_key,
        plan_sha256="sha2",
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=1,
        survivor_count=0,
        provenance=ProvenanceRef(transcript_step="t.jsonl#L13"),
        notes="Row 1 already enforced in hk.pkl ruff and ty static checks.",
        state_dir=state_dir,
    )
    assert no_err_ref.rc == 1
    assert no_err_ref.errors[0].code is LearnErrorCode.ERR_INVALID_INPUT

    # Cannot bypass anti-rubber-stamp via outcome=proposed or outcome=applied with survivor_count=0
    for verdict in (ReviewVerdict.PROPOSED, ReviewVerdict.APPLIED):
        vacuous = record_review(
            plan_key=plan_key,
            plan_sha256="sha2",
            outcome=verdict,
            error_count=1,
            survivor_count=0,
            proposal_ids=[],
            provenance=ProvenanceRef(transcript_step="t.jsonl#L13b"),
            state_dir=state_dir,
        )
        assert vacuous.rc == 1, verdict
        assert vacuous.errors[0].code is LearnErrorCode.ERR_INVALID_INPUT

    # Valid proposed / applied reviews with survivor_count >= 1 or proposal_ids succeed
    valid_proposed = record_review(
        plan_key=plan_key,
        plan_sha256="sha2",
        outcome=ReviewVerdict.PROPOSED,
        error_count=1,
        survivor_count=1,
        proposal_ids=["prop-1234abcd"],
        provenance=ProvenanceRef(transcript_step="t.jsonl#L13c"),
        state_dir=state_dir,
    )
    assert valid_proposed.rc == 0
    valid_applied = record_review(
        plan_key=plan_key,
        plan_sha256="sha2",
        outcome=ReviewVerdict.APPLIED,
        error_count=1,
        survivor_count=0,
        proposal_ids=["prop-1234abcd"],
        provenance=ProvenanceRef(transcript_step="t.jsonl#L13d"),
        state_dir=state_dir,
    )
    assert valid_applied.rc == 0

    # Both substantive notes (>= 40 chars) and non-placeholder plan_error_ref succeed
    accepted = record_review(
        plan_key=plan_key,
        plan_sha256="sha2",
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=1,
        survivor_count=0,
        provenance=ProvenanceRef(
            transcript_step="t.jsonl#L14",
            plan_error_ref="Errors Encountered row 1: ruff formatting failure",
        ),
        notes="Row 1 already enforced by hk.pkl ruff check; no additional rule needed.",
        state_dir=state_dir,
    )
    assert accepted.rc == 0
    assert accepted.review is not None


def test_heal_check_surfaces_bare_slug_drift_non_fatally(tmp_path: Path) -> None:
    state_dir = tmp_path / "agy-learn"
    state_dir.mkdir(parents=True)
    canon_path = str(
        (tmp_path / "wt" / ".planning" / "2026-11-23-legacy" / "task_plan.md").resolve()
    )
    legacy_rows = [
        {
            "review_id": "rev-legacy-bare",
            "plan_key": "2026-11-23-legacy",
            "plan_id": "2026-11-23-legacy",
            "worktree_slug": "legacy",
            "plan_sha256": "deadbeef",
            "error_count": 1,
            "outcome": "no_new_proposals",
            "survivor_count": 0,
            "proposal_ids": [],
            "provenance": {
                "transcript_step": "step-10",
                "plan_error_ref": "Errors Encountered row 1",
                "plan_path": canon_path,
            },
            "timestamp": "20261123T120000.000000001-0600",
            "timestamp_unix_ns": 1795456800000000001,
        },
        {
            "review_id": "rev-canon",
            "plan_key": canon_path,
            "plan_id": "2026-11-23-legacy",
            "worktree_slug": "legacy",
            "plan_sha256": "deadbeef",
            "error_count": 1,
            "outcome": "no_new_proposals",
            "survivor_count": 0,
            "proposal_ids": [],
            "provenance": {
                "transcript_step": "step-11",
                "plan_error_ref": "Errors Encountered row 1",
                "plan_path": canon_path,
            },
            "timestamp": "20261123T120001.000000001-0600",
            "timestamp_unix_ns": 1795456801000000001,
        },
    ]
    (state_dir / "reviews.jsonl").write_text(
        "\n".join(json.dumps(r) for r in legacy_rows) + "\n", encoding="utf-8"
    )

    report = run_heal_check(state_dir=state_dir)
    assert report.healthy is True
    assert report.errors == []
    drift_findings = [
        f
        for f in report.findings
        if f.kind is HealFindingKind.HOOK_DRIFT and f.plan_key == "2026-11-23-legacy"
    ]
    assert len(drift_findings) == 1
    assert drift_findings[0].severity is HealSeverity.WARN
    assert "prune/migrate" in drift_findings[0].message
    assert agy_learn_cli_main(["heal", "--check", "--state-dir", str(state_dir)]) == 0


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
    # Unified diff with unchanged context + unapplied '+' line must NOT false-positive as already applied
    (repo_dir / "hook.py").write_text(
        "# common header context line >= 16 chars\nold_behavior_call_here()\n",
        encoding="utf-8",
    )
    unapplied_diff = propose_item(
        dedup_key="diff:unapplied-unified",
        title="Unified diff guard",
        summary="Replace old behavior with safe helper call.",
        target_path="hook.py",
        tier=EnforcementTier.AGY_HOOK,
        retention_class=RetentionClass.SAFETY_INVARIANT,
        provenance=ProvenanceRef(transcript_step="t.jsonl#L9"),
        proposed_diff=(
            "--- a/hook.py\n"
            "+++ b/hook.py\n"
            "@@ -1,2 +1,2 @@\n"
            " # common header context line >= 16 chars\n"
            "-old_behavior_call_here()\n"
            "+new_safe_helper_call_here()\n"
        ),
        state_dir=state_dir,
    )
    assert unapplied_diff.proposal is not None
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
    assert unapplied_diff.proposal.proposal_id not in report.already_applied_ids

    # render-proposal produces thin <= 8 KB active-only file pruning already_applied & one-time-note
    rendered = render_active_proposal_md(
        out_path=out_md, state_dir=state_dir, repo_root=repo_dir
    )
    assert rendered.rc == 0
    assert rendered.rendered_bytes is not None
    assert rendered.rendered_bytes <= PROPOSAL_MD_MAX_BYTES
    md_text = out_md.read_text(encoding="utf-8")
    assert "Lock chmod 0644" not in md_text
    assert report.already_applied_ids[0] not in md_text
    assert "One-time note" not in md_text
    assert any(r.code is LearnReturnCode.RET_AUTO_PRUNED for r in rendered.returns)

    # Tiny and invalid max_bytes never crash or violate byte cap
    bad_cap = render_active_proposal_md(
        out_path=out_md, state_dir=state_dir, repo_root=repo_dir, max_bytes=0
    )
    assert bad_cap.rc == 1
    assert bad_cap.errors[0].code is LearnErrorCode.ERR_INVALID_INPUT

    tiny_md = tmp_path / "tiny.md"
    tiny_cap = render_active_proposal_md(
        out_path=tiny_md, state_dir=state_dir, repo_root=repo_dir, max_bytes=16
    )
    assert tiny_cap.rc == 0
    assert tiny_cap.rendered_bytes is not None and tiny_cap.rendered_bytes <= 16

    # Also test CLI round-trip (must NOT create agy-learn.log.jsonl or .lock on read-only heal)
    (state_dir / "agy-learn.log.jsonl").unlink(missing_ok=True)
    for lockf in state_dir.glob("*.lock"):
        lockf.unlink()
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
    assert not (state_dir / "agy-learn.log.jsonl").exists()
    assert not list(state_dir.glob("*.lock"))

    rows, _ = read_proposals(state_dir)
    assert len(rows) == 28


def test_declined_resurrection_guard_and_cli_validation_errors(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "agy-learn"
    first = propose_item(
        dedup_key="guard:dup",
        title="Guard dup",
        summary="Prevent duplicate declined resurrection",
        target_path="AGENTS.md",
        tier=EnforcementTier.RULE,
        retention_class=RetentionClass.SAFETY_INVARIANT,
        provenance=ProvenanceRef(transcript_step="t.jsonl#L1"),
        state_dir=state_dir,
    )
    assert first.proposal is not None
    # Escalate once to occurrences=2, then decline
    escalated = propose_item(
        dedup_key="guard:dup",
        title="Guard dup",
        summary="Prevent duplicate declined resurrection",
        target_path="AGENTS.md",
        tier=EnforcementTier.RULE,
        retention_class=RetentionClass.SAFETY_INVARIANT,
        provenance=ProvenanceRef(transcript_step="t.jsonl#L2"),
        state_dir=state_dir,
    )
    assert escalated.proposal is not None and escalated.proposal.occurrences == 2
    mark_proposal(
        proposal_id=first.proposal.proposal_id,
        status=ProposalStatus.DECLINED,
        state_dir=state_dir,
    )
    # Re-proposing with EITHER historical provenance (even with occurrences==2) stays blocked!
    replay_old = propose_item(
        dedup_key="guard:dup",
        title="Guard dup",
        summary="Prevent duplicate declined resurrection",
        target_path="AGENTS.md",
        tier=EnforcementTier.RULE,
        retention_class=RetentionClass.SAFETY_INVARIANT,
        provenance=ProvenanceRef(transcript_step="t.jsonl#L1"),
        state_dir=state_dir,
    )
    assert replay_old.rc == 1
    assert replay_old.errors[0].code is LearnErrorCode.ERR_DECLINED_ACTIVE

    # Empty plan_key / bad args return structured CLI rc=1/2 rather than raw Pydantic traceback
    bad_rec = record_review(
        plan_key="",
        plan_sha256="abc",
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=0,
        survivor_count=0,
        provenance=ProvenanceRef(transcript_step="t.jsonl#L1"),
        state_dir=state_dir,
    )
    assert bad_rec.rc == 1
    assert bad_rec.errors[0].code is LearnErrorCode.ERR_INVALID_INPUT
    assert agy_learn_cli_main(["", "--bogus-flag"]) == 2


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


def test_pwf_guard_errors_encountered_cross_check_and_plan_key_matching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xdg = tmp_path / "xdg-err-check"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    pwf_guard = _load_hook("agy-pwf-guard.py")

    main_repo = tmp_path / "repo"
    (main_repo / ".git").mkdir(parents=True)
    wt_dir = tmp_path / "repo-errcheck"
    pdir = wt_dir / ".planning" / "2026-11-23-errcheck"
    pdir.mkdir(parents=True)
    plan_file = pdir / "task_plan.md"
    plan_file.write_text(
        "# Task Plan: #17 ErrCheck\n\n"
        "### Phase 1: Do\n**Status:** complete\n\n"
        "## Errors Encountered\n"
        "| Error | Resolution |\n"
        "|-------|------------|\n"
        "| None yet | - |\n"
        "|   |   |\n"
        "| None | N/A |\n"
        "| First failure | Fixed first |\n"
        "| Second failure | Fixed second |\n\n"
        "## Notes\n"
        "| Col | Val |\n|---|---|\n| Not an error | ok |\n",
        encoding="utf-8",
    )
    assert pwf_guard._count_plan_errors(plan_file.read_text(encoding="utf-8")) == 2
    digest = hashlib.sha256(plan_file.read_bytes()).hexdigest()

    # Bare-slug review entry never clears worktree:rm
    reviews_path = xdg / "agy-learn" / "reviews.jsonl"
    reviews_path.parent.mkdir(parents=True)
    reviews_path.write_text(
        json.dumps(
            {
                "plan_key": "2026-11-23-errcheck",
                "plan_sha256": digest,
                "error_count": 2,
                "outcome": "no_new_proposals",
                "timestamp_unix_ns": 100,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    bare_denied = pwf_guard._run_hook(
        {
            "cwd": str(main_repo),
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "mise run worktree:rm errcheck"},
            },
        }
    )
    assert bare_denied["decision"] == "deny"

    # Undercounted error_count=1 against 2 logged error rows denies worktree:rm
    undercount = record_review(
        plan_key=str(plan_file.resolve()),
        plan_sha256=digest,
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=1,
        survivor_count=0,
        provenance=ProvenanceRef(
            transcript_step="t.jsonl#L20",
            plan_error_ref="Errors Encountered row 1",
        ),
        notes="Row 1 was a one-off typo that was already covered by ruff checks.",
    )
    assert undercount.rc == 0
    deny_undercount = pwf_guard._run_hook(
        {
            "cwd": str(main_repo),
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "mise run worktree:rm errcheck"},
            },
        }
    )
    assert deny_undercount["decision"] == "deny"
    assert "error_count=1" in deny_undercount["reason"]

    # Matching error_count=2 with repo-relative .planning/<PLAN_ID>/task_plan.md clears worktree:rm
    matched = record_review(
        plan_key=".planning/2026-11-23-errcheck/task_plan.md",
        plan_sha256=digest,
        outcome=ReviewVerdict.NO_NEW_PROPOSALS,
        error_count=2,
        survivor_count=0,
        provenance=ProvenanceRef(
            transcript_step="t.jsonl#L21",
            plan_error_ref="Errors Encountered rows 1-2",
        ),
        notes="Both logged errors are already enforced by existing hk.pkl and pytest checks.",
    )
    assert matched.rc == 0
    allow_matched = pwf_guard._run_hook(
        {
            "cwd": str(main_repo),
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "mise run worktree:rm errcheck"},
            },
        }
    )
    assert allow_matched == {"decision": "allow"}


def test_record_review_task_script_enforces_canonical_key_and_anti_rubber_stamp(
    tmp_path: Path,
) -> None:
    task_script = (
        REPO_ROOT
        / "unix"
        / "home"
        / ".config"
        / "mise"
        / "tasks"
        / "learn"
        / "record-review"
    )
    state_dir = tmp_path / "xdg" / "agy-learn"
    plan_file = tmp_path / "wt" / ".planning" / "2026-11-23-task" / "task_plan.md"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text("# Task Plan: #17 Task\n", encoding="utf-8")

    base_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("usage_") and not k.startswith("GIT_")
    }

    # Bare slug without --plan-path fails closed with structured ERR_INVALID_INPUT JSON
    bare_proc = subprocess.run(
        ["bash", str(task_script)],
        env={
            **base_env,
            "usage_plan_key": "2026-11-23-task",
            "usage_plan_sha256": "deadbeef",
            "usage_outcome": "no_new_proposals",
            "usage_error_count": "0",
            "usage_survivor_count": "0",
            "usage_transcript_step": "t.jsonl#L1",
            "usage_state_dir": str(state_dir),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert bare_proc.returncode == 1
    bare_json = orjson.loads(bare_proc.stdout)
    assert bare_json["errors"][0]["code"] == "ERR_INVALID_INPUT"

    # --plan-path with matching bare slug + error_count=1 + substantive notes + plan_error_ref succeeds
    ok_proc = subprocess.run(
        ["bash", str(task_script)],
        env={
            **base_env,
            "usage_plan_key": "2026-11-23-task",
            "usage_plan_sha256": "deadbeef",
            "usage_outcome": "no_new_proposals",
            "usage_error_count": "1",
            "usage_survivor_count": "0",
            "usage_transcript_step": "t.jsonl#L2",
            "usage_plan_error_ref": "Errors Encountered row 1: ruff formatting",
            "usage_plan_path": str(plan_file),
            "usage_notes": "Row 1 already enforced by existing hk.pkl ruff gate; zero new rules.",
            "usage_state_dir": str(state_dir),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    ok_json = orjson.loads(ok_proc.stdout)
    assert ok_json["rc"] == 0
    assert ok_json["review"]["plan_key"] == str(plan_file.resolve())


def test_propose_preserves_approved_status_stricter_tier_and_migrate_list_apply(
    tmp_path: Path,
) -> None:
    from agy_learn.ledger import (
        list_proposals,
        mark_proposal,
        migrate_reviews,
    )

    state_dir = tmp_path / "state"
    repo_dir = tmp_path / "repo"
    plan_file = repo_dir / ".planning" / "2026-11-24-slice" / "task_plan.md"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text(
        "# Slice\n\n### Phase 1: Do\n**Status:** complete\n", encoding="utf-8"
    )

    # 1. Record proposal, approve it with explicit notes, then recur -> preserves APPROVED, notes, and stricter tier
    p1 = propose_item(
        dedup_key="recur-tier-check",
        title="Enforce hk check",
        summary="Run deterministic hk gate",
        target_path="hk.pkl",
        tier=EnforcementTier.SKILL,
        retention_class=RetentionClass.SAFETY_INVARIANT,
        provenance=ProvenanceRef(transcript_step="t1.jsonl#L1"),
        state_dir=state_dir,
    )
    assert p1.rc == 0
    assert p1.proposal is not None
    pid = p1.proposal.proposal_id
    marked = mark_proposal(
        proposal_id=pid,
        status=ProposalStatus.APPROVED,
        notes="Owner approved for hk.pkl",
        state_dir=state_dir,
    )
    assert marked.rc == 0

    recurred = propose_item(
        dedup_key="recur-tier-check",
        title="Enforce hk check",
        summary="Run deterministic hk gate",
        target_path="hk.pkl",
        tier=EnforcementTier.RULE,
        retention_class=RetentionClass.SAFETY_INVARIANT,
        provenance=ProvenanceRef(transcript_step="t2.jsonl#L5"),
        state_dir=state_dir,
    )
    assert recurred.rc == 0
    assert recurred.proposal is not None
    assert recurred.proposal.status is ProposalStatus.APPROVED
    assert recurred.proposal.notes == "Owner approved for hk.pkl"
    assert recurred.proposal.recommended_tier is EnforcementTier.HK_PKL

    # 2. Check logging JSONL captures INFO events even in --json mode (WARNING console)
    rc_list = agy_learn_cli_main(
        ["list", "--actionable-only", "--state-dir", str(state_dir)]
    )
    assert rc_list == 0
    listed = list_proposals(actionable_only=True, state_dir=state_dir)
    assert [p.proposal_id for p in listed.proposals] == [pid]

    # 3. Apply approved proposals via CLI (also tests JSONL INFO sink decoupled from console WARNING) and refresh proposal markdown
    out_md = tmp_path / "learning_proposal.md"
    rc_apply = agy_learn_cli_main(
        [
            "apply",
            "--all-approved",
            "--proposal-md",
            str(out_md),
            "--repo",
            str(repo_dir),
            "--state-dir",
            str(state_dir),
        ]
    )
    assert rc_apply == 0
    assert list_proposals(actionable_only=True, state_dir=state_dir).proposals == []
    assert out_md.is_file()
    log_file = state_dir / "agy-learn.log.jsonl"
    assert log_file.is_file()
    assert "learn_proposals_applied" in log_file.read_text(encoding="utf-8")

    # 4. migrate_reviews dry-run + --apply compacts superseded bare-slug rows cleanly
    reviews_path = state_dir / "reviews.jsonl"
    canon_key = str(plan_file.resolve())
    with reviews_path.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "review_id": "rev-bare-superseded",
                    "plan_key": "2026-11-24-slice",
                    "plan_sha256": "abc123",
                    "error_count": 0,
                    "outcome": "no_new_proposals",
                    "survivor_count": 0,
                    "proposal_ids": [],
                    "provenance": {"transcript_step": "t1.jsonl#L2"},
                    "timestamp": "20261124T010000.000000001+0000",
                    "timestamp_unix_ns": 100,
                }
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "review_id": "rev-canon",
                    "plan_key": canon_key,
                    "plan_id": "2026-11-24-slice",
                    "plan_sha256": "abc123",
                    "error_count": 0,
                    "outcome": "no_new_proposals",
                    "survivor_count": 0,
                    "proposal_ids": [],
                    "provenance": {
                        "transcript_step": "t1.jsonl#L3",
                        "plan_path": canon_key,
                    },
                    "timestamp": "20261124T010001.000000001+0000",
                    "timestamp_unix_ns": 200,
                }
            )
            + "\n"
        )
    dry = migrate_reviews(apply=False, state_dir=state_dir)
    assert dry.rc == 0
    assert dry.migrated_ids == ["rev-bare-superseded"]
    assert len(reviews_path.read_text(encoding="utf-8").splitlines()) == 2

    compacted = migrate_reviews(apply=True, state_dir=state_dir)
    assert compacted.rc == 0
    assert compacted.migrated_ids == ["rev-bare-superseded"]
    kept_lines = reviews_path.read_text(encoding="utf-8").splitlines()
    assert len(kept_lines) == 1
    assert "rev-canon" in kept_lines[0]

    # 5. Portable learn hook & agy-pwf-sync pass /usr/bin/python3 --self-check
    py39 = Path("/usr/bin/python3")
    if py39.is_file():
        for script in (
            HOOKS_DIR / "agy-pwf-sync.py",
            REPO_ROOT
            / "unix"
            / "home"
            / ".config"
            / "mise"
            / "assets"
            / "hooks"
            / "portable-learn-hook.py",
        ):
            proc = subprocess.run(
                [str(py39), "-I", "-B", str(script), "--self-check"],
                capture_output=True,
                text=True,
                check=True,
            )
            assert json.loads(proc.stdout)["status"] == "ok"


def test_learn_dedup_apply_guards_mini_toml_and_portable_hook_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. migrate_reviews deduplicates canonical vs resolved bare keeping highest (ts_ns, error_count) & checks repo_root
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    for rp in (repo_a, repo_b):
        p = rp / ".planning" / "2026-11-24-s"
        p.mkdir(parents=True)
        (p / "task_plan.md").write_text("# Plan\n", encoding="utf-8")
    plan_a = str((repo_a / ".planning" / "2026-11-24-s" / "task_plan.md").resolve())
    rev_path = state_dir / "reviews.jsonl"
    rows = [
        {
            "review_id": "rev-canon-old",
            "plan_key": plan_a,
            "plan_id": "2026-11-24-s",
            "repo_root": str(repo_a),
            "plan_sha256": "deadbeef",
            "error_count": 0,
            "outcome": "no_new_proposals",
            "survivor_count": 0,
            "proposal_ids": [],
            "provenance": {"plan_path": plan_a},
            "timestamp": "20261124T010000.000000001+0000",
            "timestamp_unix_ns": 100,
        },
        {
            "review_id": "rev-bare-newer",
            "plan_key": "2026-11-24-s",
            "plan_id": "2026-11-24-s",
            "repo_root": str(repo_a),
            "plan_sha256": "deadbeef",
            "error_count": 2,
            "outcome": "no_new_proposals",
            "survivor_count": 0,
            "proposal_ids": [],
            "provenance": {"transcript_step": "t#1"},
            "timestamp": "20261124T010002.000000001+0000",
            "timestamp_unix_ns": 300,
        },
        {
            "review_id": "rev-other-repo",
            "plan_key": "2026-11-24-s",
            "plan_id": "2026-11-24-s",
            "repo_root": str(repo_b),
            "plan_sha256": "deadbeef",
            "error_count": 0,
            "outcome": "no_new_proposals",
            "survivor_count": 0,
            "proposal_ids": [],
            "provenance": {"transcript_step": "t#2"},
            "timestamp": "20261124T010000.000000001+0000",
            "timestamp_unix_ns": 50,
        },
    ]
    rev_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    mig = migrate_reviews(apply=True, state_dir=state_dir)
    assert mig.rc == 0
    kept = [
        json.loads(ln)
        for ln in rev_path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    by_key = {r["plan_key"]: r for r in kept}
    assert by_key[plan_a]["review_id"] == "rev-bare-newer"
    assert by_key[plan_a]["error_count"] == 2
    assert len(kept) == 2

    # 2. apply_proposals fails closed on --detect-shipped without --repo and refuses shipped -> applied downgrade
    fail_ds = apply_proposals(detect_shipped=True, state_dir=state_dir)
    assert fail_ds.rc == 1
    assert fail_ds.errors[0].code is LearnErrorCode.ERR_INVALID_INPUT

    p_resp = propose_item(
        dedup_key="rule-a-shipped-guard",
        title="Rule A",
        summary="summary",
        target_path="hk.pkl",
        tier=EnforcementTier.HK_PKL,
        retention_class=RetentionClass.SAFETY_INVARIANT,
        provenance=ProvenanceRef(transcript_step="t#1"),
        state_dir=state_dir,
    )
    assert p_resp.proposal is not None
    pid = p_resp.proposal.proposal_id
    mark_proposal(
        proposal_id=pid,
        status=ProposalStatus.SHIPPED,
        state_dir=state_dir,
    )
    no_down = apply_proposals(
        proposal_ids=[pid], status=ProposalStatus.APPLIED, state_dir=state_dir
    )
    assert no_down.rc == 0
    assert no_down.applied_ids == []
    assert read_proposals(state_dir=state_dir)[0][-1].status is ProposalStatus.SHIPPED

    # 3. _MiniTomlParser handles multiline """...""" with `#`, hex ints, and ISO Z datetimes
    spec = importlib.util.spec_from_file_location(
        "agy_pwf_sync", HOOKS_DIR / "agy-pwf-sync.py"
    )
    assert spec is not None and spec.loader is not None
    sync_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync_mod)
    parsed_toml = sync_mod._MiniTomlParser.loads(
        'text = """line1\n# literal hash not comment\nline3"""\n'
        "hex_num = 0xdead_beef\n"
        "ts = 2026-11-24T09:00:00Z\n"
    )
    assert parsed_toml["text"] == "line1\n# literal hash not comment\nline3"
    assert parsed_toml["hex_num"] == 0xDEADBEEF
    assert parsed_toml["ts"].year == 2026

    # 4. portable-learn-hook honors PWF_PLAN_ROOT, ignores fenced phases, and fixes Stop guidance
    phook_path = (
        REPO_ROOT
        / "unix"
        / "home"
        / ".config"
        / "mise"
        / "assets"
        / "hooks"
        / "portable-learn-hook.py"
    )
    pspec = importlib.util.spec_from_file_location("portable_learn_hook", phook_path)
    assert pspec is not None and pspec.loader is not None
    phook = importlib.util.module_from_spec(pspec)
    pspec.loader.exec_module(phook)

    pwf_root = tmp_path / "ext_pwf"
    ext_plan = pwf_root / ".planning" / "ext-slug" / "task_plan.md"
    ext_plan.parent.mkdir(parents=True)
    ext_plan.write_text(
        "```markdown\n### Phase 1\n- **Status:** pending\n```\n"
        "### Phase 1\n- [complete]\n"
        "## Errors Encountered\n| Error | Resolution |\n|-------|------------|\n| boom | fixed |\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("PLAN_ID", raising=False)
    monkeypatch.setenv("PWF_PLAN_ROOT", str(pwf_root))
    monkeypatch.setenv("AGY_LEARN_STATE_DIR", str(tmp_path / "p_state"))
    (tmp_path / "p_state").mkdir()
    sub_dir = repo_a / "sub" / "nested"
    sub_dir.mkdir(parents=True)
    res_block = phook._handle_stop({"cwd": str(sub_dir), "session_id": "s1"}, "codex")
    assert res_block.get("decision") == "block"
    reason = res_block["reason"]
    assert "--error-count 1" in reason
    assert "--plan-error-ref" in reason
    assert "or `mise run learn:propose`" not in reason

    # 5. tasks/learn/migrate refuses uv run in dry-run without --apply
    mig_task = (
        REPO_ROOT / "unix" / "home" / ".config" / "mise" / "tasks" / "learn" / "migrate"
    ).read_text(encoding="utf-8")
    assert "usage_apply:-false" in mig_task
    assert "refusing uv run fallback without --apply" in mig_task
