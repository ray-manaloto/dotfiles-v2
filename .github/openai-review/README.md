# PR review workflows

These workflows share `review.py` to send one PR's GitHub file patches to a
model API for an advisory review. See the [OpenAI pipeline runbook](../../docs/openai-pr-pipeline.md)
for automatic reviews, triage, autofix, and environment setup.

| Workflow | Provider | Environment | Secret |
| --- | --- | --- | --- |
| `openai-pr-review.yml` | OpenAI Responses API | `openai-pr-review` | `OPENAI_REVIEW_API_KEY` |
| `claude-pr-review.yml` | Claude Messages API | `claude-pr-review` | `ANTHROPIC_REVIEW_API_KEY` |

Each one sends patch data to a third party, so enable only the ones you intend
to use.

## Setup

Create the GitHub Actions environment for each workflow you use. Restrict the
Claude environment to the default branch and both OpenAI environments to the
`main` branch before adding credentials. Store the secrets in their
environments. Keep branch protection and review in place.

For Claude, create the API key in a dedicated Claude Console workspace, so the
key is scoped to one workspace. The reviewer sends the key as
`Authorization: Bearer <key>` with `anthropic-version: 2026-10-21`. It does
not send `anthropic-workspace-id`. The
[API overview](https://platform.claude.com/docs/en/api/overview#authentication)
says that header is required for a key that isn't scoped to a workspace, so an
unscoped key fails with an HTTP error.

## Running

In **Actions → OpenAI PR review** (or **Claude PR review**) **→ Run workflow**,
leave the default branch selected and enter an open PR number. Both reviewers
send GitHub file patches with paths and write the exact reviewed head in the
run summary. The OpenAI workflow posts candidates that pass triage with
defect probability of at least 0.85 and severity of at least 1.0. The job
summary and artifact include a shadow range from 0.0 up to 1.0 as described
in the runbook.
Neither review job checks out or executes PR code. Required reviews still apply.

## Sync to knowledge-base-v2

Edit shared scripts, schemas, the prompt, and their tests here. Run
`mise run openai-review:sync-kb <kb-checkout>` with the root of a linked KB
worktree, then run KB's checks before committing the mirror. KB keeps its
own `repo.json`, workflows, and Responses generator.

`mise run openai-review:check-kb-sync` compares the shared files with KB main
using read-only GitHub API requests. It prints a skip reason when KB is
unreadable. The check is for local use.

## Private review

If the repository is private, the run refuses to send patches unless you tick
**allow_private_diff** for that run. The script checks both the visibility in
the dispatch event and the current visibility GitHub returns with the PR, so a
re-run after the repository turns private is still gated. If either value is
missing, the script refuses to send patches.

## Data and cost

- **OpenAI:** one Responses API request with storage disabled per patch
  chunk (at most four), and up to one Decisions API request per chunk for the
  candidates in that chunk.
- **Claude:** one Messages API request per patch chunk (at most four). It does
  not use Claude Code Action or an OpenAI fallback.
- **Cost:** neither workflow checks credit eligibility or balance, and neither
  guarantees a free run. Check the provider's billing settings before running:
  purchased credits or auto-reload can keep usage going after any included
  credits run out.
- **Sensitive content:** review only material you are permitted to send. The
  path and content checks block common credential files and token formats,
  such as `.netrc`, `.npmrc`, `dot_*`/`private_dot_*` sources, `npm_`, `glpat-`,
  `AIza` and `*token=` literals. They are a precaution, not a guarantee. A
  matching patch fails closed, with no partial send. A secret-named directory
  or a script or doc about secrets (`tasks/secret/`, `agent-secrets.sh`) is
  still reviewed, but its text is scanned. Edits near the fake-secret fixtures in
  `test_review.py` always fail closed, so they need a human review.
- **Size limits:** the reviewer skips unavailable and incomplete patches, and
  patches over four times the 24,000-byte chunk budget as `oversized`. It
  packs files in GitHub file order into at most four review requests of up to
  24,000 serialized bytes each. It splits a file too large for one request at
  line boundaries into hunks with recomputed headers, so a large new module
  is reviewed in parts. The first part fills the room left in the current
  request, and each part carries a `part` field such as `2/3` that tells the
  reviewer and triage it is a slice. A cut never separates removed lines from
  the added lines that follow them when both fit. A file that fits one request
  but not the remaining budget stops packing, and the rest are listed as
  `over-budget`. A split file that does not fit, or has one line too long to
  split, is skipped alone. The comment starts with **Not reviewed** and names
  the largest skipped changes when a file is `oversized` or `over-budget`, or
  has 20 or more changed lines and is `missing-patch` or `sensitive-text`, so
  a "no findings" result is not read as a clean review. When no files fit or
  the file-count cap is exceeded, the run reports the reason and succeeds
  without a findings artifact.
