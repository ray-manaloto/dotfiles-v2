---
name: agy-session-ops
description: >-
  Use when restarting or resuming an Antigravity CLI session with agy-hc,
  reloading settings.json or the fnox proxy, coordinating via native
  send_message with peer agy sessions, or verifying host ~/.gemini/config/ sync.
---

# Antigravity Session & Peer Coordination Checklist

## 1. Restart / Resume via `agy-hc`

1. Check `hc get "$HC_TERMINAL_ID" --json` first to verify whether the tab
   wraps a login shell (`/bin/zsh -l`) and read `foreground.argv` for the active
   `--conversation` UUID.

2. Verify host `~/.gemini/config/` sync before telling the user to restart:

   ```sh
   mise run host:sync-gemini --check
   ```

   If drifted, run `mise run host:sync-gemini` first so updated hooks, rules,
   and skills load when `agy` restarts.

3. Give exact manual resume steps: `/exit` in the session -> verify `pwd` ->
   `agy-hc --conversation <id>` (launches `[wrappers.agy]` under a fresh `fnox`
   proxy and reloads `~/.gemini/antigravity-cli/settings.json`).

4. Verify after resume: run a blocked/allowed check such as `fnox --version` for
   `settings.json` and a live research query for `fnox` proxy Doppler tokens.

## 2. Native `agy <-> agy` Peer Messaging

- Never use `hc` for `agy <-> agy`; use native `send_message` to the peer's
  `conversationId`.
- Always request explicit receipt confirmation when sending a message to
  another agent.
- Identify peer terminals as `tab-name (id)` alongside the channel (`native
  send_message`), and distinguish **dispatched (receipt pending)** from
  **receipt confirmed** when reporting to the owner.

## 3. Hook Self-Checks Before Shipping Changes

- Run `mise doctor project` (`agy-pwf-guard`, `agy-interactive-questions`,
  `post-task-learn`, `agy-skill-cutoff`, `host-gemini-sync`) after editing any
  managed `.gemini/config/` file.
