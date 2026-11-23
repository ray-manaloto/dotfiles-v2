---
trigger: always_on
---

# Antigravity 24 KB Skill Cutoff

Antigravity truncates loaded `SKILL.md` bodies at 24,000 bytes. At `SessionStart`,
`~/.gemini/config/hooks/agy-skill-cutoff.py` scans active AGY skills and writes
the current cutoff table to `~/.local/share/agy-skill-cutoff/manifest.json`
(`${XDG_DATA_HOME:-~/.local/share}/agy-skill-cutoff/manifest.json`).

Whenever any skill listed in `truncated` (or surfaced by a `[agy-skill-cutoff]`
notice, such as `last30days`) is invoked or active:

1. Immediately call `read_file` on that skill's `SKILL.md` from
   `StartLine: <tail_start_line>` to EOF so all post-cutoff sections (self-checks,
   handoff rules, security boundaries) are in context before executing or
   presenting results.
2. If `~/.local/share/agy-skill-cutoff/manifest.json` is missing or unreadable, or
   if the file changed since the scan, read the full `SKILL.md` from line `1` to
   EOF instead of guessing the cutoff line.
3. For `planning-with-files`, continue following `planning-with-files-agy.md`,
   which also governs Codex revision sync, `PLAN_ID` selection, and sole-writer
   attestation.
