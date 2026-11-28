# AgentsView hub: remote sync and embeddings

What the pc AgentsView hub does that is not obvious from its config. Read this
before changing `hub:mac-pull-key`, `hub:up --mac-ssh`, `hub:vector` or the
`agentsview-mac-pull` row of `hub:status`. Facts are for the pinned AgentsView
v0.48.0; links point at that tag.

Upstream sources: [configuration.md][cfg], [semantic-search.md][ss],
[semantic-search-internals.md][ssi].

## Remote sync (the Mac pull)

The hub pulls the Mac's sessions through an `[[remote_hosts]]` entry with
`transport = "ssh"` ([Remote Hosts][cfg-remote]). `hub:mac-pull-key` sets up
the restricted key and ssh alias; `hub:up --mac-ssh <alias>` writes the entry.

- **No user setting excludes a provider from remote imports.** `disabled_agents`
  "applies only to local filesystem discovery, targeted local file sync, file
  watching, and scheduled polling" and "does not affect HTTP remote imports"
  ([Disabling Session Providers][cfg-disable]). The docs don't mention SSH; a
  test on 2026-11-27 showed it does not stop an SSH pull either. The only
  exclusion is the built-in `RemoteSyncExcluded` provider capability.
- **`[agents.opencode] homes` fails the whole pull.** OpenCode does not support
  alternate homes ([Alternate Agent Homes][cfg-homes]), and the config loader
  rejects it with `agents.opencode.homes: provider does not support alternate
  homes` ([`agent_directories.go`][src-homes]).
- **The remote side reads its own `config.toml`.** One invalid key in the Mac's
  `~/.agentsview/config.toml` fails that host's pull from the hub.
