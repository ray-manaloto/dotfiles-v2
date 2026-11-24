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
BANNED_CALL_CONSTRUCTORS = frozenset(
    {
        "TypedDict",
        "NamedTuple",
        "Enum",
        "StrEnum",
        "IntEnum",
        "make_dataclass",
        "create_model",
    }
)
BANNED_IMPORTS = BANNED_BASES | BANNED_DECORATORS | BANNED_CALL_CONSTRUCTORS


def _is_exempt_path(rel_posix: str) -> bool:
    if rel_posix.startswith("unix/home/.config/mise/lib/dotfiles_py/generated/"):
        return True
    if rel_posix.startswith(("tests/", "unix/home/.config/mise/lib/herdr_roll/tests/")):
        return True
    return Path(rel_posix).name.startswith("test_")


def _attr_or_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _attr_or_name(node.func)
    if isinstance(node, ast.Subscript):
        return _attr_or_name(node.value)
    return None


def _has_ignore_comment(lines: list[str], node: ast.AST) -> bool:
    candidates = [getattr(node, "lineno", 1)]
    for dec in getattr(node, "decorator_list", ()):
        candidates.append(dec.lineno)
    first_line = min(candidates)
    last_line = max(first_line, getattr(node, "lineno", first_line))
    for line_no in range(max(1, first_line - 1), min(len(lines), last_line) + 1):
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
        if isinstance(node, ast.ImportFrom):
            if _has_ignore_comment(lines, node):
                continue
            for alias in node.names:
                if alias.name in BANNED_IMPORTS and alias.asname:
                    violations.append(
                        f"{rel_posix}:{node.lineno}: aliased DTO/enum import "
                        f"`{alias.name} as {alias.asname}` bypasses schema-first AST checks."
                    )
            continue

        if isinstance(node, ast.Call):
            fn_name = _attr_or_name(node.func)
            if fn_name in BANNED_CALL_CONSTRUCTORS and not _has_ignore_comment(
                lines, node
            ):
                violations.append(
                    f"{rel_posix}:{node.lineno}: functional DTO/enum constructor `{fn_name}(...)` "
                    "is banned outside generated modules; declare in schemas/**/*.schema.json."
                )
            continue

        if not isinstance(node, ast.ClassDef):
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
