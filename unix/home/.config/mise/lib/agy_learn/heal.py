"""Read-only self-healing inspection and thin <= 8 KB proposal rendering for `agy_learn`."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from agy_learn.ledger import (
    PROPOSAL_MD_MAX_BYTES,
    TIER_RANK,
    is_bare_plan_key,
    latest_proposals_map,
    latest_reviews_by_plan,
    read_proposals,
    read_reviews,
    recommend_tier_for_occurrences,
    resolve_state_dir,
    write_text_atomic,
)
from dotfiles_py.generated import (
    HealFinding,
    HealFindingKind,
    HealReport,
    HealSeverity,
    LearnErrorCode,
    LearnErrorEntry,
    LearnProposal,
    LearnResponse,
    LearnReturnCode,
    LearnReturnEntry,
    ProposalStatus,
    RetentionClass,
    ReviewVerdict,
)
from dotfiles_py.logging import get_logger
from dotfiles_py.time_ns import now_ns_pair

_LOG = get_logger("agy_learn.heal")


def _find_git_root(repo_root: Path | None) -> Path | None:
    if repo_root is None:
        return None
    cur = repo_root.expanduser().resolve(strict=False)
    for cand in (cur, *cur.parents):
        if (cand / ".git").exists():
            return cand
    return None


def _resolve_target_file(target_path: str, repo_root: Path | None) -> Path | None:
    raw = Path(os.path.expanduser(target_path))
    if raw.is_absolute():
        return raw if raw.is_file() else None
    if repo_root is not None:
        cand = repo_root / raw
        if cand.is_file():
            return cand
        git_root = _find_git_root(repo_root)
        if git_root is not None:
            git_cand = git_root / raw
            if git_cand.is_file():
                return git_cand
    return None


def _recent_git_log(repo_root: Path | None) -> str:
    git_root = _find_git_root(repo_root)
    if git_root is None:
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(git_root), "log", "-n", "30", "--format=%h %s"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return proc.stdout if proc.returncode == 0 else ""
    except OSError, subprocess.SubprocessError:
        return ""


def is_proposal_already_applied(
    proposal: LearnProposal,
    repo_root: Path | None = None,
    *,
    git_log_cache: str | None = None,
) -> str | None:
    """Read-only check detecting whether a pending proposal is already shipped on disk or in git."""
    log_text = _recent_git_log(repo_root) if git_log_cache is None else git_log_cache
    if log_text and (
        proposal.proposal_id in log_text or proposal.dedup_key in log_text
    ):
        return f"Detected {proposal.proposal_id} / {proposal.dedup_key} in recent git history"

    target = _resolve_target_file(proposal.target_path, repo_root)
    if target is None:
        return None
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    if proposal.proposed_diff:
        diff_lines = proposal.proposed_diff.splitlines()
        is_unified = any(
            line.startswith(("--- ", "+++ ", "@@ "))
            or (
                (line.startswith("+") and not line.startswith("+++"))
                or (line.startswith("-") and not line.startswith("---"))
            )
            for line in diff_lines
        )
        if is_unified:
            added = [
                line[1:].strip()
                for line in diff_lines
                if line.startswith("+")
                and not line.startswith("+++")
                and len(line[1:].strip()) >= 12
            ]
            removed = [
                line[1:].strip()
                for line in diff_lines
                if line.startswith("-")
                and not line.startswith("---")
                and len(line[1:].strip()) >= 12
            ]
            if (
                (added or removed)
                and all(line in content for line in added[:5])
                and all(line not in content for line in removed[:5])
            ):
                return f"Unified diff already applied in {proposal.target_path}"
        else:
            meaningful = [
                line.strip()
                for line in diff_lines
                if line.strip()
                and not line.startswith("```")
                and len(line.strip()) >= 16
            ]
            if meaningful and all(line in content for line in meaningful[:3]):
                return f"All proposed lines already present in {proposal.target_path}"
    return None


def _discover_proposal_md(
    state_dir: Path,
    proposal_md_path: Path | None,
) -> Path | None:
    if proposal_md_path is not None:
        return proposal_md_path.expanduser()
    env_art = os.environ.get("AGY_ARTIFACT_DIR", "").strip()
    if env_art:
        cand = Path(env_art).expanduser() / "learning_proposal.md"
        if cand.is_file():
            return cand
    state_cand = state_dir / "learning_proposal.md"
    return state_cand if state_cand.is_file() else None


def run_heal_check(
    *,
    state_dir: Path | None = None,
    repo_root: Path | None = None,
    proposal_md_path: Path | None = None,
) -> HealReport:
    """Run strictly read-only self-healing checks across ledger reviews, proposals, and budgets."""
    ts, ns = now_ns_pair()
    resolved_state = resolve_state_dir(state_dir)
    reviews, rev_errs = read_reviews(resolved_state)
    proposals, prop_errs = read_proposals(resolved_state)

    errors: list[LearnErrorEntry] = [*rev_errs, *prop_errs]
    returns: list[LearnReturnEntry] = []
    findings: list[HealFinding] = []

    latest_reviews = latest_reviews_by_plan(reviews)
    canonical_keys = {key for key in latest_reviews if not is_bare_plan_key(key)}
    owing_plans: list[str] = []
    for plan_key, rec in sorted(latest_reviews.items()):
        if is_bare_plan_key(plan_key):
            plan_path_hint = (rec.provenance.plan_path or "").strip()
            has_canonical = bool(plan_path_hint) and (
                plan_path_hint in canonical_keys
                or str(Path(os.path.expanduser(plan_path_hint)).resolve(strict=False))
                in canonical_keys
            )
            remediation = (
                f"canonical review already exists at {plan_path_hint}; prune/migrate legacy bare-slug row from reviews.jsonl"
                if has_canonical
                else (
                    f"re-record using canonical path ({plan_path_hint})"
                    if plan_path_hint
                    else "re-record using canonical task_plan.md path"
                )
            )
            findings.append(
                HealFinding(
                    kind=HealFindingKind.HOOK_DRIFT,
                    severity=HealSeverity.WARN,
                    subject=rec.review_id,
                    plan_key=plan_key,
                    message=(
                        f"Bare non-path plan_key {plan_key!r} in reviews.jsonl "
                        f"({rec.review_id}): {remediation}."
                    ),
                )
            )
        if rec.outcome is ReviewVerdict.OWING_LEARN:
            owing_plans.append(plan_key)
            findings.append(
                HealFinding(
                    kind=HealFindingKind.OWING_LEARN,
                    severity=HealSeverity.ACTION_REQUIRED,
                    subject=rec.plan_id or plan_key,
                    plan_key=plan_key,
                    message=(
                        f"Completed plan {rec.plan_id or plan_key} has unreviewed errors "
                        f"(error_count={rec.error_count}); run `/learn` and record review."
                    ),
                )
            )
            errors.append(
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_OWING_LEARN,
                    message=f"Plan {plan_key} is owing_learn",
                )
            )

    latest_props = latest_proposals_map(proposals)
    git_log_text = _recent_git_log(repo_root)
    active_proposals: list[LearnProposal] = []
    auto_escalated_ids: list[str] = []
    already_applied_ids: list[str] = []
    expired_declined_ids: list[str] = []

    for prop in sorted(latest_props.values(), key=lambda p: p.timestamp_unix_ns):
        if prop.status is ProposalStatus.DECLINED:
            if (
                prop.declined_until_unix_ns is not None
                and ns >= prop.declined_until_unix_ns
            ):
                expired_declined_ids.append(prop.proposal_id)
                findings.append(
                    HealFinding(
                        kind=HealFindingKind.DECLINED_EXPIRED,
                        severity=HealSeverity.INFO,
                        subject=prop.proposal_id,
                        proposal_id=prop.proposal_id,
                        message=f"30-day declined buffer expired for {prop.proposal_id} ({prop.title}).",
                    )
                )
            continue

        if prop.status in {
            ProposalStatus.APPLIED,
            ProposalStatus.SHIPPED,
            ProposalStatus.SUPERSEDED,
        }:
            continue

        active_proposals.append(prop)
        rec_tier, needs_esc = recommend_tier_for_occurrences(
            prop.recommended_tier, prop.target_path, prop.occurrences
        )
        if needs_esc or (
            prop.occurrences >= 2
            and TIER_RANK[prop.recommended_tier] < TIER_RANK[prop.tier]
        ):
            auto_escalated_ids.append(prop.proposal_id)
            findings.append(
                HealFinding(
                    kind=HealFindingKind.AUTO_ESCALATE,
                    severity=HealSeverity.WARN,
                    subject=prop.proposal_id,
                    proposal_id=prop.proposal_id,
                    recommended_tier=rec_tier,
                    message=(
                        f"Proposal {prop.proposal_id} recurred {prop.occurrences}x; "
                        f"escalate from {prop.tier.value} to {rec_tier.value}."
                    ),
                )
            )

        applied_reason = is_proposal_already_applied(
            prop, repo_root, git_log_cache=git_log_text
        )
        if applied_reason:
            already_applied_ids.append(prop.proposal_id)
            findings.append(
                HealFinding(
                    kind=HealFindingKind.ALREADY_APPLIED,
                    severity=HealSeverity.INFO,
                    subject=prop.proposal_id,
                    proposal_id=prop.proposal_id,
                    message=(
                        f"Proposal {prop.proposal_id} appears already applied ({applied_reason}); "
                        "mark as shipped and prune active learning_proposal.md."
                    ),
                )
            )
        else:
            findings.append(
                HealFinding(
                    kind=HealFindingKind.ACTIVE_PROPOSAL,
                    severity=HealSeverity.INFO,
                    subject=prop.proposal_id,
                    proposal_id=prop.proposal_id,
                    recommended_tier=prop.recommended_tier,
                    message=f"Active proposal {prop.proposal_id}: {prop.title} ({prop.recommended_tier.value}).",
                )
            )

    md_target = _discover_proposal_md(resolved_state, proposal_md_path)
    md_bytes = 0
    if md_target is not None and md_target.is_file():
        try:
            md_bytes = len(md_target.read_bytes())
        except OSError as exc:
            errors.append(
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_IO_FAILURE,
                    message=f"Failed reading {md_target}: {exc}",
                )
            )
    within_budget = md_bytes <= PROPOSAL_MD_MAX_BYTES
    if not within_budget:
        errors.append(
            LearnErrorEntry(
                code=LearnErrorCode.ERR_PROPOSAL_BUDGET_EXCEEDED,
                message=(
                    f"{md_target} is {md_bytes} bytes (> {PROPOSAL_MD_MAX_BYTES} B cap); "
                    "prune via `agy-learn render-proposal`."
                ),
            )
        )
        findings.append(
            HealFinding(
                kind=HealFindingKind.PROPOSAL_BLOAT,
                severity=HealSeverity.ACTION_REQUIRED,
                subject=str(md_target),
                message=f"learning_proposal.md is {md_bytes} B (cap {PROPOSAL_MD_MAX_BYTES} B).",
            )
        )

    healthy = not errors and not owing_plans and within_budget
    ret_code = (
        LearnReturnCode.RET_HEAL_CLEAN
        if healthy and not findings
        else LearnReturnCode.RET_HEAL_ACTION_REQUIRED
    )
    returns.append(
        LearnReturnEntry(
            code=ret_code,
            message=(
                f"healthy={healthy} reviews={len(latest_reviews)} "
                f"active_proposals={len(active_proposals)} owing={len(owing_plans)}"
            ),
        )
    )
    report = HealReport(
        healthy=healthy,
        read_only=True,
        state_dir=str(resolved_state),
        reviews_count=len(latest_reviews),
        proposals_count=len(latest_props),
        active_proposals=active_proposals,
        owing_plans=owing_plans,
        auto_escalated_ids=auto_escalated_ids,
        already_applied_ids=already_applied_ids,
        expired_declined_ids=expired_declined_ids,
        findings=findings,
        proposal_md_bytes=md_bytes,
        proposal_md_within_budget=within_budget,
        timestamp=ts,
        timestamp_unix_ns=ns,
        errors=errors,
        returns=returns,
    )
    _LOG.info(
        "learn_heal_checked",
        healthy=healthy,
        owing_count=len(owing_plans),
        active_count=len(active_proposals),
    )
    return report


def render_active_proposal_md(
    *,
    out_path: Path,
    state_dir: Path | None = None,
    repo_root: Path | None = None,
    max_bytes: int = PROPOSAL_MD_MAX_BYTES,
) -> LearnResponse:
    """Render a thin `<= 8 KB` active-only `learning_proposal.md` pruning resolved/shipped items."""
    ts, ns = now_ns_pair()
    if max_bytes < 1:
        return LearnResponse(
            command="render-proposal",
            rc=1,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_INVALID_INPUT,
                    message=f"max_bytes must be >= 1, got {max_bytes}",
                )
            ],
            returns=[],
        )
    resolved_state = resolve_state_dir(state_dir)
    proposals, read_errs = read_proposals(resolved_state)
    if read_errs:
        return LearnResponse(
            command="render-proposal",
            rc=1,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=read_errs,
            returns=[],
        )

    latest = latest_proposals_map(proposals)
    git_log_text = _recent_git_log(repo_root)
    active: list[LearnProposal] = []
    pruned_count = 0

    for prop in sorted(
        latest.values(),
        key=lambda p: (TIER_RANK[p.recommended_tier], -p.occurrences, p.proposal_id),
    ):
        if prop.status not in {ProposalStatus.PENDING, ProposalStatus.APPROVED}:
            pruned_count += 1
            continue
        if prop.retention_class is RetentionClass.ONE_TIME_NOTE:
            pruned_count += 1
            continue
        if is_proposal_already_applied(prop, repo_root, git_log_cache=git_log_text):
            pruned_count += 1
            continue
        active.append(prop)

    header_lines = [
        "# Active `/learn` Proposals",
        "",
        f"Generated `{ts}` (`active={len(active)}`, `pruned={pruned_count}`, `cap={max_bytes} B`).",
        "",
    ]

    if not active:
        body = "\n".join(
            [*header_lines, "No active proposals pending owner review.", ""]
        )
    else:
        sections: list[str] = [
            *header_lines,
            "| ID | Tier | Class | Occurrences | Target | Summary |",
            "|---|---|---|---:|---|---|",
        ]
        for prop in active:
            tier_lbl = (
                f"{prop.tier.value} -> {prop.recommended_tier.value}"
                if prop.recommended_tier != prop.tier
                else prop.recommended_tier.value
            )
            summary_short = prop.summary.replace("\n", " ").strip()[:120]
            sections.append(
                f"| `{prop.proposal_id}` | `{tier_lbl}` | `{prop.retention_class.value}` "
                f"| {prop.occurrences} | `{prop.target_path}` | {summary_short} |"
            )
        sections.append("")

        for prop in active:
            prov = (
                prop.provenance.plan_error_ref
                or prop.provenance.transcript_step
                or "verified"
            )
            block_lines = [
                f"## {prop.proposal_id}: {prop.title}",
                "",
                f"- **Tier:** `{prop.recommended_tier.value}` (occurrences={prop.occurrences}, class=`{prop.retention_class.value}`)",
                f"- **Target:** `{prop.target_path}`",
                f"- **Provenance:** `{prov}`",
                f"- **Summary:** {prop.summary.strip()}",
                "",
            ]
            if prop.proposed_diff:
                diff_trimmed = prop.proposed_diff.strip()[:600]
                block_lines.extend(["```text", diff_trimmed, "```", ""])
            candidate = "\n".join([*sections, *block_lines])
            if len(candidate.encode("utf-8")) <= max_bytes:
                sections.extend(block_lines)
        body = "\n".join(sections)

    encoded = body.encode("utf-8")
    if len(encoded) > max_bytes:
        suffix = f"\n\n_(truncated to {max_bytes} B cap)_\n"
        suffix_bytes = len(suffix.encode("utf-8"))
        if max_bytes <= suffix_bytes:
            body = encoded[:max_bytes].decode("utf-8", errors="ignore")
        else:
            body = (
                encoded[: max_bytes - suffix_bytes]
                .decode("utf-8", errors="ignore")
                .rstrip()
                + suffix
            )
        encoded = body.encode("utf-8")

    try:
        write_text_atomic(out_path.expanduser(), body)
    except OSError as exc:
        return LearnResponse(
            command="render-proposal",
            rc=1,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_IO_FAILURE,
                    message=f"Failed writing {out_path}: {exc}",
                )
            ],
            returns=[],
        )

    returns = [
        LearnReturnEntry(
            code=LearnReturnCode.RET_RENDERED,
            message=f"Rendered {len(active)} active proposal(s) ({len(encoded)} B) to {out_path}",
        )
    ]
    if pruned_count > 0:
        returns.append(
            LearnReturnEntry(
                code=LearnReturnCode.RET_AUTO_PRUNED,
                message=f"Auto-pruned {pruned_count} resolved/shipped/one-time item(s).",
            )
        )
    return LearnResponse(
        command="render-proposal",
        rc=0,
        rendered_path=str(out_path.expanduser()),
        rendered_bytes=len(encoded),
        timestamp=ts,
        timestamp_unix_ns=ns,
        errors=[],
        returns=returns,
    )
