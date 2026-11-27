# Tracked plugin updates

`mise run plugins:update` keeps third-party plugins listed in
`unix/home/.config/mise/assets/plugins-tracked.json` (currently
`planning-with-files`) on their latest upstream GitHub release for Codex and
Claude user scope.

## Flow

1. Resolve the latest release tag and its commit SHA with `gh api`.
2. Compare it with the installed Codex version (`codex plugin list --json`) and
   the Claude user-scope version (`claude plugin list --json`, `scope == "user"`).
3. When either trails, run the strict tree gate, then update:
   - Codex: download the tarball of the resolved commit SHA and compare it with
     the installed cache directory
     (`~/.codex/plugins/cache/<mkt>/<plugin>/<version>`). If clean, run
     `mise run codex:plugin-update <plugin>@<mkt> --ref <sha> --apply`. After the
     install, the installed cache is re-diffed against the gated tree. A mismatch is
     exit 1 with a loud error (Codex has no documented plugin-disable command, so it
     names `codex plugin remove`).
   - Claude: `claude plugin update` installs the marketplace clone's HEAD, not the
     release tag. So run `claude plugin marketplace update <mkt>` (a catalog refresh
     only), diff the clone (`installLocation` from `claude plugin marketplace list
--json`, plugin dir from the catalog `source`) against the installed
     `installPath`, and only if clean run `claude plugin update <plugin>@<mkt>
--scope user`. After the install the new `installPath` is re-diffed against the
     gated clone snapshot. A mismatch runs `claude plugin disable <plugin>@<mkt>
--scope user` and exits 1. The clone HEAD SHA is reported; if it is not at or
     after the latest release commit a note says so, without blocking.
4. The default is a dry-run. `--apply` performs the updates. The dry-run still
   refreshes the Claude marketplace clone so it can compare real content.

## Strict tree gate

1. In the OLD (installed) tree, every occurrence of the old version string is
   replaced textually with the new version string, then both trees are compared.
2. Every file is compared byte for byte, plus mode bits (exec and
   setuid/setgid/sticky) and symlink targets. Nothing is skipped except `.git/` and
   `.in_use/` at the root of the OLD installed tree, Codex's root `.codex-marketplace-install.json` on
   the installed side, and `__pycache__`/`*.pyc` on the old installed side only
   (Python writes them at runtime; they are never ignored in a tree being installed).
3. A differing file is auto-approved only if it is a regular non-executable file at
   root `README.md`, `CHANGELOG.md`, `CONTRIBUTORS.md`, `CITATION.cff`, or
   `docs/**/*.md`, or `media/**` with a `.png/.svg/.jpg/.jpeg/.gif/.webp` suffix, and
   neither its path, its basename, `docs/` nor `media/` appears in any non-doc file
   of the new tree (so a glob such as `docs/*.md` forces review).
   Everything else that differs, including any SKILL.md or agent body change,
   needs owner review.

`codex_trust.collect_hook_surface` remains the narrower bookkeeping used by
`codex:plugin-update` itself.

Release tags must be plain dotted numbers (`^\d+(\.\d+){1,3}$` after the tag
prefix); anything else, including pre-releases, fails closed with exit 1 before any
text substitution or install. Installed versions are validated the same way.

## Exit codes and approval

| Code | Meaning                                                                                              |
| ---- | ---------------------------------------------------------------------------------------------------- |
| 0    | Up to date, or updates applied (or available in a dry-run)                                           |
| 1    | Error: `gh`, `codex`, or `claude` missing or failing, plugin not installed, or post-install mismatch |
| 3    | Tree changed beyond version strings: owner review required                                           |

On exit 3 the output shows the diff and two commands per harness:
`mise run plugins:update --plugin <id>` (shows the diff again) and
`mise run plugins:update --plugin <id> --approve <sha256>`. The hash covers the
pinned commit and the exact changed files, modes, and contents, and the approval
applies only if the freshly recomputed gate hashes to the same value. On Codex,
an approved apply passes `--trust-hook-diff`; it is never passed otherwise.
The digest also binds the harness label and the plugin id, so an approval for one
harness cannot be reused for the other. `--approve` requires `--plugin` and is rejected in `--scheduled` mode.

## Schedule and drift check

On the Mac host, the pitchfork daemon `plugins-update` runs
`mise run plugins:update --apply --scheduled` once a day and logs to
`~/.local/state/plugins-update/plugins-update.log.jsonl`. On exit 3 or exit 1 it
raises a macOS notification (exit 3 then returns 0). The warn-only doctor check
`plugin-drift` reports trailing plugins using a 6-hour GitHub cache under
`~/.cache/dotfiles-v2/`, bounds each tool call to 15 seconds, and never fails
the doctor.

## Residual risk

The Claude post-install check runs after `claude plugin update` has installed the
clone HEAD, so a clone that changes between the gate and the install could run hooks
before the mismatch is detected and the plugin is disabled. The gate narrows this by
snapshotting the clone right before the install, but it cannot be closed without an
install-from-staged-directory command.
