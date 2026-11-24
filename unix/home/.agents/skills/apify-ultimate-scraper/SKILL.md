---
name: apify-ultimate-scraper
description: >-
  Use when extracting structured web data across social, search, maps,
  e-commerce, and general sites via `apify` (`npm:apify-cli`) through scoped
  `fnox` research proxy or `pc` gateway.
---

# Apify CLI (`apify-cli`) Workflow

Run Apify Actors via the `apify` CLI instead of stdio/remote MCP servers.

## Authentication & Network Transport

- **macOS host (`fnox` Arch B strict research profile):**
  `fnox` injects `APIFY_TOKEN` onto `api.apify.com` and exports `HTTPS_PROXY`, `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, and `NODE_USE_ENV_PROXY=1`. Never strip `NODE_EXTRA_CA_CERTS` on Node 26 CLIs.

  ```sh
  fnox --non-interactive -c ~/.config/fnox/research.toml -P research --no-defaults proxy run -- \
    apify actors search "google maps" --json --limit 10
  ```

- **Guests / VM (`2–4`) over Tailscale SOCKS/HTTP proxy or `pc` gateway:**
  Keep `APIFY_TOKEN` off guests. Route through the `pc` gateway URL when configured, or pass Tailscale proxy explicitly without `ALL_PROXY`:

  ```sh
  env -u ALL_PROXY -u all_proxy HTTPS_PROXY=http://127.0.0.1:1055 NODE_USE_ENV_PROXY=1 \
    apify datasets get-items "$DATASET_ID" --format json
  ```

## Standard Workflow

1. Pass `--json` on `actors` commands (`--format json` on `datasets get-items`) and `--user-agent apify-agent-skills/apify-ultimate-scraper`; redirect stderr (`2>/dev/null`) when parsing JSON.
2. Search and inspect input schema before calling an Actor:

```sh
fnox --non-interactive -c ~/.config/fnox/research.toml -P research --no-defaults proxy run -- \
  apify actors search "<keywords>" --user-agent apify-agent-skills/apify-ultimate-scraper --json --limit 10 2>/dev/null

fnox --non-interactive -c ~/.config/fnox/research.toml -P research --no-defaults proxy run -- \
  apify actors info "<actor-id>" --user-agent apify-agent-skills/apify-ultimate-scraper --input 2>/dev/null
```

3. Execute synchronously with `--input` / `--input-file` and pull dataset rows:

```sh
fnox --non-interactive -c ~/.config/fnox/research.toml -P research --no-defaults proxy run -- \
  apify actors call "<actor-id>" --input-file input.json \
  --user-agent apify-agent-skills/apify-ultimate-scraper --json 2>/dev/null

fnox --non-interactive -c ~/.config/fnox/research.toml -P research --no-defaults proxy run -- \
  apify datasets get-items "<dataset-id>" \
  --user-agent apify-agent-skills/apify-ultimate-scraper --format json
```
