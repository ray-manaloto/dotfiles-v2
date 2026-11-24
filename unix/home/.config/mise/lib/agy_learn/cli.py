"""Cyclopts-backed CLI entrypoint for `agy_learn` self-healing ledger and proposal tasks."""

from __future__ import annotations

import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import cyclopts
import orjson
from pydantic import BaseModel, ValidationError

from agy_learn.heal import render_active_proposal_md, run_heal_check
from agy_learn.ledger import (
    mark_proposal,
    propose_item,
    record_review,
    resolve_state_dir,
)
from dotfiles_py.generated import (
    EnforcementTier,
    LearnErrorCode,
    LearnErrorEntry,
    LearnResponse,
    LearnReturnCode,
    LearnReturnEntry,
    ProposalStatus,
    ProvenanceRef,
    RetentionClass,
    ReviewVerdict,
)
from dotfiles_py.logging import configure_logging
from dotfiles_py.time_ns import now_ns_pair


def _emit_json(model_obj: BaseModel) -> None:
    payload = model_obj.model_dump(mode="json")
    sys.stdout.write(orjson.dumps(payload).decode("utf-8") + "\n")


def _configure_agy_learn_logging(
    state_dir: Path | None,
    *,
    json_mode: bool,
    read_only: bool = False,
) -> None:
    resolved = resolve_state_dir(state_dir)
    level = logging.WARNING if (json_mode or read_only) else logging.INFO
    jsonl_path = None if read_only else resolved / "agy-learn.log.jsonl"
    configure_logging(
        jsonl_path=jsonl_path,
        level=level,
        logger_name="agy_learn",
    )


