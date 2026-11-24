# Claude Code Global Instructions

@../.agents/AGENTS.md

## Claude Code-Specific Instructions

- Install, update, enable, or disable plugins only via the `claude plugin` CLI
  (never edit `~/.claude/plugins/**`); uninstall with
  `claude plugin uninstall --keep-data` and verify with `claude plugin list --json`.
- Omit `--bare` whenever authenticating with `CLAUDE_CODE_OAUTH_TOKEN`
  (`--bare` skips OAuth and fails headless runs).
- Default headless smoke checks to
  `claude -p --model haiku --effort low --no-session-persistence --output-format text`
  with `</dev/null` on stdin.
- Claude Code does not read `~/.agents/skills/` directly: canonical skills are
  mirrored as `~/.claude/skills/<name> -> ../../.agents/skills/<name>` by
  `mise run bootstrap:skills`.
