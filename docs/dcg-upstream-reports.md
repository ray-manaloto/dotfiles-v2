# dcg upstream report drafts (#116)

These are the two dcg cases from the 2026-11-27 session, for
[destructive_command_guard](https://github.com/Dicklesworthstone/destructive_command_guard).
Report 1 reproduces and is ready to file. Report 2 does not reproduce, so don't
file it.

`mise run dcg:repro` sends each fixture in `tests/fixtures/dcg/` to the
installed `dcg`. It uses the same PreToolUse payload that Claude Code sends. It
prints `REPRO` for each fixture that dcg still decides wrongly, and `OK` for
the others. A block shows the rule ID, and `-- --verbose` adds dcg's full
message. It exits 1 while any fixture reproduces.

The environment is `dcg 0.17.2` (from `unix/home/.config/mise/config.toml`),
with the policy in
[`unix/home/.config/dcg/config.toml`](../unix/home/.config/dcg/config.toml).
That policy enables the `containers.docker`, `containers.compose`,
`secret_disclosure`, and `system.permissions` packs, adds the custom
`custom.mise_protect` pack, and sets `[heredoc] enabled = true`.

Output on 2026-11-27 (macOS, dcg 0.17.2, the installed `~/.config/dcg`
policy):

```text
OK     bash-heredoc-find-delete: block (expected block) [core.filesystem:find-delete-root-home]
OK     bash-heredoc-find-exec-rm: block (expected block) [core.filesystem:rm-rf-general]
OK     local-trash-mv-files: allow (expected allow)
OK     local-trash-mv-suggested: allow (expected allow)
OK     quoted-heredoc-find-delete: allow (expected allow)
OK     quoted-heredoc-find-exec-rm: allow (expected allow)
REPRO  ssh-inline-trash-mv: block (expected allow) [core.filesystem:mv-sensitive-source-root-home]
```

## Report 1: the suggested Trash move is blocked when sent over ssh

**Fixtures:** `ssh-inline-trash-mv.json` (the repro), with
`local-trash-mv-files.json` and `local-trash-mv-suggested.json` as controls.

```sh
ssh pc 'mv ~/dev/tmp/av-status-probe ~/.local/share/Trash/files/'
```

**Actual:** dcg 0.17.2 blocks it:

```text
Reason: Embedded shell command blocked: mv touching a sensitive system or home
path is the cross-segment recursive-force-delete bypass. EXTREMELY DANGEROUS.
(ssh inline script)
...
- Soft-delete into the platform Trash (recoverable; allowed for a source under
  a home directory, under a temp directory, or a relative path, not for any
  other absolute path):
  `mv <path> ~/.Trash/` on macOS, `mv <path> ~/.local/share/Trash/` on Linux.
...
Rule: core.filesystem:mv-sensitive-source-root-home
```

**Expected:** dcg allows the move, as it does for the same command run
locally. The block message itself recommends this Trash move.

**Cause:** dcg's ssh-inline parsing, not the pack config. Run locally, both
`mv ~/dev/tmp/av-status-probe ~/.local/share/Trash/files/` and the suggested
`.../Trash/` form are allowed. Only the same `mv` inside `ssh pc '...'` is
blocked. dcg evaluates the extracted inline script without the Trash
exception that applies to the top-level command.

**Local fix:** none in the pack config. dcg's own block message for these rules
offers `dcg allowlist add-command '<exact command>'`. That entry matches one
literal command, so it covers one path and not the general case. Even when
fixed, dcg's Trash exception accepts any source below home, including
`~/.ssh` (the message says so). A broader allowlist would therefore let agents
move credentials over ssh. Use `mise run pc:trash '~/dev/tmp/av-status-probe'`
instead. It streams `tasks/py/pc_trash.py` to the host and runs it there. The
script moves the path into `~/.local/share/Trash/files/` with a timestamp
suffix and writes a matching `.trashinfo` file. dcg sees only the `mise run`
command, so the script does the guarding: it refuses top-level entries of home,
credentials, the mise toolchain, the dcg policy, the Trash and the dotfiles
checkout (see `PROTECTED` in the script).

## Report 2 (not reproduced): data in a quoted heredoc scanned as commands

**Fixtures:** `quoted-heredoc-find-delete.json` and
`quoted-heredoc-find-exec-rm.json` (one trigger each), with
`bash-heredoc-find-delete.json` and `bash-heredoc-find-exec-rm.json` as
controls.

```sh
cat >> task_plan.md <<'EOF'
| 2026-11-27 | probe cleanup blocked | suggested `find ~/dev/tmp/av-status-probe -delete`; used the Edit tool |
EOF
```

**Observed:** dcg 0.17.2 allows both `cat` fixtures. The controls feed the
same text to `bash`, and dcg blocks them with
`core.filesystem:find-delete-root-home` and `core.filesystem:rm-rf-general`
("line 1 of heredoc"). So heredoc scanning is on, and dcg already treats a
quoted heredoc fed to `cat` as data.

The session recorded no rule ID or message for its two blocks, so the exact
trigger is unknown. These fixtures don't reproduce them. No upstream report is
warranted until a new block is captured with `dcg test --stdin --explain`. If
that happens, add the captured command as a fixture. The four heredoc
fixtures stay as regression guards.

**Related:** #93 tracks the hint for inline scripts in this repository.

**If it recurs:** append the text with the editor's write tool instead of a
shell heredoc. Never reword the text to get past the guard. The shared rule in
`unix/home/.agents/AGENTS.md` says the same.
