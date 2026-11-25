"""Schema-first cross-OS dependency, OCI, uv, hk, harness, and OpenWiki auditor."""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from dotfiles_py.generated.deps_audit_schema import (
    AuditCategoryStats,
    AuditItem,
    DepsAuditErrorCode,
    DepsAuditErrorEntry,
    DepsAuditResponse,
    DepsAuditReturnCode,
    DepsAuditReturnEntry,
    DepsAuditSourceKind,
    DepsAuditStatus,
    DepsAuditSummary,
)
from dotfiles_py.logging import get_logger
from dotfiles_py.time_ns import now_ns_pair

logger = get_logger("deps_audit")

_HTTP_TIMEOUT_S = 10
_CMD_TIMEOUT_S = 15


def resolve_deps_audit_state_dir(override: Path | None = None) -> Path:
    """Resolve `$XDG_STATE_HOME/dotfiles-v2` cleanly."""
    if override is not None:
        return override
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = (
        Path(os.path.expanduser(xdg_state))
        if xdg_state
        else Path.home() / ".local" / "state"
    )
    return base / "dotfiles-v2"


def _clean_mise_env(repo_root: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for var in ("__MISE_DIFF", "__MISE_SHIM", "__MISE_SESSION", "__MISE_ORIG_PATH"):
        env.pop(var, None)
    env["MISE_OFFLINE"] = "0"
    env["MISE_MINIMUM_RELEASE_AGE"] = "0s"
    env["MISE_YES"] = "1"
    if repo_root is not None:
        mise_dir = repo_root / "unix" / "home" / ".config" / "mise"
        env["MISE_CONFIG_DIR"] = str(mise_dir)
        existing_trust = env.get("MISE_TRUSTED_CONFIG_PATHS", "")
        parts = [str(repo_root), str(mise_dir)]
        if existing_trust:
            parts.append(existing_trust)
        env["MISE_TRUSTED_CONFIG_PATHS"] = os.pathsep.join(parts)
    return env


def _http_json(url: str) -> Any | None:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json, application/json",
            "User-Agent": "dotfiles-v2-deps-audit/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (
        OSError,
        ValueError,
        urllib.error.URLError,
        http.client.HTTPException,
    ) as exc:
        logger.debug("http_json_failed", url=url, error=str(exc))
        return None


def _http_head_exists(url: str) -> bool | None:
    """Check whether `url` exists via HTTP HEAD (`False` on 404/410, `None` on transport error)."""
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "dotfiles-v2-deps-audit/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            return 200 <= getattr(resp, "status", 200) < 400
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 410}:
            return False
        return None
    except OSError, ValueError, urllib.error.URLError, http.client.HTTPException:
        return None


def _gh_latest_tag(repo: str) -> tuple[str | None, str | None]:
    if shutil.which("gh"):
        try:
            out = subprocess.check_output(
                [
                    "gh",
                    "api",
                    f"repos/{repo}/releases/latest",
                    "--jq",
                    ".tag_name // empty",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                timeout=_HTTP_TIMEOUT_S,
            ).strip()
            if out:
                return out, f"https://github.com/{repo}/releases/tag/{out}"
        except (OSError, subprocess.SubprocessError):  # fmt: skip
            pass
    payload = _http_json(f"https://api.github.com/repos/{repo}/releases/latest")
    if isinstance(payload, dict):
        tag = payload.get("tag_name")
        if isinstance(tag, str) and tag:
            return tag, f"https://github.com/{repo}/releases/tag/{tag}"
    return None, f"https://github.com/{repo}/releases"


def _normalize_ver(ver: str | None) -> str:
    if not ver:
        return ""
    return ver.strip().lstrip("v")


def _pep440_equal(a: str | None, b: str | None) -> bool:
    """Compare PyPI/PEP 440 versions treating trailing `.0` numeric segments equivalently."""
    sa = _normalize_ver(a)
    sb = _normalize_ver(b)
    if sa == sb:
        return True
    if re.fullmatch(r"\d+(?:\.\d+)*", sa) and re.fullmatch(r"\d+(?:\.\d+)*", sb):
        pa = [int(x) for x in sa.split(".")]
        pb = [int(x) for x in sb.split(".")]
        while len(pa) > 1 and pa[-1] == 0:
            pa.pop()
        while len(pb) > 1 and pb[-1] == 0:
            pb.pop()
        return pa == pb
    return sa.lower() == sb.lower()


def _is_prerelease_version(ver: str) -> bool:
    if "-" in ver and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ver):
        return True
    return bool(
        re.search(
            r"(?:(?<=\d)(?:a|b|rc)\d+|\.(?:dev|post)\d+|(?:alpha|beta|pre)\d*)",
            ver,
            re.IGNORECASE,
        )
    )


def _compare_url_github(
    repo: str, current: str | None, latest: str | None
) -> str | None:
    if not current or not latest:
        return None
    cur_norm = _normalize_ver(current)
    lat_norm = _normalize_ver(latest)
    if not cur_norm or not lat_norm or cur_norm == lat_norm:
        return None
    c_ref = current if current.startswith("v") else f"v{cur_norm}"
    l_ref = latest if latest.startswith("v") else f"v{lat_norm}"
    return f"https://github.com/{repo}/compare/{c_ref}...{l_ref}"


