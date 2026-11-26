"""Offline tests. No secrets, runners, package installs, or network calls."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import review


def pr():
    return {
        "state": "open",
        "base": {
            "repo": {"full_name": "example/repo", "private": False},
            "ref": "main",
            "sha": "b" * 40,
        },
        "head": {"sha": "a" * 40},
        "changed_files": 1,
    }


def claude_ok():
    return {
        "type": "message",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "No actionable findings."}],
    }


def item(**overrides):
    out = {
        "filename": "src/file.cpp",
        "additions": 1,
        "deletions": 1,
        "patch": "@@ -1 +1 @@\n-old\n+new",
    }
    return dict(out, **overrides)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.summary = Path(self.directory.name) / "summary"
        self.env = {
            "GITHUB_REPOSITORY": "example/repo",
            "DEFAULT_BRANCH": "main",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "PR_NUMBER": "5",
            "REPO_PRIVATE": "false",
            "ALLOW_PRIVATE_DIFF": "false",
            "REVIEW_API_KEY": "synthetic-api-key-for-tests",
            "GITHUB_TOKEN": "synthetic-gh-token-for-tests",
            "GITHUB_STEP_SUMMARY": str(self.summary),
        }

    def test_sends_patches_only_and_escapes_summary(self):
        output = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "<img src=x> Fix the boundary."}
                    ],
                }
            ],
        }
        send = Mock(side_effect=[pr(), [item()], pr(), output])
        review.run(self.env, send)
        self.assertEqual(send.call_count, 4)
        endpoint, secret = send.call_args.args
        self.assertEqual(endpoint, "https://api.openai.com/v1/responses")
        self.assertEqual(secret, self.env["REVIEW_API_KEY"])
        body = send.call_args.kwargs["body"]
        self.assertEqual(json.loads(body["input"])[0]["path"], "src/file.cpp")
        self.assertFalse(body["store"])
        self.assertNotIn("tools", body)
        self.assertNotIn(self.env["GITHUB_TOKEN"], json.dumps(body))
        summary = self.summary.read_text()
        self.assertIn("&lt;img src=x&gt;", summary)
        self.assertIn("a" * 40, summary)

    def test_ref_and_number_checks_before_network(self):
        for overrides in (
            {"GITHUB_REF": "refs/heads/not-main"},
            {"PR_NUMBER": "1; bad"},
            {"REVIEW_API_KEY": ""},
        ):
            with self.subTest(overrides=overrides):
                send = Mock()
                with self.assertRaises(review.ReviewError):
                    review.run(dict(self.env, **overrides), send)
                send.assert_not_called()

    def test_blocks_sensitive_binary_incomplete_and_large_patches(self):
        for bad in (
            item(filename=".env"),
            item(previous_filename="tls.key"),
            item(patch=None),
            item(additions=2),
            item(patch="@@ -1 +1 @@\n-old\n+password = 'syntheticValue1234'"),
            item(patch="@@ -1 +1 @@\n-old\n+" + "z" * review.MAX_PATCH_BYTES),
            item(filename="home/.netrc"),
            item(filename="private_dot_ssh/config"),
            item(filename="dot_npmrc"),
            item(
                patch="@@ -1 +1 @@\n-old\n+//registry.npmjs.org/:_authToken=npm_"
                + "A" * 36
            ),
            item(patch="@@ -1 +1 @@\n-old\n+token = glpat-" + "b" * 20),
            item(patch="@@ -1 +1 @@\n-old\n+key: AIza" + "c" * 35),
            item(patch="@@ -1 +1 @@\n-old\n+auth_token = 'syntheticValue1234'"),
            item(patch='@@ -1 +1 @@\n-old\n+"apiKey": "syntheticValue1234"'),
        ):
            with self.subTest(path=bad["filename"]):
                send = Mock(side_effect=[pr(), [bad]])
                with self.assertRaises(review.ReviewError):
                    review.run(self.env, send)
                self.assertEqual(send.call_count, 2)

    def test_updated_pr_never_reaches_openai(self):
        moved = pr()
        moved["head"]["sha"] = "c" * 40
        send = Mock(side_effect=[pr(), [item()], moved])
        with self.assertRaises(review.ReviewError):
            review.run(self.env, send)
        self.assertEqual(send.call_count, 3)
        self.assertFalse(self.summary.exists())

    def test_review_output_quoting_key_names_is_kept(self):
        text = "Finding: auth_token = some_long_identifier should come from a file."
        self.assertEqual(review.checked_text([text]), text)
        with self.assertRaises(review.ReviewError):
            review.checked_text(["access_token: abcdefghijklmnop"])
        with self.assertRaises(review.ReviewError):
            review.checked_text(["leaked ghp_" + "a" * 20])

    def test_claude_stop_reason_is_named(self):
        with self.assertRaisesRegex(review.ReviewError, "stop_reason=max_tokens"):
            review.claude_text({"type": "message", "stop_reason": "max_tokens"})

    def test_http_error_reports_only_safe_error_type(self):
        import email.message
        import io
        import urllib.error

        for body, expected in (
            (
                b'{"error": {"type": "authentication_error", "message": "x"}}',
                " (authentication_error)",
            ),
            (b'{"error": {"type": "Bad Type!"}}', ""),
            (b"not json", ""),
        ):
            with self.subTest(body=body):
                exc = urllib.error.HTTPError(
                    "u", 401, "m", email.message.Message(), io.BytesIO(body)
                )
                self.assertEqual(review.error_type(exc), expected)

    def test_truncated_http_error_body_is_safe(self):
        import http.client

        exc = Mock()  # stands in for an HTTPError whose body read is cut short
        exc.read.side_effect = http.client.IncompleteRead(b"{")
        self.assertEqual(review.error_type(exc), "")

    def test_path_and_variable_references_are_not_flagged(self):
        for patch in (
            '@@ -1 +1 @@\n-old\n+auth_token_file = "/run/agent-secrets/token"',
            '@@ -1 +1 @@\n-old\n+token="${BOARD_TOKEN_VALUE}"',
            "@@ -1 +1 @@\n-old\n+max_tokens = 1800",
            '@@ -1 +1 @@\n-old\n+secret = "DOPPLER_TOKEN_DOTFILES"',
            "@@ -1 +1 @@\n-old\n+exe_token = cmdline.split()",
            "@@ -1 +1 @@\n-old\n+SEARXNG_SECRET:-dev-only-secret-value",
        ):
            with self.subTest(patch=patch):
                self.assertIsNone(review.SENSITIVE_TEXT.search(patch))

    def test_secret_named_dirs_code_and_docs_are_reviewable(self):
        for name in (
            "unix/home/.config/mise/tasks/secret/set",
            "tasks/agent-secrets.sh",
            "docs/secrets.md",
            "tasks/py/password_rules.py",
        ):
            with self.subTest(name=name):
                self.assertIsNone(review.SENSITIVE_PATH.search(name))
        for name in ("config/db_password.txt", "credentials.json", "app/secrets"):
            with self.subTest(name=name):
                self.assertIsNotNone(review.SENSITIVE_PATH.search(name))

    def test_more_token_formats_are_blocked(self):
        for line in (
            "-----BEGIN PGP PRIVATE KEY BLOCK-----",
            "AS" + "IA" + "B" * 16,
            "sk" + "_live_" + "c" * 20,
            "url = postgres://" + "app:" + "longpassw0rd@db.example",
        ):
            with self.subTest(line=line[:12]):
                self.assertIsNotNone(review.SENSITIVE_TEXT.search(line))
                self.assertIsNotNone(review.SENSITIVE_OUTPUT.search(line))

    def test_long_line_scan_stays_linear(self):
        import time

        line = "@@ -1 +1 @@\n-old\n+" + "a" * (review.MAX_PATCH_BYTES - 40)
        start = time.monotonic()
        self.assertIsNone(review.SENSITIVE_TEXT.search(line))
        self.assertLess(time.monotonic() - start, 1.0)

    def test_oversized_patch_is_rejected_before_content_scan(self):
        big = item(patch="@@ -1 +1 @@\n-old\n+" + "a" * (review.MAX_PATCH_BYTES + 1))
        with self.assertRaisesRegex(review.ReviewError, "patch size limit"):
            review.patch_data([big])

    def test_private_repo_requires_explicit_opt_in(self):
        for private, allow, blocked in (
            ("true", "false", True),
            ("true", "", True),
            ("true", "true", False),
            ("false", "false", False),
            (None, "true", True),
            ("", "true", True),
        ):
            with self.subTest(private=private, allow=allow):
                env = dict(self.env, ALLOW_PRIVATE_DIFF=allow)
                if private is None:
                    env.pop("REPO_PRIVATE")
                else:
                    env["REPO_PRIVATE"] = private
                if blocked:
                    send = Mock()
                    with self.assertRaises(review.ReviewError):
                        review.run(env, send)
                    send.assert_not_called()
                else:
                    send = Mock(side_effect=[pr(), [item()], pr(), claude_ok()])
                    review.run(dict(env, REVIEW_PROVIDER="claude"), send)
                    self.assertEqual(send.call_count, 4)

    def test_live_visibility_overrides_stale_event_payload(self):
        made_private = pr()
        made_private["base"]["repo"]["private"] = True
        unknown = pr()
        del unknown["base"]["repo"]["private"]
        for live in (made_private, unknown):
            with self.subTest(private=live["base"]["repo"].get("private")):
                send = Mock(side_effect=[live])
                with self.assertRaises(review.ReviewError):
                    review.run(dict(self.env, REVIEW_PROVIDER="claude"), send)
                self.assertEqual(send.call_count, 1)
        send = Mock(side_effect=[made_private, [item()], made_private, claude_ok()])
        review.run(
            dict(self.env, REVIEW_PROVIDER="claude", ALLOW_PRIVATE_DIFF="true"), send
        )
        self.assertEqual(send.call_count, 4)

    def test_quoted_json_secret_keys_are_blocked(self):
        for line in (
            '{"pass' + 'word":"syntheticValue1234"}',
            "{'client_" + "secret': 'syntheticValue1234'}",
        ):
            with self.subTest(line=line[:12]):
                self.assertIsNotNone(review.SENSITIVE_TEXT.search(line))
                self.assertIsNotNone(review.SENSITIVE_OUTPUT.search(line))

    def test_non_object_api_response_maps_to_safe_message(self):
        for provider in ("claude", "openai"):
            with self.subTest(provider=provider):
                send = Mock(side_effect=[pr(), [item()], pr(), ["not", "a", "dict"]])
                with self.assertRaises(review.MALFORMED_ERRORS):
                    review.run(dict(self.env, REVIEW_PROVIDER=provider), send)

    def test_incomplete_or_empty_review_is_not_success(self):
        for output in ({"status": "incomplete"}, {"status": "completed", "output": []}):
            with self.assertRaises(review.ReviewError):
                review.response_text(output)

    def test_claude_uses_only_messages_and_renders_summary(self):
        response = {
            "type": "message",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Check <boundary>."}],
        }
        send = Mock(side_effect=[pr(), [item()], pr(), response])
        review.run(dict(self.env, REVIEW_PROVIDER="claude"), send)
        self.assertEqual(send.call_count, 4)
        endpoint, key = send.call_args.args
        self.assertEqual(endpoint, review.CLAUDE_ENDPOINT)
        self.assertEqual(key, self.env["REVIEW_API_KEY"])
        body = send.call_args.kwargs["body"]
        self.assertEqual(body["model"], review.CLAUDE_MODEL)
        self.assertEqual(body["max_tokens"], 4000)
        self.assertNotIn("tools", body)
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertEqual(
            json.loads(body["messages"][0]["content"])[0]["path"], "src/file.cpp"
        )
        self.assertIn("Check &lt;boundary&gt;.", self.summary.read_text())

    def test_claude_rejects_partial_results_and_unknown_providers(self):
        with self.assertRaises(review.ReviewError):
            review.claude_text({"type": "message", "stop_reason": "max_tokens"})
        send = Mock()
        with self.assertRaises(review.ReviewError):
            review.run(dict(self.env, REVIEW_PROVIDER="unsupported"), send)
        send.assert_not_called()

    def test_claude_sends_documented_headers_only_to_claude(self):
        from unittest.mock import patch

        with patch("review.urllib.request.build_opener") as build_opener:
            opener = build_opener.return_value
            opener.open.return_value.__enter__.return_value.read.return_value = b"{}"
            review.request_json(
                review.CLAUDE_ENDPOINT, "fake-key", body={"max_tokens": 10}
            )
            request = opener.open.call_args.args[0]
            headers = {name.lower(): value for name, value in request.header_items()}
            self.assertEqual(headers["anthropic-version"], "2026-10-21")
            self.assertEqual(headers["authorization"], "Bearer fake-key")
            self.assertEqual(request.get_method(), "POST")
            review.request_json(review.OPENAI_ENDPOINT, "fake-openai", body={})
            request = opener.open.call_args.args[0]
            self.assertNotIn(
                "anthropic-version",
                {name.lower() for name, _ in request.header_items()},
            )


class RunnerCompatibilityTests(unittest.TestCase):
    def test_sources_parse_on_runner_python_312(self):
        # ubuntu-24.04's stock python3 is 3.12; the repo toolchain is newer.
        import ast

        for name in ("review.py", "test_review.py"):
            source = (Path(__file__).parent / name).read_text(encoding="utf-8")
            ast.parse(source, filename=name, feature_version=(3, 12))
