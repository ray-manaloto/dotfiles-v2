---
trigger: always_on
description: Require /code-review:code-review (+ code-review-commons) and HEAD receipt before git push, merge --ff-only into main, or PR creation on non-doc diffs, plus /code-review:pr-code-review on open PRs.
---

# Code Review Protocol

1. **Pre-ship `/code-review:code-review` (`+ code-review-commons`) gate (mandatory):**
   Before running `git push`, `git merge --ff-only` into `main`/`master`,
   `repotools pr-create`, `mise run ship`, or `gh pr create` for any change that
   touches non-documentation files:
   - Run `/code-review:code-review` (which activates `code-review-commons`) over
     `git diff -U5 --merge-base origin/HEAD` (or `main..HEAD` in a linked
     worktree).
   - Fan out diffs larger than ~1,000 changed lines across parallel read-only
     subagent slices by file group, then verify every finding at `path:line`.
   - Fix confirmed `CRITICAL`, `HIGH`, and `MEDIUM` findings and re-run repo
     checks before shipping.
   - Record the HEAD receipt immediately via:
     `python3 -I -B ~/.gemini/config/hooks/agy-code-review-guard.py --record --summary "<summary>"`
   - Documentation-only diffs (`.md`, `.mdx`, `.markdown`, `.rst`, `.adoc`,
     `.txt`) auto-pass without a receipt.

2. **Open pull requests (`/code-review:pr-code-review`):**
   When reviewing or updating an open GitHub pull request, follow
   `/code-review:pr-code-review` (`+ code-review-commons`).

3. **Distinction from human PR review:**
   This local automated agent self-review gate enforces quality before code
   leaves a branch or worktree and does not alter the owner ruling (2026-11-17)
   that external/human PR approval is not required to merge.