def _audit_mise_binary(repo_root: Path, *, offline: bool) -> list[AuditItem]:
    items: list[AuditItem] = []
    versions: dict[str, str] = {}
    root_toml = repo_root / "mise.toml"
    global_toml = repo_root / "unix" / "home" / ".config" / "mise" / "config.toml"
    dockerfile = repo_root / "docker" / "ubuntu" / "Dockerfile"
    vm_up = repo_root / "tasks" / "vm" / "up"
    env_doctor = repo_root / "tasks" / "env" / "doctor"

    for rel, path in (
        ("mise.toml", root_toml),
        ("unix/home/.config/mise/config.toml", global_toml),
    ):
        if path.is_file():
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
                mv = data.get("min_version")
                if isinstance(mv, str) and mv:
                    versions[rel] = mv
            except OSError, tomllib.TOMLDecodeError:
                pass

    docker_digest: str | None = None
    if dockerfile.is_file():
        try:
            df_txt = dockerfile.read_text(encoding="utf-8")
        except OSError:
            df_txt = ""
        m = re.search(
            r"FROM\s+(?:--[^\s=]+=\S+\s+)*(?:ghcr\.io/jdx|jdxcode)/mise:([0-9]+\.[0-9]+\.[0-9]+)@(sha256:[0-9a-f]+)",
            df_txt,
        )
        if m:
            versions["docker/ubuntu/Dockerfile"] = m.group(1)
            docker_digest = m.group(2)

    packslip_pin: str | None = None
    if vm_up.is_file():
        try:
            vm_txt = vm_up.read_text(encoding="utf-8")
        except OSError:
            vm_txt = ""
        mv_m = re.search(r'^MISE_VER="([0-9]+\.[0-9]+\.[0-9]+)"', vm_txt, re.M)
        if mv_m:
            versions["tasks/vm/up"] = mv_m.group(1)
        pin_m = re.search(r'(?:--pin\s+|PACKSLIP_MISE_PIN=")(ps1_[a-z0-9]+)', vm_txt)
        if pin_m:
            packslip_pin = pin_m.group(1)

    if env_doctor.is_file():
        try:
            doc_txt = env_doctor.read_text(encoding="utf-8")
        except OSError:
            doc_txt = ""
        doc_vers = re.findall(
            r'(?:version_ge\s+"\$\{mise_ver\}"\s+"|printf\s+\'%s\\n%s\\n\'\s+")([0-9]+\.[0-9]+\.[0-9]+)"',
            doc_txt,
        )
        if doc_vers:
            versions["tasks/env/doctor"] = (
                doc_vers[0]
                if len(set(doc_vers)) == 1
                else ",".join(sorted(set(doc_vers)))
            )

    for wf_rel in (".github/workflows/ci.yml", ".github/workflows/health.yml"):
        wf_path = repo_root / wf_rel
        if wf_path.is_file():
            try:
                wf_txt = wf_path.read_text(encoding="utf-8")
            except OSError:
                wf_txt = ""
            wf_vers = re.findall(
                r"uses:\s*jdx/mise-action[^\n]*\n(?:[ \t]+[^\n]*\n)*?[ \t]+version:\s*['\"]?([0-9]+\.[0-9]+\.[0-9]+)['\"]?",
                wf_txt,
            )
            if wf_vers:
                versions[wf_rel] = (
                    wf_vers[0]
                    if len(set(wf_vers)) == 1
                    else ",".join(sorted(set(wf_vers)))
                )

    cur_ver = versions.get("unix/home/.config/mise/config.toml") or versions.get(
        "mise.toml"
    )
    skew = len(set(versions.values())) > 1
    latest_tag: str | None = None
    rel_url = "https://github.com/jdx/mise/releases"
    if not offline:
        latest_tag, rel_url = _gh_latest_tag("jdx/mise")
    latest_ver = _normalize_ver(latest_tag) if latest_tag else None

    if skew:
        status = DepsAuditStatus.OUTDATED
        note = "Mise pin skew across files: " + ", ".join(
            f"{k}={v}" for k, v in sorted(versions.items())
        )
    elif offline:
        status = DepsAuditStatus.CURRENT if cur_ver else DepsAuditStatus.UNKNOWN
        note = (
            f"offline parity ok; docker_digest={docker_digest or '-'} "
            f"packslip_pin={packslip_pin or '-'}"
        )
    elif latest_ver is None:
        status = DepsAuditStatus.UNKNOWN
        note = (
            f"upstream mise release lookup failed; docker_digest={docker_digest or '-'} "
            f"packslip_pin={packslip_pin or '-'}"
        )
    elif cur_ver and latest_ver != cur_ver:
        status = DepsAuditStatus.OUTDATED
        note = (
            f"docker_digest={docker_digest or '-'} packslip_pin={packslip_pin or '-'}"
        )
    else:
        status = DepsAuditStatus.CURRENT if cur_ver else DepsAuditStatus.UNKNOWN
        note = (
            f"docker_digest={docker_digest or '-'} packslip_pin={packslip_pin or '-'}"
        )

    items.append(
        AuditItem(
            name="mise",
            source_kind=DepsAuditSourceKind.MISE_BINARY,
            file_path="unix/home/.config/mise/config.toml",
            os_scope=["macos", "linux", "container", "vm"],
            current_version=cur_ver,
            latest_version=latest_ver,
            status=status,
            release_url=rel_url,
            compare_url=_compare_url_github("jdx/mise", cur_ver, latest_ver),
            note=note,
        )
    )
    return items


