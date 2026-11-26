# PR shipping protocol (dotfiles-v2)

Status: v2, adopted 2026-11-25 after an adversarial (EGM) review of v1.
Applies to every agent-authored change in this public repository.

## Decision

- **Option A**: the lead agent gates, pushes a `wt/*` branch, opens the PR, waits on GitHub events, and merges with the
  owner-sanctioned merge form. Run it with `mise run pr:ship`.
- **Claude Code `/ship` and `/steward` are retired for this repository.** Their unattended watch fetches, merges and pushes
  with `git -c core.hooksPath=/dev/null`, runs only "the project's own fast checks", and so lands code that never passed this
  repository's gates. They stay available for the owner's own ad-hoc use elsewhere.
- **Repository auto-merge is disabled** (`allow_auto_merge = false`). Auto-merge would merge on required CI alone, bypassing
  the local gates and review.
- Hosted Claude Ship (Code Review, PR Steward) is out of scope: the docs restrict it to Team and Enterprise organizations.

## Roles

| Role | Does | Never |
|---|---|---|
| Lead agent | edits in its own worktree, runs gates, pushes `wt/*`, opens PRs, merges with the sanctioned form | force-push, delete branches, `--auto`, `--squash`, `--rebase`, `--admin` |
| Independent reviewer (fresh subagent) | reviews the exact diff read-only, reports verified findings | edits files |
| Review bots (Greptile, Graphify, CodeRabbit, Repowise) | review once when the PR opens | block a merge (advisory; quota-skipped is a pass) |
| Owner | decisions, OAuth sign-ins | routine steps |

Single writer: one agent owns a worktree until its PR merges or closes.

## Steps

1. **Worktree**: `mise run worktree:new <slug>`; commit on `wt/<slug>`.
2. **Gates on the exact head**, in order:
   1. `hk check --all`, `DOTFILES_DOCTOR_SCOPE=repo mise doctor project`, `mise run py:test`.
   2. An independent read-only subagent review of `git diff origin/main...HEAD`; fix every CRITICAL, HIGH and MEDIUM finding,
      then re-run 2.1. Every follow-up push gets a new review: bots do not re-review.
   3. Record Stage 1 (`agy-code-review-guard.py --record --summary ...`) and Stage 2 (`--verify --run ... --proof ...`).
      The pre-push hook refuses a push without these receipts for the head being pushed.
3. **Ship**: `mise run pr:ship [--title T --body-file F]`. The task is re-runnable and reads state before every step:
   preflight (`wt/*` branch, clean tree, repository auto-merge off, no auto-merge request on the PR) → local gates → push →
   find or open the PR → wait → bot sweep → verdict.
4. **Wait** (inside `pr:ship`): read the required checks for the head commit, then
   `gh event listen --resource <PR> --event ...check_suite.status.changed.v1,...head.changed.v1,...state.changed.v1 --timeout 10m`,
   then read state again. Events are not replayed, so state is always re-read after a wait or timeout. The task stops on a
   red required check (exit 1), on a moved head (exit 3), or after `--max-waits` (exit 2; re-run to resume). No sleep loops.
5. **Bot sweep**: read every bot line comment once; fix real findings by going back to step 2. At most three pushes for one
   cause; after that, report to the owner.
6. **Merge** after `pr:ship` prints READY: re-run `hk check --all` on that exact head with a clean tree, then
   `gh pr merge N --merge --match-head-commit <sha> --author-email <owner noreply>` (the form the owner's auto-mode policy
   allows). A moved head makes the merge fail closed.
7. **After merge**: fast-forward the main checkout (`git merge --ff-only origin/main`) and verify the change is live.

## Preconditions kept by guards

| Precondition | Guard |
|---|---|
| No global env var that turns off Claude Code feature-flag fetching (`DO_NOT_TRACK`, `DISABLE_TELEMETRY`, `CLAUDE_CODE_DISABLE_FEATURE_FLAGS`, `DISABLE_GROWTHBOOK`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `CLAUDE_CODE_DISABLE_SHIP`) in mise `[env]`, Dockerfile `ENV` or managed shell start-up files | `config-hygiene` doctor check |
| Same for managed Claude settings (`env`, `skillOverrides`, `disableBundledSkills`) and, on the live host, `~/.claude/settings.json` and the current shell | `claude-ship-env` doctor check |
| No checkout `.venv` on PATH (`python.uv_venv_auto = false`; tasks use `uv run` or `.venv/bin/python`) | `config-hygiene` |
| Claude Code runtime state under `.claude/` (for example `scheduled_tasks.lock`) never fails a check; only `.claude/skills` and `.agents/skills` are forbidden at the repository root | `no-repo-skills` |
| Repository auto-merge stays off | `pr:ship` preflight |

Scope telemetry opt-outs to the one command that needs them (for example `DO_NOT_TRACK=1 repowise init`), never globally.

## Known limits

- `gh event listen` is a preview feature; if it fails, `pr:ship` falls back to re-reading state after each timeout.
- Bots review only when a PR opens; their silence after a follow-up push is expected, not a failure.
- A crashed lead resumes by re-running `pr:ship`; it rebuilds its state from the PR.
