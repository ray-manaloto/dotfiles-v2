"""Native machine-readable environment inventory collectors, privacy sanitizer, and atomic persistence."""

from __future__ import annotations

import getpass
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import orjson
from filelock import FileLock, Timeout
from pydantic import BaseModel, ValidationError

from dotfiles_py.generated import (
    ContainerOciMetadata,
    EnvInventoryResponse,
    EnvInventorySummary,
    HarnessesInventory,
    HarnessMcpEntry,
    HarnessPluginEntry,
    HarnessStatus,
    InventoryErrorCode,
    InventoryErrorEntry,
    InventoryHistoryRunRecord,
    InventoryReturnCode,
    InventoryReturnEntry,
    InventoryTargetKind,
    MiseInventory,
    MiseSkillEntry,
    MiseToolEntry,
    TargetInventory,
)
from dotfiles_py.logging import get_logger
from dotfiles_py.time_ns import now_ns_pair

type CommandRunner = Callable[
    [list[str], float, dict[str, str] | None], tuple[int, str, str]
]

ALL_TARGETS: tuple[InventoryTargetKind, ...] = (
    InventoryTargetKind.HOST,
    InventoryTargetKind.PC,
    InventoryTargetKind.MAC,
    InventoryTargetKind.VM,
)

CONTAINER_NAME = "dotfiles-v2"
VM_NAME = "dotfiles-v2-macos"
DEFAULT_IMAGE_TAG = "dotfiles-v2:dev"
LOCK_TIMEOUT_S = 10.0

CANONICAL_HOSTNAMES = frozenset(
    {
        "host",
        "pc",
        "mac",
        "vm",
        "localhost",
        "127.0.0.1",
        "::1",
        "docker-desktop",
        "dotfiles-v2",
        "dotfiles-v2-pc",
        "dotfiles-v2-mac",
        "dotfiles-v2-vm",
    }
)
SAFE_GENERIC_USERS = frozenset({"dev", "root", "admin", "nobody"})

_USERS_PATH_RE = re.compile(r"/Users/[A-Za-z0-9._-]+")
_HOME_PATH_RE = re.compile(r"/home/[A-Za-z0-9._-]+")
_TS_NET_HOST_RE = re.compile(
    r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\.ts\.net\b", re.IGNORECASE
)
_SECRET_TOKEN_RE = re.compile(
    r"(?:"
    r"\bgh[pousr]_[A-Za-z0-9_]{4,}\b"
    r"|\bgithub_pat_[A-Za-z0-9_]{4,}\b"
    r"|\bsk-[A-Za-z0-9_-]{6,}\b"
    r"|\btskey-[A-Za-z0-9_-]{4,}\b"
    r"|\bdp\.st\.[A-Za-z0-9_.-]{4,}\b"
    r"|\bxox[baprs]-[A-Za-z0-9-]{4,}\b"
    r"|\bBearer\s+[A-Za-z0-9._~+/=-]{4,}"
    r"|(?<=://)[^/\s:@\"']+?:[^/\s@\"']+?(?=@)"
    r")"
)

_CONTAINER_ARCH_MAP = {
    "amd64": "x86_64",
    "arm64": "aarch64",
}

_VM_EXEC_WRAPPER_SH = (
    'export PATH="${HOME}/.local/share/mise/shims:${HOME}/.opencode/bin:'
    '${HOME}/.local/bin:/usr/local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:${PATH}"; '
    'export MISE_ENV=vm; exec "$@"'
)

_SKILLS_PROBE_PY = (
    "import json, os, pathlib\n"
    "d = pathlib.Path(os.path.expanduser(os.sys.argv[1]))\n"
    "skills = sorted(e.name for e in os.scandir(d) "
    'if not e.name.startswith(".")) if d.is_dir() else []\n'
    "links = []\n"
    'm = d / ".mise-skills.json"\n'
    "try:\n"
    "  raw = json.loads(m.read_text(encoding='utf-8')) if m.is_file() else {}\n"
    "  links_obj = raw.get('links') if isinstance(raw, dict) else None\n"
    "  if isinstance(links_obj, dict):\n"
    "    links = sorted(str(k) for k in links_obj)\n"
    "except (OSError, ValueError, TypeError):\n"
    "  links = []\n"
    "print(json.dumps({'skills': skills, 'mise_skill_links': links}))\n"
)

_LOG = get_logger("env_inventory.collector")


