---
name: post-task-learn
description: >-
  Run a bounded post-ticket or post-task learning pass mirroring Antigravity's
  built-in /learn command after completing a ticket, PR packet, or mutating
  task. Record validated reviews and proposals in the schema-first agy-learn
  ledger using the 5-tier enforcement ladder, or record a 0-survivor
  no_new_proposals review when nothing reusable emerged.
---

# Post-Task Learning Pass (`/learn`)

Executes the Antigravity built-in `/learn` workflow
(`https://antigravity.google/docs/commands/learn/`) automatically after a
completed ticket or task. Never edit rules, skills, hooks, or configuration
files before explicit user approval.

## 1. Identify What to Learn

- Analyze recent user and reviewer messages in this session for explicit
  corrections (`"no"`, `"instead"`, `"that failed"`, review findings).
- Compare failed attempts against the eventual verified fix to isolate the
  pivotal change, root cause, and scope (workspace vs. global).
- When triggered by a completed `planning-with-files` plan, inspect its
  `task_plan.md` (`Errors Encountered`, `Decisions Made`), `findings.md`, and
  `progress.md` alongside recent user messages.
- **Ask-question boundary**: `ask_question` does not trigger the `Stop` hook,
  so after completing a mutating task before calling `ask_question`, run one
  bounded `/learn` check first.
- **Anti-fast-exit gate**: Do NOT skip review when `task_plan.md` logged
  `Errors Encountered`, commands failed and needed retries, a multi-step
  workflow was discovered, or global `~/.gemini/config/` / `AGENTS.md` files
  changed without verifying local Mac host sync.
- **Host sync check**: Whenever `dotfiles-v2` modifies global `.gemini/config/**`
  files, verify host symlinks and `~/.gemini/config/hooks.json` match before
  closing the task.
- **Valid `0`-survivor review**: If the completed task had zero reusable
  survivors after checking errors and retries, record
  `mise run learn:record-review --outcome no_new_proposals --survivor-count 0`
  with provenance (`--transcript-step` / `--plan-error-ref`) and stop cleanly.

## 2. 5-Tier Enforcement Ladder & Retention Classes

Prefer the highest deterministic tier that enforces an invariant cleanly:

1. **Tier 1 — `hk.pkl` (`hk_pkl`)**: git-hook / `ruff` / `ty` / `agnix` /
   schema gates (`pre-commit` / `pre-push`).
2. **Tier 2 — `mise doctor project` (`mise_doctor`)**: environment, lockfile,
   sync, and runtime health invariants.
3. **Tier 3 — Antigravity Hook (`agy_hook`)**: `PreToolUse` / `Stop` /
   `SessionStart` lifecycle guards (`$HOME/.gemini/config/hooks/`).
4. **Tier 4 — Skill (`skill`)**: repeatable multi-step tool chains, flag
   combinations, and debugging checklists (`skills/<name>/SKILL.md`).
5. **Tier 5 — Rule (`rule`)**: concise universal behavioral guardrails
   (`AGENTS.md` or `~/.gemini/config/rules/<name>.md` with `trigger:`
   frontmatter; `24 KB` max per file).

- **Auto-escalation (`occurrences >= 2`)**: Any `skill` or `rule` proposal
  that recurs twice or more automatically escalates to a deterministic Tier 1–3
  gate (`hk_pkl`, `mise_doctor`, or `agy_hook`).
- **Retention classes**: Tag every proposal with `safety-invariant`,
  `workflow-skill`, or `one-time-note` (`one-time-note` is auto-pruned during
  rendering).
- **30-day `declined` buffer**: Previously declined items are skipped for 30
  days unless `occurrences >= 2` overrides the buffer and escalates.
- **`SKILL.md` lint preflight (`agnix` + `rumdl`)**: Avoid hardcoded
  home-directory path prefixes in prose or examples (use `$HOME/...` or `~/`
  to satisfy `agnix` `XP-SK-002`), keep blank lines around fenced blocks and
  lists (`rumdl` `MD031`/`MD076`), and run `rumdl fmt` + `agnix --strict`.
- **No meta-tests for tooling/config**: Prefer `mise doctor project` and
  `hk check` over unit tests for upstream CLIs, git hooks, or doctor snippets.
- **Repository governance**: Global rules, hooks, and skills are managed in
  `dotfiles-v2`; in `knowledge-base-v2`, wiki mapping/QA rounds use `kb:retro`
  - `session-retro` while code/ticket tasks use `post-task-learn`.

## 3. Ledger & Thin Proposal Workflow

1. Record every review in `$XDG_STATE_HOME/agy-learn/reviews.jsonl` via
   `mise run learn:record-review` (with mandatory `--transcript-step` or
   `--plan-error-ref` provenance).
2. Add surviving proposals to `$XDG_STATE_HOME/agy-learn/proposals.jsonl` via
   `mise run learn:propose` (with `--tier`, `--retention-class`, and provenance).
3. Render a thin `<= 8 KB` active-only `learning_proposal.md` via
   `mise run learn:render-proposal --out <artifact_dir>/learning_proposal.md`
   (which auto-prunes applied, shipped, declined, and `one-time-note` items).
4. **Feedback metadata rule**: Always set `RequestFeedback: false` during
   automated `Stop`-hook runs and `0`-survivor reviews so Herdr tabs do not
   raise false `blocked` dialogs; reserve `RequestFeedback: true` exclusively
   for explicit user `/learn` approval prompts.
5. Run read-only `mise run learn:heal --check` at session checks or before
   closing to detect `owing_learn`, escalations, or already-shipped items
   without mutating rules, hooks, or GitHub issues without approval.
