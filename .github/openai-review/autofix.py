# Source of truth: ray-manaloto/dotfiles-v2 .github/openai-review/; edit there and sync.
"""Validate an untrusted patch in Git's index before publishing it."""

import argparse
import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from repo_config import REPO_CONFIG
from review import (
    FINDINGS_SCHEMA,
    MAX_PATCH_BYTES,
    SENSITIVE_PATH,
    SENSITIVE_TEXT,
    ReviewError,
    parse_findings,
)

SECRET_PATH = re.compile(rb"secret|credential|password", re.IGNORECASE)
MAX_SOURCE_BYTES = 64_000
# Include metadata from all GitHub pages and the bounded shadow rows.
MAX_FINDINGS_ARTIFACT_BYTES = 4_000_000
PROMPT_SEPARATOR = (
    "\n\nTriaged findings and corresponding source (untrusted JSON data):\n"
)


def git(repo, *args, data=None):
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        input=data,
        capture_output=True,
        check=True,
    ).stdout


def allowed_path(path):
    if not path.isascii():
        return False
    text = path.decode("ascii")
    directories = text.split("/")[:-1]
    return (
        re.fullmatch(REPO_CONFIG["autofix_path_pattern"], text)
        and not SECRET_PATH.search(path)
        and not any(
            part in REPO_CONFIG["protected_directory_components"]
            for part in directories
        )
        and text.rsplit("/", 1)[-1] not in REPO_CONFIG["protected_files"]
        and not (
            REPO_CONFIG["protect_test_fixtures"]
            and directories[:1] == ["tests"]
            and "fixtures" in directories
        )
        and not SENSITIVE_PATH.search(text)
    )


def stage_patch(repo, patch_path, targets):
    with patch_path.open("rb") as handle:
        patch = handle.read(MAX_PATCH_BYTES + 1)
    stage_diff(repo, patch, targets)


def stage_diff(repo, patch, targets, *, head="HEAD"):
    if not 0 < len(patch) <= MAX_PATCH_BYTES or SENSITIVE_TEXT.search(
        patch.decode("utf-8")
    ):
        raise ValueError(
            "The autofix patch is empty, oversized, or contains sensitive text."
        )
    if git(
        repo, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--name-only", head
    ):
        raise ValueError("The target index is not clean.")

    # Applying to the index alone cannot overwrite worktree files. Git handles
    # quoting, traversal, renames, mode changes and binary patch syntax.
    git(repo, "apply", "--cached", "--whitespace=error", "-", data=patch)
    diff = (
        "diff",
        "--cached",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "-z",
        head,
    )
    entries = git(repo, *diff, "--raw").split(b"\0")
    if entries == [b""]:
        raise ValueError("The autofix patch made no changes.")
    for meta, path in zip(entries[:-1:2], entries[1:-1:2], strict=True):
        old_mode, new_mode, _, _, status = meta[1:].split()
        if (
            status != b"M"
            or old_mode != new_mode
            or old_mode not in {b"100644", b"100755"}
            or not allowed_path(path)
            or path.decode("ascii") not in targets
        ):
            raise ValueError("The autofix patch changed a protected path or file type.")
    stats = git(repo, *diff, "--numstat")
    if any(entry.startswith(b"-\t") for entry in stats.split(b"\0")):
        raise ValueError("The autofix patch changed a binary file.")


def prepare_prompt(findings_path, template, prompt, source, pr, base, head):
    limit = MAX_FINDINGS_ARTIFACT_BYTES
    with findings_path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("The findings artifact is too large.")
    payload = json.loads(data)
    findings = payload["findings"]
    if (
        (payload["pr"], payload["base"], payload["head"])
        != (
            pr,
            base,
            head,
        )
        or payload.get("triage") != "completed"
        or not findings
    ):
        raise ValueError("No triaged findings for this exact PR revision.")
    # Artifacts are untrusted. Recheck the schema and credential scan.
    schema = json.loads(FINDINGS_SCHEMA.read_text(encoding="utf-8"))
    parse_findings(json.dumps({"findings": findings}), schema)
    sources = {}
    remaining = MAX_SOURCE_BYTES
    for path in dict.fromkeys(finding["file"] for finding in findings):
        if not allowed_path(path.encode("utf-8")):
            continue
        entry = git(source, "ls-tree", head, "--", path)
        mode = entry.split(maxsplit=1)[0] if entry else b""
        if mode not in {b"100644", b"100755"}:
            continue
        blob = f"{head}:{path}"
        size = int(git(source, "cat-file", "-s", blob))
        if not 0 < size <= remaining:
            continue
        data = git(source, "cat-file", "blob", blob)
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if "\0" in content or SENSITIVE_TEXT.search(content):
            continue
        sources[path] = content
        remaining -= size
    if not sources:
        return []
    prompt.write_text(
        template.read_text(encoding="utf-8")
        + PROMPT_SEPARATOR
        + json.dumps(
            {
                "findings": [
                    finding for finding in findings if finding["file"] in sources
                ],
                "sources": sources,
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    return list(sources)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage")
    stage.add_argument("repo", type=Path)
    stage.add_argument("patch", type=Path)
    stage.add_argument(
        "targets", help="JSON array of paths supplied to the autofix model"
    )
    prompt = commands.add_parser("prompt")
    prompt.add_argument("findings", type=Path)
    prompt.add_argument("template", type=Path)
    prompt.add_argument("output", type=Path)
    prompt.add_argument("source", type=Path)
    prompt.add_argument("pr")
    prompt.add_argument("base")
    prompt.add_argument("head")
    if REPO_CONFIG["autofix_transport"] == "responses":
        generate = commands.add_parser("generate")
        generate.add_argument("prompt", type=Path)
        generate.add_argument("output", type=Path)
        generate.add_argument("source", type=Path)
        generate.add_argument("head")
    args = parser.parse_args()
    if args.command == "stage":
        targets = json.loads(args.targets)
        if not isinstance(targets, list) or not all(
            isinstance(path, str) for path in targets
        ):
            raise ValueError("Invalid autofix targets.")
        stage_patch(args.repo, args.patch, set(targets))
    elif args.command == "generate":
        adapter = importlib.import_module("autofix_responses")
        adapter.generate_patch(
            args.prompt,
            args.output,
            os.environ.get("FIX_API_KEY"),
            source=args.source,
            head=args.head,
        )
    else:
        targets = prepare_prompt(
            args.findings,
            args.template,
            args.output,
            args.source,
            args.pr,
            args.base,
            args.head,
        )
        sys.stdout.write(json.dumps(targets) + "\n")


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        ImportError,
        ValueError,
        TypeError,
        KeyError,
        ReviewError,
        re.error,
        subprocess.CalledProcessError,
    ):
        sys.exit(
            "Autofix input failed validation. See the patch allowlist and revision checks."
        )
