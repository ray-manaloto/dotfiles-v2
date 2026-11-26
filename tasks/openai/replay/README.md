# Historical review replay

The [retrospective](retro.md) describes each phase. The [report](report.md)
contains the historical results.

Private repository inputs require an explicit `--allow-private` on
`prepare`, `submit`, and live `finish` and `regrade`. It authorizes sending
private patches, findings, and follow-up excerpts to OpenAI during the
applicable phases. Cached patch inputs with unknown visibility require the
same opt-in.

Cache-only reproduction uses existing local receipts and needs no provider
credentials or private-repository opt-in.

```sh
mise run openai:replay:run --cached-only regrade
mise run openai:replay:run --cached-only finish
```
