---
name: post-task-learn
description: >-
  Run a bounded post-ticket or post-task learning pass mirroring Antigravity's
  built-in /learn command after completing a ticket, PR packet, or mutating
  task. Inspect recent user corrections, failed attempts, and verified fixes;
  propose durable Rule or Skill updates via learning_proposal.md with
  RequestFeedback=true, or fast-exit when nothing reusable emerged.
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
- **Anti-fast-exit gate**: Do NOT fast-exit when `task_plan.md` logged
  `Errors Encountered`, commands failed and needed retries, a multi-step
  workflow was discovered, or global `~/.gemini/config/` / `AGENTS.md` files
  changed without verifying local Mac host sync.
- **Host sync check**: Whenever `dotfiles-v2` modifies global `.gemini/config/**`
  files, verify host symlinks and `~/.gemini/config/hooks.json` match before
  closing the task.
- **Fast exit**: Only if the completed task had zero errors, retries, or
  reusable lessons, output `Post-task /learn: no reusable customization updates needed.`
  and stop immediately without modifying `learning_proposal.md`.

## 2. Classify Rules vs. Skills & Prefer Updates

- Consult the `agy-customizations` skill (`docs/rules.md`, `docs/skills.md`,
  `docs/hooks.md`) before drafting changes so schemas, frontmatter, and
  budgets match (`24 KB` max per rule file; `20,000`-token aggregate rule
  budget).
- **Skill-first extraction**: Prefer creating or updating a
  progressive-disclosure Skill (`skills/<name>/SKILL.md`) for repeatable
  workflows, debugging checklists, and tool/environment gotchas rather than
  appending runbook details to always-on `AGENTS.md` or rule files.
- **Update Existing (Prefer)**: update an active rule, skill, or workflow doc
  when it was outdated, missed an edge case, or diverged from the verified fix.
- **Classify**:
  - **Rule**: universal behavioral guardrails, constraints, or formatting
    invariants (`AGENTS.md` / `GEMINI.md` are always active with no frontmatter;
    files under `.agents/rules/<name>.md` or `~/.gemini/config/rules/<name>.md`
    require YAML frontmatter with a valid `trigger`).
  - **Skill**: multi-step tool chains, flag combinations, or checklists
    (`.agents/skills/<name>/SKILL.md` or `~/.gemini/config/skills/<name>/SKILL.md`).
  - **Hook**: deterministic lifecycle enforcement (`hooks.json`).
- **Repository governance**:
  - Global rules, hooks, and skills (`~/.gemini/GEMINI.md`,
    `~/.config/agents/AGENTS.md`, `~/.gemini/config/{hooks,rules,skills,hooks.json}`)
    are managed in `dotfiles-v2` (coordinate updates via `dotfiles-v2-lead`).
  - In `knowledge-base-v2`, wiki mapping/QA rounds use `kb:retro` +
    `session-retro`, while code/ticket tasks use `post-task-learn`.
  - Coordinate with `ticket-retro` / `session-retro` when closing a ticket or
    session so findings are linked rather than duplicated.

## 3. Mandatory Proposal Workflow

1. Create or update `learning_proposal.md` in the conversation artifact
   directory with `ArtifactMetadata`: `UserFacing: true` and
   `RequestFeedback: true`.
2. Record the classification (`Rule`, `Skill`, or `Hook`), rationale, target
   file path, and exact text additions/diffs.
3. Point the user to `learning_proposal.md` with only key open decisions, and
   apply changes only after explicit user approval.
