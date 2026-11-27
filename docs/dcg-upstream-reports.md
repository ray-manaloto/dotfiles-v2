# dcg upstream report drafts (#116)

These are drafts of two reports for
[destructive_command_guard](https://github.com/Dicklesworthstone/destructive_command_guard).
They come from the 2026-11-27 session. File them after a host with dcg confirms
them with `mise run dcg:repro`.

`mise run dcg:repro` sends each fixture in `tests/fixtures/dcg/` to the
installed `dcg`. It uses
the same PreToolUse payload that Claude Code sends. It prints `REPRO` for each
fixture that dcg still decides wrongly, and `OK` for the others. It exits 1
while any fixture reproduces. Paste its output into the report.

The environment is `dcg 0.17.2` (from `unix/home/.config/mise/config.toml`),
with the policy in
[`unix/home/.config/dcg/config.toml`](../unix/home/.config/dcg/config.toml).
That policy enables the `containers.docker`, `containers.compose`,
`secret_disclosure`, and `system.permissions` packs, adds the custom
`custom.mise_protect` pack, and sets `[heredoc] enabled = true`.

Neither case has a local fix in the pack config. As far as we can tell from
dcg's pack model, the repo's custom pack can add safe patterns only for its own
rules. It cannot exempt a single command from a core rule. A dcg allowlist entry for the whole rule would allow every `mv`
from the home directory, which is too broad. Turning off `[heredoc]` would stop
dcg from scanning real `bash`/`python` heredocs. Use the local workarounds
listed with each report until upstream fixes the cases.

## Report 1: the suggested Trash move is blocked when sent over ssh

**Fixtures:** `ssh-inline-trash-mv.json` (the repro), with
`local-trash-mv-files.json` and `local-trash-mv-suggested.json` as controls.

```sh
ssh pc 'mv ~/dev/tmp/av-status-probe ~/.local/share/Trash/files/'
```

**Actual:** dcg blocks it with `core.filesystem:mv-sensitive-source-root-home`.
The block message lists `mv <path> ~/.local/share/Trash/` as the allowed
soft-delete on Linux.

**Expected:** dcg allows the move. Its target is the freedesktop Trash, which is
the destination the block message recommends.

**Triage:** Compare the controls. If both local `mv` forms are allowed, dcg
parses the inline ssh command without applying the Trash exception. If
`local-trash-mv-files.json` is blocked too, the exception matches only
`Trash/` and misses `Trash/files/`, which is the directory the spec actually
uses for files.

**Local workaround:** `mise run pc:trash '~/dev/tmp/av-status-probe'`. It
streams `tasks/py/pc_trash.py` to the host and runs it there. The script moves
the path into `~/.local/share/Trash/files/` with a timestamp suffix and writes
a matching `.trashinfo` file. dcg sees only the `mise run` command.

## Report 2: data in a quoted heredoc is scanned as commands

**Fixture:** `quoted-heredoc-data.json`.

```sh
cat >> task_plan.md <<'EOF'
| 2026-11-27 | probe cleanup blocked | suggested `find ... -delete`; used the Edit tool |
EOF
```

**Actual:** The append is blocked. The heredoc body is markdown text that
names a destructive `find` command, and dcg evaluates that text as a command.

**Expected:** dcg allows it. The delimiter is quoted (`<<'EOF'`), so the shell
does no expansion in the body, and `cat` only copies it into a file. The body
is data unless the heredoc goes to an interpreter (`bash`, `sh`, `python`, and
so on). dcg already treats `git commit -m "..."` text as data in the same way.

**Related:** #93 tracks the hint for inline scripts in this repository.

**Local workaround:** Append the text with the editor's write tool instead of
a shell heredoc. Never reword the text to get past the guard. The shared rule
in `unix/home/.agents/AGENTS.md` says the same.
