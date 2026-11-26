"""Offline request and worktree receipts for the coding profile."""

import asyncio
import json
import os
import subprocess
from unittest.mock import Mock

import httpx2
import pytest
from openai import APIStatusError

from dotfiles_py.generated.responses_agent_schema import CodingResponse
from responses_agent.cli import main
from responses_agent.runner import (
    ENDPOINT,
    KEY_NAME,
    MAX_OUTPUT_TOKENS,
    MAX_TURNS,
    MODEL,
    PLACEHOLDER,
    CredentialError,
    UsageTracker,
    coding_tools,
    enforce_route,
    proxy_client,
    run,
)
from responses_agent.worktree import ScopeError, open_worktree

WORKFLOW = ".github/workflows/review.yml"


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q", root], check=True)
    path = root / WORKFLOW
    path.parent.mkdir(parents=True)
    path.write_text("jobs:\n  review:\n    steps: []\n", encoding="utf-8")
    (root / "private.txt").write_text("unapproved", encoding="utf-8")
    return root


def api_response(output):
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
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 17,
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
            {
                "type": "message",
                "id": "msg_fixture",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "Updated.", "annotations": []}
                ],
            },
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
        }
        bodies.append(body)
        return httpx2.Response(200, json=api_response(next(scripted)))

    tracker = UsageTracker()
    with open_worktree(checkout, [WORKFLOW], None) as worktree:
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        asyncio.run(run(worktree, "Name the reviewer job.", client, tracker))
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


def test_proxy_ignores_other_keys_and_requires_local_fnox():
    with pytest.raises(CredentialError, match=KEY_NAME):
        proxy_client({"OPENAI_API_KEY": "fixture", "OPENAI_REVIEW_API_KEY": "fixture"})
    with pytest.raises(CredentialError, match="proxy or TLS CA"):
        proxy_client(
            {KEY_NAME: PLACEHOLDER, "HTTPS_PROXY": "http://alternate.invalid:8888"}
        )
    with pytest.raises(CredentialError, match=KEY_NAME):
        proxy_client({KEY_NAME: "raw-fixture-key"})


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
                "zizmor exit 3\n--offline\n--no-config\n--pedantic\n--strict-collection\n--\n-\n"
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
    assert main(args) == 1
    blocked = CodingResponse.model_validate_json(capsys.readouterr().out)
    assert blocked.errors[0].code.value == "ERR_PRECONDITION_FAILED"
    assert not state.exists()
    assert (checkout / WORKFLOW).read_bytes() == original