def _infer_cfg_os_scope(cfg_name: str, spec: Any) -> list[str]:
    if isinstance(spec, dict) and isinstance(spec.get("os"), list):
        return [str(x) for x in spec["os"]]
    if "container" in cfg_name:
        return ["linux", "container"]
    if "linux" in cfg_name:
        return ["linux"]
    if "vm" in cfg_name:
        return ["macos", "vm"]
    if "macos" in cfg_name:
        return ["macos"]
    return ["macos", "linux", "container", "vm"]


def _probe_mise_tool_remote(
    tool_key: str, *, prerelease: bool, repo_root: Path
) -> tuple[str | None, str | None]:
    """Query `mise ls-remote <tool_key>` returning `(latest_version, note)`."""
    base_key = tool_key.split("[", 1)[0]
    mise_dir = repo_root / "unix" / "home" / ".config" / "mise"
    try:
        out = subprocess.check_output(
            ["mise", "ls-remote", base_key],
            cwd=str(mise_dir) if mise_dir.is_dir() else str(repo_root),
            env=_clean_mise_env(repo_root),
            text=True,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=_CMD_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # fmt: skip
        return None, f"ls-remote failed: {exc}"
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines:
        return None, "ls-remote returned no versions"
    if not prerelease:
        stable = [ln for ln in lines if not _is_prerelease_version(ln)]
        if stable:
            return stable[-1], None
    return lines[-1], None


def _load_locked_versions(mise_dir: Path) -> dict[str, set[str]] | None:
    main_lock = mise_dir / "mise.lock"
    if not main_lock.is_file():
        return None
    locked: dict[str, set[str]] = {}
    for lock_path in (main_lock, mise_dir / "mise.container.lock"):
        if not lock_path.is_file():
            continue
        try:
            ldata = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        except OSError, tomllib.TOMLDecodeError:
            continue
        tools = ldata.get("tools")
        if not isinstance(tools, dict):
            continue
        for key, entries in tools.items():
            if not isinstance(entries, list):
                continue
            vers = locked.setdefault(str(key), set())
            for entry in entries:
                if isinstance(entry, dict) and entry.get("version"):
                    vers.add(_normalize_ver(str(entry["version"])))
    return locked


def _tailscale_tarball_available(version: str) -> bool | None:
    norm = _normalize_ver(version)
    if not norm:
        return None
    saw_missing = False
    saw_ok = True
    for arch in ("amd64", "arm64"):
        url = f"https://pkgs.tailscale.com/stable/tailscale_{norm}_{arch}.tgz"
        res = _http_head_exists(url)
        if res is False:
            saw_missing = True
        elif res is None:
            saw_ok = False
    if saw_missing:
        return False
    return True if saw_ok else None


def _audit_mise_tools(repo_root: Path, *, offline: bool) -> list[AuditItem]:
    mise_dir = repo_root / "unix" / "home" / ".config" / "mise"
    if not mise_dir.is_dir():
        return []
    raw_entries: list[tuple[str, str, list[str], str, bool]] = []
    for cfg_path in sorted(mise_dir.glob("config*.toml")):
        rel = cfg_path.relative_to(repo_root).as_posix()
        try:
            data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        except OSError, tomllib.TOMLDecodeError:
            continue
        tools = data.get("tools")
        if isinstance(tools, dict):
            for name, spec in tools.items():
                key = str(name)
                if isinstance(spec, str):
                    ver = spec
                    prerelease = False
                elif isinstance(spec, dict):
                    ver = str(spec.get("version") or "")
                    prerelease = bool(spec.get("prerelease"))
                else:
                    ver = ""
                    prerelease = False
                raw_entries.append(
                    (
                        key,
                        ver,
                        _infer_cfg_os_scope(cfg_path.name, spec),
                        rel,
                        prerelease,
                    )
                )
        bootstrap = data.get("bootstrap")
        if isinstance(bootstrap, dict) and isinstance(bootstrap.get("packages"), dict):
            for pkg_name, pkg_ver in bootstrap["packages"].items():
                raw_entries.append(
                    (
                        str(pkg_name),
                        str(pkg_ver),
                        _infer_cfg_os_scope(cfg_path.name, pkg_ver),
                        rel,
                        False,
                    )
                )

    locked_versions = _load_locked_versions(mise_dir)
    remote_results: dict[str, tuple[str | None, str | None]] = {}
    if not offline:
        query_keys: list[tuple[str, bool]] = []
        seen_q: set[str] = set()
        for key, _ver, _scope, _rel, pre in raw_entries:
            if key.startswith(("apt:", "brew:", "brew-cask:")):
                continue
            if key not in seen_q:
                seen_q.add(key)
                query_keys.append((key, pre))
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {
                key: pool.submit(
                    _probe_mise_tool_remote, key, prerelease=pre, repo_root=repo_root
                )
                for key, pre in query_keys
            }
            for key, fut in futs.items():
                try:
                    remote_results[key] = fut.result()
                except Exception as exc:
                    remote_results[key] = (None, f"probe raised: {exc}")

    items: list[AuditItem] = []
    for key, cur_ver, os_scope, rel, _pre in raw_entries:
        rel_url: str | None = None
        cmp_url: str | None = None
        note: str | None = None
        latest_ver: str | None = None

        if key.startswith(("github:", "packslip:")):
            slug = key.split(":", 1)[1].split("[", 1)[0]
            rel_url = f"https://github.com/{slug}/releases"
        elif key.startswith("npm:"):
            pkg = key.split(":", 1)[1].split("[", 1)[0]
            rel_url = f"https://www.npmjs.com/package/{pkg}"

        if key.startswith(("apt:", "brew:", "brew-cask:")):
            status = DepsAuditStatus.PINNED_HELD
            note = "OS package manager pin (excluded from mise ls-remote)"
        elif offline:
            base_tool_key = key.split("[", 1)[0]
            if not cur_ver:
                status = DepsAuditStatus.UNKNOWN
                note = "offline mode (unpinned version)"
            elif locked_versions is not None and (
                _normalize_ver(cur_ver) not in locked_versions.get(base_tool_key, set())
            ):
                status = DepsAuditStatus.OUTDATED
                note = f"missing or mismatched {base_tool_key}={cur_ver} in mise*.lock"
            else:
                status = DepsAuditStatus.CURRENT
                note = "offline mode"
        else:
            latest_ver, err_note = remote_results.get(key, (None, None))
            if err_note:
                status = DepsAuditStatus.UNKNOWN
                note = err_note
            elif (
                latest_ver
                and cur_ver
                and _normalize_ver(latest_ver) != _normalize_ver(cur_ver)
            ):
                if (
                    key == "tailscale"
                    and _tailscale_tarball_available(latest_ver) is False
                ):
                    status = DepsAuditStatus.PINNED_HELD
                    note = (
                        f"upstream pkgs.tailscale.com tarball 404 for {latest_ver}; "
                        f"held at {cur_ver}"
                    )
                else:
                    status = DepsAuditStatus.OUTDATED
            elif latest_ver:
                status = DepsAuditStatus.CURRENT
            else:
                status = DepsAuditStatus.UNKNOWN
            if key.startswith(("github:", "packslip:")) and latest_ver:
                slug = key.split(":", 1)[1].split("[", 1)[0]
                cmp_url = _compare_url_github(slug, cur_ver, latest_ver)

        items.append(
            AuditItem(
                name=key,
                source_kind=DepsAuditSourceKind.MISE_TOOL,
                file_path=rel,
                os_scope=os_scope,
                current_version=cur_ver or None,
                latest_version=latest_ver,
                status=status,
                release_url=rel_url,
                compare_url=cmp_url,
                note=note,
            )
        )
    return items


def _audit_uv_packages(repo_root: Path, *, offline: bool) -> list[AuditItem]:
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except OSError, tomllib.TOMLDecodeError:
        return []
    specs: list[str] = []
    proj = data.get("project")
    if isinstance(proj, dict) and isinstance(proj.get("dependencies"), list):
        specs.extend(str(s) for s in proj["dependencies"])
    groups = data.get("dependency-groups")
    if isinstance(groups, dict):
        for vals in groups.values():
            if isinstance(vals, list):
                specs.extend(str(v) for v in vals if isinstance(v, str))

    parsed: list[tuple[str, str]] = []
    for raw in specs:
        m = re.match(
            r"^([A-Za-z0-9_.\-]+(?:\[[A-Za-z0-9_,.\-]+\])?)\s*==\s*([^\s;,]+)",
            raw.strip(),
        )
        if m:
            pkg_name = m.group(1).split("[", 1)[0]
            parsed.append((pkg_name, m.group(2)))

    latest_map: dict[str, str | None] = {}
    if not offline and parsed:

        def _pypi_latest(name: str) -> str | None:
            payload = _http_json(f"https://pypi.org/pypi/{name}/json")
            if isinstance(payload, dict) and isinstance(payload.get("info"), dict):
                v = payload["info"].get("version")
                return str(v) if isinstance(v, str) and v else None
            return None

        unique_pkgs = sorted({pkg for pkg, _ in parsed})
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pkg: pool.submit(_pypi_latest, pkg) for pkg in unique_pkgs}
            for pkg, fut in futs.items():
                try:
                    latest_map[pkg] = fut.result()
                except Exception:
                    latest_map[pkg] = None

    items: list[AuditItem] = []
    for pkg_name, cur_ver in parsed:
        lat = latest_map.get(pkg_name)
        note: str | None = "offline mode" if offline else None
        if offline:
            status = DepsAuditStatus.CURRENT
        elif lat and not _pep440_equal(cur_ver, lat):
            status = DepsAuditStatus.OUTDATED
        elif lat:
            status = DepsAuditStatus.CURRENT
        else:
            status = DepsAuditStatus.UNKNOWN
            note = "pypi metadata lookup failed"
        items.append(
            AuditItem(
                name=pkg_name,
                source_kind=DepsAuditSourceKind.UV_PACKAGE,
                file_path="pyproject.toml",
                os_scope=["macos", "linux", "container", "vm"],
                current_version=cur_ver,
                latest_version=lat,
                status=status,
                release_url=f"https://pypi.org/project/{pkg_name}/"
                + (f"{lat}/" if lat else f"{cur_ver}/"),
                compare_url=None,
                note=note,
            )
        )
    return items


