---
trigger: always_on
description: Pair long run_command calls with NotificationTimeoutSeconds instead of separate schedule watchdogs; reserve schedule for crons, standalone reminders, and agent-message waits.
---

# Antigravity Background Commands (`run_command` vs `schedule`)

Antigravity CLI (`1.5.17+`) supports `NotificationTimeoutSeconds` directly on `run_command` so the host notifies you automatically when a long-running shell command exceeds the wait threshold instead of spending extra polling turns or timers.

- **Use `NotificationTimeoutSeconds` on long-running `run_command` / `hc wait` calls:** Pair `WaitMsBeforeAsync` (for example `500`–`5000`) with a bounded `NotificationTimeoutSeconds`. When running `hc wait <tab> --timeout <T>`, set `NotificationTimeoutSeconds` slightly shorter than `<T>` for midpoint check-ins without stalling.
- **Never spawn redundant `schedule` timers for `run_command` tasks:** If `run_command` is already carrying `NotificationTimeoutSeconds`, do NOT create a separate `schedule` one-shot watchdog timer for that shell command.
- **Reserve `schedule` for non-shell waits and recurring jobs:** Use `schedule` only for recurring crons (`CronExpression`), standalone timed reminders unrelated to a single `run_command`, or agent-message waits paired with `TimerCondition`.
- **Keep this rule Antigravity-only:** `NotificationTimeoutSeconds` and `schedule` are Antigravity tool features and do not belong in shared cross-harness `~/.agents/AGENTS.md`.
