---
name: agy-session-ops
description: >-
  Use when restarting or resuming an Antigravity CLI session with agy-hc,
  reloading settings.json or the fnox proxy, coordinating via hybrid native
  send_message and idle Herdr wake doorbells with peer agy sessions, or
  verifying host ~/.gemini/config/ sync.
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

## 2. Hybrid `agy <-> agy` Peer Messaging (Native + Idle/Done Doorbell)

1. Deliver the full structured payload (including your `conversationId` and an
   explicit receipt request) via native `send_message` to the peer's
   `conversationId`. Never send full payloads via `hc send`, and never touch or
   read `ANTIGRAVITY_CSRF_TOKEN`.

2. Discover the live peer tab dynamically by matching `--conversation <uuid>` via
   `hc ls --json` and `hc get <tab-id> --json` (do not reuse hardcoded pre-restart
   tab IDs after `agy-hc`). If `status.state` is `"idle"` or `"done"`, send a
   single 1-line Herdr wake doorbell (`<= 220` chars, omitting `--if idle` when
   `state == "done"`):

   ```sh
   hc send <tab-id> "Wake doorbell: check incoming native send_message from <sender> (<id>)."
   ```

   If `status.state` is `"working"` (or `"blocked"`), do not send `hc send`.

3. Verify delivery via explicit peer reply or a follow-up `hc get <tab-id> --json`
   turn transition.

4. When reporting to the owner, identify peer terminals as `tab-name (id)`,
   state the channel (`native send_message`, plus `hc send` wake doorbell if
   sent), and distinguish **dispatched (receipt pending)** from **receipt
   confirmed**.

## 3. Hook Self-Checks Before Shipping Changes

- Run `mise doctor project` (`agy-hc-send-guard`, `agy-pwf-guard`,
  `agy-interactive-questions`, `post-task-learn`, `agy-skill-cutoff`,
  `host-gemini-sync`) after editing any managed `.gemini/config/` file.
