"""Native machine-readable environment inventory collectors, privacy sanitizer, and atomic persistence."""

from __future__ import annotations

import getpass
import os
import platform
import re
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

_VM_EXEC_WRAPPER_SH = (
    'export PATH="${HOME}/.local/share/mise/shims:${HOME}/.opencode/bin:'
    '${HOME}/.local/bin:/usr/local/bin:/opt/homebrew/bin:/opt/homebrew/sbin:${PATH}"; '
    'export MISE_ENV=vm; exec "$@"'
)

_SKILLS_PROBE_PY = (
    "import json, os, pathlib; "
    "d = pathlib.Path(os.path.expanduser(os.sys.argv[1])); "
    "skills = sorted(e.name for e in os.scandir(d) "
    'if not e.name.startswith(".")) if d.is_dir() else []; '
    'm = d / ".mise-skills.json"; '
    "links = sorted(json.loads(m.read_text(encoding='utf-8')).get('links', {}).keys()) "
    "if m.is_file() else []; "
    "print(json.dumps({'skills': skills, 'mise_skill_links': links}))"
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
    if not ssh_host or ssh_host.lower() in CANONICAL_HOSTNAMES:
        # Still run `ssh -G` to check alias target hostname.
        pass
    rc, out, _ = runner(["ssh", "-G", ssh_host], 3.0, None)
    if rc != 0:
        return set()
    found: set[str] = set()
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
        if cleaned and cleaned not in SAFE_GENERIC_USERS:
            users.add(cleaned)
    try:
        cur_user = getpass.getuser().strip()
        if cur_user and cur_user not in SAFE_GENERIC_USERS:
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


def sanitize_text(text: str, ctx: Mapping[str, Any]) -> str:
    """Strip `/Users/<user>`, `/home/<user>`, raw `$USER`, and private hostnames from *text*."""
    out = text
    for home_prefix in ctx.get("homes", ()):
        if home_prefix and home_prefix in out:
            out = out.replace(home_prefix, "~")
    out = _USERS_PATH_RE.sub("~", out)
    out = _HOME_PATH_RE.sub("~", out)

    host_map: Mapping[str, str] = ctx.get("host_map", {})
    for private_host, canonical in host_map.items():
        if not private_host:
            continue
        if len(private_host) > 4:
            out = re.sub(re.escape(private_host), canonical, out, flags=re.IGNORECASE)
        else:
            out = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(private_host)}(?![A-Za-z0-9_])",
                canonical,
                out,
            )

    for user in ctx.get("users", ()):
        if not user or user in SAFE_GENERIC_USERS:
            continue
        if len(user) >= 4:
            out = out.replace(user, "<user>")
        else:
            out = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(user)}(?![A-Za-z0-9_])",
                "<user>",
                out,
            )
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
    """Return privacy leak descriptions if serialized JSON still contains sensitive paths/names."""
    leaks: list[str] = []
    if _USERS_PATH_RE.search(serialized_json):
        leaks.append("serialized JSON contains /Users/<user> path")
    if _HOME_PATH_RE.search(serialized_json):
        leaks.append("serialized JSON contains /home/<user> path")
    for home_prefix in ctx.get("homes", ()):
        if home_prefix and home_prefix in serialized_json:
            leaks.append("serialized JSON contains local home directory prefix")
            break
    for user in ctx.get("users", ()):
        if not user or user in SAFE_GENERIC_USERS:
            continue
        matched = (
            user in serialized_json
            if len(user) >= 4
            else bool(
                re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(user)}(?![A-Za-z0-9_])",
                    serialized_json,
                )
            )
        )
        if matched:
            leaks.append("serialized JSON contains raw username")
            break
    for private_host in ctx.get("host_map", {}):
        if len(private_host) <= 4:
            continue
        if re.search(re.escape(private_host), serialized_json, flags=re.IGNORECASE):
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
    if not isinstance(doc_data, dict):
        errors.append(
            InventoryErrorEntry(
                code=InventoryErrorCode.ERR_SUBPROCESS_FAILED,
                message=f"mise doctor --json failed (rc={rc_doc}): {(err_doc or out_doc).strip()[:200]}",
            )
        )
        return MiseInventory(
            installed=True,
            errors=[],
            warnings=[],
            tools=[],
            skills=[],
        )

    build_info = doc_data.get("build_info")
    build_target = (
        str(build_info.get("target"))
        if isinstance(build_info, dict) and build_info.get("target")
        else None
    )
    config_files = [
        str(x) for x in (doc_data.get("config_files") or []) if isinstance(x, str)
    ]
    doc_errors = [str(x) for x in (doc_data.get("errors") or [])]
    doc_warnings = [str(x) for x in (doc_data.get("warnings") or [])]

    tools: list[MiseToolEntry] = []
    rc_ls, out_ls, err_ls = run_target(
        ["mise", "-C", target_home, "ls", "--global", "--installed", "--json"], 20.0
    )
    ls_data = _parse_json_output(out_ls) if rc_ls == 0 else None
    if isinstance(ls_data, dict):
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
    elif rc_ls != 0:
        errors.append(
            InventoryErrorEntry(
                code=InventoryErrorCode.ERR_SUBPROCESS_FAILED,
                message=f"mise ls --global --installed --json failed (rc={rc_ls}): {err_ls.strip()[:200]}",
            )
        )

    skills: list[MiseSkillEntry] = []
    rc_sk, out_sk, err_sk = run_target(
        ["mise", "-C", target_home, "skills", "ls", "--json"], 15.0
    )
    sk_data = _parse_json_output(out_sk) if rc_sk == 0 else None
    if isinstance(sk_data, list):
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
    elif rc_sk != 0:
        errors.append(
            InventoryErrorEntry(
                code=InventoryErrorCode.ERR_SUBPROCESS_FAILED,
                message=f"mise skills ls --json failed (rc={rc_sk}): {err_sk.strip()[:200]}",
            )
        )

    return MiseInventory(
        installed=True,
        version=(
            str(doc_data["version"]) if doc_data.get("version") is not None else None
        ),
        activated=(
            bool(doc_data["activated"])
            if isinstance(doc_data.get("activated"), bool)
            else None
        ),
        build_target=build_target,
        config_files=config_files,
        errors=doc_errors,
        warnings=doc_warnings,
        tools=tools,
        skills=skills,
    )


