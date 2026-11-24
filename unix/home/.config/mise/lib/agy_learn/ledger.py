"""Filelock-guarded JSONL ledger, 5-tier escalation, and 30-day declined buffer for `agy_learn`."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

import orjson
from filelock import FileLock, Timeout
from pydantic import BaseModel, ValidationError

from dotfiles_py.generated import (
    EnforcementTier,
    LearnErrorCode,
    LearnErrorEntry,
    LearnProposal,
    LearnResponse,
    LearnReturnCode,
    LearnReturnEntry,
    LearnReviewRecord,
    ProposalStatus,
    ProvenanceRef,
    RetentionClass,
    ReviewVerdict,
)
from dotfiles_py.logging import get_logger
from dotfiles_py.time_ns import now_ns_pair

DECLINED_BUFFER_DAYS = 30
DECLINED_BUFFER_NS = DECLINED_BUFFER_DAYS * 86_400 * 1_000_000_000
PROPOSAL_MD_MAX_BYTES = 8192
LOCK_TIMEOUT_S = 10.0

TIER_RANK: dict[EnforcementTier, int] = {
    EnforcementTier.HK_PKL: 1,
    EnforcementTier.MISE_DOCTOR: 2,
    EnforcementTier.AGY_HOOK: 3,
    EnforcementTier.SKILL: 4,
    EnforcementTier.RULE: 5,
}

_LOG = get_logger("agy_learn.ledger")


def resolve_state_dir(state_dir: Path | None = None) -> Path:
    """Resolve `$XDG_STATE_HOME/agy-learn` (or `~/.local/state/agy-learn`)."""
    if state_dir is not None:
        return state_dir.expanduser()
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = (
        Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    )
    return base / "agy-learn"


def has_valid_provenance(provenance: ProvenanceRef) -> bool:
    """Return True when `transcript_step` or `plan_error_ref` is non-empty."""
    step = (provenance.transcript_step or "").strip()
    err_ref = (provenance.plan_error_ref or "").strip()
    return bool(step or err_ref)


def _provenance_signature(provenance: ProvenanceRef) -> tuple[str, str]:
    return (
        (provenance.transcript_step or "").strip(),
        (provenance.plan_error_ref or "").strip(),
    )


def recommend_tier_for_occurrences(
    tier: EnforcementTier,
    target_path: str,
    occurrences: int,
) -> tuple[EnforcementTier, bool]:
    """Escalate recurring (`>= 2`) Tier 4-5 proposals to deterministic Tier 1-3 gates."""
    if occurrences < 2 or TIER_RANK[tier] <= 3:
        return tier, False
    norm = target_path.lower()
    if "hk.pkl" in norm or norm.endswith(".pkl"):
        return EnforcementTier.HK_PKL, True
    if "mise.toml" in norm or "doctor" in norm:
        return EnforcementTier.MISE_DOCTOR, True
    return EnforcementTier.AGY_HOOK, True


def _make_id(prefix: str, key: str, ns: int) -> str:
    digest = hashlib.sha256(f"{key}:{ns}".encode()).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _parse_jsonl_bytes[ModelT: BaseModel](
    path: Path,
    raw_bytes: bytes,
    model_cls: type[ModelT],
) -> tuple[list[ModelT], list[LearnErrorEntry]]:
    items: list[ModelT] = []
    errors: list[LearnErrorEntry] = []
    for line_no, raw_line in enumerate(raw_bytes.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            obj: Any = orjson.loads(stripped)
            items.append(model_cls.model_validate(obj))
        except (orjson.JSONDecodeError, ValidationError, ValueError) as exc:
            errors.append(
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_INVALID_INPUT,
                    message=f"{path.name}:{line_no}: {exc}",
                )
            )
    return items, errors


def _read_jsonl_models[ModelT: BaseModel](
    path: Path,
    model_cls: type[ModelT],
    *,
    use_lock: bool = False,
) -> tuple[list[ModelT], list[LearnErrorEntry]]:
    if not path.is_file():
        return [], []
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    try:
        if use_lock:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(lock_path), timeout=LOCK_TIMEOUT_S):
                raw_bytes = path.read_bytes()
        else:
            raw_bytes = path.read_bytes()
    except Timeout as exc:
        return [], [
            LearnErrorEntry(
                code=LearnErrorCode.ERR_LOCK_CONFLICT,
                message=f"Timed out acquiring lock for {path}: {exc}",
            )
        ]
    except OSError as exc:
        return [], [
            LearnErrorEntry(
                code=LearnErrorCode.ERR_IO_FAILURE,
                message=f"Failed reading {path}: {exc}",
            )
        ]
    return _parse_jsonl_bytes(path, raw_bytes, model_cls)


def _append_locked_bytes(path: Path, line: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def write_text_atomic(path: Path, text: str) -> None:
    """Atomically replace *path* with UTF-8 *text*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def read_reviews(
    state_dir: Path | None = None,
) -> tuple[list[LearnReviewRecord], list[LearnErrorEntry]]:
    """Load all review records from `reviews.jsonl`."""
    resolved = resolve_state_dir(state_dir)
    return _read_jsonl_models(resolved / "reviews.jsonl", LearnReviewRecord)


