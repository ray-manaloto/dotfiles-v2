"""Offline request and worktree receipts for the coding profile."""

import asyncio
import json
import logging
import os
import ssl
from hashlib import sha256
from pathlib import Path
from secrets import token_hex
from unittest.mock import Mock

import certifi
import httpx2
import pytest
from openai import APIStatusError

from dotfiles_py.generated.responses_agent_schema import CodingResponse
from responses_agent.cli import main
from responses_agent.runner import (
    ENDPOINT,
    KEY_NAME,
    MAX_OUTPUT_TOKENS,
    MAX_RUN_OUTPUT_TOKENS,
    MAX_TURNS,
    MODEL,
    PLACEHOLDER,
    CredentialError,
    OutputBudgetError,
    UsageTracker,
    coding_tools,
    enforce_route,
    proxy_client,
    run,
)
from responses_agent.worktree import ScopeError, git, open_worktree

WORKFLOW = ".github/workflows/review.yml"


@pytest.fixture
def checkout(tmp_path):
    primary = tmp_path / "primary"
    root = tmp_path / "checkout"
    primary.mkdir()
    git(primary, "init", "-q")
    path = primary / WORKFLOW
    path.parent.mkdir(parents=True)
    path.write_text("jobs:\n  review:\n    steps: []\n", encoding="utf-8")
    (primary / "private.txt").write_text("unapproved", encoding="utf-8")
    (primary / "mise.toml").write_text("# managed\n", encoding="utf-8")
    git(primary, "add", "--", WORKFLOW, "private.txt", "mise.toml")
    git(
        primary,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-qm",
        "fixture",
    )
    git(primary, "worktree", "add", "--detach", str(root), "HEAD")
    return root


def api_response(output, output_tokens=5):
    return {
        "id": "resp_fixture",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": MODEL,
        "output": [output],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 12,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 12 + output_tokens,
        },
    }


def tool_call(name, **arguments):
    return {
        "type": "function_call",
        "id": f"fc_{name}",
        "call_id": f"call_{name}",
        "name": name,
        "arguments": json.dumps(arguments),
        "status": "completed",
    }


