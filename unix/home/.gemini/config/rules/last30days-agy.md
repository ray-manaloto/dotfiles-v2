---
trigger: always_on
description: Antigravity 24,000-byte skill cutoff compensation for the last30days plugin.
---

# `last30days` in Antigravity

Antigravity truncates loaded `SKILL.md` bodies at 24,000 bytes, while `last30days`'s `SKILL.md` (`30,135`-byte body on v4.6.0) exceeds that cap around line 216.

Whenever the `last30days` skill is invoked or active:

1. Immediately read `~/.gemini/config/plugins/last30days-skill/skills/last30days/SKILL.md` from `StartLine: 200` to EOF with `read_file` so post-cutoff sections (`## PRE-PRESENT SELF-CHECK`, `## HTML and follow-up handoff`, and `## Security & Permissions`) are in context before executing or presenting results.
2. If a future update grows `SKILL.md` earlier than line 200 (or `read_file` shows `## PRE-PRESENT SELF-CHECK` starts earlier), read the full file from line 1 to EOF.
