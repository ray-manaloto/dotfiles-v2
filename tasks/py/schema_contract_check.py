#!/usr/bin/env python3
"""Draft 2020-12 JSON Schema contract gate for schemas/**/*.schema.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
NS_PATTERN = "^[0-9]{8}T[0-9]{6}\\.[0-9]{9}(?:Z|[+-][0-9]{4})$"
ENVELOPE_SUFFIXES = ("Response", "Outcome", "RunRecord", "Summary")
REQUIRED_ENVELOPE_FIELDS = ("timestamp", "timestamp_unix_ns", "errors", "returns")


def _check_object_additional_properties(
    node: Any,
    *,
    location: str,
    violations: list[str],
) -> None:
    if not isinstance(node, dict):
        return
    node_type = node.get("type")
    is_object_type = (
        node_type == "object"
        or (isinstance(node_type, list) and "object" in node_type)
        or (
            node_type is None
            and (
                isinstance(node.get("properties"), dict)
                or isinstance(node.get("patternProperties"), dict)
            )
        )
    )
    if (
        is_object_type
        and not (
            location.endswith(".properties.detail") or location.endswith(".detail")
        )
        and node.get("additionalProperties") is not False
    ):
        violations.append(
            f'{location}: object definition must set `"additionalProperties": false`'
        )
    for map_key in ("properties", "patternProperties"):
        mapping = node.get(map_key)
        if isinstance(mapping, dict):
            for prop_name, prop_schema in mapping.items():
                _check_object_additional_properties(
                    prop_schema,
                    location=f"{location}.{map_key}.{prop_name}",
                    violations=violations,
                )
    items = node.get("items")
    if isinstance(items, dict):
        _check_object_additional_properties(
            items,
            location=f"{location}.items",
            violations=violations,
        )
    prefix_items = node.get("prefixItems")
    if isinstance(prefix_items, list):
        for idx, sub in enumerate(prefix_items):
            _check_object_additional_properties(
                sub,
                location=f"{location}.prefixItems[{idx}]",
                violations=violations,
            )
    add_props = node.get("additionalProperties")
    if isinstance(add_props, dict):
        _check_object_additional_properties(
            add_props,
            location=f"{location}.additionalProperties",
            violations=violations,
        )
    for comb in ("anyOf", "oneOf", "allOf"):
        branches = node.get(comb)
        if isinstance(branches, list):
            for idx, branch in enumerate(branches):
                _check_object_additional_properties(
                    branch,
                    location=f"{location}.{comb}[{idx}]",
                    violations=violations,
                )
    for sub_key in ("not", "if", "then", "else"):
        sub_schema = node.get(sub_key)
        if isinstance(sub_schema, dict):
            _check_object_additional_properties(
                sub_schema,
                location=f"{location}.{sub_key}",
                violations=violations,
            )


def check_schema_file(path: Path, repo_root: Path) -> list[str]:
    """Validate Draft 2020-12, `additionalProperties: false`, and envelope fields on *path*."""
    try:
        rel = path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        rel = path.as_posix()

    violations: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{rel}: invalid JSON: {exc}"]

    if not isinstance(data, dict):
        return [f"{rel}: top-level schema must be a JSON object"]

    if data.get("$schema") != DRAFT_2020_12:
        violations.append(f"{rel}: `$schema` must equal {DRAFT_2020_12!r}")

    if not isinstance(data.get("$id"), str) or not data["$id"].strip():
        violations.append(f"{rel}: missing non-empty `$id`")

    defs = data.get("$defs")
    if isinstance(defs, dict):
        if rel.endswith("common/envelope.schema.json"):
            ns_def = defs.get("NanosecondTimestamp")
            if not isinstance(ns_def, dict) or ns_def.get("pattern") != NS_PATTERN:
                violations.append(
                    f"{rel}: NanosecondTimestamp must enforce pattern {NS_PATTERN!r}"
                )
            unix_def = defs.get("UnixTimestampNs")
            if (
                not isinstance(unix_def, dict)
                or unix_def.get("type") != "integer"
                or unix_def.get("minimum") != 0
            ):
                violations.append(
                    f"{rel}: UnixTimestampNs must be integer with minimum 0"
                )

        for def_name, def_schema in defs.items():
            _check_object_additional_properties(
                def_schema,
                location=f"{rel}#/$defs/{def_name}",
                violations=violations,
            )
            if def_name.endswith(ENVELOPE_SUFFIXES) and isinstance(def_schema, dict):
                required = set(def_schema.get("required") or [])
                props = def_schema.get("properties") or {}
                for field_name in REQUIRED_ENVELOPE_FIELDS:
                    if field_name not in required or field_name not in props:
                        violations.append(
                            f"{rel}#/$defs/{def_name}: envelope model missing required field `{field_name}`"
                        )
    if data.get("type") == "object":
        _check_object_additional_properties(
            data,
            location=rel,
            violations=violations,
        )

    return violations


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    repo_root = Path.cwd().resolve()
    schemas_dir = repo_root / "schemas"

    if args:
        targets = [Path(arg) for arg in args if arg.endswith(".schema.json")]
        if not targets:
            sys.stdout.write(
                "schema-contract-check: 0 schema file(s) matched arguments\n"
            )
            return 0
    else:
        targets = sorted(schemas_dir.rglob("*.schema.json"))

    if not (schemas_dir / "common" / "envelope.schema.json").is_file():
        sys.stderr.write("schemas/common/envelope.schema.json is missing\n")
        return 1

    violations: list[str] = []
    for target in targets:
        violations.extend(check_schema_file(target, repo_root))

    if violations:
        sys.stderr.write("\n".join(violations) + "\n")
        return 1

    sys.stdout.write(
        f"schema-contract-check: {len(targets)} schema file(s) valid Draft 2020-12\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