def default_run_command(
    argv: list[str],
    timeout: float = 15.0,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Execute *argv* non-interactively with `stdin=DEVNULL` and return `(rc, stdout, stderr)`."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", f"command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"command timed out after {timeout}s: {' '.join(argv)}"
    except (OSError, subprocess.SubprocessError) as exc:  # fmt: skip
        return 1, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def resolve_inventory_state_dir(state_dir: Path | None = None) -> Path:
    """Resolve `$XDG_STATE_HOME/dotfiles-v2/inventory` (or `~/.local/state/dotfiles-v2/inventory`)."""
    if state_dir is not None:
        return state_dir.expanduser()
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = (
        Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    )
    return base / "dotfiles-v2" / "inventory"


def _strip_git_core_from_path(path_value: str, git_exec_path: str = "") -> str:
    norm_exec = git_exec_path.rstrip(os.sep)
    suffix = f"{os.sep}git-core"
    kept = [
        entry
        for entry in path_value.split(os.pathsep)
        if not entry.rstrip(os.sep).endswith(suffix)
        and (not norm_exec or entry.rstrip(os.sep) != norm_exec)
    ]
    return os.pathsep.join(kept)


def _normalize_host_mise_env(
    repo_root: Path | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Strip shim-mode `mise` task-injected PATH prefixes before running `mise doctor --json`."""
    env = dict(os.environ if base_env is None else base_env)
    path_value = env.get("PATH", "")
    if not path_value:
        return env
    cleaned = _strip_git_core_from_path(path_value, env.get("GIT_EXEC_PATH", ""))
    if env.get("MISE_SHELL") or env.get("__MISE_DIFF"):
        env["PATH"] = cleaned
        return env

    home = Path.home()
    raw_data_dir = env.get("MISE_DATA_DIR")
    if raw_data_dir:
        data_dir = Path(raw_data_dir).expanduser()
    elif env.get("XDG_DATA_HOME"):
        data_dir = Path(env["XDG_DATA_HOME"]).expanduser() / "mise"
    else:
        data_dir = home / ".local" / "share" / "mise"

    bases = tuple(dict.fromkeys([data_dir, data_dir.resolve(strict=False)]))
    shims_dirs = {os.path.normpath(str(d / "shims")) for d in bases}
    entries = [entry for entry in cleaned.split(os.pathsep) if entry]
    if not any(
        os.path.normpath(entry) in shims_dirs or os.path.realpath(entry) in shims_dirs
        for entry in entries
    ):
        env["PATH"] = cleaned
        return env

    installs_exact = {os.path.normpath(str(d / "installs")) for d in bases}
    installs_prefixes = tuple(f"{p}{os.sep}" for p in installs_exact)
    wrappers_dirs = {
        os.path.normpath(str(d / "command-wrappers" / "bin")) for d in bases
    }
    root = repo_root if repo_root is not None else Path.cwd()
    venv_dirs = {
        os.path.normpath(str(r / ".venv" / "bin"))
        for r in (root, root.resolve(strict=False))
    }

    def _is_injected(entry: str) -> bool:
        for cand in (os.path.normpath(entry), os.path.realpath(entry)):
            if (
                cand in installs_exact
                or cand.startswith(installs_prefixes)
                or cand in wrappers_dirs
                or cand in venv_dirs
            ):
                return True
        return False

    env["PATH"] = os.pathsep.join(entry for entry in entries if not _is_injected(entry))
    return env


def _discover_local_hostnames(runner: CommandRunner) -> set[str]:
    names: set[str] = set()
    for cand in (
        socket.gethostname(),
        socket.getfqdn(),
        platform.node(),
        os.uname().nodename,
    ):
        cleaned = cand.strip()
        if cleaned:
            names.add(cleaned)
            if "." in cleaned:
                names.add(cleaned.split(".", 1)[0])
    if sys.platform == "darwin":
        for key in ("LocalHostName", "ComputerName", "HostName"):
            rc, out, _ = runner(["scutil", "--get", key], 3.0, None)
            if rc == 0 and out.strip():
                names.add(out.strip())
    return {
        n
        for n in names
        if n.lower() not in CANONICAL_HOSTNAMES and not _is_ip_literal(n)
    }


def _discover_ssh_hostname(ssh_host: str, runner: CommandRunner) -> set[str]:
    found: set[str] = set()
    cleaned_alias = ssh_host.strip()
    if (
        cleaned_alias
        and cleaned_alias.lower() not in CANONICAL_HOSTNAMES
        and not _is_ip_literal(cleaned_alias)
    ):
        found.add(cleaned_alias)
        if "." in cleaned_alias:
            found.add(cleaned_alias.split(".", 1)[0])

    if not cleaned_alias:
        return found

    rc, out, _ = runner(["ssh", "-G", cleaned_alias], 3.0, None)
    if rc != 0:
        return found
    for line in out.splitlines():
        if line.lower().startswith("hostname "):
            val = line.split(None, 1)[1].strip()
            if (
                val
                and val.lower() not in CANONICAL_HOSTNAMES
                and not _is_ip_literal(val)
            ):
                found.add(val)
                if "." in val:
                    found.add(val.split(".", 1)[0])
            break
    return found


def _is_ip_literal(value: str) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, value)
            return True
        except OSError:
            continue
    return False


def _merge_privacy_ctx(
    ctx: Mapping[str, Any],
    *,
    extra_homes: Iterable[str] = (),
    extra_users: Iterable[str] = (),
    extra_hostnames: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    homes = set(ctx.get("homes", ()))
    for h in extra_homes:
        cleaned = str(h).strip().rstrip("/")
        if cleaned:
            homes.add(cleaned)

    users = set(ctx.get("users", ()))
    for u in extra_users:
        cleaned = str(u).strip()
        if (
            cleaned
            and cleaned.lower() not in SAFE_GENERIC_USERS
            and cleaned.lower() not in CANONICAL_HOSTNAMES
        ):
            users.add(cleaned)

    host_map = dict(ctx.get("host_map", {}))
    if extra_hostnames:
        for canonical, raw_names in extra_hostnames.items():
            for raw in raw_names:
                cleaned = str(raw).strip()
                if (
                    cleaned
                    and cleaned.lower() not in CANONICAL_HOSTNAMES
                    and not _is_ip_literal(cleaned)
                ):
                    host_map[cleaned] = canonical

    return {
        "homes": tuple(sorted(homes, key=len, reverse=True)),
        "users": tuple(sorted(users, key=len, reverse=True)),
        "host_map": dict(
            sorted(host_map.items(), key=lambda kv: len(kv[0]), reverse=True)
        ),
    }


def build_privacy_context(
    *,
    ssh_host: str = "pc",
    runner: CommandRunner | None = None,
    extra_homes: Iterable[str] = (),
    extra_users: Iterable[str] = (),
    extra_hostnames: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """Build normalized privacy-redaction rules for homes, users, and private hostnames."""
    active_runner = default_run_command if runner is None else runner
    homes: set[str] = {
        str(Path.home()),
        str(Path.home().resolve(strict=False)),
        "/home/dev",
    }
    for h in extra_homes:
        cleaned = str(h).strip().rstrip("/")
        if cleaned:
            homes.add(cleaned)

    users: set[str] = set()
    for u in (
        os.environ.get("USER", ""),
        os.environ.get("LOGNAME", ""),
        Path.home().name,
        *extra_users,
    ):
        cleaned = str(u).strip()
        if (
            cleaned
            and cleaned.lower() not in SAFE_GENERIC_USERS
            and cleaned.lower() not in CANONICAL_HOSTNAMES
        ):
            users.add(cleaned)
    try:
        cur_user = getpass.getuser().strip()
        if (
            cur_user
            and cur_user.lower() not in SAFE_GENERIC_USERS
            and cur_user.lower() not in CANONICAL_HOSTNAMES
        ):
            users.add(cur_user)
    except (OSError, KeyError):  # fmt: skip
        pass

    host_map: dict[str, str] = {}
    for name in _discover_local_hostnames(active_runner):
        host_map[name] = "host"
    for name in _discover_ssh_hostname(ssh_host, active_runner):
        host_map[name] = "pc"
    if extra_hostnames:
        for canonical, raw_names in extra_hostnames.items():
            for raw in raw_names:
                cleaned = str(raw).strip()
                if (
                    cleaned
                    and cleaned.lower() not in CANONICAL_HOSTNAMES
                    and not _is_ip_literal(cleaned)
                ):
                    host_map[cleaned] = canonical

    return {
        "homes": tuple(sorted(homes, key=len, reverse=True)),
        "users": tuple(sorted(users, key=len, reverse=True)),
        "host_map": dict(
            sorted(host_map.items(), key=lambda kv: len(kv[0]), reverse=True)
        ),
    }


def _short_token_re(token: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])",
        flags=re.IGNORECASE,
    )


def sanitize_text(text: str, ctx: Mapping[str, Any]) -> str:
    """Strip `/Users/<user>`, `/home/<user>`, `.ts.net`, tokens, raw `$USER`, and private hostnames."""
    out = text
    for home_prefix in ctx.get("homes", ()):
        if home_prefix and home_prefix in out:
            out = out.replace(home_prefix, "~")
    out = _USERS_PATH_RE.sub("~", out)
    out = _HOME_PATH_RE.sub("~", out)
    out = _SECRET_TOKEN_RE.sub("<redacted>", out)
    out = _TS_NET_HOST_RE.sub("<tailnet-host>", out)

    host_map: Mapping[str, str] = ctx.get("host_map", {})
    for private_host, canonical in host_map.items():
        if not private_host:
            continue
        if len(private_host) > 4:
            out = re.sub(re.escape(private_host), canonical, out, flags=re.IGNORECASE)
        else:
            out = _short_token_re(private_host).sub(canonical, out)

    for user in ctx.get("users", ()):
        if not user or user.lower() in SAFE_GENERIC_USERS:
            continue
        if len(user) >= 4:
            out = re.sub(re.escape(user), "<user>", out, flags=re.IGNORECASE)
        else:
            out = _short_token_re(user).sub("<user>", out)
    return out


def _sanitize_value(value: Any, ctx: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        return sanitize_text(value, ctx)
    if isinstance(value, list):
        return [_sanitize_value(item, ctx) for item in value]
    if isinstance(value, dict):
        return {
            sanitize_text(str(k), ctx): _sanitize_value(v, ctx)
            for k, v in value.items()
        }
    return value


def sanitize_model[ModelT: BaseModel](model: ModelT, ctx: Mapping[str, Any]) -> ModelT:
    """Recursively sanitize a generated Pydantic model and re-validate strictly."""
    raw = model.model_dump(mode="json")
    cleaned = _sanitize_value(raw, ctx)
    return type(model).model_validate(cleaned)


def validate_no_privacy_leaks(
    serialized_json: str, ctx: Mapping[str, Any]
) -> list[str]:
    """Return privacy leak descriptions if serialized JSON still contains sensitive paths/names/secrets."""
    leaks: list[str] = []
    if _USERS_PATH_RE.search(serialized_json):
        leaks.append("serialized JSON contains /Users/<user> path")
    if _HOME_PATH_RE.search(serialized_json):
        leaks.append("serialized JSON contains /home/<user> path")
    if _TS_NET_HOST_RE.search(serialized_json):
        leaks.append("serialized JSON contains .ts.net tailnet hostname")
    if _SECRET_TOKEN_RE.search(serialized_json):
        leaks.append("serialized JSON contains secret or credential token")
    for home_prefix in ctx.get("homes", ()):
        if home_prefix and home_prefix in serialized_json:
            leaks.append("serialized JSON contains local home directory prefix")
            break
    for user in ctx.get("users", ()):
        if not user or user.lower() in SAFE_GENERIC_USERS:
            continue
        matched = (
            bool(re.search(re.escape(user), serialized_json, flags=re.IGNORECASE))
            if len(user) >= 4
            else bool(_short_token_re(user).search(serialized_json))
        )
        if matched:
            leaks.append("serialized JSON contains raw username")
            break
    for private_host in ctx.get("host_map", {}):
        if not private_host:
            continue
        matched = (
            bool(
                re.search(re.escape(private_host), serialized_json, flags=re.IGNORECASE)
            )
            if len(private_host) > 4
            else bool(_short_token_re(private_host).search(serialized_json))
        )
        if matched:
            leaks.append("serialized JSON contains private hostname")
            break
    return leaks


def _parse_json_output(raw: str) -> Any | None:
    stripped = raw.strip()
    if not stripped:
        return None
    try:
        return orjson.loads(stripped)
    except orjson.JSONDecodeError:
        return None


def _record_probe_failure(
    errors: list[InventoryErrorEntry],
    label: str,
    rc: int,
    detail: str,
) -> None:
    cleaned = detail.strip()[:200] or f"rc={rc}"
    if rc == 124:
        errors.append(
            InventoryErrorEntry(
                code=InventoryErrorCode.ERR_TIMEOUT,
                message=f"{label} timed out: {cleaned}",
            )
        )
    else:
        errors.append(
            InventoryErrorEntry(
                code=InventoryErrorCode.ERR_SUBPROCESS_FAILED,
                message=f"{label} failed (rc={rc}): {cleaned}",
            )
        )


def _read_local_skills(skills_dir: Path) -> tuple[list[str], list[str]]:
    skills: list[str] = []
    if skills_dir.is_dir():
        try:
            skills = sorted(
                entry.name
                for entry in os.scandir(skills_dir)
                if not entry.name.startswith(".")
            )
        except OSError:
            skills = []
    links: list[str] = []
    manifest = skills_dir / ".mise-skills.json"
    if manifest.is_file():
        try:
            data = orjson.loads(manifest.read_bytes())
            if isinstance(data, dict) and isinstance(data.get("links"), dict):
                links = sorted(str(k) for k in data["links"])
        except (OSError, orjson.JSONDecodeError):  # fmt: skip
            links = []
    return skills, links


def _probe_mise(
    run_target: Callable[[list[str], float], tuple[int, str, str]],
    target_home: str,
    errors: list[InventoryErrorEntry],
) -> MiseInventory:
    rc_doc, out_doc, err_doc = run_target(
        ["mise", "-C", target_home, "doctor", "--json"], 20.0
    )
    if rc_doc == 127:
        return MiseInventory(
            installed=False, errors=[], warnings=[], tools=[], skills=[]
        )

    doc_data = _parse_json_output(out_doc)
    version: str | None = None
    activated: bool | None = None
    build_target: str | None = None
    config_files: list[str] = []
    doc_errors: list[str] = []
    doc_warnings: list[str] = []

    if isinstance(doc_data, dict):
        if doc_data.get("version") is not None:
            version = str(doc_data["version"])
        if isinstance(doc_data.get("activated"), bool):
            activated = bool(doc_data["activated"])
        build_info = doc_data.get("build_info")
        if isinstance(build_info, dict) and build_info.get("target"):
            build_target = str(build_info["target"])
        config_files = [
            str(x) for x in (doc_data.get("config_files") or []) if isinstance(x, str)
        ]
        doc_errors = [str(x) for x in (doc_data.get("errors") or [])]
        doc_warnings = [str(x) for x in (doc_data.get("warnings") or [])]
    else:
        _record_probe_failure(
            errors, "mise doctor --json", rc_doc, err_doc or out_doc or "invalid JSON"
        )

    tools: list[MiseToolEntry] = []
    rc_ls, out_ls, err_ls = run_target(
        ["mise", "-C", target_home, "ls", "--global", "--installed", "--json"], 20.0
    )
    ls_data = _parse_json_output(out_ls) if rc_ls == 0 else None
    if rc_ls != 0:
        _record_probe_failure(
            errors, "mise ls --global --installed --json", rc_ls, err_ls or out_ls
        )
    elif not isinstance(ls_data, dict):
        _record_probe_failure(
            errors,
            "mise ls --global --installed --json",
            rc_ls,
            "expected JSON object",
        )
    else:
        for tool_name, entries in ls_data.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                src = entry.get("source")
                src_type = (
                    str(src.get("type"))
                    if isinstance(src, dict) and src.get("type")
                    else None
                )
                src_path = (
                    str(src.get("path"))
                    if isinstance(src, dict) and src.get("path")
                    else None
                )
                tools.append(
                    MiseToolEntry(
                        name=str(tool_name),
                        version=str(entry.get("version") or ""),
                        requested_version=(
                            str(entry["requested_version"])
                            if entry.get("requested_version") is not None
                            else None
                        ),
                        active=bool(entry.get("active", False)),
                        installed=bool(entry.get("installed", False)),
                        install_path=(
                            str(entry["install_path"])
                            if entry.get("install_path") is not None
                            else None
                        ),
                        source_type=src_type,
                        source_path=src_path,
                    )
                )
        tools.sort(key=lambda t: (t.name, t.version, t.install_path or ""))

    skills: list[MiseSkillEntry] = []
    rc_sk, out_sk, err_sk = run_target(
        ["mise", "-C", target_home, "skills", "ls", "--json"], 15.0
    )
    sk_data = _parse_json_output(out_sk) if rc_sk == 0 else None
    if rc_sk != 0:
        _record_probe_failure(errors, "mise skills ls --json", rc_sk, err_sk or out_sk)
    elif not isinstance(sk_data, list):
        _record_probe_failure(
            errors, "mise skills ls --json", rc_sk, "expected JSON list"
        )
    else:
        for entry in sk_data:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            skills.append(
                MiseSkillEntry(
                    name=str(entry["name"]),
                    tool=str(entry["tool"]) if entry.get("tool") is not None else None,
                    version=(
                        str(entry["version"])
                        if entry.get("version") is not None
                        else None
                    ),
                    path=str(entry["path"]) if entry.get("path") is not None else None,
                )
            )
        skills.sort(key=lambda s: (s.name, s.tool or ""))

    return MiseInventory(
        installed=True,
        version=version,
        activated=activated,
        build_target=build_target,
        config_files=config_files,
        errors=doc_errors,
        warnings=doc_warnings,
        tools=tools,
        skills=skills,
    )


def _normalize_container_arch(raw_arch: str | None) -> str | None:
    if raw_arch is None:
        return None
    cleaned = raw_arch.strip()
    if not cleaned:
        return None
    return _CONTAINER_ARCH_MAP.get(cleaned.lower(), cleaned)


def _normalize_image_ref(ref: str) -> str:
    cleaned = ref.strip()
    for prefix in ("docker.io/library/", "index.docker.io/library/"):
        if cleaned.startswith(prefix):
            return cleaned[len(prefix) :]
    return cleaned


def _metadata_matches_inspected_image(
    meta: Mapping[str, Any],
    *,
    image_tag: str,
    repo_tags: list[str],
    inspected_digests: set[str],
) -> tuple[bool, str | None]:
    raw_names = meta.get("image.name")
    if isinstance(raw_names, str) and raw_names.strip():
        meta_names = {
            _normalize_image_ref(part) for part in raw_names.split(",") if part.strip()
        }
        want_names = {_normalize_image_ref(image_tag)} | {
            _normalize_image_ref(t) for t in repo_tags if t
        }
        if meta_names and not (meta_names & want_names):
            return (
                False,
                f"buildx metadata image.name {sorted(meta_names)} does not match {image_tag}",
            )

    meta_digests: set[str] = set()
    for key in ("containerimage.digest", "containerimage.config.digest"):
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            meta_digests.add(val.strip())
    desc = meta.get("containerimage.descriptor")
    if isinstance(desc, dict) and isinstance(desc.get("digest"), str):
        d = desc["digest"].strip()
        if d:
            meta_digests.add(d)

    if meta_digests and inspected_digests and not (meta_digests & inspected_digests):
        return (
            False,
            "buildx metadata digest does not intersect inspected image digests",
        )
    return True, None


def _probe_container_oci(
    target: InventoryTargetKind,
    image_tag: str,
    state_dir: Path,
    docker_base: list[str],
    runner: CommandRunner,
    errors: list[InventoryErrorEntry],
    returns: list[InventoryReturnEntry],
) -> ContainerOciMetadata:
    rc_img, out_img, err_img = runner(
        [*docker_base, "image", "inspect", image_tag], 15.0, None
    )
    if rc_img == 124:
        _record_probe_failure(
            errors, f"docker image inspect {image_tag}", rc_img, err_img or out_img
        )

    inspected = False
    image_id: str | None = None
    image_digest: str | None = None
    descriptor_media_type: str | None = None
    architecture: str | None = None
    os_name: str | None = None
    size_bytes: int | None = None
    created: str | None = None
    repo_digests: list[str] = []
    repo_tags: list[str] = []

    parsed_img = _parse_json_output(out_img) if rc_img == 0 else None
    if isinstance(parsed_img, list) and parsed_img and isinstance(parsed_img[0], dict):
        item = parsed_img[0]
        inspected = True
        image_id = str(item["Id"]) if item.get("Id") is not None else None
        descriptor = item.get("Descriptor")
        if isinstance(descriptor, dict):
            if descriptor.get("digest") is not None:
                image_digest = str(descriptor["digest"])
            if descriptor.get("mediaType") is not None:
                descriptor_media_type = str(descriptor["mediaType"])
        raw_arch = (
            str(item["Architecture"]) if item.get("Architecture") is not None else None
        )
        architecture = _normalize_container_arch(raw_arch)
        os_name = str(item["Os"]).lower() if item.get("Os") is not None else None
        raw_size = item.get("Size")
        if (
            isinstance(raw_size, int)
            and not isinstance(raw_size, bool)
            and raw_size >= 0
        ):
            size_bytes = raw_size
        created = str(item["Created"]) if item.get("Created") is not None else None
        repo_digests = sorted(
            str(x) for x in (item.get("RepoDigests") or []) if isinstance(x, str)
        )
        repo_tags = [str(x) for x in (item.get("RepoTags") or []) if isinstance(x, str)]

    meta_file = state_dir / f"buildx-{target.value}.metadata.json"
    buildx_metadata_present = False
    buildx_metadata_path: str | None = None
    buildx_build_ref: str | None = None
    sbom_present = False
    provenance_present = False
    provenance_build_type: str | None = None
    provenance_materials: list[str] = []

    if inspected and meta_file.is_file():
        try:
            meta = orjson.loads(meta_file.read_bytes())
        except (OSError, orjson.JSONDecodeError):  # fmt: skip
            meta = None
        if isinstance(meta, dict):
            inspected_digests: set[str] = set()
            if image_id:
                inspected_digests.add(image_id)
            if image_digest:
                inspected_digests.add(image_digest)
            for rd in repo_digests:
                if "@" in rd:
                    inspected_digests.add(rd.split("@", 1)[1].strip())

            matches, mismatch_reason = _metadata_matches_inspected_image(
                meta,
                image_tag=image_tag,
                repo_tags=repo_tags,
                inspected_digests=inspected_digests,
            )
            if not matches:
                returns.append(
                    InventoryReturnEntry(
                        code=InventoryReturnCode.RET_PARTIAL,
                        message=f"ignored stale buildx metadata for {target.value}: {mismatch_reason}",
                    )
                )
            else:
                buildx_metadata_present = True
                buildx_metadata_path = str(meta_file)
                if meta.get("buildx.build.ref") is not None:
                    buildx_build_ref = str(meta["buildx.build.ref"])
                if (
                    image_digest is None
                    and meta.get("containerimage.digest") is not None
                ):
                    image_digest = str(meta["containerimage.digest"])
                desc_meta = meta.get("containerimage.descriptor")
                if isinstance(desc_meta, dict):
                    if image_digest is None and desc_meta.get("digest") is not None:
                        image_digest = str(desc_meta["digest"])
                    if (
                        descriptor_media_type is None
                        and desc_meta.get("mediaType") is not None
                    ):
                        descriptor_media_type = str(desc_meta["mediaType"])
                prov = meta.get("buildx.build.provenance")
                if isinstance(prov, dict) and prov:
                    provenance_present = True
                    if prov.get("buildType") is not None:
                        provenance_build_type = str(prov["buildType"])
                    raw_materials = prov.get("materials") or []
                    if isinstance(raw_materials, list):
                        provenance_materials = sorted(
                            str(m["uri"])
                            for m in raw_materials
                            if isinstance(m, dict) and m.get("uri")
                        )
                    sbom_present = any(
                        "buildkit-syft-scanner" in u or "sbom" in u.lower()
                        for u in provenance_materials
                    )

    return ContainerOciMetadata(
        image_tag=image_tag,
        inspected=inspected,
        image_id=image_id,
        image_digest=image_digest,
        descriptor_media_type=descriptor_media_type,
        architecture=architecture,
        os=os_name,
        size_bytes=size_bytes,
        created=created,
        repo_digests=repo_digests,
        buildx_metadata_present=buildx_metadata_present,
        buildx_metadata_path=buildx_metadata_path,
        buildx_build_ref=buildx_build_ref,
        sbom_present=sbom_present,
        provenance_present=provenance_present,
        provenance_build_type=provenance_build_type,
        provenance_materials=provenance_materials,
    )


def _first_nonempty_line(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _probe_version_cmd(
    run_target: Callable[[list[str], float], tuple[int, str, str]],
    exe: str,
    errors: list[InventoryErrorEntry],
    *,
    join_lines: bool = False,
) -> tuple[bool, str | None]:
    rc, out, err = run_target([exe, "--version"], 15.0)
    if rc == 127:
        return False, None
    if rc != 0:
        _record_probe_failure(errors, f"{exe} --version", rc, err or out)
        return False, None
    if join_lines:
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if not lines:
            return False, None
        return True, " | ".join(lines)
    ver = _first_nonempty_line(out)
    return bool(ver), ver


def _probe_harnesses(
    target: InventoryTargetKind,
    run_target: Callable[[list[str], float], tuple[int, str, str]],
    target_home: str,
    errors: list[InventoryErrorEntry],
) -> HarnessesInventory:
    # 1. agy + ~/.agents/skills
    agy_installed, agy_ver = _probe_version_cmd(run_target, "agy", errors)

    if target is InventoryTargetKind.HOST:
        skills_list, mise_skill_links = _read_local_skills(
            Path(target_home) / ".agents" / "skills"
        )
    else:
        rc_sk, out_sk, err_sk = run_target(
            ["python3", "-c", _SKILLS_PROBE_PY, f"{target_home}/.agents/skills"],
            10.0,
        )
        skills_list, mise_skill_links = [], []
        if rc_sk not in (0, 127):
            _record_probe_failure(errors, "skills probe", rc_sk, err_sk or out_sk)
        elif rc_sk == 0:
            sk_parsed = _parse_json_output(out_sk)
            if isinstance(sk_parsed, dict):
                skills_list = sorted(
                    str(x)
                    for x in (sk_parsed.get("skills") or [])
                    if isinstance(x, str)
                )
                mise_skill_links = sorted(
                    str(x)
                    for x in (sk_parsed.get("mise_skill_links") or [])
                    if isinstance(x, str)
                )
            else:
                _record_probe_failure(
                    errors, "skills probe", rc_sk, "expected JSON object"
                )

    agy_status = HarnessStatus(
        name="agy",
        installed=agy_installed,
        version=agy_ver,
        skills=skills_list,
        mise_skill_links=mise_skill_links,
    )

    # 2. claude
    cl_installed, cl_ver = _probe_version_cmd(run_target, "claude", errors)
    cl_plugins: list[HarnessPluginEntry] = []
    if cl_installed:
        rc_cp, out_cp, err_cp = run_target(["claude", "plugin", "list", "--json"], 15.0)
        if rc_cp != 0:
            _record_probe_failure(
                errors, "claude plugin list --json", rc_cp, err_cp or out_cp
            )
        else:
            cp_parsed = _parse_json_output(out_cp)
            if not isinstance(cp_parsed, list):
                _record_probe_failure(
                    errors, "claude plugin list --json", rc_cp, "expected JSON list"
                )
            else:
                for p in cp_parsed:
                    if not isinstance(p, dict) or not p.get("id"):
                        continue
                    cl_plugins.append(
                        HarnessPluginEntry(
                            id=str(p["id"]),
                            name=(
                                str(p["name"]) if p.get("name") is not None else None
                            ),
                            version=(
                                str(p["version"])
                                if p.get("version") is not None
                                else None
                            ),
                            scope=(
                                str(p["scope"]) if p.get("scope") is not None else None
                            ),
                            enabled=bool(p.get("enabled", False)),
                            install_path=(
                                str(p["installPath"])
                                if p.get("installPath") is not None
                                else None
                            ),
                            project_path=(
                                str(p["projectPath"])
                                if p.get("projectPath") is not None
                                else None
                            ),
                        )
                    )
                cl_plugins.sort(
                    key=lambda e: (e.id, e.scope or "", e.project_path or "")
                )
    claude_status = HarnessStatus(
        name="claude",
        installed=cl_installed,
        version=cl_ver,
        plugins=cl_plugins,
    )

    # 3. codex
    cx_installed, cx_ver = _probe_version_cmd(run_target, "codex", errors)
    cx_plugins: list[HarnessPluginEntry] = []
    cx_mcps: list[HarnessMcpEntry] = []
    if cx_installed:
        rc_cxp, out_cxp, err_cxp = run_target(
            ["codex", "plugin", "list", "--json"], 15.0
        )
        if rc_cxp != 0:
            _record_probe_failure(
                errors, "codex plugin list --json", rc_cxp, err_cxp or out_cxp
            )
        else:
            cxp_parsed = _parse_json_output(out_cxp)
            if not isinstance(cxp_parsed, dict):
                _record_probe_failure(
                    errors, "codex plugin list --json", rc_cxp, "expected JSON object"
                )
            else:
                for p in cxp_parsed.get("installed") or []:
                    if not isinstance(p, dict):
                        continue
                    pid = str(p.get("pluginId") or p.get("name") or "").strip()
                    if not pid:
                        continue
                    src = p.get("source")
                    install_path = (
                        str(src["path"])
                        if isinstance(src, dict) and src.get("path") is not None
                        else None
                    )
                    cx_plugins.append(
                        HarnessPluginEntry(
                            id=pid,
                            name=(
                                str(p["name"]) if p.get("name") is not None else None
                            ),
                            version=(
                                str(p["version"])
                                if p.get("version") is not None
                                else None
                            ),
                            marketplace=(
                                str(p["marketplaceName"])
                                if p.get("marketplaceName") is not None
                                else None
                            ),
                            enabled=bool(p.get("enabled", False)),
                            installed=(
                                bool(p["installed"])
                                if isinstance(p.get("installed"), bool)
                                else None
                            ),
                            install_path=install_path,
                        )
                    )
                cx_plugins.sort(key=lambda e: (e.id, e.version or ""))

        rc_cxm, out_cxm, err_cxm = run_target(["codex", "mcp", "list", "--json"], 15.0)
        if rc_cxm != 0:
            _record_probe_failure(
                errors, "codex mcp list --json", rc_cxm, err_cxm or out_cxm
            )
        else:
            cxm_parsed = _parse_json_output(out_cxm)
            if not isinstance(cxm_parsed, list):
                _record_probe_failure(
                    errors, "codex mcp list --json", rc_cxm, "expected JSON list"
                )
            else:
                for m in cxm_parsed:
                    if not isinstance(m, dict) or not m.get("name"):
                        continue
                    tr = m.get("transport")
                    tr_dict = tr if isinstance(tr, dict) else {}
                    env_vars = sorted(
                        str(v)
                        for v in (tr_dict.get("env_vars") or [])
                        if isinstance(v, str)
                    )
                    cx_mcps.append(
                        HarnessMcpEntry(
                            name=str(m["name"]),
                            enabled=bool(m.get("enabled", False)),
                            disabled_reason=(
                                str(m["disabled_reason"])
                                if m.get("disabled_reason") is not None
                                else None
                            ),
                            transport_type=(
                                str(tr_dict["type"])
                                if tr_dict.get("type") is not None
                                else None
                            ),
                            command=(
                                str(tr_dict["command"])
                                if tr_dict.get("command") is not None
                                else None
                            ),
                            url=(
                                str(tr_dict["url"])
                                if tr_dict.get("url") is not None
                                else None
                            ),
                            auth_status=(
                                str(m["auth_status"])
                                if m.get("auth_status") is not None
                                else None
                            ),
                            env_vars=env_vars,
                        )
                    )
                cx_mcps.sort(key=lambda e: e.name)

    codex_status = HarnessStatus(
        name="codex",
        installed=cx_installed,
        version=cx_ver,
        plugins=cx_plugins,
        mcps=cx_mcps,
    )

    # 4. opencode
    oc_installed, oc_ver = _probe_version_cmd(run_target, "opencode", errors)
    opencode_status = HarnessStatus(
        name="opencode",
        installed=oc_installed,
        version=oc_ver,
    )

    # 5. zcode (graceful optional probe)
    zc_installed, zc_ver = _probe_version_cmd(
        run_target, "zcode", errors, join_lines=True
    )
    zcode_status = HarnessStatus(
        name="zcode",
        installed=zc_installed,
        version=zc_ver,
    )

    return HarnessesInventory(
        agy=agy_status,
        claude=claude_status,
        codex=codex_status,
        opencode=opencode_status,
        zcode=zcode_status,
    )


def _empty_harnesses() -> HarnessesInventory:
    return HarnessesInventory(
        agy=HarnessStatus(name="agy", installed=False),
        claude=HarnessStatus(name="claude", installed=False),
        codex=HarnessStatus(name="codex", installed=False),
        opencode=HarnessStatus(name="opencode", installed=False),
        zcode=HarnessStatus(name="zcode", installed=False),
    )


def _collect_target_inventory_with_discovered(
    target: InventoryTargetKind,
    *,
    ssh_host: str = "pc",
    image_tag: str = DEFAULT_IMAGE_TAG,
    state_dir: Path | None = None,
    repo_root: Path | None = None,
    runner: CommandRunner | None = None,
    privacy_ctx: Mapping[str, Any] | None = None,
) -> tuple[TargetInventory, tuple[str, ...], tuple[str, ...]]:
    active_runner = default_run_command if runner is None else runner
    resolved_state = resolve_inventory_state_dir(state_dir)
    ts, ns = now_ns_pair()
    errors: list[InventoryErrorEntry] = []
    returns: list[InventoryReturnEntry] = []
    extra_homes: list[str] = []
    extra_users: list[str] = []

    arch: str | None = None
    os_name: str | None = None
    reachable = False
    container_meta: ContainerOciMetadata | None = None
    mise_inv = MiseInventory(
        installed=False, errors=[], warnings=[], tools=[], skills=[]
    )
    harnesses = _empty_harnesses()

    if target is InventoryTargetKind.HOST:
        reachable = True
        arch = platform.machine() or None
        raw_sys = platform.system().lower()
        os_name = "macos" if raw_sys == "darwin" else (raw_sys or None)
        target_home = str(Path.home())
        host_env = _normalize_host_mise_env(repo_root=repo_root)

        def run_host(cmd: list[str], timeout: float) -> tuple[int, str, str]:
            cmd_env = host_env if cmd and cmd[0] == "mise" else None
            return active_runner(cmd, timeout, cmd_env)

        mise_inv = _probe_mise(run_host, target_home, errors)
        harnesses = _probe_harnesses(target, run_host, target_home, errors)
        returns.append(
            InventoryReturnEntry(
                code=(
                    InventoryReturnCode.RET_PARTIAL
                    if errors
                    else InventoryReturnCode.RET_OK
                ),
                message=f"collected {target.value} inventory",
            )
        )

    elif target in (InventoryTargetKind.PC, InventoryTargetKind.MAC):
        docker_base = (
            ["env", f"DOCKER_HOST=ssh://{ssh_host}", "docker"]
            if target is InventoryTargetKind.PC
            else ["env", "-u", "DOCKER_HOST", "docker", "--context", "desktop-linux"]
        )
        container_meta = _probe_container_oci(
            target,
            image_tag,
            resolved_state,
            docker_base,
            active_runner,
            errors,
            returns,
        )
        arch = _normalize_container_arch(container_meta.architecture)
        os_name = container_meta.os

        rc_insp, out_insp, err_insp = active_runner(
            [
                *docker_base,
                "container",
                "inspect",
                "--format",
                "{{.State.Running}}",
                CONTAINER_NAME,
            ],
            10.0,
            None,
        )
        if rc_insp == 0 and out_insp.strip() == "true":
            reachable = True
            target_home = "/home/dev"

            def run_container(cmd: list[str], timeout: float) -> tuple[int, str, str]:
                return active_runner(
                    [
                        *docker_base,
                        "exec",
                        "-i",
                        "-w",
                        "/workspace",
                        CONTAINER_NAME,
                        *cmd,
                    ],
                    timeout,
                    None,
                )

            rc_un, out_un, _ = run_container(["uname", "-ms"], 10.0)
            if rc_un == 0 and out_un.strip():
                parts = out_un.strip().split()
                if len(parts) >= 2:
                    os_name = parts[0].lower()
                    arch = _normalize_container_arch(parts[1])

            mise_inv = _probe_mise(run_container, target_home, errors)
            harnesses = _probe_harnesses(target, run_container, target_home, errors)
            returns.append(
                InventoryReturnEntry(
                    code=(
                        InventoryReturnCode.RET_PARTIAL
                        if errors
                        else InventoryReturnCode.RET_OK
                    ),
                    message=f"collected {target.value} container inventory",
                )
            )
        else:
            detail = (err_insp or out_insp).strip() or "container not running"
            if rc_insp == 124:
                _record_probe_failure(
                    errors,
                    f"{target.value} container {CONTAINER_NAME} inspect",
                    rc_insp,
                    detail,
                )
            else:
                errors.append(
                    InventoryErrorEntry(
                        code=InventoryErrorCode.ERR_TARGET_UNREACHABLE,
                        message=f"{target.value} container {CONTAINER_NAME} unavailable: {detail[:200]}",
                    )
                )
            returns.append(
                InventoryReturnEntry(
                    code=(
                        InventoryReturnCode.RET_PARTIAL
                        if container_meta.inspected
                        or container_meta.buildx_metadata_present
                        else InventoryReturnCode.RET_SKIPPED
                    ),
                    message=f"{target.value} container unreachable",
                )
            )

    elif target is InventoryTargetKind.VM:
        rc_vm, _, err_vm = active_runner(["tart", "exec", VM_NAME, "true"], 10.0, None)
        if rc_vm == 0:
            reachable = True

            def run_vm(cmd: list[str], timeout: float) -> tuple[int, str, str]:
                return active_runner(
                    [
                        "tart",
                        "exec",
                        "-i",
                        VM_NAME,
                        "/bin/bash",
                        "-lc",
                        _VM_EXEC_WRAPPER_SH,
                        "bash",
                        *cmd,
                    ],
                    timeout,
                    None,
                )

            rc_home, out_home, _ = run_vm(["printenv", "HOME"], 10.0)
            target_home = (
                out_home.strip()
                if rc_home == 0 and out_home.strip()
                else "/Users/admin"
            )
            extra_homes.append(target_home)
            vm_user = Path(target_home).name
            if vm_user:
                extra_users.append(vm_user)

            rc_un, out_un, _ = run_vm(["uname", "-ms"], 10.0)
            if rc_un == 0 and out_un.strip():
                parts = out_un.strip().split()
                if len(parts) >= 2:
                    os_name = (
                        "macos" if parts[0].lower() == "darwin" else parts[0].lower()
                    )
                    arch = parts[1]

            mise_inv = _probe_mise(run_vm, target_home, errors)
            harnesses = _probe_harnesses(target, run_vm, target_home, errors)
            returns.append(
                InventoryReturnEntry(
                    code=(
                        InventoryReturnCode.RET_PARTIAL
                        if errors
                        else InventoryReturnCode.RET_OK
                    ),
                    message="collected vm inventory",
                )
            )
        else:
            if rc_vm == 124:
                _record_probe_failure(
                    errors, f"Tart VM {VM_NAME}", rc_vm, err_vm or "timed out"
                )
            else:
                errors.append(
                    InventoryErrorEntry(
                        code=InventoryErrorCode.ERR_TARGET_UNREACHABLE,
                        message=f"Tart VM {VM_NAME} unreachable: {err_vm.strip()[:200] or 'not running'}",
                    )
                )
            returns.append(
                InventoryReturnEntry(
                    code=InventoryReturnCode.RET_SKIPPED,
                    message="vm unreachable",
                )
            )

    base_ctx = (
        build_privacy_context(
            ssh_host=ssh_host,
            runner=active_runner,
            extra_homes=extra_homes,
            extra_users=extra_users,
        )
        if privacy_ctx is None
        else privacy_ctx
    )
    merged_ctx = _merge_privacy_ctx(
        base_ctx,
        extra_homes=extra_homes,
        extra_users=extra_users,
    )

    raw_model = TargetInventory(
        schema_version=1,
        target=target,
        reachable=reachable,
        arch=arch,
        os=os_name,
        mise=mise_inv,
        container=container_meta,
        harnesses=harnesses,
        timestamp=ts,
        timestamp_unix_ns=ns,
        errors=errors,
        returns=returns,
    )
    return sanitize_model(raw_model, merged_ctx), tuple(extra_homes), tuple(extra_users)


def collect_target_inventory(
    target: InventoryTargetKind,
    *,
    ssh_host: str = "pc",
    image_tag: str = DEFAULT_IMAGE_TAG,
    state_dir: Path | None = None,
    repo_root: Path | None = None,
    runner: CommandRunner | None = None,
    privacy_ctx: Mapping[str, Any] | None = None,
) -> TargetInventory:
    """Collect, sanitize, and validate inventory for a single canonical *target*."""
    inv, _, _ = _collect_target_inventory_with_discovered(
        target,
        ssh_host=ssh_host,
        image_tag=image_tag,
        state_dir=state_dir,
        repo_root=repo_root,
        runner=runner,
        privacy_ctx=privacy_ctx,
    )
    return inv


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.chmod(0o644)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _format_json_with_oxfmt(
    raw_bytes: bytes,
    *,
    virtual_path: Path,
    repo_root: Path,
) -> bytes:
    """Format JSON bytes via `oxfmt --stdin-filepath` when available so repo checks stay clean."""
    oxfmt_bin = shutil.which("oxfmt")
    if not oxfmt_bin:
        shim = Path.home() / ".local" / "share" / "mise" / "shims" / "oxfmt"
        if shim.is_file() and os.access(shim, os.X_OK):
            oxfmt_bin = str(shim)
    if not oxfmt_bin:
        return raw_bytes
    try:
        proc = subprocess.run(
            [oxfmt_bin, "--stdin-filepath", str(virtual_path)],
            input=raw_bytes,
            capture_output=True,
            cwd=str(repo_root) if repo_root.is_dir() else None,
            timeout=15,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout if proc.stdout.endswith(b"\n") else proc.stdout + b"\n"
    except OSError, subprocess.SubprocessError:
        return raw_bytes
    return raw_bytes


def _append_jsonl_locked(path: Path, line: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    with FileLock(str(lock_path), timeout=LOCK_TIMEOUT_S), path.open("ab") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def collect_inventory(
    *,
    requested_target: str = "host",
    ssh_host: str = "pc",
    image_tag: str = DEFAULT_IMAGE_TAG,
    write_repo: bool = False,
    repo_root: Path | None = None,
    out_dir: Path | None = None,
    state_dir: Path | None = None,
    runner: CommandRunner | None = None,
    privacy_ctx: Mapping[str, Any] | None = None,
) -> EnvInventoryResponse:
    """Collect inventory for `host|pc|mac|vm|all`, sanitize, and optionally write sanitized files."""
    active_runner = default_run_command if runner is None else runner
    resolved_repo = (repo_root or Path.cwd()).resolve()
    resolved_state = resolve_inventory_state_dir(state_dir)
    ctx = (
        build_privacy_context(ssh_host=ssh_host, runner=active_runner)
        if privacy_ctx is None
        else privacy_ctx
    )

    norm_target = requested_target.strip().lower()
    if norm_target == "all":
        selected = list(ALL_TARGETS)
    else:
        try:
            selected = [InventoryTargetKind(norm_target)]
        except ValueError:
            ts, ns = now_ns_pair()
            err = InventoryErrorEntry(
                code=InventoryErrorCode.ERR_INVALID_INPUT,
                message=f"Unsupported target {requested_target!r} (expected host, pc, mac, vm, or all)",
            )
            ret = InventoryReturnEntry(
                code=InventoryReturnCode.RET_SKIPPED,
                message="invalid target",
            )
            return EnvInventoryResponse(
                command="env:inventory",
                rc=2,
                summary=EnvInventorySummary(
                    requested_target=norm_target or "<empty>",
                    targets_collected=0,
                    targets_reachable=0,
                    timestamp=ts,
                    timestamp_unix_ns=ns,
                    errors=[err],
                    returns=[ret],
                ),
                inventories=[],
                timestamp=ts,
                timestamp_unix_ns=ns,
                errors=[err],
                returns=[ret],
            )

    if len(selected) > 1:
        with ThreadPoolExecutor(max_workers=len(selected)) as pool:
            results = list(
                pool.map(
                    lambda tgt: _collect_target_inventory_with_discovered(
                        tgt,
                        ssh_host=ssh_host,
                        image_tag=image_tag,
                        state_dir=resolved_state,
                        repo_root=resolved_repo,
                        runner=active_runner,
                        privacy_ctx=ctx,
                    ),
                    selected,
                )
            )
    else:
        results = [
            _collect_target_inventory_with_discovered(
                selected[0],
                ssh_host=ssh_host,
                image_tag=image_tag,
                state_dir=resolved_state,
                repo_root=resolved_repo,
                runner=active_runner,
                privacy_ctx=ctx,
            )
        ]

    discovered_homes: list[str] = []
    discovered_users: list[str] = []
    raw_inventories: list[TargetInventory] = []
    for inv, homes_found, users_found in results:
        raw_inventories.append(inv)
        discovered_homes.extend(homes_found)
        discovered_users.extend(users_found)

    if discovered_homes or discovered_users:
        ctx = _merge_privacy_ctx(
            ctx,
            extra_homes=discovered_homes,
            extra_users=discovered_users,
        )
    inventories = [sanitize_model(inv, ctx) for inv in raw_inventories]

    should_write = write_repo or (out_dir is not None)
    target_out_dir = (
        out_dir.expanduser().resolve()
        if out_dir is not None
        else resolved_repo / "inventory"
    )
    written_files: list[str] = []
    history_path_str: str | None = None
    top_errors: list[InventoryErrorEntry] = []
    top_returns: list[InventoryReturnEntry] = []

    for inv in inventories:
        top_errors.extend(inv.errors)

    ts, ns = now_ns_pair()
    rc = 0
    if not any(inv.reachable for inv in inventories):
        rc = 1

    if should_write:
        pending_writes: list[tuple[Path, bytes, str]] = []
        leak_detected = False
        for inv in inventories:
            dest = target_out_dir / f"latest.{inv.target.value}.json"
            raw_json_bytes = (
                orjson.dumps(
                    inv.model_dump(mode="json"),
                    option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
                )
                + b"\n"
            )
            pretty_bytes = _format_json_with_oxfmt(
                raw_json_bytes,
                virtual_path=dest,
                repo_root=resolved_repo,
            )
            leaks = validate_no_privacy_leaks(pretty_bytes.decode("utf-8"), ctx)
            if leaks:
                top_errors.append(
                    InventoryErrorEntry(
                        code=InventoryErrorCode.ERR_PRIVACY_LEAK,
                        message=f"Refusing to write {inv.target.value}: {'; '.join(leaks)}",
                    )
                )
                rc = 1
                leak_detected = True
                break
            try:
                rel_dest = dest.relative_to(resolved_repo).as_posix()
            except ValueError:
                rel_dest = sanitize_text(str(dest), ctx)
            pending_writes.append((dest, pretty_bytes, rel_dest))

        if not leak_detected:
            planned_files = [rel_dest for _, _, rel_dest in pending_writes]
            history_file = resolved_state / "history.jsonl"
            history_record = sanitize_model(
                InventoryHistoryRunRecord(
                    schema_version=1,
                    requested_target=norm_target,
                    targets=inventories,
                    written_files=planned_files,
                    timestamp=ts,
                    timestamp_unix_ns=ns,
                    errors=top_errors,
                    returns=[
                        InventoryReturnEntry(
                            code=InventoryReturnCode.RET_WRITTEN,
                            message=f"wrote {len(planned_files)} inventory file(s)",
                        )
                    ],
                ),
                ctx,
            )
            history_line = orjson.dumps(history_record.model_dump(mode="json")) + b"\n"
            hist_leaks = validate_no_privacy_leaks(history_line.decode("utf-8"), ctx)
            if hist_leaks:
                top_errors.append(
                    InventoryErrorEntry(
                        code=InventoryErrorCode.ERR_PRIVACY_LEAK,
                        message=f"Refusing to append history.jsonl: {'; '.join(hist_leaks)}",
                    )
                )
                rc = 1
            else:
                try:
                    for dest, payload, rel_dest in pending_writes:
                        _write_bytes_atomic(dest, payload)
                        written_files.append(rel_dest)
                    _append_jsonl_locked(history_file, history_line)
                    history_path_str = sanitize_text(str(history_file), ctx)
                    top_returns.append(
                        InventoryReturnEntry(
                            code=InventoryReturnCode.RET_WRITTEN,
                            message=f"wrote {len(written_files)} file(s) and appended history.jsonl",
                        )
                    )
                except (OSError, Timeout, ValidationError) as exc:  # fmt: skip
                    top_errors.append(
                        InventoryErrorEntry(
                            code=InventoryErrorCode.ERR_IO_FAILURE,
                            message=f"Failed writing inventory artifacts: {exc}",
                        )
                    )
                    rc = 1

    if not top_returns:
        top_returns.append(
            InventoryReturnEntry(
                code=(
                    InventoryReturnCode.RET_PARTIAL
                    if top_errors
                    else InventoryReturnCode.RET_OK
                ),
                message=f"collected {len(inventories)} target(s)",
            )
        )

    reachable_count = sum(1 for inv in inventories if inv.reachable)
    summary = sanitize_model(
        EnvInventorySummary(
            requested_target=norm_target,
            targets_collected=len(inventories),
            targets_reachable=reachable_count,
            repo_files=written_files,
            history_path=history_path_str,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=top_errors,
            returns=top_returns,
        ),
        ctx,
    )
    resp = sanitize_model(
        EnvInventoryResponse(
            command="env:inventory",
            rc=rc,
            summary=summary,
            inventories=inventories,
            timestamp=ts,
            timestamp_unix_ns=ns,
            errors=top_errors,
            returns=top_returns,
        ),
        ctx,
    )
    _LOG.info(
        "env_inventory_collected",
        requested_target=norm_target,
        targets_collected=len(inventories),
        targets_reachable=reachable_count,
        rc=rc,
    )
    return resp
