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
_STRIP_PUNCT = " .,:;!?-_`\"'()[]—"

_PLACEHOLDER_STRINGS = frozenset(
    {
        "",
        "-",
        "—",
        "none",
        "none yet",
        "no error",
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
_ERROR_REF_BOILERPLATE = _BOILERPLATE_WORDS | {"error", "errors"}

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
        path_posix = raw_path.replace("\\", "/")
        path_obj = Path(path_posix)
        if (
            is_bare_plan_key(raw_path)
            or raw_path.endswith(("/", "\\"))
            or ".." in path_obj.parts
            or path_obj.name != "task_plan.md"
        ):
            return (
                None,
                f"plan_path {raw_path!r} must point to a task_plan.md file without '..' traversal.",
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
        key_posix = raw_key.replace("\\", "/")
        key_obj = Path(key_posix)
        if (
            raw_key.endswith(("/", "\\"))
            or ".." in key_obj.parts
            or key_obj.name != "task_plan.md"
        ):
            return (
                None,
                f"plan_key {raw_key!r} must point to a task_plan.md file without '..' traversal.",
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
    key_posix = raw_key.replace("\\", "/")
    key_obj = Path(key_posix)
    if (
        raw_key.endswith(("/", "\\"))
        or ".." in key_obj.parts
        or key_obj.name != "task_plan.md"
    ):
        return (
            None,
            f"plan_key {raw_key!r} must point to a task_plan.md file without '..' traversal.",
        )
    k_exp = Path(os.path.expanduser(raw_key))
    if k_exp.is_absolute():
        return str(k_exp.resolve(strict=False)), None
    if root_path is not None:
        return str((root_path / k_exp).resolve(strict=False)), None
    norm_posix = key_obj.as_posix()
    if norm_posix in (".", "..") or ".." in Path(norm_posix).parts:
        return None, f"Invalid relative plan_key {raw_key!r}."
    return norm_posix, None


def is_non_placeholder_error_ref(plan_error_ref: str | None) -> bool:
    """Return True when *plan_error_ref* is non-empty, >=3 chars, and not a trivial placeholder."""
    raw = (plan_error_ref or "").strip()
    if not raw:
        return False
    norm = re.sub(r"\s+", " ", raw).strip(_STRIP_PUNCT).lower()
    if len(norm) < 3 or re.search(r"[a-z0-9]", norm) is None:
        return False
    if norm in _PLACEHOLDER_STRINGS:
        return False
    tokens = re.findall(r"[a-z0-9]+", norm)
    return any(tok not in _ERROR_REF_BOILERPLATE for tok in tokens)


def explain_error_ref_failure(plan_error_ref: str | None) -> str:
    """Name the specific rule that made *plan_error_ref* fail `is_non_placeholder_error_ref`."""
    raw = (plan_error_ref or "").strip()
    example = 'describe the rows, e.g. --plan-error-ref "E1 dcg block, E2 typo"'
    if not raw:
        return f"--plan-error-ref is missing; {example}"
    norm = re.sub(r"\s+", " ", raw).strip(_STRIP_PUNCT).lower()
    if len(norm) < 3 or re.search(r"[a-z0-9]", norm) is None:
        return (
            f"plan_error_ref {raw!r} is shorter than 3 characters (after trimming "
            f"punctuation); {example}"
        )
    return (
        f"plan_error_ref {raw!r} is a placeholder or only generic words; "
        f'name the Errors Encountered rows, e.g. --plan-error-ref "E1 dcg block, E2 typo"'
    )


def count_plan_error_rows(plan_path: str | None) -> int | None:
    """Count data rows of the plan's `## Errors Encountered` table; None if unreadable or absent."""
    if not plan_path:
        return None
    try:
        text = Path(plan_path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return None
    bullets: list[str] = []
    table_rows: list[str] = []
    in_section = False
    found_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if in_section:
                break
            in_section = stripped.lower().startswith("## errors encountered")
            found_section = found_section or in_section
            continue
        if not in_section or not stripped:
            continue
        if re.match(r"^(?:[-*+]|\d+[.)])\s+\S", stripped):
            bullets.append(stripped)  # bullet or numbered list entry
            continue
        if not stripped.startswith("|"):
            continue
        if re.fullmatch(r"\|[\s:|-]+\|?", stripped):
            if table_rows:
                table_rows.pop()  # the row just above a separator is its header
            continue
        cells = [c.strip().strip("*_`").lower() for c in stripped.strip("|").split("|")]
        if any(c and c not in _PLACEHOLDER_STRINGS for c in cells):
            table_rows.append(stripped)
    # A table without a separator row has no reliable header, so every row counts (never a false reject).
    rows = len(bullets) + len(table_rows)
    return rows if found_section else None


def validate_substantive_notes(notes: str | None) -> str | None:
    """Return an error reason if *notes* is missing, <40 chars, or trivial boilerplate; else None."""
    if notes is None or not notes.strip():
        return (
            f"--notes is required: >= {MIN_SUBSTANTIVE_NOTES_LEN} non-whitespace chars "
            "explaining why logged errors yielded 0 proposals"
        )
    raw_stripped = notes.strip()
    collapsed = " ".join(raw_stripped.split())
    if (
        len(raw_stripped) < MIN_SUBSTANTIVE_NOTES_LEN
        or len(collapsed) < MIN_SUBSTANTIVE_NOTES_LEN
    ):
        return (
            f"--notes must be >= {MIN_SUBSTANTIVE_NOTES_LEN} meaningful chars "
            f"(got {len(collapsed)})"
        )
    norm = collapsed.lower().strip(_STRIP_PUNCT)
    if norm in _PLACEHOLDER_STRINGS:
        return "notes cannot be trivial placeholder boilerplate"
    alpha_words = re.findall(r"[A-Za-z]{2,}", collapsed)
    unique_tokens = {tok.lower() for tok in re.findall(r"[A-Za-z0-9]{2,}", collapsed)}
    if len(alpha_words) < 5 or len(unique_tokens) < 4:
        return "notes must contain a substantive explanation (not repeated filler or punctuation)"
    non_boilerplate = [tok for tok in unique_tokens if tok not in _BOILERPLATE_WORDS]
    if len(non_boilerplate) < 2:
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
                    message='Provenance is missing: pass --transcript-step "<what happened>" or --plan-error-ref "E1 <row>".',
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

    clean_proposal_ids = [pid.strip() for pid in (proposal_ids or ()) if pid.strip()]

    if outcome in (ReviewVerdict.PROPOSED, ReviewVerdict.APPLIED) and (
        survivor_count < 1 and not clean_proposal_ids
    ):
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
                        f"{outcome.value} review requires survivor_count >= 1 or non-empty proposal_ids; "
                        "use no_new_proposals for 0-survivor reviews."
                    ),
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
                reasons.append(explain_error_ref_failure(provenance.plan_error_ref))
            if count_plan_error_rows(provenance.plan_path) == 0:
                reasons.append(
                    f"the '## Errors Encountered' table in {provenance.plan_path} has no "
                    f"rows but error_count={error_count}; add rows to that table first"
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
                    proposal_ids=clean_proposal_ids,
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
            preserved_status = ProposalStatus.PENDING
            preserved_declined_until: int | None = None
            preserved_shipped_commit: str | None = None
            preserved_notes = notes
            if matched is not None:
                if matched.status in {
                    ProposalStatus.APPROVED,
                    ProposalStatus.APPLIED,
                    ProposalStatus.SHIPPED,
                }:
                    preserved_status = matched.status
                    if matched.status in {
                        ProposalStatus.APPLIED,
                        ProposalStatus.SHIPPED,
                    }:
                        preserved_shipped_commit = matched.shipped_commit
                if notes is None:
                    preserved_notes = matched.notes
                if TIER_RANK[matched.recommended_tier] < TIER_RANK[rec_tier]:
                    rec_tier = matched.recommended_tier
                escalated = TIER_RANK[rec_tier] < TIER_RANK[tier]
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
                    status=preserved_status,
                    occurrences=new_occurrences,
                    provenance=provenance,
                    proposed_diff=proposed_diff,
                    declined_until_unix_ns=preserved_declined_until,
                    shipped_commit=preserved_shipped_commit,
                    notes=preserved_notes,
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


def _canonical_matches_bare_review(
    bare_rec: LearnReviewRecord, canon_rec: LearnReviewRecord
) -> bool:
    bare = bare_rec.plan_key.strip()
    if not bare:
        return False
    if bare_rec.repo_root and canon_rec.repo_root:
        bare_repo = Path(os.path.expanduser(bare_rec.repo_root)).resolve(strict=False)
        canon_repo = Path(os.path.expanduser(canon_rec.repo_root)).resolve(strict=False)
        if bare_repo != canon_repo:
            return False
    canon_dir = Path(canon_rec.plan_key).parent.name
    slug_match = (
        canon_dir == bare
        or canon_dir.endswith(f"-{bare}")
        or canon_rec.worktree_slug == bare
        or canon_rec.plan_id == bare
    )
    if not slug_match:
        return False
    return (
        canon_rec.plan_sha256 == bare_rec.plan_sha256
        or canon_rec.timestamp_unix_ns >= bare_rec.timestamp_unix_ns
    )


def _resolve_bare_review_key(rec: LearnReviewRecord) -> str | None:
    if rec.provenance.plan_path and not is_bare_plan_key(rec.provenance.plan_path):
        resolved, err = canonicalize_plan_key(
            rec.plan_key,
            plan_path=rec.provenance.plan_path,
            repo_root=rec.repo_root,
            plan_id=rec.plan_id,
            worktree_slug=rec.worktree_slug,
        )
        if err is None and resolved is not None and not is_bare_plan_key(resolved):
            return resolved
    if rec.repo_root:
        repo = Path(os.path.expanduser(rec.repo_root))
        for cand_slug in (rec.plan_id, rec.plan_key, rec.worktree_slug):
            if not cand_slug or not cand_slug.strip():
                continue
            cand = repo / ".planning" / cand_slug.strip() / "task_plan.md"
            if cand.is_file():
                return str(cand.resolve(strict=False))
    return None


def _compact_reviews_plan(
    rows: list[LearnReviewRecord],
) -> tuple[list[LearnReviewRecord], list[str]]:
    """Deduplicate canonical + resolved bare rows by `(plan_key, plan_sha256, outcome)`."""
    canonical_rows = [r for r in rows if not is_bare_plan_key(r.plan_key)]
    migrated_ids: list[str] = []
    # Map (plan_key, plan_sha256, outcome) -> (position_idx, rank_tuple, record)
    best_by_sig: dict[
        tuple[str, str, ReviewVerdict],
        tuple[int, tuple[int, int, int], LearnReviewRecord],
    ] = {}
    unresolved_bare: list[tuple[int, LearnReviewRecord]] = []

    for idx, rec in enumerate(rows):
        bare = is_bare_plan_key(rec.plan_key)
        if not bare:
            target_key = rec.plan_key
            target_rec = rec
            rank = (rec.timestamp_unix_ns, rec.error_count, 1)
        else:
            resolved_key = _resolve_bare_review_key(rec)
            if resolved_key is not None:
                migrated_ids.append(rec.review_id)
                target_key = resolved_key
                target_rec = rec.model_copy(update={"plan_key": resolved_key})
                rank = (rec.timestamp_unix_ns, rec.error_count, 0)
            elif any(_canonical_matches_bare_review(rec, c) for c in canonical_rows):
                migrated_ids.append(rec.review_id)
                continue
            else:
                unresolved_bare.append((idx, rec))
                continue

        sig = (target_key, rec.plan_sha256, rec.outcome)
        prev = best_by_sig.get(sig)
        if prev is None:
            best_by_sig[sig] = (idx, rank, target_rec)
        elif rank > prev[1]:
            best_by_sig[sig] = (prev[0], rank, target_rec)

    ordered: list[tuple[int, LearnReviewRecord]] = [
        (pos, rec) for pos, _rank, rec in best_by_sig.values()
    ]
    ordered.extend(unresolved_bare)
    ordered.sort(key=lambda item: item[0])
    return [rec for _, rec in ordered], migrated_ids


def migrate_reviews(
    *,
    apply: bool = False,
    state_dir: Path | None = None,
) -> LearnResponse:
    """Compact superseded bare-slug rows in `reviews.jsonl` (dry-run unless `apply=True`)."""
    resolved = resolve_state_dir(state_dir)
    reviews_path = resolved / "reviews.jsonl"
    lock_path = reviews_path.with_suffix(f"{reviews_path.suffix}.lock")
    migrated_ids: list[str] = []
    try:
        if apply:
            resolved.mkdir(parents=True, exist_ok=True)
            with FileLock(str(lock_path), timeout=LOCK_TIMEOUT_S):
                raw_bytes = reviews_path.read_bytes() if reviews_path.is_file() else b""
                ts, ns = now_ns_pair()
                rows, read_errs = _parse_jsonl_bytes(
                    reviews_path, raw_bytes, LearnReviewRecord
                )
                if read_errs:
                    return LearnResponse(
                        command="migrate",
                        rc=1,
                        timestamp=ts,
                        timestamp_unix_ns=ns,
                        errors=read_errs,
                        returns=[],
                    )
                kept_rows, migrated_ids = _compact_reviews_plan(rows)
                if migrated_ids or len(kept_rows) != len(rows):
                    payload = b"".join(
                        orjson.dumps(row.model_dump(mode="json")) + b"\n"
                        for row in kept_rows
                    ).decode("utf-8")
                    write_text_atomic(reviews_path, payload)
        else:
            ts, ns = now_ns_pair()
            rows, read_errs = _read_jsonl_models(reviews_path, LearnReviewRecord)
            if read_errs:
                return LearnResponse(
                    command="migrate",
                    rc=1,
                    timestamp=ts,
                    timestamp_unix_ns=ns,
                    errors=read_errs,
                    returns=[],
                )
            _, migrated_ids = _compact_reviews_plan(rows)
    except Timeout as exc:
        ts, ns = now_ns_pair()
        return LearnResponse(
            command="migrate",
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
            command="migrate",
            rc=1,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_IO_FAILURE,
                    message=f"Failed accessing {reviews_path}: {exc}",
                )
            ],
            returns=[],
        )

    if apply:
        ret_code = (
            LearnReturnCode.RET_APPLIED if migrated_ids else LearnReturnCode.RET_OK
        )
        msg = (
            f"Compacted/migrated {len(migrated_ids)} bare-slug review row(s)."
            if migrated_ids
            else "No bare-slug review rows required migration."
        )
    else:
        ret_code = (
            LearnReturnCode.RET_SKIPPED if migrated_ids else LearnReturnCode.RET_OK
        )
        msg = (
            f"Dry-run: {len(migrated_ids)} bare-slug review row(s) eligible for migration (--apply)."
            if migrated_ids
            else "Dry-run: no bare-slug review rows require migration."
        )
    if apply and migrated_ids:
        _LOG.info(
            "learn_reviews_migrated", count=len(migrated_ids), migrated_ids=migrated_ids
        )
    return LearnResponse(
        command="migrate",
        rc=0,
        migrated_ids=migrated_ids,
        timestamp=ts,
        timestamp_unix_ns=ns,
        errors=[],
        returns=[LearnReturnEntry(code=ret_code, message=msg)],
    )


def list_proposals(
    *,
    statuses: list[ProposalStatus] | None = None,
    tiers: list[EnforcementTier] | None = None,
    actionable_only: bool = False,
    state_dir: Path | None = None,
) -> LearnResponse:
    """List latest proposals from `proposals.jsonl`, filtered cleanly."""
    ts, ns = now_ns_pair()
    rows, read_errs = read_proposals(state_dir)
    if read_errs:
        return LearnResponse(
            command="list",
            rc=1,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=read_errs,
            returns=[],
        )
    latest = list(latest_proposals_map(rows).values())
    allowed_statuses: set[ProposalStatus] | None = None
    if statuses:
        allowed_statuses = set(statuses)
    elif actionable_only:
        allowed_statuses = {ProposalStatus.PENDING, ProposalStatus.APPROVED}
    allowed_tiers = set(tiers) if tiers else None

    filtered = [
        prop
        for prop in latest
        if (allowed_statuses is None or prop.status in allowed_statuses)
        and (allowed_tiers is None or prop.recommended_tier in allowed_tiers)
    ]
    filtered.sort(
        key=lambda p: (
            TIER_RANK[p.recommended_tier],
            -p.occurrences,
            -p.timestamp_unix_ns,
        )
    )
    return LearnResponse(
        command="list",
        rc=0,
        proposals=filtered,
        timestamp=ts,
        timestamp_unix_ns=ns,
        errors=[],
        returns=[
            LearnReturnEntry(
                code=LearnReturnCode.RET_OK,
                message=f"Listed {len(filtered)} proposal(s).",
            )
        ],
    )


def apply_proposals(
    *,
    proposal_ids: list[str] | None = None,
    all_approved: bool = False,
    detect_shipped: bool = False,
    status: ProposalStatus = ProposalStatus.APPLIED,
    shipped_commit: str | None = None,
    notes: str | None = None,
    repo_root: Path | None = None,
    proposal_md: Path | None = None,
    state_dir: Path | None = None,
) -> LearnResponse:
    """Transition specified/approved/detected-shipped proposals to `applied` or `shipped`."""
    if status not in {ProposalStatus.APPLIED, ProposalStatus.SHIPPED}:
        ts, ns = now_ns_pair()
        return LearnResponse(
            command="apply",
            rc=1,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_INVALID_INPUT,
                    message=f"apply target status must be applied or shipped, got {status.value!r}.",
                )
            ],
            returns=[],
        )

    if detect_shipped and repo_root is None:
        ts, ns = now_ns_pair()
        return LearnResponse(
            command="apply",
            rc=1,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=[
                LearnErrorEntry(
                    code=LearnErrorCode.ERR_INVALID_INPUT,
                    message="--detect-shipped requires --repo <path>.",
                )
            ],
            returns=[],
        )

    from agy_learn.heal import (
        _recent_git_log,
        is_proposal_already_applied,
        render_active_proposal_md,
    )

    resolved = resolve_state_dir(state_dir)
    proposals_path = resolved / "proposals.jsonl"
    lock_path = proposals_path.with_suffix(f"{proposals_path.suffix}.lock")
    applied_ids: list[str] = []
    last_updated: LearnProposal | None = None
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
                    command="apply",
                    rc=1,
                    timestamp=ts,
                    timestamp_unix_ns=ns,
                    errors=read_errs,
                    returns=[],
                )
            by_id = latest_proposals_map(rows)
            requested = [pid.strip() for pid in (proposal_ids or []) if pid.strip()]
            missing = [pid for pid in requested if pid not in by_id]
            if missing:
                return LearnResponse(
                    command="apply",
                    rc=1,
                    timestamp=ts,
                    timestamp_unix_ns=ns,
                    errors=[
                        LearnErrorEntry(
                            code=LearnErrorCode.ERR_PROPOSAL_NOT_FOUND,
                            message=f"Unknown proposal_id(s): {', '.join(missing)}",
                        )
                    ],
                    returns=[],
                )

            targets: dict[str, str | None] = {pid: notes for pid in requested}
            if all_approved:
                for pid, prop in by_id.items():
                    if prop.status is ProposalStatus.APPROVED and pid not in targets:
                        targets[pid] = notes
            if detect_shipped and repo_root is not None:
                git_log_cache = _recent_git_log(repo_root)
                for pid, prop in by_id.items():
                    if pid in targets:
                        continue
                    if prop.status not in {
                        ProposalStatus.PENDING,
                        ProposalStatus.APPROVED,
                    }:
                        continue
                    reason = is_proposal_already_applied(
                        prop, repo_root, git_log_cache=git_log_cache
                    )
                    if reason:
                        targets[pid] = (
                            notes or f"Auto-detected already shipped: {reason}"
                        )

            for idx, (pid, item_notes) in enumerate(targets.items()):
                existing = by_id[pid]
                if existing.status is status or (
                    existing.status is ProposalStatus.SHIPPED
                    and status is ProposalStatus.APPLIED
                ):
                    continue
                updated = existing.model_copy(
                    update={
                        "status": status,
                        "declined_until_unix_ns": None,
                        "shipped_commit": shipped_commit or existing.shipped_commit,
                        "notes": item_notes
                        if item_notes is not None
                        else existing.notes,
                        "timestamp": ts,
                        "timestamp_unix_ns": ns + idx,
                    }
                )
                _append_locked_bytes(
                    proposals_path,
                    orjson.dumps(updated.model_dump(mode="json")) + b"\n",
                )
                applied_ids.append(pid)
                last_updated = updated
    except Timeout as exc:
        ts, ns = now_ns_pair()
        return LearnResponse(
            command="apply",
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
            command="apply",
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

    if proposal_md is not None and applied_ids:
        render_resp = render_active_proposal_md(
            out_path=proposal_md,
            state_dir=state_dir,
            repo_root=repo_root,
        )
        if render_resp.rc != 0:
            return LearnResponse(
                command="apply",
                rc=render_resp.rc,
                proposal=last_updated,
                applied_ids=applied_ids,
                timestamp=ts,
                timestamp_unix_ns=ns,
                errors=render_resp.errors,
                returns=render_resp.returns,
            )

    ret_code = (
        LearnReturnCode.RET_APPLIED if applied_ids else LearnReturnCode.RET_SKIPPED
    )
    msg = (
        f"Marked {len(applied_ids)} proposal(s) as {status.value}."
        if applied_ids
        else "No matching proposals to apply."
    )
    if applied_ids:
        _LOG.info("learn_proposals_applied", applied_ids=applied_ids, status=status)
    return LearnResponse(
        command="apply",
        rc=0,
        proposal=last_updated,
        applied_ids=applied_ids,
        timestamp=ts,
        timestamp_unix_ns=ns,
        errors=[],
        returns=[LearnReturnEntry(code=ret_code, message=msg)],
    )
