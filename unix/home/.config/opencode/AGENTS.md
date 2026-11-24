# OpenCode-Specific Instructions

Shared cross-harness rules load from `~/.agents/AGENTS.md` via
`opencode.json` `instructions`; this file only adds OpenCode specifics.

- Run headless checks with `opencode --standalone run --archived` (never the
  daemonized/shared session path) and keep `"update": "disable"` in
  `opencode.json`.
- Default free runs to zero-retention `opencode/deepseek-v4.1-flash-free`;
  treat HTTP 429 from the free tier as a rate-limit skip, not a failure.
- Use the `opencode-paid` shell alias (scoped
  `fnox -c ~/.config/fnox/opencode-paid.toml -P opencode-paid --no-defaults proxy run`)
  for paid providers instead of exporting provider keys into the shell.
- Skills are discovered natively from `~/.agents/skills/`; do not create
  per-harness skill mirrors under `~/.config/opencode/skills/`.
