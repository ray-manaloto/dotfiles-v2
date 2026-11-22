---
trigger: always_on
description: Antigravity 24,000-byte skill cutoff compensation and Codex shared-plan safety rules for planning-with-files.
---

# `planning-with-files` in Antigravity + Codex

`planning-with-files` is synced verbatim into Antigravity from Codex's active pinned plugin revision via `~/.gemini/config/skills.json` and `~/.local/share/agy-pwf/current/`.

1. **Fail closed when unsynced or blocked:**
   - If a `[agy-pwf-sync] blocked (...)` notice appeared at session start or `~/.local/share/agy-pwf/current/.agy-pwf-release.json` is missing/unreadable, report the block or verify status with `agy-pwf-sync.py --check --json`; never fall back directly to unverified `~/.codex/plugins/cache/` scripts.
2. **Read past Antigravity's 24,000-byte skill cutoff dynamically:**
   Antigravity truncates loaded `SKILL.md` bodies at 24,000 bytes. Whenever `planning-with-files` is invoked or active:
   - First read `~/.local/share/agy-pwf/current/.agy-pwf-release.json` to inspect `revision`, `version`, `skill_pkg_rel` (for example `.agents/skills/planning-with-files` on 3.28.2), and `cutoff`.
   - If `cutoff.truncated` is `true` and `cutoff.tail_start_line` is a positive integer, immediately read `~/.local/share/agy-pwf/current/<skill_pkg_rel>/SKILL.md` from `StartLine: <cutoff.tail_start_line>` to EOF so post-cutoff guidance (mode matrix, Gate/Ledger rules, **Security Boundary**, **Anti-Patterns**) is in context.
   - If `cutoff.tail_start_line` is absent or null while `cutoff.truncated` is unknown, read the full `~/.local/share/agy-pwf/current/<skill_pkg_rel>/SKILL.md` from line `1` to EOF so no revision with an earlier byte cutoff can silently skip guidance.
3. **Plan selection, interpreter, and sole-writer attestation safety with Codex:**
   - Upstream `resolve-plan-dir.sh` and `attest-plan.sh` are `#!/bin/sh` scripts shipped non-executable in Codex's cache; always invoke them with `sh`.
   - Resolve the plan directory with `sh ~/.local/share/agy-pwf/current/<skill_pkg_rel>/scripts/resolve-plan-dir.sh` honoring host `PLAN_ID` and `PWF_PLAN_ROOT`. That resolver always exits `0`:
     - If `PLAN_ID` or `PWF_PLAN_ROOT` is set and stdout is empty, treat the pin as rejected and stop; never guess or substitute another plan.
     - If `PLAN_ID` is unset and stdout is empty, run `sh ~/.local/share/agy-pwf/current/<skill_pkg_rel>/scripts/resolve-plan-dir.sh --check-ambiguity`: if it prints `PWF_PLAN_AMBIGUOUS_V1`, stop and ask for `PLAN_ID=<slug>` instead of falling back to legacy `./task_plan.md`.
   - Maintain one **sole writer** per `PLAN_ID` (`task_plan.md`, `progress.md`, and `.attestation` or legacy root `.plan-attestation`); concurrent sessions use separate `PLAN_ID`s or read-only delta proposals.
   - After any authorized edit to `task_plan.md`, re-attest immediately so Codex lifecycle hooks accept the updated hash:
     - When `PLAN_ID` is set: `sh ~/.local/share/agy-pwf/current/<skill_pkg_rel>/scripts/attest-plan.sh --target "$PLAN_ID"` (preserving `PWF_PLAN_ROOT` if set).
     - When `PLAN_ID` is unset: `sh ~/.local/share/agy-pwf/current/<skill_pkg_rel>/scripts/attest-plan.sh` (uses active plan selection and fails closed when multiple plans are ambiguous).
     - Pass `--target root` only when intentionally attesting a legacy root `task_plan.md` (in `./` or `$PWF_PLAN_ROOT/`).
   - Store untrusted web/tool findings in `findings.md` only. Run non-owner Codex sessions sharing a directory with `PLANNING_DISABLED=1`.