def read_proposals(
    state_dir: Path | None = None,
) -> tuple[list[LearnProposal], list[LearnErrorEntry]]:
    """Load all proposal history rows from `proposals.jsonl`."""
    resolved = resolve_state_dir(state_dir)
    return _read_jsonl_models(resolved / "proposals.jsonl", LearnProposal)


def latest_reviews_by_plan(
    reviews: list[LearnReviewRecord],
) -> dict[str, LearnReviewRecord]:
    """Fold review records to the latest entry per `plan_key`."""
    latest: dict[str, LearnReviewRecord] = {}
    for rec in reviews:
        prev = latest.get(rec.plan_key)
        if prev is None or rec.timestamp_unix_ns >= prev.timestamp_unix_ns:
            latest[rec.plan_key] = rec
    return latest


def latest_proposals_map(
    proposals: list[LearnProposal],
) -> dict[str, LearnProposal]:
    """Fold proposal rows to the latest state per `proposal_id`."""
    latest: dict[str, LearnProposal] = {}
    for prop in proposals:
        prev = latest.get(prop.proposal_id)
        if prev is None or prop.timestamp_unix_ns >= prev.timestamp_unix_ns:
            latest[prop.proposal_id] = prop
    return latest


def record_review(
    *,
    plan_key: str,
    plan_sha256: str,
    outcome: ReviewVerdict,
    error_count: int,
    survivor_count: int,
    provenance: ProvenanceRef,
    plan_id: str | None = None,
    worktree_slug: str | None = None,
    proposal_ids: list[str] | None = None,
    repo_root: str | None = None,
    conversation_id: str | None = None,
    notes: str | None = None,
    state_dir: Path | None = None,
) -> LearnResponse:
    """Validate and append a `LearnReviewRecord` to `$XDG_STATE_HOME/agy-learn/reviews.jsonl`."""
    if not has_valid_provenance(provenance):
        ts, ns = now_ns_pair()
        return LearnResponse(
            command="record-review",
            rc=1,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_MISSING_PROVENANCE,
                    message="Provenance requires non-empty transcript_step or plan_error_ref.",
                )
            ],
            returns=[],
        )

    if outcome is ReviewVerdict.NO_NEW_PROPOSALS and survivor_count != 0:
        ts, ns = now_ns_pair()
        return LearnResponse(
            command="record-review",
            rc=1,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_INVALID_INPUT,
                    message="no_new_proposals review requires survivor_count=0.",
                )
            ],
            returns=[],
        )

    resolved = resolve_state_dir(state_dir)
    reviews_path = resolved / "reviews.jsonl"
    lock_path = reviews_path.with_suffix(f"{reviews_path.suffix}.lock")
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        with FileLock(str(lock_path), timeout=LOCK_TIMEOUT_S):
            ts, ns = now_ns_pair()
            try:
                review = LearnReviewRecord(
                    review_id=_make_id("rev", f"{plan_key}:{plan_sha256}", ns),
                    plan_key=plan_key,
                    plan_id=plan_id,
                    worktree_slug=worktree_slug,
                    plan_sha256=plan_sha256,
                    error_count=error_count,
                    outcome=outcome,
                    survivor_count=survivor_count,
                    proposal_ids=list(proposal_ids or ()),
                    provenance=provenance,
                    repo_root=repo_root,
                    conversation_id=conversation_id,
                    notes=notes,
                    timestamp=ts,
                    timestamp_unix_ns=ns,
                )
            except ValidationError as exc:
                return LearnResponse(
                    command="record-review",
                    rc=1,
                    timestamp=ts,
                    timestamp_unix_ns=ns,
                    errors=[
                        LearnErrorEntry(
                            code=LearnErrorCode.ERR_INVALID_INPUT,
                            message=str(exc),
                        )
                    ],
                    returns=[],
                )
            _append_locked_bytes(
                reviews_path, orjson.dumps(review.model_dump(mode="json")) + b"\n"
            )
    except Timeout as exc:
        ts, ns = now_ns_pair()
        return LearnResponse(
            command="record-review",
            rc=1,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_LOCK_CONFLICT,
                    message=f"Timed out acquiring lock for {reviews_path}: {exc}",
                )
            ],
            returns=[],
        )
    except OSError as exc:
        ts, ns = now_ns_pair()
        return LearnResponse(
            command="record-review",
            rc=1,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_IO_FAILURE,
                    message=f"Failed writing {reviews_path}: {exc}",
                )
            ],
            returns=[],
        )

    ret_code = (
        LearnReturnCode.RET_NO_NEW_PROPOSALS
        if outcome is ReviewVerdict.NO_NEW_PROPOSALS
        else LearnReturnCode.RET_OK
    )
    errors = (
        [
            LearnErrorEntry(
                code=LearnErrorCode.ERR_OWING_LEARN,
                message=f"Plan {plan_key} marked owing_learn after retry exhaustion.",
            )
        ]
        if outcome is ReviewVerdict.OWING_LEARN
        else []
    )
    _LOG.info("learn_review_recorded", review=review, outcome=outcome)
    return LearnResponse(
        command="record-review",
        rc=0,
        review=review,
        timestamp=ts,
        timestamp_unix_ns=ns,
        errors=errors,
        returns=[
            LearnReturnEntry(
                code=ret_code,
                message=f"Recorded {outcome.value} review for {plan_key}",
            )
        ],
    )


