# Local OpenAI Responses coding profile

Run `mise deps uv` in the dotfiles checkout to install the locked Python environment. The
OpenAI Agents SDK drives the local tool loop using the general-purpose `gpt-6.2-sol`
model at `https://api.openai.com/v1/responses`.
[OpenAI model reference](https://developers.openai.com/api/docs/models/gpt-6.2-sol)

The lock pins `openai-agents==0.25.0`, which provides the Responses adapter and
run hooks used here. The newer release was within uv's seven-day release quarantine
when the lock was generated. Both the Agents SDK and the OpenAI client declare
`httpx2`, which the runner uses to supply its scoped proxy transport.

## Dry run

Put the task instructions in a local UTF-8 text file. From the dotfiles checkout, pass a
clean, detached, linked Git worktree root and the exact tracked files you approve
for reading and editing. The primary checkout and repository control files such as
`mise.toml`, `hk.pkl`, `.gitattributes`, and `.gitmodules` are refused.
This example uses a disposable worktree of hk-config.

```sh
mise run api:code --dry-run \
  --worktree ../hk-config-pilot \
  --prompt-file ./pilot-task.txt \
  --files .github/workflows/claude-pr-review.yml,.github/workflows/openai-pr-review.yml
```

The command validates the scope and emits JSON with `RET_DRY_RUN`, the target
commit, and the run limits.
Dry runs need no API credentials or network access and do not edit the approved files.

## Live run

Use a dedicated `OPENAI_CODING_API_KEY` in Doppler for the project in the linked API
organization. The task invokes the checkout's strict-egress fnox overlay, which injects
the coding credential only for `api.openai.com`.

After owner approval, verify the linked organization's current promotional balance covers
the run and confirm its billing settings preclude paid fallback. OpenAI uses promotional
credits before purchased API credits. The local runner cannot enforce that billing policy.
[OpenAI linked credit billing](https://help.openai.com/en/articles/20001626-api-credits-benefit-for-eligible-chatgpt-subscriptions)

Replace `--dry-run` with `--credit-confirmed` only after those checks. Each run allows up
to 10 model requests, 2,048 output tokens per request, 4,096 output tokens across
the entire run, nine tool calls,
and 120 seconds in the agent loop. External SDK tracing and Responses storage are
disabled. The usage log under `${XDG_STATE_HOME:-~/.local/state}/dotfiles-v2/responses-agent/`
records the model, elapsed time, and reported token/request counts.

The model can read or replace text in approved files, run offline pedantic zizmor
on approved workflows, and run a fixed offline Python unittest command for the
hk-config reviewer suite. The runner verifies both test files against their
reviewed content hashes before copying them from the target commit into an
isolated temporary directory.

The live JSON response contains paths to the Git patch, approved command results,
and final model summary in a private per-run state directory. The summary is
available when the model returns a final response. Use the patch and command
results for human review. Changes are local and may be partial if a run hits a
limit or errors.
