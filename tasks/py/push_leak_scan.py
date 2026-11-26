#!/usr/bin/env python3
"""Scan every commit a `git push` would publish with betterleaks (hk pre-push step).

hk hands a pre-push step a single from_ref/to_ref pair, so a push of several refs
(`git push origin a b`, or a delete plus an update) used to scan only one of them.
This reads the full ref list git gives the pre-push hook (`{{hook_stdin}}`) and scans
the union of all pushed commits that the target remote does not have yet.

Without hook input (`hk run pre-push` by hand, `--from-ref`), it falls back to the
HK_FROM_REF..HK_TO_REF range. Stdlib only.
"""

from __future__ import annotations

import os
import subprocess
import sys

BETTERLEAKS = ["betterleaks", "git", "--redact", "--verbose", "--no-banner"]


def is_zero(oid: str) -> bool:
    """True for git's all-zero object id (a deleted or new ref), any hash length."""
    return bool(oid) and set(oid) == {"0"}


def known_commits(oids: list[str]) -> set[str]:
    """The subset of ``oids`` that name commits in the local object store (one git call)."""
    if not oids:
        return set()
    result = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objecttype)"],
        input="".join(f"{oid}^{{commit}}\n" for oid in oids),
        capture_output=True,
        text=True,
        check=False,
    )
    answers = result.stdout.splitlines()
    if result.returncode != 0 or len(answers) != len(oids):
        return set()
    return {oids[i] for i, kind in enumerate(answers) if kind == "commit"}


def server_oids(remote: str) -> list[str]:
    """Branch and tag oids the push target holds right now (`git ls-remote`).

    Local tracking refs can be stale (a server branch deleted without `fetch --prune`),
    so they must not decide what counts as already published. Raises if the server
    cannot be listed, which blocks the push rather than scanning too little.
    """
    result = subprocess.run(
        ["git", "ls-remote", "--heads", "--tags", "--", remote],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"git ls-remote {remote} failed: {result.stderr.strip()}")
    oids: list[str] = []
    for row in result.stdout.splitlines():
        oid = row.split("\t", 1)[0].strip()
        if oid and oid not in oids:
            oids.append(oid)
    return oids


def push_log_opts(hook_stdin: str, remote: str) -> list[str] | None:
    """git-log arguments covering every commit the push publishes, or None if it publishes none.

    Each hook line is ``<local ref> <local oid> <remote ref> <remote oid>``. Deletes
    (all-zero local oid) publish nothing. Every other local oid is a tip to scan; the
    remote oids it replaces bound the scan, plus (for new refs or unfetched bases) the
    branch and tag tips the server holds.
    """
    tips: list[str] = []
    replaced: list[str] = []
    new_refs = 0
    for raw in hook_stdin.splitlines():
        if not raw.strip():
            continue
        fields = raw.split()
        if len(fields) != 4:
            raise ValueError(f"unexpected pre-push line: {raw!r}")
        _local_ref, local_oid, _remote_ref, remote_oid = fields
        if is_zero(local_oid):
            continue
        tips.append(local_oid)
        if is_zero(remote_oid):
            new_refs += 1
        else:
            replaced.append(remote_oid)
    if not tips:
        return None
    # A replaced remote oid we never fetched (e.g. before a force push) cannot bound the
    # scan; leaving it out only makes the scan wider.
    known = known_commits(replaced)
    bases = [oid for oid in replaced if oid in known]
    # New refs (or unfetched bases) have no oid of their own to stop at, so bound them by
    # what the server holds now. Server oids we never fetched cannot bound anything.
    if new_refs or len(bases) < len(replaced):
        if not remote:
            raise ValueError("pre-push input has new refs but no remote to list")
        listed = server_oids(remote)
        on_server = known_commits(listed)
        bases += [oid for oid in listed if oid in on_server and oid not in bases]
    return [*tips, "--not", *bases]


def range_log_opts(from_ref: str, to_ref: str) -> list[str]:
    """Fallback for runs without hook input: the hk range.

    A delete's all-zero to_ref is no range at all, and hk may have picked it from a
    push that also updates other refs, so scan every local commit no remote has yet.
    """
    if not from_ref or not to_ref:
        raise ValueError("no pre-push input and no HK_FROM_REF/HK_TO_REF range")
    if is_zero(to_ref):
        return ["--all", "--not", "--remotes"]
    return [f"{from_ref}..{to_ref}"]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: push_leak_scan.py <hook_stdin> <hook_args>\n")
        return 2
    hook_stdin, hook_args = argv
    # git passes `<remote name> <push url>`; the URL is where this push actually goes.
    words = hook_args.split()
    remote = words[-1] if words else ""
    try:
        if hook_stdin.strip():
            opts = push_log_opts(hook_stdin, remote)
        else:
            opts = range_log_opts(
                os.environ.get("HK_FROM_REF", ""), os.environ.get("HK_TO_REF", "")
            )
    except ValueError as exc:
        sys.stderr.write(f"push-leak-scan: {exc}\n")
        return 2
    if opts is None:
        sys.stdout.write("push-leak-scan: push publishes no commits; nothing to scan\n")
        return 0
    command = [*BETTERLEAKS, "--log-opts=" + " ".join(opts)]
    sys.stdout.write("push-leak-scan: " + " ".join(command) + "\n")
    sys.stdout.flush()
    try:
        return subprocess.run(command, check=False).returncode
    except OSError as exc:  # e.g. argument list too long on a huge --mirror push
        sys.stderr.write(f"push-leak-scan: could not run betterleaks: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