def _audit_oci_images(repo_root: Path, *, offline: bool) -> list[AuditItem]:
    dockerfile = repo_root / "docker" / "ubuntu" / "Dockerfile"
    if not dockerfile.is_file():
        return []
    try:
        df_lines = dockerfile.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    items: list[AuditItem] = []
    seen: set[str] = set()
    for line in df_lines:
        m = re.match(r"^\s*FROM\s+(?:--[^\s=]+=\S+\s+)*([^\s]+)", line)
        if not m:
            continue
        ref = m.group(1)
        if ref.lower() == "scratch":
            continue
        if ref in seen:
            continue
        seen.add(ref)
        name, _, digest = ref.partition("@")
        latest_digest: str | None = None
        note: str | None = None
        if offline:
            status = DepsAuditStatus.CURRENT if digest else DepsAuditStatus.UNKNOWN
            note = "offline mode"
        else:
            status = DepsAuditStatus.UNKNOWN
            if shutil.which("docker"):
                try:
                    raw = subprocess.check_output(
                        ["docker", "buildx", "imagetools", "inspect", name],
                        text=True,
                        stderr=subprocess.DEVNULL,
                        stdin=subprocess.DEVNULL,
                        timeout=_CMD_TIMEOUT_S,
                    )
                    dm = re.search(r"Digest:\s*(sha256:[0-9a-f]+)", raw)
                    if dm:
                        latest_digest = dm.group(1)
                        status = (
                            DepsAuditStatus.CURRENT
                            if digest and latest_digest == digest
                            else DepsAuditStatus.OUTDATED
                        )
                    else:
                        note = "docker imagetools inspect returned no Digest"
                except (OSError, subprocess.SubprocessError) as exc:  # fmt: skip
                    note = f"docker imagetools inspect unavailable: {exc}"
            else:
                note = "docker CLI unavailable on PATH"
        items.append(
            AuditItem(
                name=name,
                source_kind=DepsAuditSourceKind.OCI_IMAGE,
                file_path="docker/ubuntu/Dockerfile",
                os_scope=["linux", "container"],
                current_version=digest or name,
                latest_version=latest_digest,
                status=status,
                release_url=None,
                compare_url=None,
                note=note,
            )
        )
    return items


