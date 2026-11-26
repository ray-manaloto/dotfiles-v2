"""Validate an untrusted patch in Git's index before publishing it."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from review import (
    FINDINGS_SCHEMA,
    MAX_PATCH_BYTES,
    SENSITIVE_PATH,
    SENSITIVE_TEXT,
    ReviewError,
    parse_findings,
)

ALLOWED_PATH = re.compile(
    rb"(?:tests/|unix/home/\.config/mise/lib/)(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.py"
)
SECRET_PATH = re.compile(rb"secret|credential|password", re.IGNORECASE)
IMPORT_HOOKS = {b"conftest.py", b"__init__.py", b"tasks_py_loader.py"}
MAX_SOURCE_BYTES = 64_000
MAX_FINDINGS_ARTIFACT_BYTES = 64_000
# Up to 16 shadow rows with bounded 1024/400-character path/title fields.
# JSON escapes can take 12 bytes per character.
MAX_SHADOW_BYTES = 16 * (12 * (1024 + 400) + 64)


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
    parts = path.split(b"/")
    return (
        ALLOWED_PATH.fullmatch(path)
        and not SECRET_PATH.search(path)
        and b"generated" not in parts
        and parts[-1] not in IMPORT_HOOKS
        and not (parts[0] == b"tests" and b"fixtures" in parts)
        and not SENSITIVE_PATH.search(path.decode("ascii"))
    )


def stage_patch(repo, patch_path, targets):
    with patch_path.open("rb") as handle:
        patch = handle.read(MAX_PATCH_BYTES + 1)
    if not 0 < len(patch) <= MAX_PATCH_BYTES or SENSITIVE_TEXT.search(
        patch.decode("utf-8")
    ):
        raise ValueError(
            "The autofix patch is empty, oversized, or contains sensitive text."
        )
    if git(repo, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--name-only"):
        raise ValueError("The target index is not clean.")

    # Applying to the index alone cannot overwrite worktree files. Git handles
    # quoting, traversal, renames, mode changes and binary patch syntax.
    git(repo, "apply", "--cached", "--whitespace=error", "-", data=patch)
    diff = ("diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-renames", "-z")
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
    limit = MAX_FINDINGS_ARTIFACT_BYTES + MAX_SHADOW_BYTES
    with findings_path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("The findings artifact is too large.")
    payload = json.loads(data)
    if "shadow" in payload:
        payload.pop("shadow")
        core_size = len(json.dumps(payload).encode("utf-8")) + 1
    else:
        core_size = len(data)
    if core_size > MAX_FINDINGS_ARTIFACT_BYTES:
        raise ValueError("The findings artifact is too large.")
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
        + "\n\nTriaged findings and corresponding source (untrusted JSON data):\n"
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
    args = parser.parse_args()
    if args.command == "stage":
        targets = json.loads(args.targets)
        if not isinstance(targets, list) or not all(
            isinstance(path, str) for path in targets
        ):
            raise ValueError("Invalid autofix targets.")
        stage_patch(args.repo, args.patch, set(targets))
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
        ValueError,
        TypeError,
        KeyError,
        ReviewError,
        subprocess.CalledProcessError,
    ):
        sys.exit(
            "Autofix input failed validation. See the patch allowlist and revision checks."
        )
