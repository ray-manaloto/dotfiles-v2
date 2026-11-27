"""Search Antigravity CLI (agy) history on disk when AgentsView holds no agy sessions (#90).

Stdlib only and Python 3.9 compatible: /usr/bin/python3 is 3.9.6 on macOS. Run it
as `python3 agy-history.py ...` (no shebang, so no executable bit to keep).

    agy-history.py search <term>      conversations/* holding <term>, with deduped snippets
    agy-history.py brain [--term T]   brain/<uuid>/*.md artifacts, newest updatedAt first
    agy-history.py read <uuid>        implementation_plan, walkthrough and learning_proposal
    agy-history.py --self-check       run every subcommand on a scratch fixture

--json and --root go before or after the subcommand. The root defaults to $AGY_HISTORY_ROOT or ~/.gemini/antigravity-cli. Conversation
files are binary, so text is pulled out like `strings` does: runs of printable
ASCII or UTF-8 characters. The same text repeats several times per file, so
snippets are deduplicated.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_KINDS = ("implementation_plan", "walkthrough", "learning_proposal")
METADATA_SUFFIX = ".metadata.json"
# Printable ASCII (plus tab) or a whole UTF-8 multibyte sequence, 4 or more in a row.
_PRINTABLE_RUN = re.compile(rb"(?:[\x20-\x7e\t]|[\xc2-\xf4][\x80-\xbf]{1,3}){4,}")
_ISO_TS = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$"
)


def default_root() -> Path:
    env = os.environ.get("AGY_HISTORY_ROOT")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".gemini" / "antigravity-cli"


def parse_ts(value: object) -> datetime | None:
    """Parse an ISO 8601 timestamp of any fraction length (3.9 fromisoformat cannot)."""
    if not isinstance(value, str):
        return None
    match = _ISO_TS.match(value.strip())
    if not match:
        return None
    day, clock, frac, zone = match.groups()
    frac = ((frac or "") + "000000")[:6]
    if not zone or zone == "Z":
        zone = "+00:00"
    elif ":" not in zone:
        zone = zone[:3] + ":" + zone[3:]
    try:
        return datetime.fromisoformat(f"{day}T{clock}.{frac}{zone}").astimezone(
            timezone.utc
        )
    except ValueError:
        return None


def mtime_utc(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_strings(data: bytes) -> list[str]:
    """Return printable runs of *data*, like `strings`, but keeping UTF-8 text whole."""
    return [
        m.group(0).decode("utf-8", errors="replace")
        for m in _PRINTABLE_RUN.finditer(data)
    ]


def snippets(data: bytes, term: str, context: int, limit: int) -> tuple[int, list[str]]:
    """Count runs holding *term* and return up to *limit* distinct snippets around it."""
    needle = term.lower()
    seen: set[str] = set()
    out: list[str] = []
    hits = 0
    if not needle.strip():
        return hits, out  # "".find("") never advances: refuse instead of spinning
    for text in extract_strings(data):
        low = text.lower()
        start = low.find(needle)
        while start != -1:
            hits += 1
            lo = max(0, start - context)
            hi = min(len(text), start + len(term) + context)
            snippet = " ".join(text[lo:hi].split())
            if snippet not in seen and len(out) < limit:
                seen.add(snippet)
                out.append(snippet)
            start = low.find(needle, start + len(needle))
    return hits, out


def search(root: Path, term: str, context: int = 80, limit: int = 5) -> list[dict]:
    """Conversations under root/conversations holding *term* (case-insensitive), newest first.

    Files sharing a conversation uuid (``<uuid>.db`` plus SQLite sidecars such as
    ``<uuid>.db-wal``) merge into one row; ``paths`` lists every matching file.
    """
    conv_dir = root / "conversations"
    brain_dir = root / "brain"
    if not term.strip():
        raise ValueError("search term is empty")
    # bytes.lower() folds ASCII only, so the cheap prefilter (what `rg -a -l -i`
    # does) is safe for ASCII terms; other terms go straight to string extraction.
    needle = term.lower().encode("utf-8") if term.isascii() else None
    merged: dict[str, dict] = {}
    if not conv_dir.is_dir():
        return []
    for path in sorted(p for p in conv_dir.rglob("*") if p.is_file()):
        try:
            data = path.read_bytes()
            modified = mtime_utc(path)
        except OSError:  # vanished mid-scan, unreadable, ...
            continue
        if needle is not None and needle not in data.lower():
            continue
        hits, found = snippets(data, term, context, limit)
        if not hits:
            continue
        conv_id = path.name.split(".", 1)[0]
        row = merged.get(conv_id)
        if row is None:
            merged[conv_id] = {
                "conversation": conv_id,
                "path": str(path),
                "paths": [str(path)],
                "modified": iso(modified),
                "hits": hits,
                "brain": str(brain_dir / conv_id)
                if (brain_dir / conv_id).is_dir()
                else None,
                "snippets": found,
            }
            continue
        row["paths"].append(str(path))
        row["modified"] = max(row["modified"], iso(modified))
        row["hits"] += hits
        for snip in found:
            if snip not in row["snippets"] and len(row["snippets"]) < limit:
                row["snippets"].append(snip)
    results = list(merged.values())
    results.sort(key=lambda r: (r["modified"], r["conversation"]), reverse=True)
    return results


def _read_metadata(md_path: Path) -> dict:
    meta_path = md_path.with_name(md_path.name + METADATA_SUFFIX)
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def brain(root: Path, term: str | None = None, limit: int = 20) -> list[dict]:
    """brain/<uuid>/*.md artifacts sorted by metadata updatedAt (file mtime if absent)."""
    brain_dir = root / "brain"
    rows: list[dict] = []
    if not brain_dir.is_dir():
        return rows
    needle = term.lower() if term else None
    for md_path in sorted(brain_dir.glob("*/*.md")):
        if not md_path.is_file():  # a directory named *.md, a dangling symlink
            continue
        meta = _read_metadata(md_path)
        summary = str(meta.get("summary") or "")
        try:
            if needle is not None:
                body = md_path.read_text(encoding="utf-8", errors="replace")
                if needle not in body.lower() and needle not in summary.lower():
                    continue
            updated = parse_ts(meta.get("updatedAt")) or mtime_utc(md_path)
        except OSError:  # vanished mid-scan or unreadable
            continue
        rows.append(
            {
                "updated_at": iso(updated),
                "conversation": md_path.parent.name,
                "artifact": md_path.stem,
                "path": str(md_path),
                "summary": summary,
            }
        )
    rows.sort(key=lambda r: (r["updated_at"], r["path"]), reverse=True)
    return rows[:limit] if limit > 0 else rows


