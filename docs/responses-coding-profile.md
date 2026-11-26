# Local OpenAI Responses coding profile

Run `mise deps uv` in the dotfiles checkout to install the locked Python environment. The
OpenAI Agents SDK drives the local tool loop using the general-purpose `gpt-6.2-sol`
model at `https://api.openai.com/v1/responses`.
[OpenAI model reference](https://developers.openai.com/api/docs/models/gpt-6.2-sol)

## Dry run

Put the task instructions in a local UTF-8 text file. From the dotfiles checkout, pass a
Git worktree root and the exact existing files you approve for reading and editing.
This example uses a disposable worktree of hk-config.

```sh
mise run api:code --dry-run \
  --worktree ../hk-config-pilot \
  --prompt-file ./pilot-task.txt \
  --files .github/workflows/claude-pr-review.yml,.github/workflows/openai-pr-review.yml
```

The command validates the scope and emits JSON with `RET_DRY_RUN` and the run limits.
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
to 10 model requests, 2,048 output tokens per request (20,480 total), nine tool calls,
and 120 seconds in the agent loop. External SDK tracing and Responses storage are
disabled. The usage log under `${XDG_STATE_HOME:-~/.local/state}/dotfiles-v2/responses-agent/`
records the model, elapsed time, and reported token/request counts.

The model can read or replace text in approved files and run offline pedantic zizmor
on approved workflows. Run any broader project tests yourself after reviewing the
worktree diff. Changes are local and may be partial if a run hits a limit or errors.