def _audit_hk_preset(repo_root: Path, *, offline: bool) -> list[AuditItem]:
    hk_pkl = repo_root / "hk.pkl"
    if not hk_pkl.is_file():
        return []
    try:
        txt = hk_pkl.read_text(encoding="utf-8")
    except OSError:
        return []
    items: list[AuditItem] = []

    cfg_vers = re.findall(
        r"raw\.githubusercontent\.com/ray-manaloto/hk-config/v([0-9]+\.[0-9]+\.[0-9]+)/",
        txt,
    )
    # SHA-pinned form: raw URLs use a 40-hex commit and a `// hk-config vX.Y.Z` comment names it.
    if re.search(
        r"raw\.githubusercontent\.com/ray-manaloto/hk-config/[0-9a-f]{40}/", txt
    ):
        sha_vers = re.findall(
            r"^//\s*hk-config v([0-9]+\.[0-9]+\.[0-9]+)\b", txt, re.MULTILINE
        )
        cfg_vers += sha_vers or ["unversioned-sha"]
        if len(set(re.findall(r"ray-manaloto/hk-config/([0-9a-f]{40})/", txt))) > 1:
            cfg_vers.append("mixed-commit-shas")
    if cfg_vers:
        unique_cfg = sorted(set(cfg_vers))
        cur = unique_cfg[0]
        skew_cfg = (
            f"hk.pkl references mixed hk-config versions: {', '.join(unique_cfg)}"
            if len(unique_cfg) > 1
            else None
        )
        lat_tag, rel_url = (None, "https://github.com/ray-manaloto/hk-config/releases")
        if not offline:
            lat_tag, rel_url = _gh_latest_tag("ray-manaloto/hk-config")
        lat = _normalize_ver(lat_tag) if lat_tag else None
        if skew_cfg or (lat and lat != cur):
            st = DepsAuditStatus.OUTDATED
            note_cfg = skew_cfg
        elif offline:
            st = DepsAuditStatus.CURRENT
            note_cfg = "offline mode"
        elif lat is None:
            st = DepsAuditStatus.UNKNOWN
            note_cfg = "upstream release lookup failed"
        else:
            st = DepsAuditStatus.CURRENT
            note_cfg = None
        items.append(
            AuditItem(
                name="ray-manaloto/hk-config",
                source_kind=DepsAuditSourceKind.HK_PRESET,
                file_path="hk.pkl",
                os_scope=["macos", "linux", "container", "vm"],
                current_version=cur,
                latest_version=lat,
                status=st,
                release_url=rel_url,
                compare_url=_compare_url_github("ray-manaloto/hk-config", cur, lat),
                note=note_cfg,
            )
        )

    hk_matches = re.findall(
        r"github\.com/jdx/hk/releases/download/v([0-9]+\.[0-9]+\.[0-9]+)/hk@([0-9]+\.[0-9]+\.[0-9]+)",
        txt,
    )
    if hk_matches:
        all_hk = sorted({v for pair in hk_matches for v in pair})
        cur_hk = all_hk[0]
        cfg_hk: str | None = None
        global_toml = repo_root / "unix" / "home" / ".config" / "mise" / "config.toml"
        if global_toml.is_file():
            try:
                cdata = tomllib.loads(global_toml.read_text(encoding="utf-8"))
                hk_spec = (cdata.get("tools") or {}).get("hk")
                if isinstance(hk_spec, dict):
                    cfg_hk = str(hk_spec.get("version") or "")
                elif isinstance(hk_spec, str):
                    cfg_hk = hk_spec
            except OSError, tomllib.TOMLDecodeError:
                pass
        lat_tag, rel_url = (None, "https://github.com/jdx/hk/releases")
        if not offline:
            lat_tag, rel_url = _gh_latest_tag("jdx/hk")
        lat_hk = _normalize_ver(lat_tag) if lat_tag else None
        skew_notes: list[str] = []
        if len(all_hk) > 1:
            skew_notes.append(f"hk.pkl mixed versions: {', '.join(all_hk)}")
        if cfg_hk and cfg_hk != cur_hk:
            skew_notes.append(f"hk.pkl={cur_hk} != config.toml hk={cfg_hk}")
        skew_note = "; ".join(skew_notes) if skew_notes else None
        if skew_note or (lat_hk and lat_hk != cur_hk):
            st_hk = DepsAuditStatus.OUTDATED
            note_hk = skew_note
        elif offline:
            st_hk = DepsAuditStatus.CURRENT
            note_hk = "offline mode"
        elif lat_hk is None:
            st_hk = DepsAuditStatus.UNKNOWN
            note_hk = "upstream release lookup failed"
        else:
            st_hk = DepsAuditStatus.CURRENT
            note_hk = None
        items.append(
            AuditItem(
                name="jdx/hk-builtins",
                source_kind=DepsAuditSourceKind.HK_PRESET,
                file_path="hk.pkl",
                os_scope=["macos", "linux", "container", "vm"],
                current_version=cur_hk,
                latest_version=lat_hk,
                status=st_hk,
                release_url=rel_url,
                compare_url=_compare_url_github("jdx/hk", cur_hk, lat_hk),
                note=note_hk,
            )
        )
    return items