def _probe_container_oci(
    target: InventoryTargetKind,
    image_tag: str,
    state_dir: Path,
    docker_base: list[str],
    runner: CommandRunner,
) -> ContainerOciMetadata:
    rc_img, out_img, _ = runner(
        [*docker_base, "image", "inspect", image_tag], 15.0, None
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
        architecture = (
            str(item["Architecture"]) if item.get("Architecture") is not None else None
        )
        os_name = str(item["Os"]) if item.get("Os") is not None else None
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

    meta_file = state_dir / f"buildx-{target.value}.metadata.json"
    buildx_metadata_present = False
    buildx_metadata_path: str | None = None
    buildx_build_ref: str | None = None
    sbom_present = False
    provenance_present = False
    provenance_build_type: str | None = None
    provenance_materials: list[str] = []

    if meta_file.is_file():
        try:
            meta = orjson.loads(meta_file.read_bytes())
        except (OSError, orjson.JSONDecodeError):  # fmt: skip
            meta = None
        if isinstance(meta, dict):
            buildx_metadata_present = True
            buildx_metadata_path = str(meta_file)
            if meta.get("buildx.build.ref") is not None:
                buildx_build_ref = str(meta["buildx.build.ref"])
            if image_digest is None and meta.get("containerimage.digest") is not None:
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


def _probe_harnesses(
    target: InventoryTargetKind,
    run_target: Callable[[list[str], float], tuple[int, str, str]],
    target_home: str,
) -> HarnessesInventory:
    # 1. agy + ~/.agents/skills
    rc_agy, out_agy, _ = run_target(["agy", "--version"], 15.0)
    agy_installed = rc_agy == 0 and bool(_first_nonempty_line(out_agy))
    agy_ver = _first_nonempty_line(out_agy) if agy_installed else None

    if target is InventoryTargetKind.HOST:
        skills_list, mise_skill_links = _read_local_skills(
            Path(target_home) / ".agents" / "skills"
        )
    else:
        rc_sk, out_sk, _ = run_target(
            ["python3", "-c", _SKILLS_PROBE_PY, f"{target_home}/.agents/skills"],
            10.0,
        )
        sk_parsed = _parse_json_output(out_sk) if rc_sk == 0 else None
        if isinstance(sk_parsed, dict):
            skills_list = sorted(
                str(x) for x in (sk_parsed.get("skills") or []) if isinstance(x, str)
            )
            mise_skill_links = sorted(
                str(x)
                for x in (sk_parsed.get("mise_skill_links") or [])
                if isinstance(x, str)
            )
        else:
            skills_list, mise_skill_links = [], []

    agy_status = HarnessStatus(
        name="agy",
        installed=agy_installed,
        version=agy_ver,
        skills=skills_list,
        mise_skill_links=mise_skill_links,
    )

    # 2. claude
    rc_cl, out_cl, _ = run_target(["claude", "--version"], 15.0)
    cl_installed = rc_cl == 0 and bool(_first_nonempty_line(out_cl))
    cl_ver = _first_nonempty_line(out_cl) if cl_installed else None
    cl_plugins: list[HarnessPluginEntry] = []
    if cl_installed:
        rc_cp, out_cp, _ = run_target(["claude", "plugin", "list", "--json"], 15.0)
        cp_parsed = _parse_json_output(out_cp) if rc_cp == 0 else None
        if isinstance(cp_parsed, list):
            for p in cp_parsed:
                if not isinstance(p, dict) or not p.get("id"):
                    continue
                cl_plugins.append(
                    HarnessPluginEntry(
                        id=str(p["id"]),
                        name=str(p["name"]) if p.get("name") is not None else None,
                        version=(
                            str(p["version"]) if p.get("version") is not None else None
                        ),
                        scope=str(p["scope"]) if p.get("scope") is not None else None,
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
            cl_plugins.sort(key=lambda e: (e.id, e.scope or "", e.project_path or ""))
    claude_status = HarnessStatus(
        name="claude",
        installed=cl_installed,
        version=cl_ver,
        plugins=cl_plugins,
    )

    # 3. codex
    rc_cx, out_cx, _ = run_target(["codex", "--version"], 15.0)
    cx_installed = rc_cx == 0 and bool(_first_nonempty_line(out_cx))
    cx_ver = _first_nonempty_line(out_cx) if cx_installed else None
    cx_plugins: list[HarnessPluginEntry] = []
    cx_mcps: list[HarnessMcpEntry] = []
    if cx_installed:
        rc_cxp, out_cxp, _ = run_target(["codex", "plugin", "list", "--json"], 15.0)
        cxp_parsed = _parse_json_output(out_cxp) if rc_cxp == 0 else None
        if isinstance(cxp_parsed, dict):
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
                        name=str(p["name"]) if p.get("name") is not None else None,
                        version=(
                            str(p["version"]) if p.get("version") is not None else None
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

        rc_cxm, out_cxm, _ = run_target(["codex", "mcp", "list", "--json"], 15.0)
        cxm_parsed = _parse_json_output(out_cxm) if rc_cxm == 0 else None
        if isinstance(cxm_parsed, list):
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
    rc_oc, out_oc, _ = run_target(["opencode", "--version"], 15.0)
    oc_installed = rc_oc == 0 and bool(_first_nonempty_line(out_oc))
    oc_ver = _first_nonempty_line(out_oc) if oc_installed else None
    opencode_status = HarnessStatus(
        name="opencode",
        installed=oc_installed,
        version=oc_ver,
    )

    # 5. zcode (graceful optional probe)
    rc_zc, out_zc, _ = run_target(["zcode", "--version"], 15.0)
    zc_lines = [line.strip() for line in out_zc.splitlines() if line.strip()]
    zc_installed = rc_zc == 0 and bool(zc_lines)
    zcode_status = HarnessStatus(
        name="zcode",
        installed=zc_installed,
        version=" | ".join(zc_lines) if zc_installed else None,
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
        harnesses = _probe_harnesses(target, run_host, target_home)
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
            target, image_tag, resolved_state, docker_base, active_runner
        )
        arch = container_meta.architecture
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
                    arch = parts[1]

            mise_inv = _probe_mise(run_container, target_home, errors)
            harnesses = _probe_harnesses(target, run_container, target_home)
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
            harnesses = _probe_harnesses(target, run_vm, target_home)
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

    ctx = (
        build_privacy_context(
            ssh_host=ssh_host,
            runner=active_runner,
            extra_homes=extra_homes,
            extra_users=extra_users,
        )
        if privacy_ctx is None
        else privacy_ctx
    )
    if extra_homes or extra_users:
        ctx = {
            "homes": tuple(
                sorted(
                    set(ctx.get("homes", ())) | set(extra_homes),
                    key=len,
                    reverse=True,
                )
            ),
            "users": tuple(
                sorted(
                    set(ctx.get("users", ()))
                    | {u for u in extra_users if u not in SAFE_GENERIC_USERS},
                    key=len,
                    reverse=True,
                )
            ),
            "host_map": dict(ctx.get("host_map", {})),
        }

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
    return sanitize_model(raw_model, ctx)


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
                    requested_target=requested_target,
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
            inventories = list(
                pool.map(
                    lambda tgt: collect_target_inventory(
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
        inventories = [
            collect_target_inventory(
                selected[0],
                ssh_host=ssh_host,
                image_tag=image_tag,
                state_dir=resolved_state,
                repo_root=resolved_repo,
                runner=active_runner,
                privacy_ctx=ctx,
            )
        ]

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
        # Validate every target payload BEFORE writing to disk.
        pending_writes: list[tuple[Path, bytes]] = []
        for inv in inventories:
            pretty_bytes = (
                orjson.dumps(
                    inv.model_dump(mode="json"),
                    option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
                )
                + b"\n"
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
                break
            dest = target_out_dir / f"latest.{inv.target.value}.json"
            pending_writes.append((dest, pretty_bytes))

        if not any(e.code is InventoryErrorCode.ERR_PRIVACY_LEAK for e in top_errors):
            try:
                for dest, payload in pending_writes:
                    _write_bytes_atomic(dest, payload)
                    try:
                        rel_dest = dest.relative_to(resolved_repo).as_posix()
                    except ValueError:
                        rel_dest = sanitize_text(str(dest), ctx)
                    written_files.append(rel_dest)

                history_file = resolved_state / "history.jsonl"
                history_record = sanitize_model(
                    InventoryHistoryRunRecord(
                        schema_version=1,
                        requested_target=norm_target,
                        targets=inventories,
                        written_files=written_files,
                        timestamp=ts,
                        timestamp_unix_ns=ns,
                        errors=top_errors,
                        returns=[
                            InventoryReturnEntry(
                                code=InventoryReturnCode.RET_WRITTEN,
                                message=f"wrote {len(written_files)} inventory file(s)",
                            )
                        ],
                    ),
                    ctx,
                )
                history_line = (
                    orjson.dumps(history_record.model_dump(mode="json")) + b"\n"
                )
                hist_leaks = validate_no_privacy_leaks(
                    history_line.decode("utf-8"), ctx
                )
                if hist_leaks:
                    top_errors.append(
                        InventoryErrorEntry(
                            code=InventoryErrorCode.ERR_PRIVACY_LEAK,
                            message=f"Refusing to append history.jsonl: {'; '.join(hist_leaks)}",
                        )
                    )
                    rc = 1
                else:
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
