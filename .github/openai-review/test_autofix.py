import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autofix import git, stage_patch


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
        hooks = Path(directory.name) / "hooks"
        hooks.mkdir()
        git(self.repo, "config", "core.hooksPath", str(hooks))
        for name in (
            "tests/test_something.py",
            ".github/workflows/ci.yml",
            "unix/home/.config/mise/mise.lock",
            "unix/home/.config/mise/lib/generated/model.py",
            "unix/home/.config/mise/lib/stuff.py",
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
        stage_patch(self.repo, self.patch)
        self.assertEqual(
            git(self.repo, "diff", "--cached", "--name-only"),
            b"unix/home/.config/mise/lib/stuff.py\n",
        )
        self.assertEqual(file.read_text(), "before\n")

    def test_protected_paths_are_rejected_before_worktree_changes(self):
        for name in (
            ".github/workflows/ci.yml",
            "unix/home/.config/mise/mise.lock",
            "unix/home/.config/mise/lib/generated/model.py",
        ):
            with self.subTest(name=name):
                path = self.repo / name
                path.write_text("after\n")
                self.make_patch()
                with self.assertRaisesRegex(ValueError, "protected path"):
                    stage_patch(self.repo, self.patch)
                self.assertEqual(path.read_text(), "before\n")
                git(self.repo, "restore", "--staged", "--", ".")

    def test_mixed_patch_fails_when_any_entry_is_protected(self):
        (self.repo / "tests/test_something.py").write_text("after\n")
        (self.repo / ".github/workflows/ci.yml").write_text("after\n")
        self.make_patch()
        with self.assertRaisesRegex(ValueError, "protected path"):
            stage_patch(self.repo, self.patch)

    def test_new_file_and_symlink_replacement_fail(self):
        new = self.repo / "tests/test_new.py"
        new.write_text("after\n")
        git(self.repo, "add", "--", "tests/test_new.py")
        self.make_patch()
        with self.assertRaisesRegex(ValueError, "protected path"):
            stage_patch(self.repo, self.patch)
        git(self.repo, "restore", "--source=HEAD", "--staged", "--worktree", "--", ".")
        existing = self.repo / "tests/test_something.py"
        existing.unlink()
        existing.symlink_to("../.github/workflows/ci.yml")
        self.make_patch()
        with self.assertRaisesRegex(ValueError, "protected path"):
            stage_patch(self.repo, self.patch)
