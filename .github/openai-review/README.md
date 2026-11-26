# Manual PR review (OpenAI or Claude)

Two `workflow_dispatch`-only workflows send one PR's GitHub file patches to a
model API for an advisory review. They share `review.py`:

| Workflow | Provider | Environment | Secret |
| --- | --- | --- | --- |
| `openai-pr-review.yml` | OpenAI Responses API | `openai-pr-review` | `OPENAI_REVIEW_API_KEY` |
| `claude-pr-review.yml` | Claude Messages API | `claude-pr-review` | `ANTHROPIC_REVIEW_API_KEY` |

Each one sends patch data to a third party, so enable only the ones you intend
to use.

## Setup

After merging to the default branch, create the GitHub Actions environment for
each workflow you use. Restrict deployments to the default **branch** only, add
no tag rules, and disable bypass where your plan allows. Add the secret to that
environment only. Do not add a repository-wide secret. Anyone who can modify
the trusted branch can ultimately change workflows on that branch, so keep its
protection and review policies in place.

For Claude, create the API key in a dedicated Claude Console workspace, so the
key is scoped to one workspace. The reviewer sends the key as
`Authorization: Bearer <key>` with `anthropic-version: 2026-10-21`. It does
not send `anthropic-workspace-id`. The
[API overview](https://platform.claude.com/docs/en/api/overview#authentication)
says that header is required for a key that isn't scoped to a workspace, so an
unscoped key fails with an HTTP error.

## Running

In **Actions → OpenAI PR review** (or **Claude PR review**) **→ Run workflow**,
leave the default branch selected and enter an open PR number. Only GitHub file
patches (with paths) are sent. There is no PR checkout or PR code execution,
no comments, no merge approval, and no automatic runs. A review appears in the
run summary with the head it reviewed. The advisory result does not replace
any required review.

If the repository is private, the run refuses to send patches unless you tick
**allow_private_diff** for that run. The workflow passes the repository's
visibility to the script. If the visibility is missing, the script refuses to
send patches.

## Data and cost

- **OpenAI:** one Responses API request with storage disabled.
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
