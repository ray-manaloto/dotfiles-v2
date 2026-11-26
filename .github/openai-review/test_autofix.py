import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autofix import allowed_path, git, prepare_prompt, stage_patch


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ))
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
            "unix/home/.config/mise/mise.lock",
            "unix/home/.config/mise/lib/generated/model.py",
            "unix/home/.config/mise/lib/stuff.py",
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
        file = self.repo / "unix/home/.config/mise/lib/stuff.py"
        file.write_text("after\n")
        self.make_patch()
        stage_patch(self.repo, self.patch, {"unix/home/.config/mise/lib/stuff.py"})
        self.assertEqual(
            git(self.repo, "diff", "--cached", "--name-only"),
            b"unix/home/.config/mise/lib/stuff.py\n",
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
            "unix/home/.config/mise/mise.lock",
            "unix/home/.config/mise/lib/generated/model.py",
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
        skipped = "unix/home/.config/mise/lib/stuff.py"
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
        findings_path.write_text(json.dumps(dict(payload, triage="completed")))
        targets = prepare_prompt(
            findings_path, template, prompt, self.repo, "5", base, head
        )
        self.assertEqual(targets, ["tests/test_something.py"])
        data = json.loads(prompt.read_text().split("\n", 3)[3])
        self.assertEqual(data["sources"], {"tests/test_something.py": "before\n"})
        self.assertEqual(data["findings"], findings[:1])