def propose_item(
    *,
    dedup_key: str,
    title: str,
    summary: str,
    target_path: str,
    tier: EnforcementTier,
    retention_class: RetentionClass,
    provenance: ProvenanceRef,
    proposed_diff: str | None = None,
    proposal_id: str | None = None,
    notes: str | None = None,
    state_dir: Path | None = None,
) -> LearnResponse:
    """Record or escalate a proposal, honoring the 30-day declined buffer (`occurrences >= 2` overrides)."""
    if not has_valid_provenance(provenance):
        ts, ns = now_ns_pair()
        return LearnResponse(
            command="propose",
            rc=1,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_MISSING_PROVENANCE,
                    message="Proposal requires non-empty transcript_step or plan_error_ref provenance.",
                )
            ],
            returns=[],
        )

    resolved = resolve_state_dir(state_dir)
    proposals_path = resolved / "proposals.jsonl"
    lock_path = proposals_path.with_suffix(f"{proposals_path.suffix}.lock")
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        with FileLock(str(lock_path), timeout=LOCK_TIMEOUT_S):
            raw_bytes = proposals_path.read_bytes() if proposals_path.is_file() else b""
            ts, ns = now_ns_pair()
            existing_rows, read_errs = _parse_jsonl_bytes(
                proposals_path, raw_bytes, LearnProposal
            )
            if read_errs:
                return LearnResponse(
                    command="propose",
                    rc=1,
                    timestamp=ts,
                    timestamp_unix_ns=ns,
                    errors=read_errs,
                    returns=[],
                )

            by_id = latest_proposals_map(existing_rows)
            matched: LearnProposal | None = None
            if proposal_id and proposal_id in by_id:
                matched = by_id[proposal_id]
            else:
                for prop in sorted(
                    by_id.values(),
                    key=lambda p: p.timestamp_unix_ns,
                    reverse=True,
                ):
                    if prop.dedup_key == dedup_key:
                        matched = prop
                        break

            returns: list[LearnReturnEntry] = []
            if matched is not None:
                seen_provs = {
                    _provenance_signature(row.provenance)
                    for row in existing_rows
                    if row.proposal_id == matched.proposal_id
                }
                same_prov = _provenance_signature(provenance) in seen_provs
                new_occurrences = (
                    matched.occurrences if same_prov else matched.occurrences + 1
                )
                if (
                    matched.status is ProposalStatus.DECLINED
                    and matched.declined_until_unix_ns is not None
                    and ns < matched.declined_until_unix_ns
                    and (same_prov or new_occurrences < 2)
                ):
                    return LearnResponse(
                        command="propose",
                        rc=1,
                        proposal=matched,
                        timestamp=ts,
                        timestamp_unix_ns=ns,
                        errors=[
                            LearnErrorEntry(
                                code=LearnErrorCode.ERR_DECLINED_ACTIVE,
                                message=(
                                    f"Proposal {matched.proposal_id} is within 30-day declined buffer "
                                    "and has no new recurrence."
                                ),
                            )
                        ],
                        returns=[
                            LearnReturnEntry(
                                code=LearnReturnCode.RET_SKIPPED,
                                message="Skipped within 30-day declined buffer.",
                            )
                        ],
                    )
                if same_prov and matched.status in {
                    ProposalStatus.PENDING,
                    ProposalStatus.APPROVED,
                    ProposalStatus.APPLIED,
                    ProposalStatus.SHIPPED,
                    ProposalStatus.DECLINED,
                }:
                    return LearnResponse(
                        command="propose",
                        rc=0,
                        proposal=matched,
                        timestamp=ts,
                        timestamp_unix_ns=ns,
                        errors=[],
                        returns=[
                            LearnReturnEntry(
                                code=LearnReturnCode.RET_SKIPPED,
                                message=f"Proposal {matched.proposal_id} already recorded for identical provenance.",
                            )
                        ],
                    )
                pid = matched.proposal_id
            else:
                new_occurrences = 1
                pid = proposal_id or _make_id("prop", dedup_key, ns)

            rec_tier, escalated = recommend_tier_for_occurrences(
                tier, target_path, new_occurrences
            )
            if escalated:
                returns.append(
                    LearnReturnEntry(
                        code=LearnReturnCode.RET_ESCALATED,
                        message=(
                            f"Auto-escalated {pid} from {tier.value} to {rec_tier.value} "
                            f"(occurrences={new_occurrences})."
                        ),
                    )
                )

            try:
                proposal = LearnProposal(
                    proposal_id=pid,
                    dedup_key=dedup_key,
                    title=title,
                    summary=summary,
                    target_path=target_path,
                    tier=tier,
                    recommended_tier=rec_tier,
                    retention_class=retention_class,
                    status=ProposalStatus.PENDING,
                    occurrences=new_occurrences,
                    provenance=provenance,
                    proposed_diff=proposed_diff,
                    declined_until_unix_ns=None,
                    shipped_commit=None,
                    notes=notes,
                    timestamp=ts,
                    timestamp_unix_ns=ns,
                )
            except ValidationError as exc:
                return LearnResponse(
                    command="propose",
                    rc=1,
                    timestamp=ts,
                    timestamp_unix_ns=ns,
                    errors=[
                        LearnErrorEntry(
                            code=LearnErrorCode.ERR_INVALID_INPUT,
                            message=str(exc),
                        )
                    ],
                    returns=[],
                )
            _append_locked_bytes(
                proposals_path, orjson.dumps(proposal.model_dump(mode="json")) + b"\n"
            )
    except Timeout as exc:
        ts, ns = now_ns_pair()
        return LearnResponse(
            command="propose",
            rc=1,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_LOCK_CONFLICT,
                    message=f"Timed out acquiring lock for {proposals_path}: {exc}",
                )
            ],
            returns=[],
        )
    except OSError as exc:
        ts, ns = now_ns_pair()
        return LearnResponse(
            command="propose",
            rc=1,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_IO_FAILURE,
                    message=f"Failed accessing {proposals_path}: {exc}",
                )
            ],
            returns=[],
        )

    returns.append(
        LearnReturnEntry(
            code=LearnReturnCode.RET_PROPOSED,
            message=f"Recorded proposal {pid} (tier={rec_tier.value}, occurrences={new_occurrences}).",
        )
    )
    _LOG.info("learn_proposal_recorded", proposal=proposal)
    return LearnResponse(
        command="propose",
        rc=0,
        proposal=proposal,
        timestamp=ts,
        timestamp_unix_ns=ns,
        errors=[],
        returns=returns,
    )


