# OpenAI PR review and autofix

The pipeline reviews same-repository public PRs targeting `main` when they
open, reopen, or receive a push. Documentation-only changes and fork PRs
skip automatic review. A new head cancels the previous review. Manual review
remains available from the default branch, including explicit private opt-in.

## Review and triage

The review job runs the base revision's offline tests and reads the PR's
file patches through GitHub. It checks both the event and live PR, and rejects
incomplete patches and recognized sensitive paths or credentials. PR bodies
and commit messages are excluded from model input. The round-two commit trailer
is read locally for loop control. Repository instructions appearing in a patch
are treated as data.

The Responses API produces up to 16 candidates with a file, line, severity,
claim and suggested fix. The reviewer discards locations outside the new
lines in the diff. Decisions receives the candidates and the same patch
evidence. A candidate passes at a defect probability of at least 0.85 and a
severity score of at least 1.0 on the zero-based low-to-critical scale.
The `findings.json` artifact records the PR, base, head, and accepted findings.
A separate job without an OpenAI key posts sanitized text if the PR still
matches. Review is advisory and does not change required checks.

## Autofix

A collaborator with write access can add the `autofix` label after a successful
automatic review of the current head and base. The workflow reserves up to
two rounds per PR with bot-authored reservation comments. A failed attempt
consumes its reservation. A pushed round-two commit skips automatic review
while that commit is the head. The workflow removes the label and reports
whether it pushed, left an empty patch, or stopped.

Codex receives the triaged findings and works on the exact PR head with a
read-only GitHub token. A separate job applies the artifact and runs hk and
the tests without an OpenAI key or push token. The publishing job reapplies
the same artifact from a fresh checkout and checks the live PR before a
normal fast-forward push. It never runs candidate code.

The allowlist covers modifications to existing text Python files in
`unix/home/.config/mise/lib/` and `tests/`. It excludes generated code,
sensitive paths and credential-like text. Git mode changes, binary edits,
file creation, deletion and renames are rejected. Review and merge the
result normally.

## Owner setup

- In `openai-pr-review`, set `OPENAI_REVIEW_API_KEY`. In `openai-pr-fix`, set
  `OPENAI_FIX_API_KEY` for Codex. Scope each key to its intended OpenAI project.
- Keep environment approvals and branch protections appropriate to the
  repository. Automatic jobs are triggered by same-repo `pull_request` events.
  For selected deployment branches, include `refs/pull/*/merge` and allow
  `main` in the review environment for manual dispatch. These rules allow PR
  workflow definitions from collaborators who can push to the repository.
  Keep that access limited to trusted collaborators. The secret-consuming
  review code and publishing validator are loaded from the PR base.
  [GitHub deployment branch rules](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments#deployment-branches-and-tags)
- Install a GitHub App on this repository with **Contents: Read and write**.
  Set the App's client ID as `AUTOFIX_APP_ID` and its private key as
  `AUTOFIX_APP_PRIVATE_KEY` in `openai-pr-fix`.
  The publisher mints a single-repository token so that its push triggers
  PR CI. If both App credentials are absent, the publisher uses
  `GITHUB_TOKEN` and dispatches `ci.yml` against the PR branch. That workflow
  supplies both required checks. The fallback requires the branch's CI workflow
  to match the base before pushing. If dispatch fails after a successful push,
  run CI for the new branch head from Actions or approve the queued PR runs.
  [GitHub workflow event behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow#triggering-a-workflow-from-a-workflow)

## Cost and limits

| Stage | Standard rates per million tokens | Per-run limits |
| --- | --- | --- |
| Review, GPT-6.2 Sol | $2 input, $0.10 cached input, $10 output | One request, 30 files and 24,000 bytes of patch input, 6,000 output tokens including reasoning |
| Decisions, GPT-6 Luna | $0.10 uncached input, no output charge | At most one request, 64,000 bytes for the full request, 16 candidates and 32 questions |
| Autofix, GPT-6.2 Sol | $2 input, $0.10 cached input, $10 output at short context | One Codex invocation per reservation, 15 minutes for generation, at most two reservations per PR |

The review output ceiling is $0.06 at Standard rates, plus actual input cost.
Byte limits bound request size. Use provider token usage for billing because
byte count and token count differ. GPT-6.2 Sol has higher per-token prices
above 272,000 input tokens for a request. The Codex action has no run-wide
token cap. The timeout and reservation cap limit attempts while the job may
make multiple model calls. Measure usage and check the credit balance before
expanding rollout. [OpenAI pricing](https://developers.openai.com/api/docs/pricing)
[GPT-6.2 Sol](https://developers.openai.com/api/docs/models/gpt-6.2-sol)
[Decisions](https://developers.openai.com/api/docs/guides/decisions#pricing)

## Upstream reuse

- The OpenAI Codex Action supplies the workspace sandbox, write-access
  check, API-key proxy and PR-bot pattern for separating comments.
  [codex-action v1.14](https://github.com/openai/codex-action/tree/v1.14)
- The Codex issue deduplicator supplied the structured output pattern.
  [Issue deduplicator workflow](https://github.com/openai/codex/blob/main/.github/workflows/issue-deduplicator.yml)
- The OpenAI Autofix CI example supplied the patch-artifact handoff.
  [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode#example-autofix-ci-failures-in-github-actions)
- PR-Agent's authorization gate informed the explicit permission check
  before reserving a powered run.
  [PR-Agent identity provider](https://github.com/qodo-ai/pr-agent/tree/main/pr_agent/identity_providers)

## Rollout after merge

The lead runs the first three measured runs and records model usage, review
outcomes, the exact CI head, and whether the App or dispatch path ran.
Once those results are accepted, copy the workflow and reviewer directory
to `knowledge-base-v2`, then apply them to the drafts at `hk-config#5`,
`renovate-config#7`, `skills#5`, and `cpp-core#36`. In each repo, adapt the
default branch, patch allowlist, setup and test commands, dispatchable CI
workflows, and owner-managed environments. Reuse the schema, Decisions
thresholds, patch validation, and separate generation, validation and push
jobs. Keep merge human-controlled.
