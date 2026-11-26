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
run summary. OpenAI also triages candidates and posts the result to the PR.
Neither review job checks out or executes PR code. Required reviews still apply.

If the repository is private, the run refuses to send patches unless you tick
**allow_private_diff** for that run. The script checks both the visibility in
the dispatch event and the current visibility GitHub returns with the PR, so a
re-run after the repository turns private is still gated. If either value is
missing, the script refuses to send patches.

## Data and cost

- **OpenAI:** one Responses API request with storage disabled and up to one
  Decisions API request for the candidates in the diff.
- **Claude:** one Messages API request. It does not use Claude Code Action or
  an OpenAI fallback.
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
- **Size limits:** oversized, unavailable or incomplete patches fail rather
  than being silently truncated.