def _build_app() -> cyclopts.App:
    app = cyclopts.App(
        name="agy-learn",
        help="Schema-first self-optimizing and self-healing /learn ledger CLI.",
        version_flags=(),
    )

    @app.command(name="heal")
    def heal_cmd(
        *,
        check: Annotated[bool, cyclopts.Parameter(name="--check", negative="")] = True,
        json_out: Annotated[
            bool, cyclopts.Parameter(name="--json", negative="")
        ] = False,
        state_dir: Annotated[
            Path | None, cyclopts.Parameter(name="--state-dir")
        ] = None,
        repo: Annotated[Path | None, cyclopts.Parameter(name="--repo")] = None,
        proposal_md: Annotated[
            Path | None, cyclopts.Parameter(name="--proposal-md")
        ] = None,
    ) -> int:
        """Run read-only self-healing checks across agy-learn state and budgets."""
        _ = check
        _configure_agy_learn_logging(state_dir, json_mode=json_out, read_only=True)
        report = run_heal_check(
            state_dir=state_dir,
            repo_root=repo,
            proposal_md_path=proposal_md,
        )
        if json_out:
            _emit_json(report)
        else:
            sys.stdout.write(
                f"agy-learn heal: healthy={report.healthy} reviews={report.reviews_count} "
                f"proposals={report.proposals_count} active={len(report.active_proposals)} "
                f"owing={len(report.owing_plans)} escalations={len(report.auto_escalated_ids)} "
                f"already_applied={len(report.already_applied_ids)} "
                f"md_bytes={report.proposal_md_bytes} ({report.timestamp})\n"
            )
            for finding in report.findings:
                sys.stdout.write(
                    f"  [{finding.severity.value}] {finding.kind.value}: {finding.message}\n"
                )
        return 0 if report.healthy else 1

    @app.command(name="record-review")
    def record_review_cmd(
        *,
        plan_key: Annotated[str, cyclopts.Parameter(name="--plan-key")],
        plan_sha256: Annotated[str, cyclopts.Parameter(name="--plan-sha256")],
        outcome: Annotated[ReviewVerdict, cyclopts.Parameter(name="--outcome")],
        error_count: Annotated[int, cyclopts.Parameter(name="--error-count")] = 0,
        survivor_count: Annotated[int, cyclopts.Parameter(name="--survivor-count")] = 0,
        transcript_step: Annotated[
            str | None, cyclopts.Parameter(name="--transcript-step")
        ] = None,
        plan_error_ref: Annotated[
            str | None, cyclopts.Parameter(name="--plan-error-ref")
        ] = None,
        transcript_path: Annotated[
            str | None, cyclopts.Parameter(name="--transcript-path")
        ] = None,
        plan_path: Annotated[str | None, cyclopts.Parameter(name="--plan-path")] = None,
        plan_id: Annotated[str | None, cyclopts.Parameter(name="--plan-id")] = None,
        worktree_slug: Annotated[
            str | None, cyclopts.Parameter(name="--worktree-slug")
        ] = None,
        proposal_ids: Annotated[
            list[str] | None, cyclopts.Parameter(name="--proposal-id")
        ] = None,
        repo_root: Annotated[str | None, cyclopts.Parameter(name="--repo-root")] = None,
        conversation_id: Annotated[
            str | None, cyclopts.Parameter(name="--conversation-id")
        ] = None,
        notes: Annotated[str | None, cyclopts.Parameter(name="--notes")] = None,
        state_dir: Annotated[
            Path | None, cyclopts.Parameter(name="--state-dir")
        ] = None,
        json_out: Annotated[
            bool, cyclopts.Parameter(name="--json", negative="")
        ] = True,
    ) -> int:
        """Append a validated review record to reviews.jsonl."""
        _configure_agy_learn_logging(state_dir, json_mode=json_out)
        resp = record_review(
            plan_key=plan_key,
            plan_sha256=plan_sha256,
            outcome=outcome,
            error_count=error_count,
            survivor_count=survivor_count,
            provenance=ProvenanceRef(
                transcript_step=transcript_step,
                plan_error_ref=plan_error_ref,
                transcript_path=transcript_path,
                plan_path=plan_path,
            ),
            plan_id=plan_id,
            worktree_slug=worktree_slug,
            proposal_ids=list(proposal_ids or ()),
            repo_root=repo_root,
            conversation_id=conversation_id,
            notes=notes,
            state_dir=state_dir,
        )
        _emit_json(resp)
        return resp.rc

    @app.command(name="propose")
    def propose_cmd(
        *,
        dedup_key: Annotated[str, cyclopts.Parameter(name="--dedup-key")],
        title: Annotated[str, cyclopts.Parameter(name="--title")],
        summary: Annotated[str, cyclopts.Parameter(name="--summary")],
        target_path: Annotated[str, cyclopts.Parameter(name="--target-path")],
        tier: Annotated[EnforcementTier, cyclopts.Parameter(name="--tier")],
        retention_class: Annotated[
            RetentionClass, cyclopts.Parameter(name="--retention-class")
        ],
        transcript_step: Annotated[
            str | None, cyclopts.Parameter(name="--transcript-step")
        ] = None,
        plan_error_ref: Annotated[
            str | None, cyclopts.Parameter(name="--plan-error-ref")
        ] = None,
        transcript_path: Annotated[
            str | None, cyclopts.Parameter(name="--transcript-path")
        ] = None,
        plan_path: Annotated[str | None, cyclopts.Parameter(name="--plan-path")] = None,
        proposed_diff: Annotated[
            str | None, cyclopts.Parameter(name="--proposed-diff")
        ] = None,
        proposal_id: Annotated[
            str | None, cyclopts.Parameter(name="--proposal-id")
        ] = None,
        notes: Annotated[str | None, cyclopts.Parameter(name="--notes")] = None,
        state_dir: Annotated[
            Path | None, cyclopts.Parameter(name="--state-dir")
        ] = None,
        json_out: Annotated[
            bool, cyclopts.Parameter(name="--json", negative="")
        ] = True,
    ) -> int:
        """Create or escalate a proposal with mandatory provenance and 30-day declined buffer."""
        _configure_agy_learn_logging(state_dir, json_mode=json_out)
        resp = propose_item(
            dedup_key=dedup_key,
            title=title,
            summary=summary,
            target_path=target_path,
            tier=tier,
            retention_class=retention_class,
            provenance=ProvenanceRef(
                transcript_step=transcript_step,
                plan_error_ref=plan_error_ref,
                transcript_path=transcript_path,
                plan_path=plan_path,
            ),
            proposed_diff=proposed_diff,
            proposal_id=proposal_id,
            notes=notes,
            state_dir=state_dir,
        )
        _emit_json(resp)
        return resp.rc

    @app.command(name="mark")
    def mark_cmd(
        *,
        proposal_id: Annotated[str, cyclopts.Parameter(name="--proposal-id")],
        status: Annotated[ProposalStatus, cyclopts.Parameter(name="--status")],
        shipped_commit: Annotated[
            str | None, cyclopts.Parameter(name="--shipped-commit")
        ] = None,
        notes: Annotated[str | None, cyclopts.Parameter(name="--notes")] = None,
        state_dir: Annotated[
            Path | None, cyclopts.Parameter(name="--state-dir")
        ] = None,
        json_out: Annotated[
            bool, cyclopts.Parameter(name="--json", negative="")
        ] = True,
    ) -> int:
        """Update proposal status (approved, applied, declined, shipped, superseded)."""
        _configure_agy_learn_logging(state_dir, json_mode=json_out)
        resp = mark_proposal(
            proposal_id=proposal_id,
            status=status,
            shipped_commit=shipped_commit,
            notes=notes,
            state_dir=state_dir,
        )
        _emit_json(resp)
        return resp.rc

    @app.command(name="render-proposal")
    def render_proposal_cmd(
        *,
        out: Annotated[Path, cyclopts.Parameter(name="--out")],
        state_dir: Annotated[
            Path | None, cyclopts.Parameter(name="--state-dir")
        ] = None,
        repo: Annotated[Path | None, cyclopts.Parameter(name="--repo")] = None,
        max_bytes: Annotated[int, cyclopts.Parameter(name="--max-bytes")] = 8192,
        json_out: Annotated[
            bool, cyclopts.Parameter(name="--json", negative="")
        ] = True,
    ) -> int:
        """Render thin <= 8 KB active-only learning_proposal.md."""
        _configure_agy_learn_logging(state_dir, json_mode=json_out)
        resp = render_active_proposal_md(
            out_path=out,
            state_dir=state_dir,
            repo_root=repo,
            max_bytes=max_bytes,
        )
        _emit_json(resp)
        return resp.rc

    return app


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments via `cyclopts` and return deterministic exit code."""
    arg_list = list(sys.argv[1:] if argv is None else argv)
    app = _build_app()
    try:
        result = app(
            arg_list,
            exit_on_error=False,
            print_error=False,
            result_action="return_value",
        )
        return int(result) if isinstance(result, int) else 0
    except (cyclopts.CycloptsError, ValidationError) as err:
        ts, ns = now_ns_pair()
        raw_cmd = arg_list[0].strip() if arg_list else ""
        cmd = raw_cmd or "cli"
        resp = LearnResponse(
            command=cmd,
            rc=2,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                LearnErrorEntry(code=LearnErrorCode.ERR_INVALID_INPUT, message=str(err))
            ],
            returns=[
                LearnReturnEntry(
                    code=LearnReturnCode.RET_SKIPPED, message="invalid cli arguments"
                )
            ],
        )
        sys.stderr.write(
            orjson.dumps(resp.model_dump(mode="json")).decode("utf-8") + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