def _audit_native_harnesses() -> list[AuditItem]:
    items: list[AuditItem] = []
    for bin_name, flag in (
        ("claude", "--version"),
        ("codex", "--version"),
        ("agy", "--version"),
        ("opencode", "--version"),
        ("hermes", "--version"),
        ("zcode", "--version"),
    ):
        exe = shutil.which(bin_name)
        ver: str | None = None
        note: str | None = None
        status = DepsAuditStatus.UNKNOWN
        if exe:
            try:
                out = subprocess.check_output(
                    [exe, flag],
                    text=True,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    timeout=5,
                ).strip()
                first = out.splitlines()[0] if out else ""
                m = re.search(r"([0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.]+)?)", first)
                ver = m.group(1) if m else (first or None)
                status = DepsAuditStatus.CURRENT if ver else DepsAuditStatus.UNKNOWN
            except (OSError, subprocess.SubprocessError) as exc:  # fmt: skip
                note = f"{bin_name} {flag} failed: {exc}"
        else:
            note = f"{bin_name} not on PATH"
        items.append(
            AuditItem(
                name=bin_name,
                source_kind=DepsAuditSourceKind.NATIVE_HARNESS,
                file_path=exe or bin_name,
                os_scope=["macos", "linux"],
                current_version=ver,
                latest_version=None,
                status=status,
                release_url=None,
                compare_url=None,
                note=note,
            )
        )
    return items


def _resolve_kb_pins_file(candidate: Path) -> Path:
    kb_state = os.environ.get("KB_STATE", "").strip()
    if kb_state:
        p = Path(os.path.expanduser(kb_state)) / "pins.json"
        if p.is_file():
            return p
    default_p = Path.home() / ".local" / "state" / "kb" / "pins.json"
    if default_p.is_file():
        return default_p
    return candidate / "pins.json"


