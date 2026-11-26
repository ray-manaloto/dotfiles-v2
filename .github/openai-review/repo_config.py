# Source of truth: ray-manaloto/dotfiles-v2 .github/openai-review/; edit there and sync.
"""Repository policy loaded only from the trusted pipeline checkout."""

import json
import re
from pathlib import Path

POLICY_VALUES = {
    "$schema": ("./repo.schema.json",),
    "automatic_private_consent": (None, "ALLOW_PRIVATE_AUTOMATIC"),
    "autofix_transport": ("codex-action", "responses"),
    "decline_channel": ("outputs", "artifact"),
}
PROTECTED_LISTS = ("protected_files", "protected_directory_components")
POLICY_KEYS = {
    *POLICY_VALUES,
    *PROTECTED_LISTS,
    "autofix_path_pattern",
    "protect_test_fixtures",
}

# Every alternative has a literal directory root. The remaining syntax permits
# only the supported Python filename and subdirectory patterns.
LITERAL_ROOT = r"(?:(?:\\\.)?[A-Za-z0-9_-]+/)+"
AUTOFIX_PATH_SHAPE = re.compile(
    rf"^(?:{LITERAL_ROOT}|\(\?:{LITERAL_ROOT}(?:\|{LITERAL_ROOT})+\))"
    rf"(?:\(\?:\[A-Za-z0-9_-\]\+/\)\*|\(\?:{LITERAL_ROOT}\)\?)?"
    r"\[A-Za-z0-9_-\]\+\\\.py(?![\s\S])"
)
PATH_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+")


def load_repo_config(path):
    policy = json.loads(path.read_bytes())
    if not isinstance(policy, dict) or policy.keys() != POLICY_KEYS:
        raise ValueError("Invalid repository policy keys")
    for key, values in POLICY_VALUES.items():
        if policy[key] not in values:
            raise ValueError(f"Invalid repository policy {key}")
    if not isinstance(policy["protect_test_fixtures"], bool):
        raise ValueError("Invalid repository policy protect_test_fixtures")
    for key in PROTECTED_LISTS:
        items = policy[key]
        if (
            not isinstance(items, list)
            or not items
            or not all(
                isinstance(item, str) and PATH_COMPONENT.fullmatch(item)
                for item in items
            )
            or len(items) != len(set(items))
        ):
            raise ValueError(f"Invalid repository policy {key}")

    pattern = policy["autofix_path_pattern"]
    if not isinstance(pattern, str):
        raise ValueError("Invalid repository policy autofix_path_pattern")
    if not AUTOFIX_PATH_SHAPE.fullmatch(pattern):
        raise ValueError(
            "Autofix paths must use literal directory roots and Python filenames"
        )
    re.compile(pattern)
    return policy


REPO_CONFIG = load_repo_config(Path(__file__).with_name("repo.json"))
