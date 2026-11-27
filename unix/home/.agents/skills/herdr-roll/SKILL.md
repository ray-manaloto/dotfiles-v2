---
name: herdr-roll
description: >-
  Use when restarting, snapshotting, stopping, resuming, or self-restarting
  Herdr agent sessions across agy, codex, claude, opencode, hermes, and zcode
  tabs via global `mise run roll:*` tasks.
---

# Herdr Rolling Restarts (`roll:*`)

Use the clean-room `herdr_roll` `mise` tasks to snapshot, restart, stop, resume,
and verify Herdr sessions across **Antigravity (`agy`)**, **Codex (`codex`)**,
**Claude Code (`claude`)**, **OpenCode (`opencode`)**, **Hermes (`hermes`)**,
and **ZCode (`zcode`)**.

## Safety Rules

1. **Dry-run first:** Mutating commands (`roll:snapshot`, `roll:restart`,
   `roll:stop`, `roll:start`, `roll:self`) print their exact plan and change
   nothing unless `--apply` is passed (`roll:verify` runs read-only checks
   either way, writing logs only with `--apply`).
2. **Self-restarts vs restarting from another tab:** Run
   `mise run roll:self --apply` only **from inside** the active agent tab
   (`$HC_TERMINAL_ID`) you want restarted, then end your turn cleanly so the
   detached `roll` tab stops, resumes, and closes itself. From a separate shell
   tab, run `mise run roll:restart --tabs <tab> --apply` instead.
3. **Fail closed on dialogs:** If verification reports `blocked-dialog` (for
   example `"Trust this folder"` or `"Hooks need review"`), never auto-approve it;
   report `tab-name (id)` to the owner.
4. **Never use legacy `herdr`, `pkill`, or `hc server stop`:** Only `hc` and
   `herdr-app` (always `--id` for `herdr-app do`).

## Common Commands

```sh
# 1. Inspect the live 6-harness session graph (add --apply to persist graph.json + ledger.md)
mise run roll:snapshot

# 2. Dry-run rolling restart (defaults: zero-token deterministic, --concurrency 4, clean env -i)
mise run roll:restart --tabs cos-shadow
mise run roll:restart --spaces chief-of-staff --harnesses codex
mise run roll:restart --all --exclude cpp-core

# 3. Execute live restart after reviewing dry-run output
mise run roll:restart --spaces chief-of-staff --apply

# 4. Restart the caller's own active agent tab via a self-closing roll tab
mise run roll:self          # dry-run preview (run inside the agent tab)
mise run roll:self --apply  # spawns roll tab, then end your turn

# 5. Reboot shutdown / restore lifecycle
mise run roll:snapshot --apply
mise run roll:stop --all --apply
mise run roll:start --all --apply
mise run roll:verify --all
```

## Verification Proof & Nanosecond Evidence (`roll:verify`)

Every `herdr_roll` run directory, `runs.jsonl` entry, snapshot artifact, and
`SessionOutcome` emits zero-padded 9-decimal-place nanosecond timestamps
(`YYYYmmddTHHMMSS.NNNNNNNNN±HHMM`, matching `\.\d{9}`).

`roll:verify` avoids eyeballing TUI screen captures by surfacing deterministic
process and startup-log proof on every outcome:

- **`process_started_at` & `process_lstart` (`psutil`):** Exact foreground
  process start time (`YYYYmmddTHHMMSS.NNNNNNNNN±HHMM` plus `ps lstart` string)
  so PID advancement and restart freshness are machine-verifiable.
- **`agy_log_proof` (Antigravity tabs):** Reads the specific `cli-*.log` file
  held open by the tab's live process tree (`~/.gemini/antigravity-cli/log/`)
  and confirms:
  1. `pid`: `Starting language server process with pid <N>` matches a live PID
     in the tab's process tree (`pid_matched: true`).
  2. `conversation_id`: `Resuming conversation` / `GetConversationDetail` /
     `Starting conversation update stream` matches `session_id`
     (`conversation_matched: true`).
  3. `loaded_hook_count` vs `expected_hook_count`: `loaded <N> named hooks from
     1 hooks.json file(s)` matches the named hook count in
     `~/.gemini/config/hooks.json` (`hooks_matched: true`).

## Dialog & Screen Triage

### Why Resumed `agy` Screens Show Prior Scrollback + Placeholder Text

On `agy --conversation <uuid>`, Antigravity replays prior conversation history
into the terminal (`Resuming conversation`, `RerenderAll`) and draws an empty
input box with placeholder text; in-place restarts also leave earlier terminal
scrollback above the refreshed TUI. Do not judge restart success or hook loading
from `hc read` alone — run `mise run roll:verify --tabs <tab>` and check
`process_started_at` + `agy_log_proof`.

### How `RequestFeedback` Triggers `blocked-dialog`

When an `agy` agent calls `write_file` or `edit_file` with
`ArtifactMetadata.RequestFeedback = true` (for example on `implementation_plan.md`
or `learning_proposal.md`), Antigravity displays an interactive Proceed/feedback
bar and Herdr sets `status.state = "blocked"`. `herdr_roll` fails closed with
`blocked-dialog` (`ERR_BLOCKED_DIALOG`) whenever `status.state` is `"blocked"`,
`"permission"`, or `"error"`. Report `tab-name (id)` to the owner so they can
approve or dismiss the feedback bar before re-running `roll:*`.

### Inspecting Codex Plugin Hook Prompts (`codex app-server` `hooks/list`)

Codex separates per-repo folder trust (`"Trust this folder"`) from global or
plugin hook review (`"Hooks need review"` / `"Continue without trusting"`). When
a Codex tab fails closed with `blocked-dialog`, query `codex app-server`
JSON-RPC `hooks/list` (passing `{"cwds": ["<folder>"]}` or `{"threadId":
"<uuid>"}`) to inspect `data[].hooks[]` fields (`source`, `sourcePath`,
`eventName`, `handlerType`, `command`, `enabled`, and `trustStatus`:
`"managed" | "trusted" | "untrusted" | "modified"`). Report any `"untrusted"`
or `"modified"` plugin hooks to the owner; do not auto-approve hook prompts or
bypass hook trust checks during `roll:*`.
