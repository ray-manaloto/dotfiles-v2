"""Stdlib-only helpers the hub tasks run on pc: AgentsView config rewrites and the Mac-pull log check.

The tasks ship this file's source to pc (base64 in an env var) and run it with `python3 -I -B -c`, so pc
needs no checkout and always runs the same version as the task. Keep it stdlib-only.

Subcommands:
  hub-config <config.toml> <token-file> <mac-ssh-alias|""> <interval>   (hub:up)
  vector <config.toml> <enable|disable> <true|false dry-run>            (hub:vector; recipe in $RECIPE_B64)
  mac-pull <agentsview-data-dir>                                        (hub:status; prints JSON [state, detail])
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path


class RewriteError(Exception):
    """A rewrite was refused; the config file is left unchanged."""


TABLE_HEADER = re.compile(r"""^\s*\[\[?\s*[A-Za-z0-9_.\"' -]+\s*\]\]?\s*(?:#.*)?$""")
# Bare, "quoted" and 'literal' spellings name the same array of tables.
REMOTE_HOSTS_HEADER = re.compile(
    r"""^\s*\[\[\s*(?:remote_hosts|"remote_hosts"|'remote_hosts')\s*\]\]\s*(?:#.*)?$"""
)
VECTOR_HEADER = re.compile(r"""^\s*\[\s*(?:vector|"vector"|'vector')\s*(?:\]|\.)""")
OWNED_TOP_LEVEL = re.compile(r"^\s*(?:auth_token|require_auth|host)\s*=")


def clean_starts(rows: list[str]) -> list[bool]:
    """True when a line starts outside any multi-line string or array.

    Only such a line can hold a table header or a key; bracket or `key =` text inside values is data.
    """
    clean: list[bool] = []
    in_ml: str | None = None
    depth = 0
    for s in rows:
        clean.append(in_ml is None and depth == 0)
        j = 0
        while j < len(s):
            if in_ml:
                k = s.find(in_ml, j)
                if k < 0:
                    break
                in_ml, j = None, k + 3
                continue
            if s.startswith(('"""', "'''"), j):
                in_ml, j = s[j : j + 3], j + 3
                continue
            c = s[j]
            if c in "\"'":
                j += 1
                while j < len(s) and s[j] != c:
                    j += 2 if c == '"' and s[j] == "\\" else 1
            elif c == "#":
                break
            elif c == "[":
                depth += 1
            elif c == "]":
                depth = max(0, depth - 1)
            j += 1
    return clean


def split_tables(lines: list[str]) -> tuple[list[str], list[list[str]], list[bool]]:
    """Split TOML lines into the top-level preamble and tables (each starting at its header)."""
    clean = clean_starts(lines)
    starts = [
        i for i, line in enumerate(lines) if clean[i] and TABLE_HEADER.match(line)
    ]
    first = starts[0] if starts else len(lines)
    tables = [
        lines[a:b] for a, b in zip(starts, [*starts[1:], len(lines)], strict=False)
    ]
    return lines[:first], tables, clean


def toml_str(value: str) -> str:
    # JSON string escapes are a valid subset of TOML basic-string escapes.
    return json.dumps(value, ensure_ascii=False)


def _parse(text: str, what: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RewriteError(f"{what} ({exc})") from exc


# --- hub:up -------------------------------------------------------------------------------------


def rewrite_hub_config(
    original: str, token: str, mac_ssh: str, mac_interval: str
) -> str:
    """Own auth_token/require_auth/host and (with mac_ssh) the Mac [[remote_hosts]] entry; keep the rest."""
    mac_ssh, mac_interval = mac_ssh.strip(), mac_interval.strip()
    before = _parse(original, "not valid TOML")
    lines = original.splitlines()
    preamble_rows, tables, clean = split_tables(lines)
    # Top-level auth_token/require_auth/host are owned here; table keys ([[remote_hosts]] host) are not.
    preamble = [
        line
        for i, line in enumerate(preamble_rows)
        if not (clean[i] and OWNED_TOP_LEVEL.match(line))
    ]

    def is_mac(entry: dict) -> bool:
        return entry.get("host") == "mac" or (
            bool(mac_ssh) and entry.get("destination") == mac_ssh
        )

    def is_mac_table(table: list[str]) -> bool:
        if not REMOTE_HOSTS_HEADER.match(table[0]):
            return False
        try:
            return is_mac(tomllib.loads("\n".join(table[1:])))
        except tomllib.TOMLDecodeError:
            return False

    if mac_ssh:
        # Update the first Mac entry in place (keeping the owner's other keys) and drop duplicates,
        # so reruns are idempotent.
        kept: list[list[str]] = []
        have_mac = False
        for table in tables:
            if not is_mac_table(table):
                kept.append(table)
                continue
            if have_mac:
                continue
            have_mac = True
            managed = {
                "transport": toml_str("ssh"),
                "destination": toml_str(mac_ssh),
                "interval": toml_str(mac_interval),
            }
            rows = table[1:]
            row_clean = clean_starts(rows)
            body = []
            for line, at_key in zip(rows, row_clean, strict=True):
                m = (
                    re.match(r"^\s*(transport|destination|interval)\s*=", line)
                    if at_key
                    else None
                )
                body.append(
                    f"{m.group(1)} = {managed.pop(m.group(1))}"
                    if m and m.group(1) in managed
                    else line
                )
            has_host = any(
                ok and re.match(r"^\s*host\s*=", x)
                for x, ok in zip(rows, row_clean, strict=True)
            )
            head = [] if has_host else [f"host = {toml_str('mac')}"]
            tail = [f"{k} = {v}" for k, v in managed.items()]
            kept.append([table[0], *head, *body, *tail])
        tables = kept
        if not have_mac:
            tables.append(
                [
                    "[[remote_hosts]]",
                    f"host = {toml_str('mac')}",
                    'transport = "ssh"',
                    f"destination = {toml_str(mac_ssh)}",
                    f"interval = {toml_str(mac_interval)}",
                ]
            )

    out = [f"auth_token = {toml_str(token)}", "require_auth = true", *preamble]
    while out and not out[-1].strip():
        out.pop()
    for table in tables:
        while table and not table[-1].strip():
            table.pop()
        out.extend(["", *table])
    text = "\n".join(out).rstrip() + "\n"
    after = _parse(
        text, "rewrite would not parse; remote_hosts must use [[remote_hosts]] tables"
    )

    def mac_entries(d: dict) -> list[dict]:
        hosts = d.get("remote_hosts")
        return (
            [h for h in hosts if isinstance(h, dict) and is_mac(h)]
            if isinstance(hosts, list)
            else []
        )

    def unrelated(d: dict) -> dict:
        # Everything this function does not own must survive the rewrite unchanged.
        d = {
            k: v
            for k, v in d.items()
            if k not in ("auth_token", "require_auth", "host")
        }
        hosts = d.get("remote_hosts")
        if isinstance(hosts, list) and mac_ssh:
            d["remote_hosts"] = [
                h for h in hosts if not (isinstance(h, dict) and is_mac(h))
            ]
        if not d.get("remote_hosts"):
            d.pop("remote_hosts", None)
        return d

    if unrelated(before) != unrelated(after):
        raise RewriteError("rewrite would change settings it does not own")
    if mac_ssh:
        # Exactly one Mac entry: the first prior one with only transport/destination/interval
        # (and a missing host) set, so owner keys inside it cannot change either.
        prior = mac_entries(before)
        expected = dict(prior[0]) if prior else {}
        expected.setdefault("host", "mac")
        expected.update(transport="ssh", destination=mac_ssh, interval=mac_interval)
        if mac_entries(after) != [expected]:
            raise RewriteError(
                "rewrite would not update the Mac [[remote_hosts]] entry exactly"
            )
    return text


# --- hub:vector ---------------------------------------------------------------------------------


def rewrite_vector(original: str, recipe_text: str, enable: bool) -> str:
    """Drop every [vector] / [vector.*] table and, when enabling, append the recipe's tables."""
    recipe = _parse(recipe_text, "recipe is not valid TOML")
    if set(recipe) != {"vector"}:
        raise RewriteError("the recipe may only hold [vector] tables")
    before = _parse(original, "not valid TOML")
    preamble, tables, _ = split_tables(original.splitlines())
    kept = list(preamble)
    for table in tables:
        if not VECTOR_HEADER.match(table[0]):
            kept.extend(table)
    while kept and not kept[-1].strip():
        kept.pop()
    text = "\n".join(kept) + "\n" if kept else ""
    if enable:
        body = [
            ln for ln in recipe_text.splitlines() if not ln.lstrip().startswith("#")
        ]
        text += ("\n" if text else "") + "\n".join(body).strip() + "\n"
    after = _parse(
        text,
        "rewrite would not parse; move any top-level vector keys into [vector] tables",
    )

    def rest(d: dict) -> dict:
        return {k: v for k, v in d.items() if k != "vector"}

    if rest(before) != rest(after):
        raise RewriteError("rewrite would change settings outside [vector]")
    if after.get("vector") != (recipe["vector"] if enable else None):
        raise RewriteError(
            "rewrite would not produce the recipe [vector] tables exactly; "
            "move any top-level vector keys into [vector] tables"
        )
    return text


# --- hub:status ---------------------------------------------------------------------------------

LOG_STAMP = re.compile(r"^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d) ")
ABORTED = "journal_outcome=retained(aborted-before-processing)"


def parse_duration(text: str) -> int:
    m = re.fullmatch(r"([0-9]+)(s|m|h)", text.strip())
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)] if m else 0


def _ago(ts: float, now: float) -> str:
    mins = int((now - ts) // 60)
    return f"{mins}m ago" if mins < 120 else f"{mins // 60}h ago"


def mac_pull_state(av_dir: Path, now: float | None = None) -> tuple[str, str]:
    """WARN-only freshness of the hub's SSH pull of the Mac, read from config.toml and the debug log.

    AgentsView v0.48 logs no success line. A pull's import logs "remote sync delta import: host=mac";
    a failed pull then logs "remote sync mac failed: ..." (or "scheduled remote sync mac: ..."), and
    the next attempt logs "remote sync: host=mac phase=targets". Aborted imports carry ABORTED and are
    ignored. debug.log rotates to debug.log.1. See docs/agentsview-hub.md.
    """
    now = time.time() if now is None else now
    try:
        cfg = tomllib.loads((av_dir / "config.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "SKIPPED", f"no config.toml in {av_dir}"
    except Exception as exc:
        return "WARN", f"config.toml unreadable: {type(exc).__name__}"
    mac = [
        h
        for h in cfg.get("remote_hosts", [])
        if isinstance(h, dict) and h.get("host") == "mac"
    ]
    if not mac:
        return (
            "SKIPPED",
            "no Mac [[remote_hosts]] entry (run hub:mac-pull-key, then hub:up --mac-ssh)",
        )
    every = f"interval {mac[0].get('interval', '?')}"
    interval = parse_duration(str(mac[0].get("interval", "")))
    last_ok = last_fail = pending = None
    fail_text = ""
    for name in ("debug.log.1", "debug.log"):
        try:
            lines = (
                (av_dir / name)
                .read_text(encoding="utf-8", errors="replace")
                .splitlines()
            )
        except OSError:
            continue
        for line in lines:
            m = LOG_STAMP.match(line)
            if not m:
                continue
            try:
                ts = datetime.strptime(m.group(1), "%Y/%m/%d %H:%M:%S").timestamp()
            except ValueError:
                continue
            body = line[m.end() :]
            if ABORTED in body:
                continue
            if body.startswith(
                ("remote sync mac failed: ", "scheduled remote sync mac: ")
            ):
                last_fail, pending = ts, None
                fail_text = body.split(": ", 1)[1]
            elif body.startswith("remote sync delta import: host=mac "):
                pending = ts
            elif (
                body.startswith("remote sync: host=mac phase=targets ")
                and pending is not None
            ):
                last_ok, pending = pending, None
    if pending is not None:
        last_ok = pending
    if last_fail is not None and (last_ok is None or last_fail > last_ok):
        good = f"last good {_ago(last_ok, now)}" if last_ok else "no good pull logged"
        return (
            "WARN",
            f"last pull failed {_ago(last_fail, now)} ({good}, {every}): {fail_text[:120]}",
        )
    if last_ok is None:
        return "WARN", f"no completed pull logged yet ({every})"
    if interval and now - last_ok > 2 * interval + 900:
        return "WARN", f"stale: last good pull {_ago(last_ok, now)} ({every})"
    return "OK", f"last good pull {_ago(last_ok, now)} ({every})"


# --- CLI (runs on pc) ---------------------------------------------------------------------------


def write_private(path: Path, text: str) -> None:
    """Replace *path* atomically with a 0600 file (the config holds auth_token).

    A symlinked config is written through to its target, and a stale `.new` file
    with a looser mode is tightened before any secret is written to it.
    """
    path = path.resolve()
    tmp = path.with_name(path.name + ".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")


def _cmd_hub_config(args: list[str]) -> int:
    cfg, token_file, mac_ssh, interval = Path(args[0]), Path(args[1]), args[2], args[3]
    original = cfg.read_text(encoding="utf-8") if cfg.is_file() else ""
    token = token_file.read_text(encoding="utf-8").strip("\r\n")
    try:
        text = rewrite_hub_config(original, token, mac_ssh, interval or "15m")
    except RewriteError as exc:
        sys.stderr.write(f"hub:up: {cfg}: {exc}; left unchanged, edit it by hand\n")
        return 1
    write_private(cfg, text)
    return 0


def _cmd_vector(args: list[str]) -> int:
    cfg, mode, dry_run = Path(args[0]), args[1], args[2] == "true"
    recipe_text = base64.b64decode(os.environ["RECIPE_B64"]).decode("utf-8")
    original = cfg.read_text(encoding="utf-8")
    try:
        text = rewrite_vector(original, recipe_text, enable=mode == "enable")
    except RewriteError as exc:
        sys.stderr.write(f"hub:vector: {exc}; {cfg} left unchanged\n")
        return 1
    changed = text != original
    _out(f"hub:vector: {mode}: config {'changes' if changed else 'already current'}")
    if mode == "enable":
        emb = tomllib.loads(recipe_text)["vector"]["embeddings"]
        endpoint = emb["servers"][emb["default_server"]]["endpoint"]
        _out(
            f"hub:vector: model {emb['model']} dimension {emb['dimension']} via {endpoint}"
        )
    if dry_run:
        return 0
    if mode == "enable":
        pull = subprocess.run(
            [os.path.expanduser("~/.local/bin/ollama"), "pull", emb["model"]],
            check=False,
            env={**os.environ, "OLLAMA_HOST": "127.0.0.1:11434"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        if pull.returncode != 0:
            sys.stderr.write(
                "hub:vector: ollama pull failed; is dev.mise.ollama-hub.service up? "
                f"run mise run hub:up first; {cfg} left unchanged\n"
            )
            return 1
    if changed:
        write_private(cfg, text)
        subprocess.run(
            ["systemctl", "--user", "restart", "dev.mise.agentsview-hub.service"],
            check=True,
        )
        _out("hub:vector: restarted dev.mise.agentsview-hub.service")
    return 0


def _cmd_mac_pull(args: list[str]) -> int:
    try:
        result = mac_pull_state(Path(args[0]).expanduser())
    except Exception as exc:  # WARN-only: hub:status must keep its other rows
        result = ("WARN", f"mac pull check failed: {type(exc).__name__}")
    _out(json.dumps(list(result)))
    return 0


COMMANDS = {
    "hub-config": (_cmd_hub_config, 4),
    "vector": (_cmd_vector, 3),
    "mac-pull": (_cmd_mac_pull, 1),
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS or len(argv) - 1 != COMMANDS[argv[0]][1]:
        sys.stderr.write(
            f"usage: remote.py {{{'|'.join(COMMANDS)}}} ARGS (see module docstring)\n"
        )
        return 2
    fn, _ = COMMANDS[argv[0]]
    return fn(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
