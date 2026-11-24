"""Filelock-guarded JSONL ledger, 5-tier escalation, and 30-day declined buffer for `agy_learn`."""

from __future__ import annotations

import hashlib
import os
import re
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
MIN_SUBSTANTIVE_NOTES_LEN = 40

_PLACEHOLDER_STRINGS = frozenset(
    {
        "",
        "-",
        "—",
        "none",
        "none yet",
        "no errors",
        "n/a",
        "na",
        "nil",
        "null",
        "tbd",
        "todo",
        "placeholder",
        "lgtm",
        "ok",
        "done",
        "looks good",
        "all good",
        "no new proposals",
        "no new proposals needed",
        "no reusable updates",
        "no reusable updates needed",
        "no reusable customization updates needed",
        "post-task /learn: no reusable customization updates needed",
        "nothing to learn",
        "nothing reusable",
        "canonical path review",
        "rubber stamp",
    }
)
_BOILERPLATE_WORDS = frozenset(
    {
        "a",
        "all",
        "canonical",
        "check",
        "clean",
        "customization",
        "done",
        "emerged",
        "for",
        "good",
        "issue",
        "learn",
        "lgtm",
        "looks",
        "n",
        "na",
        "needed",
        "new",
        "nil",
        "no",
        "none",
        "nothing",
        "ok",
        "passed",
        "path",
        "placeholder",
        "post",
        "proposal",
        "proposals",
        "reusable",
        "review",
        "same",
        "task",
        "tbd",
        "this",
        "to",
        "todo",
        "updates",
    }
)

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


def is_bare_plan_key(plan_key: str) -> bool:
    """Return True when *plan_key* is a bare non-path slug (no `/` and not ending with `task_plan.md`)."""
    raw = plan_key.strip()
    if not raw:
        return False
    return "/" not in raw and "\\" not in raw and not raw.endswith("task_plan.md")


def _normalize_rel_parts(raw: str) -> tuple[str, ...]:
    return tuple(
        part for part in Path(raw.replace("\\", "/")).parts if part not in ("", ".")
    )


def canonicalize_plan_key(
    plan_key: str,
    *,
    plan_path: str | None = None,
    repo_root: str | None = None,
    plan_id: str | None = None,
    worktree_slug: str | None = None,
) -> tuple[str | None, str | None]:
    """Canonicalize *plan_key* (against *plan_path* when given) and reject bare non-path slugs."""
    raw_key = plan_key.strip()
    raw_path = (plan_path or "").strip()
    raw_root = (repo_root or "").strip()
    root_path = (
        Path(os.path.expanduser(raw_root)).resolve(strict=False) if raw_root else None
    )

    if raw_path:
        if is_bare_plan_key(raw_path) or raw_path.endswith(("/", "\\")):
            return (
                None,
                f"plan_path {raw_path!r} must point to a task_plan.md path, not a bare slug or directory.",
            )
        p_exp = Path(os.path.expanduser(raw_path))
        resolved_plan = str(
            (root_path / p_exp).resolve(strict=False)
            if (not p_exp.is_absolute() and root_path is not None)
            else p_exp.resolve(strict=False)
        )
        if not raw_key:
            return resolved_plan, None
        if is_bare_plan_key(raw_key):
            plan_dir_name = Path(resolved_plan).parent.name
            matches_plan = (
                raw_key == plan_dir_name
                or (
                    bool(plan_id and plan_id.strip())
                    and raw_key == plan_id.strip()
                    and plan_dir_name == plan_id.strip()
                )
                or (
                    bool(worktree_slug and worktree_slug.strip())
                    and raw_key == worktree_slug.strip()
                    and (
                        plan_dir_name == raw_key
                        or plan_dir_name.endswith(f"-{raw_key}")
                    )
                )
            )
            if matches_plan:
                return resolved_plan, None
            return (
                None,
                f"Bare non-path slug plan_key {raw_key!r} is rejected and does not match plan_path {resolved_plan!r}.",
            )
        if raw_key.endswith(("/", "\\")):
            return (
                None,
                f"plan_key {raw_key!r} must reference a plan file, not a directory.",
            )
        k_exp = Path(os.path.expanduser(raw_key))
        if k_exp.is_absolute():
            resolved_key = str(k_exp.resolve(strict=False))
            if resolved_key != resolved_plan:
                return (
                    None,
                    f"plan_key {raw_key!r} ({resolved_key}) disagrees with plan_path {resolved_plan!r}.",
                )
            return resolved_plan, None
        if root_path is not None:
            resolved_key = str((root_path / k_exp).resolve(strict=False))
            if resolved_key != resolved_plan:
                return (
                    None,
                    f"plan_key {raw_key!r} ({resolved_key}) disagrees with plan_path {resolved_plan!r}.",
                )
            return resolved_plan, None
        rel_parts = _normalize_rel_parts(raw_key)
        plan_parts = Path(resolved_plan).parts
        if (
            rel_parts
            and len(rel_parts) <= len(plan_parts)
            and plan_parts[-len(rel_parts) :] == rel_parts
        ):
            return resolved_plan, None
        return (
            None,
            f"Relative plan_key {raw_key!r} does not match plan_path {resolved_plan!r}.",
        )

    if not raw_key:
        return (
            None,
            "plan_key must not be empty (provide canonical task_plan.md path or --plan-path).",
        )
    if is_bare_plan_key(raw_key):
        return (
            None,
            f"Bare non-path slug plan_key {raw_key!r} is rejected; pass canonical task_plan.md path, repo-relative .planning/<PLAN_ID>/task_plan.md, or --plan-path.",
        )
    if raw_key.endswith(("/", "\\")):
        return (
            None,
            f"plan_key {raw_key!r} must reference a plan file, not a directory.",
        )
    k_exp = Path(os.path.expanduser(raw_key))
    if k_exp.is_absolute():
        return str(k_exp.resolve(strict=False)), None
    if root_path is not None:
        return str((root_path / k_exp).resolve(strict=False)), None
    norm_posix = Path(raw_key.replace("\\", "/")).as_posix()
    if norm_posix in (".", "..") or norm_posix.endswith(("/.", "/..")):
        return None, f"Invalid relative plan_key {raw_key!r}."
    return norm_posix, None