def _audit_kb_workspace(kb_root: Path | None, *, offline: bool) -> list[AuditItem]:
    candidate = kb_root or (
        Path.home() / "dev" / "github" / "ray-manaloto" / "knowledge-base-v2"
    )
    pins_file = _resolve_kb_pins_file(candidate)
    if not candidate.is_dir() and not pins_file.is_file():
        return []
    if not pins_file.is_file():
        return []
    items: list[AuditItem] = []
    if offline:
        try:
            pins = json.loads(pins_file.read_text(encoding="utf-8"))
            if isinstance(pins, dict):
                for key, rec in sorted(pins.items()):
                    if not isinstance(rec, dict):
                        continue
                    wname = str(rec.get("name") or key)
                    items.append(
                        AuditItem(
                            name=wname,
                            source_kind=DepsAuditSourceKind.KB_WORKSPACE,
                            file_path=str(pins_file),
                            os_scope=["macos", "linux"],
                            current_version=str(
                                rec.get("ref") or rec.get("commit") or ""
                            ),
                            latest_version=None,
                            status=(
                                DepsAuditStatus.PINNED_HELD
                                if "tailscale" in wname.lower()
                                else DepsAuditStatus.CURRENT
                            ),
                            release_url=None,
                            compare_url=None,
                            note="offline kb pins.json snapshot",
                        )
                    )
        except (OSError, json.JSONDecodeError) as exc:
            items.append(
                AuditItem(
                    name="kb:pins",
                    source_kind=DepsAuditSourceKind.KB_WORKSPACE,
                    file_path=str(pins_file),
                    os_scope=["macos", "linux"],
                    current_version=None,
                    latest_version=None,
                    status=DepsAuditStatus.ERROR,
                    release_url=None,
                    compare_url=None,
                    note=f"Failed reading {pins_file}: {exc}",
                )
            )
        return items

    proto_dir = candidate / "prototype"
    if not (proto_dir / "kb" / "snap.py").is_file():
        return items
    sub_env = dict(os.environ)
    if (
        kb_root is not None
        and not sub_env.get("KB_STATE")
        and (kb_root / "pins.json").is_file()
    ):
        sub_env["KB_STATE"] = str(kb_root)
    else:
        sub_env.setdefault("KB_STATE", str(pins_file.parent))
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import json, sys; sys.path.insert(0, sys.argv[1]); "
                "from kb import snap; print(json.dumps(snap.outdated()))",
                str(proto_dir),
            ],
            env=sub_env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"snap.outdated() exited {proc.returncode}: "
                f"{(proc.stderr or proc.stdout).strip()[:200]}"
            )
        rows = json.loads(proc.stdout.strip() or "[]")
        if not isinstance(rows, list):
            raise ValueError("snap.outdated() did not return a JSON list")
        for row in rows:
            if not isinstance(row, dict):
                continue
            wname = str(row.get("name") or "")
            if not wname:
                continue
            cur = row.get("ref") or row.get("commit")
            lat = row.get("newest") or row.get("newest_commit")
            state = str(row.get("state") or "")
            err_msg = str(row.get("error") or "")
            if "tailscale" in wname.lower():
                status = DepsAuditStatus.PINNED_HELD
                note: str | None = "write hold active"
            elif state == "ERROR":
                status = DepsAuditStatus.ERROR
                note = err_msg or "kb:outdated error"
            elif state == "OUTDATED":
                status = DepsAuditStatus.OUTDATED
                note = None
            elif state in {
                "current",
                "pinned by commit (no upstream rule)",
            } or state.startswith("docs capture"):
                status = DepsAuditStatus.CURRENT
                note = state if state != "current" else None
            else:
                status = DepsAuditStatus.UNKNOWN
                note = state or None
            items.append(
                AuditItem(
                    name=wname,
                    source_kind=DepsAuditSourceKind.KB_WORKSPACE,
                    file_path=str(pins_file),
                    os_scope=["macos", "linux"],
                    current_version=str(cur) if cur else None,
                    latest_version=str(lat) if lat else None,
                    status=status,
                    release_url=None,
                    compare_url=None,
                    note=note,
                )
            )
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        items.append(
            AuditItem(
                name="kb:outdated",
                source_kind=DepsAuditSourceKind.KB_WORKSPACE,
                file_path=str(candidate),
                os_scope=["macos", "linux"],
                current_version=None,
                latest_version=None,
                status=DepsAuditStatus.ERROR,
                release_url=None,
                compare_url=None,
                note=f"kb:outdated failed: {exc}",
            )
        )
    return items


