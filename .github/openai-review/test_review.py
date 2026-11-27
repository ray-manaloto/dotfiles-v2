# Source of truth: ray-manaloto/dotfiles-v2 .github/openai-review/; edit there and sync.
"""Offline tests. No secrets, runners, package installs, or network calls."""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call
from unittest.mock import patch as mock_patch

import review
from autofix import MAX_FINDINGS_ARTIFACT_BYTES


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


def finding(**overrides):
    result = {
        "id": "F1",
        "file": "src/file.cpp",
        "line": 1,
        "severity": "high",
        "claim": "<img src=x> Fix the boundary.\n::error::do not execute @someone",
        "suggested_fix": "Restore the original boundary.",
    }
    return dict(result, **overrides)


def openai_ok(*findings):
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": json.dumps({"findings": findings})}
                ],
            }
        ],
    }


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(
            mock_patch.dict(
                review.REPO_CONFIG,
                automatic_private_consent=None,
                decline_channel="outputs",
            )
        )
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

    def assert_decline(self, reason, send, *, head="a" * 40, **overrides):
        output = Path(self.directory.name) / "output"
        job_output = Path(self.directory.name) / "job-output"
        env = dict(
            self.env, GITHUB_OUTPUT=str(job_output), REVIEW_OUTPUT_DIR=str(output)
        )
        env.update(overrides)
        with (
            mock_patch.object(review.os, "environ", env),
            mock_patch.object(review, "request_json", send),
        ):
            self.assertIsNone(review.main())
        note = f"not reviewed: {reason}"
        self.assertIn(note, self.summary.read_text())
        self.assertIn("OpenAI usage", self.summary.read_text())
        self.assertEqual(
            job_output.read_text().splitlines()[-2:],
            [f"decline_head={head}", f"decline_note={note}"],
        )
        self.assertFalse(output.exists())

    def test_sends_patches_only_and_escapes_summary(self):
        output = Path(self.directory.name) / "output"
        proposed = openai_ok(finding(), finding(id="F2", line=2))
        decisions = {
            "answers": [
                {"type": "predicate", "name": "F1_defect", "probability": 0.91},
                {"type": "score", "name": "F1_severity", "score": 2.0},
            ]
        }
        send = Mock(side_effect=[pr(), [item()], pr(), proposed, decisions])
        review.run(dict(self.env, REVIEW_OUTPUT_DIR=str(output)), send)
        self.assertEqual(send.call_count, 5)
        request = send.call_args_list[3]
        endpoint, secret = request.args
        self.assertEqual(endpoint, "https://api.openai.com/v1/responses")
        self.assertEqual(secret, self.env["REVIEW_API_KEY"])
        body = request.kwargs["body"]
        self.assertEqual(json.loads(body["input"])[0]["path"], "src/file.cpp")
        self.assertFalse(body["store"])
        self.assertNotIn("tools", body)
        self.assertNotIn(self.env["GITHUB_TOKEN"], json.dumps(body))
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(body["text"]["format"]["schema"]["type"], "object")
        summary = self.summary.read_text()
        self.assertIn("&lt;img src=x&gt;", summary)
        self.assertNotIn("\n::error::", summary)
        self.assertNotIn("@someone", summary)
        self.assertIn("a" * 40, summary)
        self.assertEqual(
            json.loads((output / "findings.json").read_text())["findings"], [finding()]
        )
        comment = (output / "comment.md").read_text()
        self.assertTrue(summary.startswith(comment))
        self.assertNotIn("OpenAI usage", comment)
        self.assertIn('OpenAI usage: `{"cost_usd": 0.0, "usage": {}}`', summary)

    def test_shadow_appears_only_in_artifact_and_step_summary(self):
        for posted in (False, True):
            with self.subTest(posted=posted):
                output = Path(self.directory.name) / str(posted)
                self.summary.unlink(missing_ok=True)
                shadow = finding(
                    id="F2",
                    severity="low",
                    claim="Shadow <entry>\n@reader",
                    suggested_fix="Shadow suggestion",
                )
                proposed = openai_ok(*([finding(), shadow] if posted else [shadow]))
                answers = [
                    {"type": "predicate", "name": "F2_defect", "probability": 0.85},
                    {"type": "score", "name": "F2_severity", "score": 0.0},
                ]
                if posted:
                    answers[:0] = [
                        {"type": "predicate", "name": "F1_defect", "probability": 0.91},
                        {"type": "score", "name": "F1_severity", "score": 1.0},
                    ]
                pull = pr()
                pull["base"]["repo"]["private"] = True
                send = Mock(
                    side_effect=[pull, [item()], pull, proposed, {"answers": answers}]
                )
                env = dict(
                    self.env,
                    REPO_PRIVATE="true",
                    ALLOW_PRIVATE_DIFF="true",
                    REVIEW_OUTPUT_DIR=str(output),
                )
                review.run(env, send)

                artifact = (output / "findings.json").read_text()
                payload = json.loads(artifact)
                self.assertEqual(payload["findings"], [finding()] if posted else [])
                self.assertEqual(
                    payload["shadow"],
                    [
                        {
                            "path": "src/file.cpp",
                            "line": 1,
                            "title": "Shadow <entry> @reader",
                        }
                    ],
                )
                summary = self.summary.read_text()
                comment = (output / "comment.md").read_text()
                self.assertTrue(summary.startswith(comment))
                self.assertIn("### Shadow triage (1)", summary)
                self.assertIn("src/file.cpp:1 Shadow &lt;entry&gt; ＠reader", summary)
                self.assertNotIn("Shadow", comment)
                self.assertNotIn("Shadow suggestion", artifact)
                self.assertNotIn("Shadow suggestion", summary)
                self.assertEqual("No findings passed triage." in comment, not posted)

    def test_automatic_review_requires_same_repo(self):
        env = dict(
            self.env,
            GITHUB_EVENT_NAME="pull_request_target",
            EXPECTED_HEAD="a" * 40,
            EXPECTED_BASE="b" * 40,
        )
        pull = pr()
        pull["head"]["repo"] = {"full_name": "fork/repo"}
        send = Mock(return_value=pull)
        self.assert_decline(
            "The PR closed, moved, or does not match the event.", send, **env
        )
        send.assert_called_once()

    def test_forged_autofix_commit_marker_does_not_skip_automatic_review(self):
        output = Path(self.directory.name) / "output"
        pull = pr()
        pull["head"]["repo"] = {"full_name": "example/repo"}
        head = pull["head"]["sha"]
        base = pull["base"]["sha"]
        env = dict(
            self.env,
            GITHUB_EVENT_NAME="pull_request_target",
            EXPECTED_HEAD=head,
            EXPECTED_BASE=base,
            REVIEW_OUTPUT_DIR=str(output),
        )
        repo_url = "https://api.github.com/repos/example/repo"
        pr_url = f"{repo_url}/pulls/5"
        responses = {
            pr_url: pull,
            f"{pr_url}/files?per_page=31": [item()],
            review.OPENAI_ENDPOINT: openai_ok(),
        }
        responses[f"{repo_url}/commits/{head}"] = {
            "commit": {
                "author": {"name": "github-actions[bot]"},
                "message": "Fix\n\nOpenAI-Autofix: #5 round 2\n",
            }
        }
        send = Mock(side_effect=lambda url, *args, **kwargs: responses[url])
        review.run(env, send)
        self.assertIn(
            call(f"{pr_url}/files?per_page=31", env["GITHUB_TOKEN"]),
            send.call_args_list,
        )
        self.assertEqual(
            json.loads((output / "findings.json").read_text())["head"], head
        )

    def test_metered_send_sums_openai_usage_and_prices_it(self):
        usage = {}
        responses = {
            review.OPENAI_ENDPOINT: {
                "usage": {
                    "input_tokens": 1_000_000,
                    "input_tokens_details": {"cached_tokens": 400_000},
                    "output_tokens": 100_000,
                }
            },
            review.triage.ENDPOINT: {"usage": {"input_tokens": 2_000_000}},
            "https://api.github.com/repos/example/repo/pulls/5": {"usage": "ignored"},
        }
        send = review.metered(lambda url, token, **kwargs: responses[url], usage)
        for url in responses:
            send(url, "token")
        send(review.OPENAI_ENDPOINT, "token", body={})
        self.assertEqual(
            usage[review.MODEL],
            {
                "input_tokens": 2_000_000,
                "cached_tokens": 800_000,
                "output_tokens": 200_000,
            },
        )
        self.assertEqual(usage[review.triage.MODEL]["input_tokens"], 2_000_000)
        self.assertEqual(len(usage), 2)
        # 1.2M fresh * $2 + 0.8M cached * $0.10 + 0.2M out * $10 + 2M luna * $0.10
        self.assertEqual(review.usage_cost(usage), 4.68)

    def test_triage_failure_writes_note_with_usage_and_no_findings(self):
        proposed = dict(openai_ok(finding()), usage={"input_tokens": 42})
        for error in (
            review.triage.TriageError("bad answer"),
            review.ReviewError("HTTP 503"),
        ):
            with self.subTest(error=error):
                send = Mock(side_effect=[pr(), [item()], pr(), proposed, error])
                self.assert_decline("Triage unavailable.", send)
                self.assertIn('"input_tokens": 42', self.summary.read_text())
                self.assertNotIn("HTTP 503", self.summary.read_text())

    def test_findings_reject_duplicate_ids_and_non_integer_locations(self):
        schema = json.loads(review.FINDINGS_SCHEMA.read_text())
        for findings in ([finding(), finding()], [finding(line=True)]):
            with self.subTest(findings=findings), self.assertRaises(review.ReviewError):
                review.parse_findings(json.dumps({"findings": findings}), schema)

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

    def test_all_skipped_aborts_without_calling_a_model(self):
        for bad in (
            item(filename=".env"),
            item(previous_filename="tls.key"),
            item(patch=None),
            item(additions=2),
            item(patch="@@ -1 +1 @@\n-old\n+password = 'syntheticValue1234'"),
            item(
                patch="@@ -1 +1 @@\n-old\n+"
                + "z" * (review.MAX_PATCH_BYTES * review.MAX_REVIEW_CHUNKS)
            ),
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
                send = Mock(side_effect=[pr(), [bad], pr()])
                self.assert_decline(
                    "No reviewable files remain after patch checks.", send
                )
                self.assertEqual(send.call_count, 3)

    def test_skip_reasons(self):
        for overrides, path, reason in (
            ({"patch": None}, "src/file.cpp", "missing-patch"),
            ({"patch": "Binary files differ"}, "src/file.cpp", "missing-patch"),
            ({"additions": 2}, "src/file.cpp", "missing-patch"),
            (
                {"patch": "@@ -1 +1 @@\n-old\n+" + "z" * review.MAX_PATCH_BYTES},
                "src/file.cpp",
                "oversized",
            ),
            ({"filename": "mise.lock"}, "mise.lock", "generated-or-lockfile"),
            (
                {"filename": "nested/other.lock"},
                "nested/other.lock",
                "generated-or-lockfile",
            ),
            (
                {"filename": "nested/package-lock.json"},
                "nested/package-lock.json",
                "generated-or-lockfile",
            ),
            (
                {"filename": "nested/npm-shrinkwrap.json"},
                "nested/npm-shrinkwrap.json",
                "generated-or-lockfile",
            ),
            (
                {"filename": "src/generated/output.py"},
                "src/generated/output.py",
                "generated-or-lockfile",
            ),
            (
                {"previous_filename": "src/generated/output.py"},
                "src/file.cpp",
                "generated-or-lockfile",
            ),
            ({"filename": "nested/.env"}, "<redacted path>", "sensitive-path"),
            (
                {"previous_filename": "nested/tls.key"},
                "<redacted path>",
                "sensitive-path",
            ),
            (
                {"patch": "@@ -1 +1 @@\n-old\n+password = 'syntheticValue1234'"},
                "src/file.cpp",
                "sensitive-text",
            ),
        ):
            with self.subTest(reason=reason, path=path):
                data, skipped = review.patch_data(
                    [item(filename="normal.py"), item(**overrides)]
                )
                self.assertEqual(
                    [entry["path"] for entry in json.loads(data)], ["normal.py"]
                )
                self.assertEqual(skipped, [{"path": path, "reason": reason}])

    def test_mixed_pr_reviews_fourteen_files_and_skips_the_large_lockfile(self):
        output = Path(self.directory.name) / "output"
        normal = [item(filename=f"src/file_{i}.py") for i in range(14)]
        lockfile = item(filename="uv.lock", patch="@@ -1 +1 @@\n-old\n+" + "z" * 99_758)
        revision = dict(pr(), changed_files=15)
        send = Mock(side_effect=[revision, [*normal, lockfile], revision, openai_ok()])
        review.run(dict(self.env, REVIEW_OUTPUT_DIR=str(output)), send)
        body = send.call_args_list[3].kwargs["body"]
        self.assertEqual(
            [patch["path"] for patch in json.loads(body["input"])],
            [row["filename"] for row in normal],
        )
        self.assertNotIn("uv.lock", json.dumps(body))
        self.assertIn(
            "uv.lock (generated-or-lockfile)", (output / "comment.md").read_text()
        )
        self.assertEqual(
            json.loads((output / "findings.json").read_text())["skipped"],
            [{"path": "uv.lock", "reason": "generated-or-lockfile"}],
        )

    def test_skipped_content_never_reaches_requests_or_outputs(self):
        output = Path(self.directory.name) / "output"
        secret = "gh" + "p_" + "x" * 25
        skipped_path = "tests/test_skipped.py"
        hidden_path = "nested/.env.private"
        revision = dict(pr(), changed_files=3)
        decisions = {
            "answers": [
                {"type": "predicate", "name": "F1_defect", "probability": 0.91},
                {"type": "score", "name": "F1_severity", "score": 2.0},
            ]
        }
        send = Mock(
            side_effect=[
                revision,
                [
                    item(),
                    item(filename=skipped_path, patch=f"@@ -1 +1 @@\n-old\n+{secret}"),
                    item(filename=hidden_path),
                ],
                revision,
                openai_ok(
                    finding(),
                    finding(id="F2", file=skipped_path),
                    finding(id="F3", file=hidden_path),
                ),
                decisions,
            ]
        )
        self.assertIsNone(
            review.run(dict(self.env, REVIEW_OUTPUT_DIR=str(output)), send)
        )
        self.assertEqual(send.call_count, 5)
        for request in send.call_args_list[3:]:
            serialized = json.dumps(request.kwargs["body"])
            for excluded in (secret, hidden_path, skipped_path):
                self.assertNotIn(excluded, serialized)
        comment = (output / "comment.md").read_text()
        artifact = (output / "findings.json").read_text()
        for result in (comment, artifact, self.summary.read_text()):
            self.assertNotIn(secret, result)
            self.assertNotIn(hidden_path, result)
        self.assertIn("&lt;redacted path&gt; (sensitive-path)", comment)
        self.assertEqual(json.loads(artifact)["findings"], [finding()])
        self.assertEqual(
            json.loads(artifact)["skipped"],
            [
                {"path": skipped_path, "reason": "sensitive-text"},
                {"path": "<redacted path>", "reason": "sensitive-path"},
            ],
        )

    def test_skipped_files_do_not_count_toward_the_reviewable_file_limit(self):
        output = Path(self.directory.name) / "output"
        first_page = [item(filename=f"locks/{i}.lock") for i in range(31)]
        revision = dict(pr(), changed_files=32)
        send = Mock(side_effect=[revision, first_page, [item()], revision, openai_ok()])
        review.run(dict(self.env, REVIEW_OUTPUT_DIR=str(output)), send)
        self.assertTrue(send.call_args_list[2].args[0].endswith("?per_page=31&page=2"))
        body = send.call_args.kwargs["body"]
        self.assertEqual(
            [row["path"] for row in json.loads(body["input"])], ["src/file.cpp"]
        )
        self.assertEqual(
            len(json.loads((output / "findings.json").read_text())["skipped"]), 31
        )

    def test_too_many_reviewable_files_are_not_sent(self):
        files = [item(filename=f"{i}.py") for i in range(review.MAX_FILES + 1)]
        revision = dict(pr(), changed_files=len(files))
        send = Mock(side_effect=[revision, files, revision])
        self.assert_decline("The PR exceeds the reviewable file limit.", send)
        self.assertEqual(send.call_count, 3)

    def test_github_file_listing_cap_declines_before_fetching_patches(self):
        send = Mock(return_value=dict(pr(), changed_files=review.GITHUB_FILE_LIMIT + 1))
        self.assert_decline("The PR exceeds the GitHub file listing limit.", send)
        send.assert_called_once()

    def test_incomplete_later_file_page_prevents_review(self):
        files = [item(filename=f"{i}.lock") for i in range(31)]
        revision = dict(pr(), changed_files=32)
        send = Mock(side_effect=[revision, files, [], revision])
        with self.assertRaisesRegex(review.ReviewError, "incomplete file list"):
            review.run(self.env, send)
        self.assertEqual(send.call_count, 4)

    def test_combined_size_limit_reviews_a_prefix_and_lists_over_budget_skips(self):
        output = Path(self.directory.name) / "output"
        patch_text = "@@ -1 +1 @@\n-old\n+" + "z" * (review.MAX_PATCH_BYTES // 2)
        marker = "over budget content"
        files = [
            item(patch=patch_text),
            item(filename="other.py", patch=patch_text + marker),
            item(filename="small.py"),
            item(filename=".env"),
        ]
        revision = dict(pr(), changed_files=len(files))
        send = Mock(side_effect=[revision, files, revision, openai_ok()])
        with mock_patch.object(review, "MAX_REVIEW_CHUNKS", 1):
            review.run(dict(self.env, REVIEW_OUTPUT_DIR=str(output)), send)
        data = send.call_args.kwargs["body"]["input"]
        self.assertLessEqual(len(data.encode()), review.MAX_PATCH_BYTES)
        self.assertEqual([row["path"] for row in json.loads(data)], ["src/file.cpp"])
        artifact = (output / "findings.json").read_text()
        self.assertEqual(
            json.loads(artifact)["skipped"],
            [
                {"path": "other.py", "reason": "over-budget"},
                {"path": "small.py", "reason": "over-budget"},
                {"path": "<redacted path>", "reason": "sensitive-path"},
            ],
        )
        for text in (
            data,
            artifact,
            self.summary.read_text(),
            (output / "comment.md").read_text(),
        ):
            self.assertNotIn(marker, text)
            self.assertNotIn(".env", text)
        comment = (output / "comment.md").read_text()
        self.assertIn(
            "**Not reviewed: 2 changed file(s) exceeded the review size limit.**"
            " The results below cover only the other files.\n\n"
            "<pre>\nother.py\nsmall.py\n</pre>",
            comment,
        )
        self.assertLess(
            comment.index("Not reviewed"), comment.index("### Skipped files")
        )
        self.assertIn("No findings passed triage in the reviewed files.", comment)

    def test_large_new_module_is_reviewed_in_hunks_across_requests(self):
        output = Path(self.directory.name) / "output"
        count = 1000
        module = "\n".join(
            f"+value_{i:04} = compute({i}, 'padding text for the line')"
            for i in range(1, count + 1)
        )
        self.assertGreater(len(module), 40_000)
        files = [
            item(
                filename="lib/remote.py",
                additions=count,
                deletions=0,
                patch=f"@@ -0,0 +1,{count} @@\n{module}",
            ),
            item(filename="small.py"),
        ]
        revision = dict(pr(), changed_files=len(files))
        github = [revision, files, revision]
        reviewed = []
        decisions = {
            "answers": [
                {"type": "predicate", "name": "F1_defect", "probability": 0.9},
                {"type": "score", "name": "F1_severity", "score": 2.0},
            ]
        }

        def send(url, token, **kwargs):
            if url == review.OPENAI_ENDPOINT:
                patches = json.loads(kwargs["body"]["input"])
                reviewed.append(patches)
                lines = review.changed_lines(patches).get("lib/remote.py")
                if not lines:
                    return openai_ok()
                severity = "low" if len(reviewed) == 1 else "critical"
                return openai_ok(
                    finding(file="lib/remote.py", line=min(lines), severity=severity)
                )
            if url == review.triage.ENDPOINT:
                return decisions
            return github.pop(0)

        review.run(dict(self.env, REVIEW_OUTPUT_DIR=str(output)), send)
        self.assertGreater(len(reviewed), 1)
        self.assertLessEqual(len(reviewed), review.MAX_REVIEW_CHUNKS)
        covered = set()
        for patches in reviewed:
            serialized = json.dumps(patches, ensure_ascii=True, separators=(",", ":"))
            self.assertLessEqual(len(serialized), review.MAX_PATCH_BYTES)
            for row in patches:
                if row["path"] == "lib/remote.py":
                    lines = review.changed_lines([row])["lib/remote.py"]
                    self.assertFalse(covered & lines)
                    covered |= lines
                    for number in lines:
                        self.assertIn(f"+value_{number:04} =", row["patch"])
        self.assertEqual(covered, set(range(1, count + 1)))
        payload = json.loads((output / "findings.json").read_text())
        self.assertEqual(payload["skipped"], [])
        # One F1 per module chunk; the first is low. Sorted by severity, renumbered.
        flagged = sum(
            any(row["path"] == "lib/remote.py" for row in patches)
            for patches in reviewed
        )
        self.assertEqual(
            [(row["id"], row["severity"]) for row in payload["findings"]],
            [
                (f"F{i}", "critical" if i < flagged else "low")
                for i in range(1, flagged + 1)
            ],
        )
        comment = (output / "comment.md").read_text()
        self.assertNotIn("Not reviewed", comment)
        self.assertIn("lib/remote.py", comment)

    def test_split_patch_keeps_new_file_line_numbers(self):
        rows = []
        for i in range(1, 301):
            rows += [f" context line {i}", f"-removed line {i}", f"+added line {i}"]
        patch = "@@ -10,600 +20,600 @@ def section():\n" + "\n".join(rows)
        patch += "\n@@ -900,1 +900,2 @@\n context\n+tail\n\\ No newline at end of file"
        pieces = review.split_patch(patch, 2000)
        self.assertGreater(len(pieces), 1)
        whole = review.changed_lines([{"path": "f", "patch": patch}])["f"]
        parts = review.changed_lines([{"path": "f", "patch": p} for p in pieces])
        self.assertEqual(parts["f"], whole)
        # Piece 1 holds 38 context/removed/added triples from old 10 and new 20.
        self.assertEqual(
            pieces[1].split("\n", 1)[0], "@@ -86,76 +96,75 @@ def section():"
        )
        for piece in pieces:
            self.assertLessEqual(len(json.dumps(piece)), 2000)
            self.assertTrue(piece.startswith("@@ -"))
        self.assertIsNone(review.split_patch("@@ -1 +1 @@\n+" + "x" * 3000, 2000))

    def test_unreviewable_module_is_named_in_the_headline(self):
        huge = "z" * (review.MAX_PATCH_BYTES * review.MAX_REVIEW_CHUNKS)
        files = [
            item(filename="lib/huge.py", patch=f"@@ -1 +1 @@\n-old\n+{huge}"),
            item(),
        ]
        revision = dict(pr(), changed_files=len(files))
        for provider, response in (("openai", openai_ok()), ("claude", claude_ok())):
            with self.subTest(provider=provider):
                output = Path(self.directory.name) / provider
                self.summary.unlink(missing_ok=True)
                send = Mock(side_effect=[revision, files, revision, response])
                env = dict(
                    self.env,
                    REVIEW_PROVIDER=provider,
                    REVIEW_OUTPUT_DIR=str(output),
                )
                review.run(env, send)
                summary = self.summary.read_text()
                heading, rest = summary.split("\n\n", 2)[1:]
                self.assertTrue(heading.startswith("Head: "))
                self.assertTrue(
                    rest.startswith(
                        "**Not reviewed: 1 changed file(s) exceeded the review size"
                        " limit.**"
                    )
                )
                self.assertIn("<pre>\nlib/huge.py\n</pre>", rest)
                self.assertIn("lib/huge.py (oversized)", rest)

    def test_serialization_overhead_can_decline_all_files(self):
        patch_text = "@@ -1 +1 @@\n-old\n+" + "z" * (review.MAX_PATCH_BYTES - 40)
        send = Mock(side_effect=[pr(), [item(patch=patch_text)], pr()])
        self.assert_decline("No file fits within the patch size limit.", send)
        self.assertEqual(send.call_count, 3)

    def test_later_first_fit_after_json_escaping_expands_a_patch(self):
        patch = "@@ -1 +1 @@\n-old\n+" + "é" * (review.MAX_PATCH_BYTES // 5)
        data, skipped = review.patch_data(
            [item(patch=patch), item(filename="small.py")]
        )
        self.assertEqual([row["path"] for row in json.loads(data)], ["small.py"])
        self.assertEqual(skipped, [{"path": "src/file.cpp", "reason": "over-budget"}])

    def test_skipped_path_metadata_fits_the_artifact_cap(self):
        files = [item()]
        for index in range(review.GITHUB_FILE_LIMIT - 1):
            files.append(item(filename=f"{'é' * 280}/{index}.lock"))
        _, skipped = review.patch_data(files)
        self.assertEqual(len(skipped), review.GITHUB_FILE_LIMIT - 1)
        self.assertEqual(skipped[0]["path"], "<path omitted: too long>")
        self.assertLess(len(json.dumps(skipped)), MAX_FINDINGS_ARTIFACT_BYTES // 2)

    def test_duplicate_file_is_a_fault(self):
        revision = dict(pr(), changed_files=2)
        send = Mock(side_effect=[revision, [item(), item()]])
        with self.assertRaisesRegex(review.ReviewError, "duplicate PR file"):
            review.run(self.env, send)
        self.assertEqual(send.call_count, 2)

    def test_automatic_revalidates_base_branch_and_head_repository(self):
        initial = pr()
        initial["head"]["repo"] = {"full_name": "example/repo"}
        for live in (
            dict(initial, base=dict(initial["base"], ref="another-branch")),
            dict(initial, head=dict(initial["head"], repo=None)),
        ):
            with self.subTest(live=live):
                send = Mock(side_effect=[initial, [item()], live])
                self.assert_decline(
                    "The PR changed during collection.",
                    send,
                    GITHUB_EVENT_NAME="pull_request_target",
                    EXPECTED_HEAD="a" * 40,
                    EXPECTED_BASE="b" * 40,
                )
                self.assertEqual(send.call_count, 3)

    def test_private_automatic_requires_its_own_consent_and_writes_decline_artifacts(
        self,
    ):
        initial = pr()
        initial["base"]["repo"]["private"] = True
        initial["head"]["repo"] = {"full_name": "example/repo"}
        output = Path(self.directory.name) / "output"
        env = dict(
            self.env,
            GITHUB_EVENT_NAME="pull_request_target",
            EXPECTED_HEAD="a" * 40,
            EXPECTED_BASE="b" * 40,
            REPO_PRIVATE="true",
            ALLOW_PRIVATE_DIFF="true",
            REVIEW_API_KEY="",
            REVIEW_OUTPUT_DIR=str(output),
        )
        with mock_patch.dict(
            review.REPO_CONFIG,
            automatic_private_consent="ALLOW_PRIVATE_AUTOMATIC",
            decline_channel="artifact",
        ):
            send = Mock(return_value=initial)
            review.run(env, send)
            send.assert_called_once()
            self.assertEqual(
                json.loads((output / "decline.json").read_text()),
                {"pr": "5", "head": "a" * 40, "base": "b" * 40, "declined": True},
            )
            self.assertIn(
                "Private patches require explicit opt-in.",
                (output / "comment.md").read_text(),
            )
            send = Mock(side_effect=[initial, [item()], initial, openai_ok()])
            review.run(
                dict(
                    env,
                    REVIEW_API_KEY=self.env["REVIEW_API_KEY"],
                    ALLOW_PRIVATE_AUTOMATIC="true",
                    REVIEW_OUTPUT_DIR="",
                ),
                send,
            )
            self.assertEqual(send.call_count, 4)

    def test_updated_pr_never_reaches_openai(self):
        moved = pr()
        moved["head"]["sha"] = "c" * 40
        for files in ([item()], []):
            with self.subTest(files=files):
                send = Mock(side_effect=[pr(), files, moved])
                self.assert_decline("The PR changed during collection.", send)
                self.assertEqual(send.call_count, 3)

    def test_mismatched_event_keeps_original_head_for_note(self):
        pull = pr()
        pull["head"] = {"sha": "c" * 40, "repo": {"full_name": "example/repo"}}
        send = Mock(return_value=pull)
        self.assert_decline(
            "The PR closed, moved, or does not match the event.",
            send,
            GITHUB_EVENT_NAME="pull_request_target",
            EXPECTED_HEAD="a" * 40,
            EXPECTED_BASE="b" * 40,
        )
        send.assert_called_once()

    def test_genuine_faults_fail_at_the_entry_point(self):
        malformed = item()
        del malformed["additions"]
        for responses, overrides, error in (
            ([review.ReviewError("GitHub returned HTTP 503.")], {}, "HTTP 503"),
            ([pr(), [malformed]], {}, "API response was invalid"),
            ([], {"REVIEW_API_KEY": ""}, "Set OPENAI_REVIEW_API_KEY"),
        ):
            with self.subTest(error=error):
                with (
                    mock_patch.object(
                        review.os, "environ", dict(self.env, **overrides)
                    ),
                    mock_patch.object(
                        review, "request_json", Mock(side_effect=responses)
                    ),
                    self.assertRaisesRegex(SystemExit, error),
                ):
                    review.main()
                self.assertFalse(self.summary.exists())
        with (
            mock_patch.object(review.os, "environ", self.env),
            mock_patch.object(
                review, "request_json", Mock(side_effect=RuntimeError("unexpected"))
            ),
            self.assertRaisesRegex(RuntimeError, "unexpected"),
        ):
            review.main()

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
            with (
                self.subTest(body=body),
                urllib.error.HTTPError(
                    "u", 401, "m", email.message.Message(), io.BytesIO(body)
                ) as exc,
            ):
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
            "tasks/config/secret/set",
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
            "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----",
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
        data, skipped = review.patch_data([item(filename="normal.py"), big])
        self.assertEqual(len(json.loads(data)), 1)
        self.assertEqual(skipped[0]["reason"], "oversized")

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
                if private == "true" and blocked:
                    send = Mock(return_value=pr())
                    self.assert_decline(
                        "Private patches require explicit opt-in.", send, **env
                    )
                    send.assert_called_once()
                elif blocked:
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
        send = Mock(return_value=made_private)
        self.assert_decline("Private patches require explicit opt-in.", send)
        send.assert_called_once()
        send = Mock(return_value=unknown)
        with self.assertRaises(review.ReviewError):
            review.run(dict(self.env, REVIEW_PROVIDER="claude"), send)
        send.assert_called_once()
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
    def test_credential_workflows_have_only_default_branch_triggers(self):
        workflows = Path(__file__).resolve().parent.parent / "workflows"
        expected = {
            "openai-pr-review.yml": {"pull_request_target", "workflow_dispatch"},
            "openai-pr-autofix.yml": {"pull_request_target"},
        }
        for path in workflows.glob("*.y*ml"):
            raw = path.read_text(encoding="utf-8")
            if not re.search(r"^\s*environment: openai-pr-(review|fix)\s*$", raw, re.M):
                continue
            with self.subTest(workflow=path.name):
                trigger = re.search(r"^on:\n((?: .+\n)+)", raw, re.M)
                if trigger is None:
                    self.fail("Credential workflows require an explicit event block.")
                events = set(re.findall(r"^  ([a-z_]+):", trigger[1], re.M))
                self.assertEqual(events, expected.pop(path.name, None))
                self.assertIn("    branches: [main]", trigger[1])
        self.assertFalse(expected)

    def test_openai_pr_workflows_keep_caching_disabled(self):
        workflows = Path(__file__).resolve().parent.parent / "workflows"
        for path in workflows.glob("openai-pr-*.y*ml"):
            with self.subTest(workflow=path.name):
                raw = path.read_text(encoding="utf-8")
                self.assertNotRegex(
                    raw, r"(?im)^\s*(?:-\s*)?uses:\s*['\"]?actions/cache[@/]"
                )
                for value in re.findall(
                    r"(?im)^\s*['\"]?(?:cache|cache-enabled|enable-cache)['\"]?\s*:\s*([^#\r\n]+)",
                    raw,
                ):
                    self.assertEqual(value.strip().strip("'\"").lower(), "false")

    def test_candidate_verifier_has_no_environment_or_secret_references(self):
        workflow = (
            Path(__file__).resolve().parent.parent / "workflows/openai-pr-autofix.yml"
        )
        raw = workflow.read_text(encoding="utf-8")
        verify = re.search(r"^  verify:[^\n]*\n(.*?)(?=^  \S|\Z)", raw, re.S | re.M)
        if verify is None:
            self.fail("Expected a separate verify job.")
        self.assertNotRegex(
            verify[1], r"""(?m)(?:^\s*|[{,]\s*)['"]?environment['"]?\s*:"""
        )
        self.assertNotRegex(verify[1], r"\$\{\{[^}]*\bsecrets\b")

    def test_sources_parse_on_runner_python_312(self):
        # ubuntu-24.04's stock python3 is 3.12; the repo toolchain is newer.
        import ast

        for path in Path(__file__).parent.glob("*.py"):
            ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
                feature_version=(3, 12),
            )
