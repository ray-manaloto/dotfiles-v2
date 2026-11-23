---
trigger: always_on
description: Always deliver agy <-> agy payloads via native send_message, discover live peer tabs dynamically by conversationId, and restrict hc send on agy tabs to a 1-line Wake doorbell when idle or done.
---

# Antigravity Peer Messaging (`agy <-> agy`)

1. **Native `send_message` for payloads (mandatory):** Always send the full
   structured message (including your `conversationId` and an explicit receipt
   request) via native `send_message` to the peer's `conversationId`. Never send
   status updates, summaries, or payloads via `hc send`, and never touch
   `ANTIGRAVITY_CSRF_TOKEN`.

2. **Discover live peer tab dynamically (never hardcode tab IDs):** `agy-hc`
   restarts keep `conversationId` unchanged but assign a new Herdr tab ID. Look
   up the live tab with `hc ls --json` / `hc get <id> --json` matching
   `--conversation <uuid>` in `foreground.argv` (or `argv`).

3. **1-line wake doorbell only when `idle` or `done`:** After `send_message`,
   check the live peer tab's `status.state`:

   - If `status.state` is `"idle"` or `"done"`, send only a single-line wake
     doorbell (`<= 220` chars) starting with `Wake doorbell:` (for example,
     `hc send <id> "Wake doorbell: check incoming native send_message from <sender>."`).
     Omit `--if idle` when `status.state == "done"`.
   - If `status.state` is `"working"` (or `"blocked"`), do not run `hc send`.

4. **Reporting:** Name terminals as `tab-name (id)`, state the channel
   (`native send_message`, plus `hc send` wake doorbell if sent), and
   distinguish **dispatched (receipt pending)** from **receipt confirmed**.