def is_non_placeholder_error_ref(plan_error_ref: str | None) -> bool:
    """Return True when *plan_error_ref* is non-empty and not a trivial placeholder."""
    raw = (plan_error_ref or "").strip()
    if not raw:
        return False
    norm = re.sub(r"\s+", " ", raw).strip(" .,:;!?-").lower()
    return norm not in _PLACEHOLDER_STRINGS


def validate_substantive_notes(notes: str | None) -> str | None:
    """Return an error reason if *notes* is missing, <40 chars, or trivial boilerplate; else None."""
    if notes is None or not notes.strip():
        return (
            f"requires substantive notes (>= {MIN_SUBSTANTIVE_NOTES_LEN} non-whitespace chars "
            "explaining why logged errors yielded 0 proposals)"
        )
    raw_stripped = notes.strip()
    collapsed = " ".join(raw_stripped.split())
    if (
        len(raw_stripped) < MIN_SUBSTANTIVE_NOTES_LEN
        or len(collapsed) < MIN_SUBSTANTIVE_NOTES_LEN
    ):
        return (
            f"notes must be >= {MIN_SUBSTANTIVE_NOTES_LEN} meaningful chars "
            f"(got {len(collapsed)})"
        )
    norm = collapsed.lower().strip(" .,:;!?-_`\"'")
    if norm in _PLACEHOLDER_STRINGS:
        return "notes cannot be trivial placeholder boilerplate"
    alpha_words = re.findall(r"[A-Za-z]{2,}", collapsed)
    unique_tokens = {
        tok.lower() for tok in re.findall(r"[A-Za-z0-9_/-]{2,}", collapsed)
    }
    if len(alpha_words) < 5 or len(unique_tokens) < 4:
        return "notes must contain a substantive explanation (not repeated filler or punctuation)"
    non_boilerplate = [
        tok for tok in unique_tokens if tok.strip("_-/") not in _BOILERPLATE_WORDS
    ]
    if not non_boilerplate:
        return "notes cannot consist solely of trivial boilerplate phrasing"
    return None


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

    canonical_key, key_err = canonicalize_plan_key(
        plan_key,
        plan_path=provenance.plan_path,
        repo_root=repo_root,
        plan_id=plan_id,
        worktree_slug=worktree_slug,
    )
    if key_err is not None or canonical_key is None:
        ts, ns = now_ns_pair()
        return LearnResponse(
            command="record-review",
            rc=1,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_INVALID_INPUT,
                    message=key_err or "Invalid plan_key.",
                )
            ],
            returns=[],
        )

    if outcome is ReviewVerdict.NO_NEW_PROPOSALS:
        if survivor_count != 0:
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
        if error_count > 0:
            reasons: list[str] = []
            notes_err = validate_substantive_notes(notes)
            if notes_err is not None:
                reasons.append(notes_err)
            if not is_non_placeholder_error_ref(provenance.plan_error_ref):
                reasons.append(
                    "requires non-empty plan_error_ref referencing Errors Encountered row(s)"
                )
            if reasons:
                ts, ns = now_ns_pair()
                return LearnResponse(
                    command="record-review",
                    rc=1,
                    timestamp=ts,
                    timestamp_unix_ns=ns,
                    errors=[
                        LearnErrorEntry(
                            code=LearnErrorCode.ERR_INVALID_INPUT,
                            message=(
                                "Anti-rubber-stamp gate rejected no_new_proposals with "
                                f"error_count={error_count}: {'; '.join(reasons)}."
                            ),
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
                    review_id=_make_id("rev", f"{canonical_key}:{plan_sha256}", ns),
                    plan_key=canonical_key,
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
                message=f"Plan {canonical_key} marked owing_learn after retry exhaustion.",
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
                message=f"Recorded {outcome.value} review for {canonical_key}",
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
