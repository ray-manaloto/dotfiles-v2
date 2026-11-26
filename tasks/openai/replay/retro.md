# Review replay retrospective

GitHub exposed 161 merged PRs across the two repositories. The replay uses
the archived PR files view that production consumes and applies the production
file and patch limits. Oversized and otherwise declined PRs remain in the
coverage denominator.

The checkout needed a scoped fnox profile for the Doppler review credential.
GitHub fetches run outside that proxy. The OpenAI phases use the checkout
profile and restrict the key to the Batch, Files, Decisions, and Responses calls they
make. Responses requests disable storage. The upload and Batch result files
have separate 48-hour expirations.

A pre-submit estimate reserves the full review output allowance and a bounded
triage and grading pass for every possible finding. A durable reservation
blocks automatic retries after an ambiguous request. Pass `--retry-reserved`
to `submit` or `finish` to explicitly authorize another attempt. Each attempt
counts against the cap, including earlier reservations without usage receipts.
An ambiguous Batch submission may have created a separate billable batch.

The cost report derives from provider token counts. Decisions bills input
tokens, including cache reads and writes. Output tokens carry no charge.
[OpenAI Decisions pricing](https://developers.openai.com/api/docs/guides/decisions#pricing-and-availability)

## Reproduce

Run from a linked worktree with an active plan and a provisioned Python
environment. Each phase reuses the same plan's ignored replay cache.

1. `mise run openai:replay:run prepare` fetches PR inputs and estimates spend.
2. `mise run openai:replay:run submit` uploads the review requests and creates the batch.
3. `mise run openai:replay:run poll` retrieves the terminal Batch result.
4. `mise run openai:replay:run evidence` reads the merged file and later same-path changes.
5. `mise run openai:replay:run finish` runs production triage, grades the raw
   candidates, and generates the aggregate report.
6. `mise run openai:replay:run regrade` grades the proposed selection and a
   seeded sample of up to 30 rejects with GPT-6 Sol under a separate $10 cap.
   It uses cached Batch, triage, Luna grade, and follow-up evidence.
7. `mise run openai:replay:run --cached-only finish` rebuilds the aggregate
   report with the independent comparison and preserves the spot-check block.

Add `--cached-only` to `regrade` to rebuild the independent summary from
saved Sol responses. Both cached-only phases run without the credential proxy.

The later-change window uses up to 20 commits touching the same path, ordered
oldest first, from the default branch tip captured by the evidence phase.
GitHub filters that history by the merge timestamp. A rename or timestamp
anomaly can hide a fix. Later patch excerpts are limited to 220 characters.
The historical graders used the excerpts already cached at replay time. The model that labels the candidates also performs triage, so
independent human labels would be needed to establish calibration.