def final_message(text):
    return {
        "type": "message",
        "id": "msg_fixture",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def test_sdk_posts_only_to_responses_and_applies_local_edit(checkout, monkeypatch):
    for name, value in {
        "OPENAI_API_KEY": "ordinary-fixture-key",
        "OPENAI_REVIEW_API_KEY": "review-fixture-key",
        "OPENAI_BASE_URL": "https://alternate.invalid/v1",
        "OPENAI_PROJECT_ID": "wrong-project",
        "OPENAI_ORG_ID": "wrong-org",
        "HTTPS_PROXY": "http://alternate.invalid:8000",
    }.items():
        monkeypatch.setenv(name, value)

    bodies = []
    scripted = iter(
        [
            tool_call("read_file", path=WORKFLOW),
            tool_call(
                "replace_text",
                path=WORKFLOW,
                old="    steps:",
                new="    name: Review\n    steps:",
            ),
            final_message("Updated."),
        ]
    )

    def respond(request):
        assert request.method == "POST"
        assert str(request.url) == ENDPOINT
        assert request.headers["Authorization"] == f"Bearer {PLACEHOLDER}"
        assert "OpenAI-Organization" not in request.headers
        assert "OpenAI-Project" not in request.headers
        body = json.loads(request.content)
        assert body["model"] == MODEL
        assert body["store"] is False
        assert body["parallel_tool_calls"] is False
        assert body["max_output_tokens"] == MAX_OUTPUT_TOKENS
        assert not body.get("background")
        assert {tool["name"] for tool in body["tools"]} == {
            "read_file",
            "replace_text",
            "lint_workflow",
            "test_reviewer",
        }
        bodies.append(body)
        return httpx2.Response(200, json=api_response(next(scripted)))

    tracker = UsageTracker()
    with open_worktree(checkout, [WORKFLOW], None) as worktree:
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        assert (
            asyncio.run(run(worktree, "Name the reviewer job.", client, tracker))
            == "Updated."
        )
    assert "name: Review" in (checkout / WORKFLOW).read_text()
    assert "steps: []" in bodies[1]["input"][-1]["output"]
    assert bodies[2]["input"][-1]["output"] == "Updated."
    usage = tracker.snapshot()
    assert usage is not None
    assert usage.model_dump() == {
        "requests": 3,
        "input_tokens": 36,
        "output_tokens": 15,
    }


@pytest.mark.parametrize(
    ("reported", "caps", "error"),
    [
        ([2040, 2040, 16], [2048, 2048, 16], "exhausted its total"),
        ([2049], [2048], "exceeded the requested"),
    ],
)
def test_run_enforces_aggregate_budget_before_another_request(
    checkout, reported, caps, error
):
    seen = []

    def respond(request):
        seen.append(json.loads(request.content)["max_output_tokens"])
        return httpx2.Response(
            200,
            json=api_response(
                tool_call("read_file", path=WORKFLOW), reported[len(seen) - 1]
            ),
        )

    with open_worktree(checkout, [WORKFLOW], None) as worktree:
        tracker = UsageTracker()
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        with pytest.raises(OutputBudgetError, match=error):
            asyncio.run(run(worktree, "Test the budget.", client, tracker))
    assert seen == caps
    assert tracker.output_tokens == sum(reported)


def test_rejects_provider_overrides_and_fails_without_fallback(checkout):
    for method, url in (
        ("POST", "https://alternate.invalid/v1/responses"),
        ("POST", "https://api.openai.com/v1/chat/completions"),
        ("GET", ENDPOINT),
        ("POST", ENDPOINT + "?override=1"),
    ):
        with pytest.raises(CredentialError, match="only sends POST"):
            asyncio.run(enforce_route(httpx2.Request(method, url)))
    with pytest.raises(CredentialError, match="dedicated fnox"):
        asyncio.run(enforce_route(httpx2.Request("POST", ENDPOINT)))

    respond = Mock(
        return_value=httpx2.Response(
            429, json={"error": {"code": "insufficient_quota", "message": "fixture"}}
        )
    )
    with open_worktree(checkout, [WORKFLOW], None) as worktree:
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        with pytest.raises(APIStatusError) as failure:
            asyncio.run(run(worktree, "Test failure.", client, UsageTracker()))
    assert failure.value.code == "insufficient_quota"
    assert respond.call_count == 1


def test_proxy_ignores_other_keys_and_requires_local_fnox(monkeypatch, tmp_path):
    with pytest.raises(CredentialError, match=KEY_NAME):
        proxy_client({"OPENAI_API_KEY": "fixture", "OPENAI_REVIEW_API_KEY": "fixture"})
    with pytest.raises(CredentialError, match=KEY_NAME):
        proxy_client({KEY_NAME: "raw-fixture-key"})
    proxy = f"http://user:{token_hex(8)}@127.0.0.1:8888"
    env = {
        KEY_NAME: PLACEHOLDER,
        "HTTPS_PROXY": proxy,
        "SSL_CERT_FILE": certifi.where(),
    }
    for overrides in (
        {"SSL_CERT_FILE": ""},
        {"SSL_CERT_FILE": str(tmp_path / "missing.pem")},
        {"HTTPS_PROXY": proxy.replace("127.0.0.1", "alternate.invalid")},
        {"HTTPS_PROXY": "http://127.0.0.1:8888"},
    ):
        with pytest.raises(CredentialError, match="proxy or TLS CA"):
            proxy_client(env | overrides)
    factory = Mock(wraps=httpx2.AsyncClient)
    monkeypatch.setattr(httpx2, "AsyncClient", factory)
    client = proxy_client(env)
    assert factory.call_args.kwargs == {
        "proxy": proxy,
        "verify": factory.call_args.kwargs["verify"],
        "trust_env": False,
        "follow_redirects": False,
    }
    assert isinstance(factory.call_args.kwargs["verify"], ssl.SSLContext)
    asyncio.run(client.aclose())


def test_waiting_for_provider_hits_wall_clock_bound(checkout, monkeypatch):
    monkeypatch.setattr("responses_agent.runner.TIMEOUT_SECONDS", 0.25)
    received = False

    async def respond(_request):
        nonlocal received
        received = True
        await asyncio.sleep(10)

    with open_worktree(checkout, [WORKFLOW], None) as worktree:
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        with pytest.raises(TimeoutError):
            asyncio.run(run(worktree, "Test timeout.", client, UsageTracker()))
    assert received


@pytest.mark.parametrize(
    "path", ["../outside.txt", "/etc/passwd", ".git/config", "a/../outside.txt"]
)
def test_rejects_outside_and_git_paths_at_configuration(checkout, path):
    with (
        pytest.raises(ScopeError, match="worktree-relative"),
        open_worktree(checkout, [path], None),
    ):
        pytest.fail("unsafe approval")


def test_only_clean_detached_linked_worktrees_and_tracked_noncontrol_files(checkout):
    with (
        pytest.raises(ScopeError, match="disposable linked"),
        open_worktree(checkout.parent / "primary", [WORKFLOW], None),
    ):
        pytest.fail("primary checkout")
    with (
        pytest.raises(ScopeError, match="control files"),
        open_worktree(checkout, ["mise.toml"], None),
    ):
        pytest.fail("repository control file")
    with (
        pytest.raises(ScopeError, match="tracked files"),
        open_worktree(checkout, ["missing.yml"], None),
    ):
        pytest.fail("untracked approval")
    dirty = checkout / "untracked"
    dirty.touch()
    with (
        pytest.raises(ScopeError, match="clean at the start"),
        open_worktree(checkout, [WORKFLOW], None),
    ):
        pytest.fail("dirty checkout")
    dirty.unlink()
    git(checkout, "switch", "-qc", "ordinary")
    with (
        pytest.raises(ScopeError, match="detached HEAD"),
        open_worktree(checkout, [WORKFLOW], None),
    ):
        pytest.fail("attached checkout")


@pytest.mark.parametrize("link", ["symlink", "directory symlink", "hardlink"])
def test_refuses_links_substituted_after_approval(checkout, tmp_path, link):
    outside = tmp_path / "review.yml"
    outside.write_text("outside approval")
    path = checkout / WORKFLOW
    with open_worktree(checkout, [WORKFLOW], None) as worktree:
        path.unlink()
        if link == "symlink":
            path.symlink_to(outside)
        elif link == "directory symlink":
            path.parent.rmdir()
            path.parent.symlink_to(tmp_path, target_is_directory=True)
        else:
            os.link(outside, path)
        with pytest.raises((ScopeError, OSError)):
            worktree.replace(WORKFLOW, "outside approval", "unexpected edit")
        with pytest.raises((ScopeError, OSError)):
            worktree.read(WORKFLOW)
    assert outside.read_text() == "outside approval"


def test_lint_uses_fixed_arguments_sanitized_environment_and_scope(
    checkout, tmp_path, monkeypatch
):
    monkeypatch.setenv(KEY_NAME, "must-not-reach-lint")
    binary = tmp_path / "zizmor"
    binary.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@"\nprintf "key-set=%s\\n" "${OPENAI_CODING_API_KEY+x}"\ncat\nexit 3\n'
    )
    binary.chmod(0o700)
    with open_worktree(checkout, [WORKFLOW, "private.txt"], binary) as worktree:

        async def exercise():
            tools = {tool.name: tool for tool in coding_tools(worktree)}
            assert "shell" not in tools
            lint = tools["lint_workflow"]
            assert (
                await lint.on_invoke_tool(
                    Mock(), json.dumps({"path": WORKFLOW, "command": "git push"})
                )
                == "Refused: invalid tool arguments."
            )
            assert "Refused" in await lint.on_invoke_tool(
                Mock(), '{"path":"private.txt"}'
            )
            edit = tools["replace_text"]
            assert "Refused" in await edit.on_invoke_tool(
                Mock(), '{"path":"../outside","old":"old","new":"new"}'
            )
            output = await lint.on_invoke_tool(Mock(), json.dumps({"path": WORKFLOW}))
            assert output == (
                f"zizmor ({WORKFLOW}): exit 3\n--offline\n--no-config\n--pedantic\n--strict-collection\n--\n-\n"
                "key-set=\njobs:\n  review:\n    steps: []\n"
            )
            for _ in range(MAX_TURNS - 5):
                assert "Refused" not in await tools["read_file"].on_invoke_tool(
                    Mock(), json.dumps({"path": WORKFLOW})
                )
            assert (
                await lint.on_invoke_tool(Mock(), json.dumps({"path": WORKFLOW}))
                == "Refused: tool call limit reached."
            )

        asyncio.run(exercise())