def read(root: Path, conv: str, kinds: tuple[str, ...] = DEFAULT_KINDS) -> list[dict]:
    """Artifacts of one brain/<uuid> (a unique uuid prefix is enough), in *kinds* order."""
    brain_dir = root / "brain"
    if not conv or "/" in conv or conv in (".", ".."):
        raise LookupError(f"brain/{conv}: not a conversation uuid")
    exact = brain_dir / conv
    if exact.is_dir():
        target = exact
    else:
        matches = (
            sorted(
                p for p in brain_dir.iterdir() if p.is_dir() and p.name.startswith(conv)
            )
            if brain_dir.is_dir()
            else []
        )
        if len(matches) != 1:
            names = ", ".join(p.name for p in matches) or "none"
            raise LookupError(f"brain/{conv}: expected one match, found {names}")
        target = matches[0]
    out: list[dict] = []
    for kind in kinds:
        md_path = target / f"{kind}.md"
        if not md_path.is_file():
            continue
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.append(
            {
                "conversation": target.name,
                "artifact": kind,
                "path": str(md_path),
                "text": text,
            }
        )
    return out


def _emit(args: argparse.Namespace, rows: list[dict]) -> None:
    if args.json:
        sys.stdout.write(json.dumps(rows, indent=2) + "\n")
        return
    for row in rows:
        if args.command == "search":
            brain_note = f"  brain={row['brain']}" if row["brain"] else ""
            sys.stdout.write(
                f"{row['modified']}  {row['conversation']}  hits={row['hits']}{brain_note}\n"
            )
            for snip in row["snippets"]:
                sys.stdout.write(f"    ... {snip} ...\n")
        elif args.command == "brain":
            sys.stdout.write(
                f"{row['updated_at']}  {row['conversation']}  {row['artifact']}"
                f"  {row['summary']}\n"
            )
        else:
            sys.stdout.write(f"==> {row['path']} <==\n{row['text'].rstrip()}\n\n")
    if not rows:
        sys.stderr.write(f"agy-history {args.command}: no matches under {args.root}\n")


def build_fixture(root: Path) -> None:
    """Write a small agy home: one binary conversation with repeated text, one brain dir."""
    conv_id = "0f3c9a51-7d2e-4b8a-9c1e-5a6b7c8d9e0f"
    other_id = "1a2b3c4d-0000-4000-8000-000000000000"
    conv_dir = root / "conversations"
    conv_dir.mkdir(parents=True)
    text = b"Decision D155: keep the OS keyring; Doppler over Infisical for pc"
    blob = b"SQLite format 3\x00\x10\x00\x01\x01" + b"\x00\x8f\x02" + text + b"\x00"
    (conv_dir / f"{conv_id}.db").write_bytes(
        blob * 3 + b"\xff\x00caf\xc3\xa9 notes\x00"
    )
    # A SQLite sidecar of the same conversation: must merge into the .db row.
    (conv_dir / f"{conv_id}.db-wal").write_bytes(b"\x37\x7f\x06\x82" + text + b"\x00")
    (conv_dir / f"{other_id}.db").write_bytes(b"\x00\x01unrelated wave3 chatter\x00")
    plans = {
        "implementation_plan": (
            "Plan: adopt Doppler pc config",
            "2026-11-25T10:00:00.123456789Z",
        ),
        "walkthrough": ("Walkthrough of the keyring change", "2026-11-26T09:30:00Z"),
        "learning_proposal": ("Learn: record D155", "2026-11-24T08:00:00+02:00"),
    }
    brain_dir = root / "brain" / conv_id
    brain_dir.mkdir(parents=True)
    for kind, (summary, updated) in plans.items():
        (brain_dir / f"{kind}.md").write_text(
            f"# {summary}\n\nDoppler body\n", encoding="utf-8"
        )
        meta = {"artifactType": kind.upper(), "summary": summary, "updatedAt": updated}
        (brain_dir / f"{kind}.md{METADATA_SUFFIX}").write_text(
            json.dumps(meta), encoding="utf-8"
        )
    (brain_dir / "task.md").write_text("# task without metadata\n", encoding="utf-8")
    (brain_dir / "notes.md").mkdir()  # a directory that matches *.md: skipped
    (brain_dir / ".system_generated" / "logs").mkdir(parents=True)


