"""Stdio JSON-RPC client for native `codex app-server` hook discovery and config edits."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import orjson

MIN_CODEX_VERSION = (0, 171, 0)

EPHEMERAL_PROXY_ENV_VARS = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "NODE_EXTRA_CA_CERTS",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        "CARGO_HTTP_CAINFO",
        "DENO_CERT",
        "RUST_LOG",
    }
)

_SEMVER_RE = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)")


class CodexRpcError(RuntimeError):
    """Raised when native `codex app-server` discovery or config write fails."""

    def __init__(self, message: str, *, code: str = "ERR_RPC_FAILED") -> None:
        super().__init__(message)
        self.code = code


def parse_semver(text: str | None) -> tuple[int, int, int] | None:
    """Extract `(major, minor, patch)` semver tuple from a Codex version or userAgent string."""
    if not isinstance(text, str):
        return None
    m = _SEMVER_RE.search(text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def semver_ge(
    version_str: str | None, minimum: tuple[int, int, int] = MIN_CODEX_VERSION
) -> bool:
    """Return True when parsed semver is >= *minimum*."""
    parsed = parse_semver(version_str)
    return parsed is not None and parsed >= minimum


def resolve_codex_binary() -> str:
    """Resolve the standalone `codex` binary cleanly, skipping wrapper/shim loops."""
    candidate = Path.home() / ".local" / "bin" / "codex"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_dir or raw_dir.endswith("/command-wrappers/bin"):
            continue
        p = Path(raw_dir) / "codex"
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    found = shutil.which("codex")
    if found:
        return found
    raise CodexRpcError("unable to locate native codex binary")


def clean_codex_env(codex_home: Path | None = None) -> dict[str, str]:
    """Return process environment stripped of ephemeral proxy/CA variables."""
    env = {k: v for k, v in os.environ.items() if k not in EPHEMERAL_PROXY_ENV_VARS}
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    return env


class CodexAppServerClient:
    """JSON-RPC client talking directly to `codex app-server --stdio`."""

    def __init__(
        self,
        *,
        codex_home: Path | None = None,
        codex_bin: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.codex_home = codex_home
        self.codex_bin = codex_bin or resolve_codex_binary()
        self.timeout = timeout
        self.user_agent: str | None = None
        self.version: str | None = None

    def _call_sequence(
        self, requests: Sequence[tuple[str, dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        env = clean_codex_env(self.codex_home)
        proc = subprocess.Popen(
            [self.codex_bin, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        stdin = proc.stdin
        stdout = proc.stdout
        assert stdin is not None
        assert stdout is not None
        results: list[dict[str, Any]] = []

        def send(payload: dict[str, Any]) -> None:
            stdin.write(orjson.dumps(payload) + b"\n")
            stdin.flush()

        def read_id(expected_id: int) -> dict[str, Any]:
            while True:
                line = stdout.readline()
                if not line:
                    err_tail = ""
                    if proc.stderr is not None:
                        err_tail = proc.stderr.read().decode("utf-8", errors="replace")[
                            -400:
                        ]
                    raise CodexRpcError(
                        f"codex app-server closed before response id={expected_id}: {err_tail}".strip()
                    )
                try:
                    msg = orjson.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict) or msg.get("id") != expected_id:
                    continue
                if "error" in msg and msg["error"] is not None:
                    raise CodexRpcError(f"codex app-server error: {msg['error']}")
                res = msg.get("result")
                if not isinstance(res, dict):
                    raise CodexRpcError(
                        f"invalid RPC result for id={expected_id}: {res!r}"
                    )
                return res

        try:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "dotfiles-v2-codex-trust",
                            "version": "1",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                }
            )
            init_res = read_id(1)
            ua = init_res.get("userAgent")
            self.user_agent = str(ua) if isinstance(ua, str) else None
            parsed = parse_semver(self.user_agent)
            if parsed is None or parsed < MIN_CODEX_VERSION:
                raise CodexRpcError(
                    f"Codex version {self.user_agent!r} is below minimum {MIN_CODEX_VERSION}",
                    code="ERR_UNSUPPORTED_CODEX_VERSION",
                )
            self.version = f"{parsed[0]}.{parsed[1]}.{parsed[2]}"
            if self.codex_home is not None:
                got_home = init_res.get("codexHome")
                if isinstance(got_home, str) and Path(got_home).resolve(
                    strict=False
                ) != self.codex_home.resolve(strict=False):
                    raise CodexRpcError(
                        f"codexHome mismatch: expected {self.codex_home}, got {got_home}"
                    )
            send({"jsonrpc": "2.0", "method": "initialized"})

            next_id = 2
            for method, params in requests:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": next_id,
                        "method": method,
                        "params": params,
                    }
                )
                results.append(read_id(next_id))
                next_id += 1
            return results
        finally:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=min(self.timeout, 5.0))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)

    def hooks_list(self, cwd: Path) -> dict[str, Any]:
        """Call `hooks/list` for *cwd* and return the raw RPC result dictionary."""
        res = self._call_sequence([("hooks/list", {"cwds": [str(cwd)]})])[0]
        return res

    def config_batch_write(
        self,
        edits: Sequence[Mapping[str, Any]],
        *,
        file_path: Path | None = None,
        reload_user_config: bool = True,
    ) -> dict[str, Any]:
        """Apply `config/batchWrite` edits atomically through `codex app-server`."""
        params: dict[str, Any] = {
            "edits": [dict(e) for e in edits],
            "reloadUserConfig": reload_user_config,
        }
        if file_path is not None:
            params["filePath"] = str(file_path)
        return self._call_sequence([("config/batchWrite", params)])[0]
