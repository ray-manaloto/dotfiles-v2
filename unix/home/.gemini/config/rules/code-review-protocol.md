---
trigger: always_on
description: Require ordered /code-review:code-review (--record) followed by post-review behavioral verification (--verify) and observed proof before pushing, merging into main, opening PRs, or claiming completion.
---

# Code Review & Post-Review Verification Protocol

1. **Ordered 2-stage pre-ship gate (mandatory on all non-doc diffs):**
   Before running `git push`, `git merge --ff-only` into `main`/`master`,
   `repotools pr-create`, `mise run ship`, or `gh pr create` for any change that
   touches non-documentation files:
   - **Stage 1 — `/code-review:code-review` (`+ code-review-commons`):**
     - Run `/code-review:code-review` over `git diff -U5 --merge-base origin/HEAD`
       (or `main..HEAD` in a linked worktree); fan out diffs >~1,000 lines across
       parallel read-only subagent slices and verify every finding at `path:line`.
     - Apply every matching section of `~/.agents/review-standards.md`
       (credential/config migrations, degraded input, destructive targets,
       narrowed environments, secrets in errors and argv size).
     - Fix confirmed `CRITICAL`, `HIGH`, and `MEDIUM` findings and commit cleanly.
     - Record Stage 1 immediately (`--record` clears any prior Stage 2 receipt):
       `python3 -I -B ~/.gemini/config/hooks/agy-code-review-guard.py --record --summary "<summary>"`
   - **Stage 2 — Mandatory post-review verification (`--verify`):**
     - **After** Stage 1 `--record`, run concrete behavioral/runtime verification
       against the exact committed tree and record Stage 2 proof:
       `python3 -I -B ~/.gemini/config/hooks/agy-code-review-guard.py --verify --run "<verify-cmd>" --proof "<observed result>"`
     - Never claim a task is done, fixed, working, or shipped until post-edit
       and post-review verification passes and concrete observed output/proof is
       reported.
   - Documentation-only diffs (`.md`, `.mdx`, `.markdown`, `.rst`, `.adoc`,
     `.txt`) auto-pass without a receipt.

2. **Open pull requests (`/code-review:pr-code-review`):**
   When reviewing or updating an open GitHub pull request, follow
   `/code-review:pr-code-review` (`+ code-review-commons`) and verify live.

3. **Distinction from human PR review:**
   This local automated gate enforces review and observed verification before
   code leaves a branch or completion is claimed, without altering the owner
   ruling (2026-11-17) that external/human PR approval is not required to merge.