def self_check() -> int:
    with tempfile.TemporaryDirectory(prefix="agy-history-") as tmp:
        root = Path(tmp)
        build_fixture(root)
        errors = []
        hits = search(root, "doppler")
        if (
            len(hits) != 1
            or hits[0]["hits"] != 4
            or len(hits[0]["paths"]) != 2
            or len(hits[0]["snippets"]) != 1
        ):
            errors.append(
                f"search: expected 1 row (.db + .db-wal), 4 hits, 1 snippet: {hits}"
            )
        try:
            search(root, " ")
            errors.append("search: an empty term was accepted")
        except ValueError:
            pass
        if search(root, "café") == []:
            errors.append("search: UTF-8 text was not found")
        order = [r["artifact"] for r in brain(root) if r["artifact"] != "task"]
        if order != ["walkthrough", "implementation_plan", "learning_proposal"]:
            errors.append(f"brain: wrong updatedAt order {order}")
        if [r["artifact"] for r in brain(root, term="D155")] != ["learning_proposal"]:
            errors.append("brain --term: summary filter failed")
        docs = [r["artifact"] for r in read(root, "0f3c")]
        if docs != list(DEFAULT_KINDS):
            errors.append(f"read: expected {list(DEFAULT_KINDS)}, got {docs}")
    if errors:
        sys.stderr.write("\n".join(errors) + "\n")
        return 1
    sys.stdout.write("agy-history: self-check ok\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    def add_common(target: argparse.ArgumentParser, root: object, flag: object) -> None:
        target.add_argument(
            "--root",
            type=Path,
            default=root,
            help="agy home (default ~/.gemini/antigravity-cli)",
        )
        target.add_argument(
            "--json", action="store_true", default=flag, help="emit JSON"
        )

    parser = argparse.ArgumentParser(
        description="Search agy conversations and brain artifacts on disk."
    )
    add_common(parser, None, False)
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="test every subcommand on a scratch fixture",
    )
    # --root/--json also go after the subcommand. SUPPRESS keeps the subparser from
    # overwriting a value given before it with its own default.
    common = argparse.ArgumentParser(add_help=False)
    add_common(common, argparse.SUPPRESS, argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")
    p_search = sub.add_parser(
        "search", parents=[common], help="conversations holding a term"
    )
    p_search.add_argument("term")
    p_search.add_argument(
        "--context", type=int, default=80, help="characters around each hit"
    )
    p_search.add_argument(
        "--snippets", type=int, default=5, help="distinct snippets per file"
    )
    p_brain = sub.add_parser(
        "brain", parents=[common], help="brain artifacts, newest updatedAt first"
    )
    p_brain.add_argument(
        "--term", default=None, help="keep artifacts whose body or summary holds this"
    )
    p_brain.add_argument("--limit", type=int, default=20, help="rows to show (0 = all)")
    p_read = sub.add_parser(
        "read", parents=[common], help="print one conversation's brain artifacts"
    )
    p_read.add_argument("conversation", help="brain/<uuid> name or unique prefix")
    p_read.add_argument(
        "--kind",
        action="append",
        default=None,
        help=f"artifact stem (default {', '.join(DEFAULT_KINDS)})",
    )
    args = parser.parse_args(argv)

    if args.self_check:
        return self_check()
    if args.command is None:
        parser.print_usage(sys.stderr)
        return 2
    if args.command == "search" and not args.term.strip():
        p_search.error("term must not be empty")
    if args.root is None:
        args.root = default_root()
    args.root = args.root.expanduser()
    if args.command == "search":
        rows = search(args.root, args.term, args.context, args.snippets)
    elif args.command == "brain":
        rows = brain(args.root, args.term, args.limit)
    else:
        try:
            rows = read(args.root, args.conversation, tuple(args.kind or DEFAULT_KINDS))
        except LookupError as error:
            sys.stderr.write(f"agy-history read: {error}\n")
            return 1
    _emit(args, rows)
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
