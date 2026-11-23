---
name: pwf-status
description: >-
  Show current planning-with-files (PWF) status across the active workspace and
  sibling git worktrees by running `mise run pwf:status`. Use when invoked via
  `/pwf-status` or when the user asks for PWF status, active plans, phase
  progress, or plan-doctor health.
---

# PWF Status

Check planning-with-files state without modifying any plan files.

## Steps

1. Run `mise run pwf:status` via `run_command` in the active workspace:
   - Pass `--doctor` if the user asks to diagnose or verify PWF health, format,
     or attestation.
   - Pass `--here` if the user asks only about the current directory without
     listing sibling worktrees.
2. If `mise run pwf:status` is unavailable in the current environment, follow
   `planning-with-files-agy.md` by reading
   `~/.local/share/agy-pwf/current/.agy-pwf-release.json` and running
   `sh ~/.local/share/agy-pwf/current/<skill_pkg_rel>/scripts/resolve-plan-dir.sh`
   and `set-active-plan.sh --list`.
3. Summarize concisely for the user:
   - Resolved plan path (or `AMBIGUOUS` / `REJECTED` / `no active plan`), `.mode`,
     `PLAN_ID`, and attestation status.
   - Phase counts and current phase list, plus worktree summary if present.
   - Do not load full `findings.md` or `progress.md` contents unless asked.
   - Never create, edit, or attest planning files during a status check.
