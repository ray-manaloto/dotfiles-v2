# OpenAI PR review and autofix

The pipeline reviews same-repository public PRs targeting `main` when they
open, reopen, or receive a push. Documentation-only changes and fork PRs
skip automatic review. A new head cancels the previous review. Manual review
remains available from the default branch, including explicit private opt-in.
GitHub loads both automatic workflows from the trusted base on `main` through
`pull_request_target`.

## Review and triage

The review job runs the base revision's offline tests and reads the PR's
file patches through GitHub. It checks both the event and live PR, and rejects
incomplete patches and recognized sensitive paths or credentials. PR bodies
and commit messages are excluded from model input. Repository instructions
appearing in a patch are treated as data.

The Responses API produces up to 16 candidates with a file, line, severity,
claim and suggested fix. The reviewer discards locations outside the new
lines in the diff. Decisions receives the candidates and the same patch
evidence. A candidate passes at a defect probability of at least 0.85 and a
severity score of at least 1.0 on the zero-based low-to-critical scale.
The `findings.json` artifact records the PR, base, head, and accepted findings.
When triage is unavailable or the review declines, the job succeeds and
writes a short `not reviewed` reason without a findings artifact. A separate
job without an OpenAI key posts the reason if the PR is still open at the
intended head. API and configuration errors outside the triage fallback
still fail the review job.

## Shadow calibration

During the shadow period, candidates with defect probability of at least
0.85 and a severity score from 0.0 up to but excluding 1.0 appear only in
the `shadow` array in `findings.json` and in the job step summary. Each entry
contains the path, line, and one-line title. PR comments and autofix consume
only the posted findings.

After validating the volume, change `MIN_SEVERITY = 1.0` to
`MIN_SEVERITY = 0.0` in `.github/openai-review/triage.py` to promote the
shadow range to normal triage.

## Autofix

A collaborator with write access can add the `autofix` label after a successful
automatic review of the current head and base. The workflow reserves up to
two rounds per PR with bot-authored reservation comments. A failed attempt
consumes its reservation. An eligible push can trigger an advisory review
after the last round. The two-round limit applies to autofix attempts. The
workflow removes the label and reports whether it pushed, left an empty
patch, or stopped.

Codex receives triaged findings and up to 64,000 bytes of credential-scanned
source from allowlisted files at the exact PR head. The source is read as Git
blobs. Codex starts in an empty workspace with sudo disabled and returns a
patch. The read-only verifier checks out the candidate, applies the patch
artifact and runs hk and the tests without an OpenAI key or push token.
The publisher reapplies the same artifact in a bare Git repository and checks
the live PR before a normal fast-forward push.

The allowlist covers modifications to existing text Python files in
`unix/home/.config/mise/lib/` and `tests/`. It excludes generated code,
sensitive paths and credential-like text. It also excludes Python files in
test fixture directories and the import hooks `conftest.py`, `__init__.py`
and `tasks_py_loader.py`. Git mode changes, binary edits, file creation,
deletion and renames are rejected. Review and merge the result normally.

## Owner setup

- In `openai-pr-review`, set `OPENAI_REVIEW_API_KEY`. In `openai-pr-fix`, set
  `OPENAI_FIX_API_KEY` for Codex. Scope each key to its intended OpenAI project.
- Under **Selected branches and tags**, allow only the `main` branch for
  both `openai-pr-review` and `openai-pr-fix`. Set these deployment rules
  before adding credentials. Keep branch protections and environment approvals.
  [GitHub deployment branch rules](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments#deployment-branches-and-tags)
- Beginning December 2, 2026, GitHub enforces its default
  `pull_request_target` block for affected public repositories. The active
  event policy for this repository covers only
  `.github/workflows/openai-pr-review.yml` and
  `.github/workflows/openai-pr-autofix.yml`. It allows
  `pull_request_target` and `workflow_dispatch`. Apply the same scope when
  rolling this pipeline out to another affected public repository.
  [GitHub default event policy](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target#default-policy-for-pull_request_target)
  [Repository Actions policy API](https://docs.github.com/en/rest/actions/policies#create-a-repository-actions-policy)
- Install the `ray-manaloto-autofix` GitHub App on this repository with
  **Contents: Read and write**. Set the App's client ID in the repository
  variable `AUTOFIX_APP_CLIENT_ID` and its private key in the
  `openai-pr-fix` environment secret `AUTOFIX_APP_PRIVATE_KEY`.
  The publisher mints a single-repository token so that its push triggers
  PR CI. If both App credentials are absent, the publisher uses
  `GITHUB_TOKEN` and dispatches `ci.yml` against the PR branch. That workflow
  supplies both required checks, **Lint, Gates, and Tests** and **Bootstrap
  Convergence**. The fallback requires the branch's CI workflow
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
