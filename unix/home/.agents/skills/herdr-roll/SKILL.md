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

1. **Dry-run first:** Mutating commands (`roll:snapshot`, `roll:restart`, `roll:stop`, `roll:start`, `roll:self`) print their exact plan and change nothing unless `--apply` is passed (`roll:verify` runs read-only checks either way, writing logs only with `--apply`).
2. **Self-restarts:** Never run `roll:restart --tabs "$HC_TERMINAL_ID"` from
   inside the tab being restarted. Run `mise run roll:self --apply` and then end
   your turn cleanly so the detached `roll` tab can stop and resume you.
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
mise run roll:restart --all --exclude cpp-standard

# 3. Execute live restart after reviewing dry-run output
mise run roll:restart --spaces chief-of-staff --apply

# 4. Restart the caller's own Herdr tab via a detached parentless roll tab
mise run roll:self          # dry-run preview
mise run roll:self --apply  # spawns roll tab, then end your turn

# 5. Reboot shutdown / restore lifecycle
mise run roll:snapshot --apply
mise run roll:stop --all --apply
mise run roll:start --all --apply
mise run roll:verify --all
```
