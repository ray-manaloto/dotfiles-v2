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

Lane and worker briefs, and any scratch harness a lane builds, set `MISE_AUTO_INSTALL=0` and `MISE_NOT_FOUND_AUTO_INSTALL=0`
(and scope `MISE_CONFIG_DIR` to the scratch directory) before running checks, so a dependency probe can never trigger a live
`mise` install. A stray auto-install, or a cancelled bulk install, leaves unhardened binaries and orphaned staging dirs that
fail host doctor.

Briefs state live-write authority explicitly. When the work could touch a live repository or organization setting (Actions
policy, environment, ruleset), say either "commit only, propose the live call" or "live write authorized". "Implement it" is
ambiguous and has produced live settings without owner approval.

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
   red required check (exit 1), on a moved head (exit 3), after `--max-waits` (exit 2; re-run to resume), or when the GitHub
   API fails three times in a row (exit 4). It refuses up front (exit 3) when a workflow that produces a required check is
   `disabled_manually` or `disabled_inactivity` (such a check can never start), and a required check with no check run on the head
   after the waits is reported as `NOT STARTED`, distinct from a failed one. No sleep loops; if event streams are unavailable it falls back to a 60 s re-read.
5. **Bot sweep**: read every bot line comment once; fix real findings by going back to step 2. At most three pushes for one
   cause; after that, report to the owner.
6. **Merge** after `pr:ship` prints READY: re-run `hk check --all` on that exact head with a clean tree, then
   `gh pr merge N --merge --match-head-commit <sha> --author-email <owner noreply>` (the form the owner's auto-mode policy
   allows; `pr:ship` prints the exact command with the owner's GitHub noreply address). A moved head makes the merge fail closed.
7. **Learn**: run the `post-task-learn` skill for the worktree and record its review
   (`mise run learn:record-review --worktree-slug <slug> --plan-id <PLAN_ID> ...`, `no_new_proposals` with provenance when
   nothing survives). The retro is this step: corrections, failed attempts and bot findings become ledger proposals.
8. **Land** (no owner prompt): `mise run pr:land` from the merged worktree. It refuses unless the PR is MERGED at the
   worktree's exact head, the tree is clean and the learn review from step 7 is recorded; then it fast-forwards the main
   checkout, checks the merge commit is on `main`, removes the worktree and its `wt/*` branch with `worktree:rm`, runs `learn:heal --check`, and runs the full live
   `mise doctor project` (host scope) on the merged `main`, printing its summary; a failure there only warns, because the land
   already happened. It then closes the finished Herdr agent tabs that worked in the removed worktree (`herdr_tidy.py --cwd`);
   a working agent, a plain shell and the terminal running `pr:land` are never closed. Verify the change is live afterwards.
   For tabs left by worktrees landed some other way, `mise run herdr:tidy` lists finished agent tabs whose folder is gone,
   and `--apply` closes them (`--keep a,b` protects named tabs).

## Preconditions kept by guards

| Precondition | Guard |
|---|---|
| No global env var that turns off Claude Code feature-flag fetching (`DO_NOT_TRACK`, `DISABLE_TELEMETRY`, `CLAUDE_CODE_DISABLE_FEATURE_FLAGS`, `DISABLE_GROWTHBOOK`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `CLAUDE_CODE_DISABLE_SHIP`) in mise `[env]`, Dockerfile `ENV` or managed shell start-up files | `config-hygiene` doctor check |
| Same for managed Claude settings (`env`, `skillOverrides`, `disableBundledSkills`) and, on the live host, `~/.claude/settings.json` and the current shell | `claude-ship-env` doctor check |
| No checkout `.venv` on PATH (`python.uv_venv_auto = false`; tasks use `uv run` or `.venv/bin/python`) | `config-hygiene` |
| Claude Code runtime state under `.claude/` (for example `scheduled_tasks.lock`) never fails a check; only `.claude/skills` and `.agents/skills` are forbidden at the repository root | `no-repo-skills` |
| Repository auto-merge stays off | `pr:ship` preflight |
| A merged worktree is landed only after its learn review is recorded and nothing unshipped remains | `pr:land` |

Scope telemetry opt-outs to the one command that needs them (for example `DO_NOT_TRACK=1 repowise init`), never globally.

## Known limits

- `gh event listen` is a preview feature; if it fails, `pr:ship` falls back to re-reading state every 60 s.
- Bots review only when a PR opens; their silence after a follow-up push is expected, not a failure.
- `gh run rerun` of a `pull_request_target` workflow replays the original workflow definition, not the fixed one on `main`.
  After fixing such a workflow, retrigger it by closing and reopening the PR (or pushing), so the run loads the current `main`.
- A crashed lead resumes by re-running `pr:ship`; it rebuilds its state from the PR.