def audit_dependencies(
    repo_root: Path,
    *,
    offline: bool = False,
    sources: set[DepsAuditSourceKind] | None = None,
    fail_on_outdated: bool = False,
    kb_root: Path | None = None,
) -> DepsAuditResponse:
    """Run a schema-first cross-OS audit across tools, OCI images, Python deps, hk, harnesses, and kb."""
    resolved_root = repo_root.resolve()
    active_sources = sources or set(DepsAuditSourceKind)
    items: list[AuditItem] = []
    errors: list[DepsAuditErrorEntry] = []

    probes: list[tuple[DepsAuditSourceKind, Callable[[], list[AuditItem]]]] = [
        (
            DepsAuditSourceKind.MISE_BINARY,
            lambda: _audit_mise_binary(resolved_root, offline=offline),
        ),
        (
            DepsAuditSourceKind.MISE_TOOL,
            lambda: _audit_mise_tools(resolved_root, offline=offline),
        ),
        (
            DepsAuditSourceKind.UV_PACKAGE,
            lambda: _audit_uv_packages(resolved_root, offline=offline),
        ),
        (
            DepsAuditSourceKind.OCI_IMAGE,
            lambda: _audit_oci_images(resolved_root, offline=offline),
        ),
        (
            DepsAuditSourceKind.HK_PRESET,
            lambda: _audit_hk_preset(resolved_root, offline=offline),
        ),
        (
            DepsAuditSourceKind.NATIVE_HARNESS,
            _audit_native_harnesses,
        ),
        (
            DepsAuditSourceKind.KB_WORKSPACE,
            lambda: _audit_kb_workspace(kb_root, offline=offline),
        ),
    ]
    for kind, fn in probes:
        if kind not in active_sources:
            continue
        try:
            items.extend(fn())
        except Exception as exc:
            logger.warning(
                "deps_audit_category_failed", source_kind=kind.value, error=str(exc)
            )
            errors.append(
                DepsAuditErrorEntry(
                    code=DepsAuditErrorCode.ERR_UNKNOWN,
                    message=f"{kind.value} probe failed: {exc}",
                )
            )
            items.append(
                AuditItem(
                    name=kind.value,
                    source_kind=kind,
                    file_path=str(resolved_root),
                    os_scope=["macos", "linux"],
                    current_version=None,
                    latest_version=None,
                    status=DepsAuditStatus.ERROR,
                    release_url=None,
                    compare_url=None,
                    note=f"probe raised: {exc}",
                )
            )

    categories: list[AuditCategoryStats] = []
    for sk in DepsAuditSourceKind:
        sub = [i for i in items if i.source_kind == sk]
        if not sub:
            continue
        categories.append(
            AuditCategoryStats(
                source_kind=sk,
                total=len(sub),
                current=sum(1 for i in sub if i.status == DepsAuditStatus.CURRENT),
                outdated=sum(1 for i in sub if i.status == DepsAuditStatus.OUTDATED),
                pinned_held=sum(
                    1 for i in sub if i.status == DepsAuditStatus.PINNED_HELD
                ),
                unknown=sum(1 for i in sub if i.status == DepsAuditStatus.UNKNOWN),
                error=sum(1 for i in sub if i.status == DepsAuditStatus.ERROR),
            )
        )

    outdated_count = sum(1 for i in items if i.status == DepsAuditStatus.OUTDATED)
    unknown_count = sum(1 for i in items if i.status == DepsAuditStatus.UNKNOWN)
    error_count = sum(1 for i in items if i.status == DepsAuditStatus.ERROR)
    if error_count > 0 and not errors:
        errors.append(
            DepsAuditErrorEntry(
                code=DepsAuditErrorCode.ERR_COMMAND_FAILED,
                message=f"{error_count} dependency audit probe(s) reported errors",
            )
        )

    returns: list[DepsAuditReturnEntry] = []
    if offline:
        returns.append(
            DepsAuditReturnEntry(
                code=DepsAuditReturnCode.RET_OFFLINE,
                message=f"Offline audit evaluated {len(items)} item(s)",
            )
        )
    elif error_count > 0 or unknown_count > 0:
        returns.append(
            DepsAuditReturnEntry(
                code=DepsAuditReturnCode.RET_PARTIAL,
                message=(
                    f"Partial audit: {error_count} error(s), {unknown_count} unknown(s), "
                    f"{outdated_count} outdated"
                ),
            )
        )
    elif outdated_count > 0:
        returns.append(
            DepsAuditReturnEntry(
                code=DepsAuditReturnCode.RET_OUTDATED,
                message=f"Detected {outdated_count} outdated item(s)",
            )
        )
    else:
        returns.append(
            DepsAuditReturnEntry(
                code=DepsAuditReturnCode.RET_OK,
                message=f"All {len(items)} audited item(s) current/held",
            )
        )

    rc = 1 if (fail_on_outdated and outdated_count > 0) or errors else 0
    ts_str, ts_ns = now_ns_pair()
    summary = DepsAuditSummary(
        total=len(items),
        current=sum(1 for i in items if i.status == DepsAuditStatus.CURRENT),
        outdated=outdated_count,
        pinned_held=sum(1 for i in items if i.status == DepsAuditStatus.PINNED_HELD),
        unknown=unknown_count,
        error=error_count,
        categories=categories,
        timestamp=ts_str,
        timestamp_unix_ns=ts_ns,
        errors=errors,
        returns=returns,
    )
    logger.info(
        "deps_audit_complete",
        offline=offline,
        total=summary.total,
        outdated=summary.outdated,
        unknown=summary.unknown,
        error=summary.error,
        rc=rc,
    )
    return DepsAuditResponse(
        command="deps:audit",
        repo_root=str(resolved_root),
        offline=offline,
        rc=rc,
        summary=summary,
        items=items,
        timestamp=ts_str,
        timestamp_unix_ns=ts_ns,
        errors=errors,
        returns=returns,
    )