- **One bad message costs a full reparse every cycle.** An OpenCode message
  holding a lone UTF-16 surrogate fails to import, so the delta import logs
  `failed=1` and persists an empty skip cache. Every later pull re-reads
  everything (about 11 minutes for the Mac). Tracked upstream as
  [kenn-io/agentsview#3437][i3437]; until it ships, the live hub has no Mac
  entry.
- **No log line marks a successful pull.** An attempt that gets past target
  planning logs `remote sync: host=<h> phase=targets`, every import logs
  `remote sync delta import: host=<h> ...` ([`prepare.go`][src-delta]), and a
  failure follows as `remote sync <h> failed:` ([`sync.go`][src-failed]) or
  `scheduled remote sync <h>:`. A good pull is a delta-import line with no
  failure line after it. Lines with `journal_outcome=retained(aborted-before-processing)`
  are aborted attempts, not results. `hub:status` reads `debug.log` and
  `debug.log.1` this way.

## Antigravity (agy) sessions

The hub holds no `antigravity-cli` sessions yet (#90), so
`agentsview session search` misses decisions made only in agy. Until it indexes
them, agents run the `agy-history-fallback` skill
([`SKILL.md`](../unix/home/.agents/skills/agy-history-fallback/SKILL.md)), which
searches `~/.gemini/antigravity-cli/conversations/` and `brain/` on each
machine. `mise doctor project` runs its `--self-check` (`agy-history-fallback`).

## Semantic search (embeddings)

`hub:vector --enable` applies the tracked recipe
[`semantic-search.toml`](../unix/home/.config/mise/assets/agentsview/semantic-search.toml):
`qwen3-embedding:0.6b` (1024 dimensions) served by the hub's loopback Ollama
(`dev.mise.ollama-hub.service`, `127.0.0.1:11434`).

- **The index lives in the data dir.** `vectors.db` sits beside `sessions.db`
  ([Storage layout][ssi-storage]), so it moves with a data-dir cutover. Build the
  index in the archive that will become live; an index built elsewhere is
  stranded.
- **A resync keeps it.** A parser-change resync swaps `sessions.db` only;
  unchanged documents keep their vectors ([Storage layout][ssi-storage]).
- **An upgrade can wipe it.** When `mirror_schema_version` changes (DDL shape
  or document identity), the next writable open drops every mirror table and
  the next build starts from scratch ([Mirror schema versioning][ssi-mirror]).
  A full build on pc's GTX 1660 SUPER takes about 25 to 32 hours (2.3 to 2.9
  chunks/s over about 262k chunks). Read release notes for vector changes
  before bumping AgentsView.
- **Config changes can re-embed.** `model`, `dimension`, `max_input_chars`,
  `query_prefix` and the other fingerprint inputs create a new generation
  ([Generations and fingerprints][ssi-gen]). Server transport settings
  (`endpoint`, `batch_size`, `concurrency`, `timeout`) do not.
- **`index embeddings build` needs a running daemon** for the same data dir.
- **`hub:up` restarts Ollama.** A running build survives the restart, but run
  `hub:up` sparingly during a long build.

### Long builds: size the watchdog from a measured rate

The 2026-11-27 build ran as transient `systemd-run --user` units (a scratch
`agentsview serve` plus `index embeddings build`) with a `RuntimeMaxSec`
watchdog. The cap came from a benchmark, and the benchmark was wrong:

- **A small sample overstates the rate.** 847 chunks gave 2.3 chunks/s, so the
  cap was set to 60h for a ~32h estimate. The full build ran at 1.5 chunks/s
  after 1.5h, 1.07 after 2.2h and 1.42 after 5.2h. Chunk length varies across
  sessions, so the rate swings for hours.

- **Measure first, then cap.** Prefix each progress line with `date +%s`, let
  the build run at least an hour, then project the remaining time from that
  rate. Set `RuntimeMaxSec` to at least twice the projection.

- **Raise the cap live with a drop-in.** On systemd 262,
  `systemctl --user set-property <unit> RuntimeMaxSec=...` is refused
  (`Cannot set property RuntimeMaxUSec`). A drop-in works on a running
  transient unit, keeping the same PID:

  ```sh
  mkdir -p ~/.config/systemd/user/<unit>.service.d
  printf '[Service]\nRuntimeMaxSec=96h\n' \
    > ~/.config/systemd/user/<unit>.service.d/runtime-max.conf
  systemctl --user daemon-reload
  systemctl --user show <unit> -p RuntimeMaxUSec -p DropInPaths
  ```

  Raise both units, the daemon and the build, because the build needs the
  daemon. Remove the drop-ins once the units have exited.

### Pausing a long build

Pause the build before `pc:sync`, an omarchy update, a reboot or anything else
that needs the GPU. Resume it afterwards:

```sh
mise run hub:embed pause     # stop the build, then its daemon; Ollama keeps running
mise run hub:embed status    # units, progress of the current run, PAUSED marker, GPU
mise run hub:embed resume    # restart both units, then check the run picked up where it stopped
```

- **Pausing keeps the work.** The daemon writes each document's vectors to
  `vectors.db` as it finishes. A plain `index embeddings build` (no
  `--full-rebuild`) fills only what the building generation is missing, so a
  pause loses at most the batch in flight. The real binary has done this once:
  on 2026-11-27 a `timeout`-killed benchmark build and its daemon were stopped,
  a new daemon and build started on the same data dir, and generation 1
  carried on with a total 945 chunks smaller. The first live `hub:embed`
  pause and resume is the first test of the task itself on the real build.

- **The daemon does the embedding.** `index embeddings build` proxies to the
  daemon over HTTP, and the daemon holds `vectors.write.lock` and the Ollama
  connection. So pause works whenever the daemon is up, even if the build unit
  has died, and stops both units. Reads in the task set `AGENTSVIEW_NO_DAEMON=1`,
  so they never start a stray daemon, and a successful read proves the unit's
  daemon answers.

- **Pause** writes `embed-pause.json` to the data dir: the agentsview version,
  the generations and the last progress line. It takes a reflink snapshot of
  `vectors.db` and its `-wal` and `-shm` files into `paused-snapshot/`, and
  appends an `EMBED-PAUSED` line to `build-progress.log`. It refuses when the
  running units belong to another data dir, because the unit names are global.
  It warns if anything still holds `vectors.db` after the stop. A lock file
  keeps two pause or resume runs on one data dir apart.

- **Resume refuses** in three cases:
  - the agentsview version changed;
  - a known generation is gone or has a new fingerprint;
  - a new generation appeared. A config change cuts a new one and keeps the
    old one listed.

  It starts the build without `--yes`, so a build that wants to confirm a full
  rebuild fails instead. It then checks the new run's first progress line. If
  more than half of the already-done chunks (or 1% of the total) reappear, it
  treats the index as reset. Every refusal or failed start stops both units and
  keeps the pause state, so you can retry `resume`. A build that exits 0 at
  once means nothing was left to embed, and resume reports it as done.

- **Restore from the snapshot** only after a refusal says the index reset.
  With both units stopped, copy the three `paused-snapshot/vectors.db*` files
  back over the data dir's, then run `resume` again.

- **The watchdog comes from the drop-in when one exists.** A drop-in under
  `~/.config/systemd/user/<unit>.service.d/` overrides `--runtime-max` for the
  restarted unit, and resume prints the effective value.

- **systemd expands `${NAME}` in `ExecStart`.** It emptied the first build's
  `${PIPESTATUS[0]}`, so that run's final `BUILD-EXIT` line has no exit code;
  judge it by chunks done against the total. Units that resume starts record
  the code through `pipefail` and `$rc`.

- **Rehearse it without the GPU.** `EMBED_UNIT_PREFIX` and `EMBED_AV` point the
  remote script, `assets/agentsview/embed-build.sh`, at throwaway units and a
  stub `agentsview`.

## Watching upstream

A read-only cloud scheduled task checks #3437 and new releases every 6 hours.
Cloud sessions block GitHub GraphQL, so it uses `gh api` (REST), and it only gets
API access because `kenn-io/agentsview` is attached as its source repo.

[cfg]: https://github.com/kenn-io/agentsview/blob/v0.48.0/docs/configuration.md
[cfg-remote]: https://github.com/kenn-io/agentsview/blob/v0.48.0/docs/configuration.md#remote-hosts
[cfg-disable]: https://github.com/kenn-io/agentsview/blob/v0.48.0/docs/configuration.md#disabling-session-providers
[cfg-homes]: https://github.com/kenn-io/agentsview/blob/v0.48.0/docs/configuration.md#alternate-agent-homes
[ss]: https://github.com/kenn-io/agentsview/blob/v0.48.0/docs/semantic-search.md
[ssi]: https://github.com/kenn-io/agentsview/blob/v0.48.0/docs/semantic-search-internals.md
[ssi-storage]: https://github.com/kenn-io/agentsview/blob/v0.48.0/docs/semantic-search-internals.md#storage-layout
[ssi-mirror]: https://github.com/kenn-io/agentsview/blob/v0.48.0/docs/semantic-search-internals.md#mirror-schema-versioning
[ssi-gen]: https://github.com/kenn-io/agentsview/blob/v0.48.0/docs/semantic-search-internals.md#generations-and-fingerprints
[src-homes]: https://github.com/kenn-io/agentsview/blob/v0.48.0/internal/config/agent_directories.go#L60
[src-delta]: https://github.com/kenn-io/agentsview/blob/v0.48.0/internal/remotesync/prepare.go#L636
[src-failed]: https://github.com/kenn-io/agentsview/blob/v0.48.0/cmd/agentsview/sync.go#L591
[i3437]: https://github.com/kenn-io/agentsview/issues/3437
