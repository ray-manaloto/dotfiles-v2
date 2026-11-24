---
name: agentsview-finding-history
description: >-
  Use when searching past Antigravity, Codex, Claude, and OpenCode sessions via
  the `agentsview` CLI before guessing about prior decisions, rationale,
  pitfalls, or repeated workflows.
---

# Finding Conversation History (`agentsview` CLI)

Search archived session transcripts via the local `agentsview` CLI (or `pc` remote server) with zero always-on MCP schema overhead.

## Quick Start

1. Check local or remote daemon health and pick the server target cleanly:
   - Local Mac host (`127.0.0.1:8080`): pass `--server http://127.0.0.1:8080 --server-token-file "$HOME/.agentsview/native-server-token"` (omitting `--server` searches local SQLite directly).
   - Remote `pc` hub / guests (when `pc` Postgres replica/Tailscale serve is configured): pass `--server "$AGENTSVIEW_SERVER"` and `--server-token-file "$HOME/.agentsview/remote-token"`.
2. Search with `--include-one-shot --include-automated` so automated/swarm turns are never hidden:

```sh
agentsview session search "<concept query>" \
  --include-one-shot --include-automated --context 2 --json --limit 10 \
  --server http://127.0.0.1:8080 \
  --server-token-file "$HOME/.agentsview/native-server-token"

agentsview session search "<exact symbol or error>" \
  --in tool_input,tool_result --include-one-shot --include-automated \
  --include-children --exclude-session "<current-session-id>" --json --limit 10 \
  --server http://127.0.0.1:8080 \
  --server-token-file "$HOME/.agentsview/native-server-token"
```

3. Read full context around matching ordinals before citing any claim:

```sh
agentsview session messages <session-id> \
  --around <ordinal> --before 8 --after 8 --role user,assistant --json \
  --server http://127.0.0.1:8080 \
  --server-token-file "$HOME/.agentsview/native-server-token"
```

4. Check structured session list or recall briefs:

```sh
agentsview session list --limit 20 --json
agentsview recall query "<topic>" --json
```

## Guardrails

- Cite project, date, session ID, and ordinal range only from messages actually read via `agentsview session messages` or `session get`.
- Distinguish user decisions from unaccepted assistant proposals; newer entries govern when they supersede earlier decisions.
- Never synthesize or drop `--server` silently if an authenticated remote archive rejected credentials — report the auth failure explicitly.
- Keep host-specific paths (`$HOME`, `~`) portable in repo outputs; avoid exposing raw tokens.
