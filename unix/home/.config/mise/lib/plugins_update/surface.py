"""Strict whole-tree comparison for plugin updates.

Rules (deliberately simple; nothing is "understood", only compared):

1. The OLD tree has every occurrence of the old version string textually replaced by the
   new version string, then both trees are compared.
2. Every file is compared byte for byte, plus mode bits (exec and setuid/setgid/sticky)
   and symlink targets. Only `.git/` at the tree ROOT is skipped, plus caller-named root-level runtime entries
   (`.in_use/`, installer artifacts) which callers pass for the OLD installed tree only.
3. A differing file is auto-approved only if it is a regular non-executable doc
   (root README/CHANGELOG/CONTRIBUTORS.md, CITATION.cff, `docs/**/*.md`, or
   `media/**` images) AND neither its path, its basename, `docs/` nor `media/` occurs in any non-doc
   file of the new tree. Everything else (SKILL.md, agents, scripts, manifests, ...) needs review.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import stat
from collections.abc import Collection, Mapping
from pathlib import Path, PurePosixPath

# kind ("file" | "symlink" | "special"), permission bits that matter, content bytes.
Entry = tuple[str, int, bytes]
Tree = dict[str, Entry]

MAX_FILES = 20_000
MAX_TOTAL_BYTES = 200 * 1024 * 1024
ROOT_SKIP = frozenset({".git"})
_MODE_MASK = 0o7111  # exec bits + setuid/setgid/sticky; umask-dependent bits ignored
_DOC_ROOT_FILES = frozenset(
    {"README.md", "CHANGELOG.md", "CONTRIBUTORS.md", "CITATION.cff"}
)
_MEDIA_SUFFIXES = frozenset({".png", ".svg", ".jpg", ".jpeg", ".gif", ".webp"})
_MAX_DIFF_LINES = 400


class TreeTooLarge(Exception):
    """The tree exceeded the file-count or byte cap."""


def read_tree(
    root: Path, skip_root: Collection[str] = (), *, runtime_pycache: bool = False
) -> Tree:
    """Map relative path to (kind, mode, bytes) for everything under *root*.

    *runtime_pycache* drops `__pycache__/` dirs and `*.pyc` files; use it only for the OLD
    installed tree, where Python writes them at runtime. They are never dropped from a tree
    that is about to be installed or from a post-install check.
    """
    skip = ROOT_SKIP | frozenset(skip_root)
    tree: Tree = {}
    total = 0
    base = root.resolve(strict=True)
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        cur = Path(dirpath)
        at_root = cur == base
        keep: list[str] = []
        for d in sorted(dirnames):
            full = cur / d
            if at_root and d in skip:
                continue
            if runtime_pycache and d == "__pycache__":
                continue
            if full.is_symlink():
                tree[full.relative_to(base).as_posix()] = (
                    "symlink",
                    0,
                    os.readlink(full).encode("utf-8", "surrogateescape"),
                )
            else:
                keep.append(d)
        dirnames[:] = keep
        for fname in sorted(filenames):
            full = cur / fname
            rel = full.relative_to(base).as_posix()
            if at_root and fname in skip:
                continue
            if runtime_pycache and fname.endswith(".pyc"):
                continue
            if full.is_symlink():
                tree[rel] = (
                    "symlink",
                    0,
                    os.readlink(full).encode("utf-8", "surrogateescape"),
                )
                continue
            st = full.lstat()
            if not stat.S_ISREG(st.st_mode):
                tree[rel] = ("special", 0, b"")
                continue
            total += st.st_size
            if len(tree) >= MAX_FILES or total > MAX_TOTAL_BYTES:
                raise TreeTooLarge(f"plugin tree exceeds caps under {root}")
            tree[rel] = ("file", st.st_mode & _MODE_MASK, full.read_bytes())
    return tree


def substitute_version(tree: Mapping[str, Entry], old: str, new: str) -> Tree:
    """Replace every textual occurrence of *old* with *new* in file contents."""
    if not old or old == new:
        return dict(tree)
    o, n = old.encode(), new.encode()
    return {
        p: (k, m, d.replace(o, n)) if k == "file" else (k, m, d)
        for p, (k, m, d) in tree.items()
    }


def strict_diff(
    a: Mapping[str, Entry], b: Mapping[str, Entry]
) -> list[tuple[str, str]]:
    """Return `[(path, added|removed|modified)]` for every byte/mode/target difference."""
    out: list[tuple[str, str]] = []
    for rel in sorted(set(a) | set(b)):
        if rel not in b:
            out.append((rel, "removed"))
        elif rel not in a:
            out.append((rel, "added"))
        elif a[rel] != b[rel]:
            out.append((rel, "modified"))
    return out


def is_doc_path(rel: str) -> bool:
    p = PurePosixPath(rel)
    if len(p.parts) == 1:
        return rel in _DOC_ROOT_FILES
    if p.parts[0] == "docs":
        return p.suffix == ".md"
    if p.parts[0] == "media":
        return p.suffix.lower() in _MEDIA_SUFFIXES
    return False


def _plain_doc(entry: Entry | None) -> bool:
    return entry is None or (entry[0] == "file" and not entry[1] & 0o111)


def gate(
    old: Mapping[str, Entry], new: Mapping[str, Entry], pin: str
) -> tuple[list[tuple[str, str]], str, str]:
    """Compare *old* (version-substituted) with *new*.

    Returns `(review_changes, diff_text, digest)`. `review_changes` lists every difference
    that is not an auto-approved doc; `digest` is a sha256 over *pin* and those differences,
    so an approval is bound to exactly the reviewed content.
    """
    changes = strict_diff(old, new)
    referenced_blob = b"\0".join(
        d for p, (k, _m, d) in new.items() if k == "file" and not is_doc_path(p)
    )
    # Globs and string building defeat literal path matching, so any mention of the doc
    # directories by a non-doc file forces review of every doc change.
    doc_dirs_mentioned = b"docs/" in referenced_blob or b"media/" in referenced_blob
    review: list[tuple[str, str]] = []
    for rel, kind in changes:
        auto = (
            is_doc_path(rel)
            and _plain_doc(old.get(rel))
            and _plain_doc(new.get(rel))
            and not doc_dirs_mentioned
            and rel.encode() not in referenced_blob
            and PurePosixPath(rel).name.encode() not in referenced_blob
        )
        if not auto:
            review.append((rel, kind))
    lines = [f"pin\t{pin}"]
    chunks: list[str] = []
    for rel, kind in review:
        o, n = old.get(rel), new.get(rel)
        lines.append(
            "\t".join(
                [
                    rel,
                    kind,
                    f"{o[0]}:{o[1]:o}:{hashlib.sha256(o[2]).hexdigest()}" if o else "-",
                    f"{n[0]}:{n[1]:o}:{hashlib.sha256(n[2]).hexdigest()}" if n else "-",
                ]
            )
        )
        chunks.append(_render_diff(rel, o, n))
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    text = "".join(chunks).splitlines(keepends=True)
    if len(text) > _MAX_DIFF_LINES:
        text = [*text[:_MAX_DIFF_LINES], f"... diff truncated ({len(text)} lines)\n"]
    return review, "".join(text), digest


def _render_diff(rel: str, o: Entry | None, n: Entry | None) -> str:
    if o and n and o[0] == n[0] == "file" and o[2] == n[2]:
        return f"mode {rel}: {o[1]:o} -> {n[1]:o}\n"

    def text(e: Entry | None) -> list[str]:
        if e is None:
            return []
        if e[0] != "file":
            return [f"<{e[0]}> {e[2].decode('utf-8', 'replace')}\n"]
        try:
            return e[2].decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            return [f"<binary {len(e[2])} bytes>\n"]

    return "".join(
        difflib.unified_diff(text(o), text(n), fromfile=f"a/{rel}", tofile=f"b/{rel}")
    )
