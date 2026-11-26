# Source of truth: ray-manaloto/dotfiles-v2 .github/openai-review/; edit there and sync.
import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autofix import (
    MAX_FINDINGS_ARTIFACT_BYTES,
    REPO_CONFIG,
    allowed_path,
    git,
    prepare_prompt,
    stage_patch,
)
from repo_config import AUTOFIX_PATH_SHAPE, load_repo_config


class RepoPolicyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "repo.json"
        self.policy = dict(REPO_CONFIG)

    def test_valid_policy_and_schema_agree_on_path_shape(self):
        self.path.write_text(json.dumps(self.policy))
        self.assertEqual(load_repo_config(self.path), self.policy)
        schema = json.loads(Path(__file__).with_name("repo.schema.json").read_bytes())
        self.assertEqual(
            schema["properties"]["autofix_path_pattern"]["pattern"],
            AUTOFIX_PATH_SHAPE.pattern,
        )

    def test_invalid_configs_are_rejected(self):
        invalid_values = {
            "$schema": ("wrong.json",),
            "automatic_private_consent": (True, "always"),
            "autofix_transport": ("unknown", []),
            "decline_channel": ("unknown", None),
            "protect_test_fixtures": (0, "true"),
            "protected_files": (
                [],
                "conftest.py",
                [0],
                ["a.py", "a.py"],
                ["nested/a.py"],
            ),
            "protected_directory_components": ([], [""], ["generated\n"]),
            "autofix_path_pattern": (
                None,
                ".*",
                r".*\.py",
                r"tests/.*\.py",
                r"tests/[A-Za-z0-9_-]+.py",
                r"(?:tests/|.*)[A-Za-z0-9_-]+\.py",
                r"tests/[",
            ),
        }
        missing = dict(self.policy)
        del missing["protected_files"]
        invalid_policies = [None, missing, {**self.policy, "extra": True}]
        invalid_policies.extend(
            {**self.policy, key: value}
            for key, values in invalid_values.items()
            for value in values
        )
        for policy in invalid_policies:
            with self.subTest(policy=policy):
                self.path.write_text(json.dumps(policy))
                with self.assertRaises(ValueError):
                    load_repo_config(self.path)

    def test_import_rejects_invalid_policy(self):
        source = Path(__file__).with_name("repo_config.py")
        module = self.path.with_name(source.name)
        module.write_bytes(source.read_bytes())
        self.path.write_text(json.dumps({**self.policy, "autofix_path_pattern": ".*"}))
        with self.assertRaisesRegex(ValueError, "literal directory roots"):
            runpy.run_path(str(module))


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(
            patch.dict(
                os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1"
            )
        )
        self.enterContext(
            patch.dict(
                REPO_CONFIG,
                autofix_path_pattern=r"(?:tests/|lib/)(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.py",
                protected_files=["conftest.py", "__init__.py", "tasks_py_loader.py"],
                protected_directory_components=["generated"],
                protect_test_fixtures=True,
            )
        )
        for variable in git(Path.cwd(), "rev-parse", "--local-env-vars").split():
            os.environ.pop(variable.decode("ascii"), None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name) / "repo"
        self.repo.mkdir()
        self.patch = Path(directory.name) / "codex.patch"
        git(self.repo, "init")
        for name in (
            "tests/test_something.py",
            ".github/workflows/ci.yml",
            "config/mise.lock",
            "lib/generated/model.py",
            "lib/stuff.py",
            "tests/conftest.py",
            "tests/helpers/__init__.py",
            "tests/tasks_py_loader.py",
            "tests/fixtures/nested/helper.py",
        ):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("before\n")
            git(self.repo, "add", "--", name)
        git(
            self.repo,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-m",
            "Before",
        )

    def make_patch(self):
        self.patch.write_bytes(git(self.repo, "diff", "HEAD", "--binary"))
        git(self.repo, "restore", "--source=HEAD", "--staged", "--worktree", "--", ".")

    def test_existing_text_python_files_can_be_staged(self):
        file = self.repo / "lib/stuff.py"
        file.write_text("after\n")
        self.make_patch()
        stage_patch(self.repo, self.patch, {"lib/stuff.py"})
        self.assertEqual(
            git(self.repo, "diff", "--cached", "--name-only"),
            b"lib/stuff.py\n",
        )
        self.assertEqual(file.read_text(), "before\n")

    def test_publisher_can_build_the_verified_commit_without_a_checkout(self):
        path = "tests/test_something.py"
        (self.repo / path).write_text("after\n")
        self.make_patch()
        head = git(self.repo, "rev-parse", "HEAD").decode().strip()
        bare = self.patch.parent / "publisher"
        git(bare.parent, "init", "--bare", str(bare))
        git(bare, "fetch", "--no-tags", self.repo.as_uri(), head)
        git(bare, "update-ref", "HEAD", head)
        git(bare, "read-tree", "HEAD")
        stage_patch(bare, self.patch, {path})
        tree = git(bare, "write-tree").decode().strip()
        commit = (
            git(
                bare,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.com",
                "commit-tree",
                tree,
                "-p",
                head,
                "-m",
                "Apply verified patch",
            )
            .decode()
            .strip()
        )
        self.assertEqual(git(bare, "show", f"{commit}:{path}"), b"after\n")
        self.assertEqual(git(bare, "rev-parse", f"{commit}^").decode().strip(), head)
        self.assertFalse((bare / path).exists())

    def test_protected_paths_are_rejected_before_worktree_changes(self):
        for name in (
            ".github/workflows/ci.yml",
            "config/mise.lock",
            "lib/generated/model.py",
            "tests/conftest.py",
            "tests/helpers/__init__.py",
            "tests/tasks_py_loader.py",
            "tests/fixtures/nested/helper.py",
        ):
            with self.subTest(name=name):
                path = self.repo / name
                path.write_text("after\n")
                self.make_patch()
                with self.assertRaisesRegex(ValueError, "protected path"):
                    stage_patch(self.repo, self.patch, {name})
                self.assertEqual(path.read_text(), "before\n")
                git(self.repo, "restore", "--staged", "--", ".")

    def test_mixed_patch_fails_when_any_entry_is_protected(self):
        (self.repo / "tests/test_something.py").write_text("after\n")
        (self.repo / ".github/workflows/ci.yml").write_text("after\n")
        self.make_patch()
        with self.assertRaisesRegex(ValueError, "protected path"):
            stage_patch(
                self.repo,
                self.patch,
                {"tests/test_something.py", ".github/workflows/ci.yml"},
            )

    def test_policy_allowed_path_outside_reviewed_targets_is_rejected(self):
        skipped = "lib/stuff.py"
        self.assertTrue(allowed_path(skipped.encode("ascii")))
        (self.repo / skipped).write_text("after\n")
        self.make_patch()
        with self.assertRaisesRegex(ValueError, "protected path"):
            stage_patch(self.repo, self.patch, {"tests/test_something.py"})

    def test_new_file_and_symlink_replacement_fail(self):
        new = self.repo / "tests/test_new.py"
        new.write_text("after\n")
        git(self.repo, "add", "--", "tests/test_new.py")
        self.make_patch()
        with self.assertRaisesRegex(ValueError, "protected path"):
            stage_patch(self.repo, self.patch, {"tests/test_new.py"})
        git(self.repo, "restore", "--source=HEAD", "--staged", "--worktree", "--", ".")
        existing = self.repo / "tests/test_something.py"
        existing.unlink()
        existing.symlink_to("../.github/workflows/ci.yml")
        self.make_patch()
        with self.assertRaisesRegex(ValueError, "protected path"):
            stage_patch(self.repo, self.patch, {"tests/test_something.py"})

    def test_prompt_contains_only_eligible_committed_source(self):
        root = self.patch.parent
        findings_path = root / "findings.json"
        prompt = root / "prompt.md"
        template = root / "template.md"
        template.write_text("Trusted instructions")
        head = git(self.repo, "rev-parse", "HEAD").decode().strip()
        base = "b" * 40
        findings = [
            {
                "id": f"F{index}",
                "file": path,
                "line": 1,
                "severity": "high",
                "claim": "Defect",
                "suggested_fix": "Fix it",
            }
            for index, path in enumerate(
                ("tests/test_something.py", "tests/conftest.py"), 1
            )
        ]
        (self.repo / "tests/test_something.py").write_text("working-tree data\n")
        payload = {"pr": "5", "head": head, "base": base, "findings": findings}
        findings_path.write_text(json.dumps(dict(payload, triage="unavailable")))
        with self.assertRaisesRegex(ValueError, "No triaged findings"):
            prepare_prompt(findings_path, template, prompt, self.repo, "5", base, head)
        shadow = [
            {
                "path": "tests/test_something.py",
                "line": 1,
                "title": "Shadow " + "a" * 390,
            }
        ] * 2
        findings_path.write_text(
            json.dumps(dict(payload, triage="completed", findings=[], shadow=shadow))
        )
        with self.assertRaisesRegex(ValueError, "No triaged findings"):
            prepare_prompt(findings_path, template, prompt, self.repo, "5", base, head)
        self.assertFalse(prompt.exists())
        payload["skipped"] = [{"path": "s" * 63_000, "reason": "over-budget"}]
        completed = dict(payload, triage="completed")
        self.assertLess(len(json.dumps(completed).encode()), 64_000)
        findings_path.write_text(json.dumps(dict(completed, shadow=shadow)) + "\n")
        self.assertGreater(findings_path.stat().st_size, 64_000)
        targets = prepare_prompt(
            findings_path, template, prompt, self.repo, "5", base, head
        )
        self.assertEqual(targets, ["tests/test_something.py"])
        data = json.loads(prompt.read_text().split("\n", 3)[3])
        self.assertEqual(data["sources"], {"tests/test_something.py": "before\n"})
        self.assertEqual(data["findings"], findings[:1])
        self.assertNotIn("Shadow", prompt.read_text())
        findings_path.write_bytes(b" " * (MAX_FINDINGS_ARTIFACT_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "artifact is too large"):
            prepare_prompt(findings_path, template, prompt, self.repo, "5", base, head)
