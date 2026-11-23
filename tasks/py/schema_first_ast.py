#!/usr/bin/env python3
"""Static AST gate banning hand-written DTOs and enums outside generated/test modules."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

BANNED_BASES = frozenset(
    {
        "BaseModel",
        "RootModel",
        "BaseSettings",
        "Enum",
        "StrEnum",
        "IntEnum",
        "TypedDict",
        "NamedTuple",
    }
)
BANNED_DECORATORS = frozenset({"dataclass"})

# Temporary narrow exemption for untouched Issue #12 hook file until migrated.
PRE_ISSUE_12_EXEMPTIONS = frozenset(
    {
        ("unix/home/.gemini/config/hooks/agy-interactive-questions.py", "_HookState"),
    }
)


def _is_exempt_path(rel_posix: str) -> bool:
    parts = Path(rel_posix).parts
    if "generated" in parts or "tests" in parts:
        return True
    return Path(rel_posix).name.startswith("test_")


def _attr_or_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _attr_or_name(node.func)
    return None


def _has_ignore_comment(lines: list[str], class_node: ast.ClassDef) -> bool:
    candidates = [class_node.lineno]
    for dec in class_node.decorator_list:
        candidates.append(dec.lineno)
    first_line = min(candidates)
    for line_no in range(max(1, first_line - 1), class_node.lineno + 1):
        text = lines[line_no - 1]
        if "# schema-first-ignore:" in text:
            _, _, reason = text.partition("# schema-first-ignore:")
            if reason.strip():
                return True
    return False


def check_file(path: Path, repo_root: Path) -> list[str]:
    """Return AST schema-first violation lines for *path*."""
    try:
        rel_posix = path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        rel_posix = path.as_posix()

    if _is_exempt_path(rel_posix) or not path.is_file():
        return []

    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source, filename=rel_posix)
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if (rel_posix, node.name) in PRE_ISSUE_12_EXEMPTIONS:
            continue
        if _has_ignore_comment(lines, node):
            continue

        for base in node.bases:
            base_name = _attr_or_name(base)
            if base_name in BANNED_BASES:
                violations.append(
                    f"{rel_posix}:{node.lineno}: hand-written `{node.name}({base_name})` "
                    "is banned outside generated modules; declare in schemas/**/*.schema.json."
                )
        for dec in node.decorator_list:
            dec_name = _attr_or_name(dec)
            if dec_name in BANNED_DECORATORS:
                violations.append(
                    f"{rel_posix}:{node.lineno}: hand-written `@{dec_name}` class `{node.name}` "
                    "is banned outside generated modules; declare in schemas/**/*.schema.json."
                )

    return violations


def _tracked_python_files(repo_root: Path) -> list[Path]:
    out = subprocess.check_output(
        ["git", "-C", str(repo_root), "ls-files", "*.py"],
        text=True,
    )
    return [repo_root / line for line in out.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    repo_root = Path.cwd().resolve()
    targets = [Path(arg) for arg in args] if args else _tracked_python_files(repo_root)

    all_violations: list[str] = []
    checked = 0
    for target in targets:
        if target.suffix != ".py":
            continue
        checked += 1
        all_violations.extend(check_file(target, repo_root))

    if all_violations:
        sys.stderr.write("\n".join(all_violations) + "\n")
        return 1

    sys.stdout.write(f"schema-first-ast: {checked} Python file(s) clean\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
