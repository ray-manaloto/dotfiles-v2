---
name: firecrawl
description: >-
  Use when fetching, scraping, searching, mapping, or crawling web pages cleanly
  via `webclaw` (free local tier), `firecrawl` (`npm:firecrawl-cli` via `fnox`
  research proxy or `pc` gateway), and `agent-browser` without Firecrawl MCP bloat.
---

# Web Extraction & Search (`webclaw` -> `firecrawl` -> `agent-browser`)

Follow the 3-tier CLI pipeline with zero global MCPs:

1. **Tier 0 (Fast local TLS fetch, 0 keys, 0 credits): `webclaw`**
   Use `webclaw` first for static docs, changelogs, and `llms.txt`:

   ```sh
   webclaw "https://mise.jdx.dev/llms.txt" --format markdown
   ```

2. **Tier 1 (JS rendering, search, site map, crawl, PDF/Office): `firecrawl` (`1.30.0`)**
   - On macOS host: invoke via scoped `fnox` research profile (keeps `NODE_EXTRA_CA_CERTS` and `NODE_USE_ENV_PROXY=1` intact for Node 26):

   ```sh
   fnox --non-interactive -c ~/.config/fnox/research.toml -P research --no-defaults proxy run -- \
     firecrawl search "mise bootstrap dotfiles" --sources web --json

   fnox --non-interactive -c ~/.config/fnox/research.toml -P research --no-defaults proxy run -- \
     firecrawl scrape "https://docs.firecrawl.dev/quickstarts/firecrawl-local" --format markdown
   ```

   - Always pass `--sources web` on `firecrawl search` unless `research` or `developer` index is explicitly intended.
   - Save large payloads with `-o <tmp-file>` and inspect slices (`head`/`grep`) instead of dumping whole pages into context.
   - On guests (`2–4`) over Tailscale `:1055` or `pc` `firecrawl-local` gateway:

   ```sh
   env -u ALL_PROXY -u all_proxy HTTPS_PROXY=http://127.0.0.1:1055 NODE_USE_ENV_PROXY=1 \
     FIRECRAWL_API_URL="$FIRECRAWL_API_URL" firecrawl scrape "<url>" --format markdown
   ```

3. **Tier 2 (Interactive browser automation / multi-step UI): `agent-browser`**
   Use `agent-browser` (`0.44.0`) only when CLI scraping cannot handle interactive login/DOM state (`--cdp` or headless Chrome where supported; not inside seccomp-blocked Linux containers).