def mark_proposal(
    *,
    proposal_id: str,
    status: ProposalStatus,
    shipped_commit: str | None = None,
    notes: str | None = None,
    state_dir: Path | None = None,
) -> LearnResponse:
    """Update a proposal's status (setting a 30-day declined buffer when `declined`)."""
    resolved = resolve_state_dir(state_dir)
    proposals_path = resolved / "proposals.jsonl"
    lock_path = proposals_path.with_suffix(f"{proposals_path.suffix}.lock")
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        with FileLock(str(lock_path), timeout=LOCK_TIMEOUT_S):
            raw_bytes = proposals_path.read_bytes() if proposals_path.is_file() else b""
            ts, ns = now_ns_pair()
            rows, read_errs = _parse_jsonl_bytes(
                proposals_path, raw_bytes, LearnProposal
            )
            if read_errs:
                return LearnResponse(
                    command="mark",
                    rc=1,
                    timestamp=ts,
                    timestamp_unix_ns=ns,
                    errors=read_errs,
                    returns=[],
                )

            by_id = latest_proposals_map(rows)
            existing = by_id.get(proposal_id)
            if existing is None:
                return LearnResponse(
                    command="mark",
                    rc=1,
                    timestamp=ts,
                    timestamp_unix_ns=ns,
                    errors=[
                        LearnErrorEntry(
                            code=LearnErrorCode.ERR_PROPOSAL_NOT_FOUND,
                            message=f"Unknown proposal_id: {proposal_id}",
                        )
                    ],
                    returns=[],
                )

            declined_until = (
                ns + DECLINED_BUFFER_NS if status is ProposalStatus.DECLINED else None
            )
            updated = existing.model_copy(
                update={
                    "status": status,
                    "declined_until_unix_ns": declined_until,
                    "shipped_commit": shipped_commit or existing.shipped_commit,
                    "notes": notes if notes is not None else existing.notes,
                    "timestamp": ts,
                    "timestamp_unix_ns": ns,
                }
            )
            _append_locked_bytes(
                proposals_path, orjson.dumps(updated.model_dump(mode="json")) + b"\n"
            )
    except Timeout as exc:
        ts, ns = now_ns_pair()
        return LearnResponse(
            command="mark",
            rc=1,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_LOCK_CONFLICT,
                    message=f"Timed out acquiring lock for {proposals_path}: {exc}",
                )
            ],
            returns=[],
        )
    except OSError as exc:
        ts, ns = now_ns_pair()
        return LearnResponse(
            command="mark",
            rc=1,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_IO_FAILURE,
                    message=f"Failed accessing {proposals_path}: {exc}",
                )
            ],
            returns=[],
        )

    ret_map = {
        ProposalStatus.APPLIED: LearnReturnCode.RET_APPLIED,
        ProposalStatus.SHIPPED: LearnReturnCode.RET_APPLIED,
        ProposalStatus.DECLINED: LearnReturnCode.RET_DECLINED,
    }
    ret_code = ret_map.get(status, LearnReturnCode.RET_OK)
    _LOG.info("learn_proposal_marked", proposal_id=proposal_id, status=status)
    return LearnResponse(
        command="mark",
        rc=0,
        proposal=updated,
        timestamp=ts,
        timestamp_unix_ns=ns,
        errors=[],
        returns=[
            LearnReturnEntry(
                code=ret_code,
                message=f"Marked {proposal_id} as {status.value}",
            )
        ],
    )
