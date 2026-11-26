# Historical review replay

The [retrospective](retro.md) describes each phase. The [report](report.md)
contains the historical results.

The live `prepare`, `submit`, `evidence`, `finish`, and `regrade` phases
check current repository visibility through read-only GitHub API requests.
Pass `--allow-private` if the replay includes any private repository or any
repository with unverified visibility. For phases that send data to OpenAI,
the flag authorizes sending the corresponding patches and evidence.

Cache-only reproduction uses locally saved visibility and receipts without
provider credentials. It still requires `--allow-private` when any saved row
is private or has unverified visibility, as in the historical cache.

```sh
mise run openai:replay:run --allow-private --cached-only regrade
mise run openai:replay:run --allow-private --cached-only finish
```

New Decisions and independent receipts include a fingerprint of the exact
request payload. Unfingerprinted historical receipts are usable only in
cache-only mode. If a payload changes, run the applicable live phase with
`--retry-reserved` to authorize a new reservation under its existing cap.
Previous reservations still count against the cap. Independent summaries
snapshot the proposed and baseline thresholds for report reproduction.