def test_cli_dry_run_and_live_opt_in(checkout, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv(KEY_NAME, raising=False)
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Name the reviewer job.")
    args = [
        "--worktree",
        str(checkout),
        "--prompt-file",
        str(prompt),
        "--files",
        WORKFLOW,
    ]
    original = (checkout / WORKFLOW).read_bytes()

    assert main([*args, "--dry-run"]) == 0
    response = CodingResponse.model_validate_json(capsys.readouterr().out)
    assert response.returns[0].code.value == "RET_DRY_RUN"
    assert response.usage is None and not response.errors
    assert response.artifacts is None
    assert response.target_commit == git(checkout, "rev-parse", "HEAD").decode().strip()
    assert main(args) == 1
    blocked = CodingResponse.model_validate_json(capsys.readouterr().out)
    assert blocked.errors[0].code.value == "ERR_PRECONDITION_FAILED"
    assert not state.exists()
    assert (checkout / WORKFLOW).read_bytes() == original


def test_cli_live_writes_artifacts_and_metadata_only_ledger(
    checkout, tmp_path, capsys, monkeypatch
):
    secret = "fixture-secret-unique"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv(KEY_NAME, PLACEHOLDER)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # dictConfig changes the process logging registry. Restore it after this call.
    for logger in [logging.root, *logging.root.manager.loggerDict.values()]:
        if isinstance(logger, logging.Logger):
            for field in ("handlers", "level", "disabled", "propagate"):
                monkeypatch.setattr(logger, field, getattr(logger, field))
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Fixture task contents.")
    binary = tmp_path / "zizmor"
    binary.write_text("#!/bin/sh\nprintf 'Lint receipt\\n'\n")
    binary.chmod(0o700)
    inputs = iter(
        [
            tool_call(
                "replace_text",
                path=WORKFLOW,
                old="    steps:",
                new="    name: Review\n    steps:",
            ),
            tool_call("lint_workflow", path=WORKFLOW),
            final_message(f"Done. {PLACEHOLDER}"),
        ]
    )
    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, json=api_response(next(inputs)))
        )
    )
    monkeypatch.setattr("responses_agent.cli.proxy_client", lambda _: client)
    assert (
        main(
            [
                "--worktree",
                str(checkout),
                "--prompt-file",
                str(prompt),
                "--files",
                WORKFLOW,
                "--credit-confirmed",
                "--zizmor",
                str(binary),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    response = CodingResponse.model_validate_json(captured.out)
    assert response.returns[0].code.value == "RET_OK"
    assert response.artifacts is not None and response.artifacts.summary is not None
    assert response.max_output_tokens_per_run == MAX_RUN_OUTPUT_TOKENS
    assert response.target_commit == git(checkout, "rev-parse", "HEAD").decode().strip()
    assert "+    name: Review\n" in Path(response.artifacts.patch).read_text()
    assert (
        f"zizmor ({WORKFLOW}): exit 0\nLint receipt"
        in Path(response.artifacts.results).read_text()
    )
    assert Path(response.artifacts.summary).read_text() == "Done. [redacted]"
    ledger = (
        tmp_path / "state/dotfiles-v2/responses-agent/usage.log.jsonl"
    ).read_text()
    event = json.loads(ledger)
    assert event["event"] == "coding_run"
    assert event["usage"] == {"requests": 3, "input_tokens": 36, "output_tokens": 15}
    assert "coding_run" in captured.err
    for sensitive in (secret, PLACEHOLDER, "Fixture task contents.", "Lint receipt"):
        assert sensitive not in ledger + captured.out + captured.err


def test_reviewer_tool_runs_only_the_pinned_offline_suite(checkout, monkeypatch):
    files = {
        "review.py": b"result = 42\n",
        "test_review.py": (
            b"import unittest\nimport review\n"
            b"class TestReview(unittest.TestCase):\n"
            b"    def test_result(self):\n        self.assertEqual(review.result, 42)\n"
        ),
    }
    monkeypatch.setattr(
        "responses_agent.worktree._REVIEW_TEST_FILES",
        {name: sha256(data).hexdigest() for name, data in files.items()},
    )

    async def exercise():
        with open_worktree(checkout, [WORKFLOW], None) as worktree:

            def fixture_git(root, *args):
                if args[0] == "show":
                    assert args[2].startswith(
                        worktree.commit + ":.github/openai-review/"
                    )
                    return files[args[2].rsplit("/", 1)[1]]
                return git(root, *args)

            monkeypatch.setattr("responses_agent.worktree.git", fixture_git)
            test_tool = next(
                tool for tool in coding_tools(worktree) if tool.name == "test_reviewer"
            )
            assert (
                await test_tool.on_invoke_tool(Mock(), '{"command":"git push"}')
                == "Refused: invalid tool arguments."
            )
            receipt = await test_tool.on_invoke_tool(Mock(), "{}")
            assert "reviewer unittest: exit 0" in receipt and "Ran 1 test" in receipt
            assert worktree.results == [receipt]
            files["review.py"] = b"unexpected source"
            assert (
                "does not contain the approved reviewer suite"
                in await test_tool.on_invoke_tool(Mock(), "{}")
            )
            assert worktree.results == [receipt]

    asyncio.run(exercise())
