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
