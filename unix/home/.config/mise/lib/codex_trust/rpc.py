"""Stdio JSON-RPC client for native `codex app-server` hook discovery and config edits."""

from __future__ import annotations

import contextlib
import os
import re
import selectors
import shutil
import subprocess
import time
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
        cleaned = raw_dir.rstrip("/")
        if not cleaned or cleaned.endswith(("/command-wrappers/bin", "/mise/shims")):
            continue
        p = Path(cleaned) / "codex"
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
        try:
            proc = subprocess.Popen(
                [self.codex_bin, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                close_fds=True,
            )
        except OSError as exc:
            raise CodexRpcError(
                f"failed to start {self.codex_bin} app-server: {exc}"
            ) from exc

        stdin = proc.stdin
        stdout = proc.stdout
        stderr = proc.stderr
        assert stdin is not None
        assert stdout is not None
        assert stderr is not None

        os.set_blocking(stdout.fileno(), False)
        os.set_blocking(stderr.fileno(), False)
        sel = selectors.DefaultSelector()
        sel.register(stdout, selectors.EVENT_READ, data="stdout")
        sel.register(stderr, selectors.EVENT_READ, data="stderr")

        stdout_buf = bytearray()
        stderr_tail = bytearray()
        stdout_eof = False
        results: list[dict[str, Any]] = []

        def _append_stderr(chunk: bytes) -> None:
            if not chunk:
                return
            stderr_tail.extend(chunk)
            if len(stderr_tail) > 32768:
                del stderr_tail[:-16384]

        def _drain_stderr_nowait() -> None:
            while True:
                try:
                    chunk = os.read(stderr.fileno(), 65536)
                except BlockingIOError, OSError:
                    break
                if not chunk:
                    break
                _append_stderr(chunk)

        def send(payload: dict[str, Any]) -> None:
            try:
                stdin.write(orjson.dumps(payload) + b"\n")
                stdin.flush()
            except (OSError, BrokenPipeError) as exc:
                _drain_stderr_nowait()
                err_tail = stderr_tail.decode("utf-8", errors="replace")[-400:].strip()
                raise CodexRpcError(
                    f"failed writing RPC request to codex app-server: {exc}"
                    + (f" ({err_tail})" if err_tail else "")
                ) from exc

        def read_id(expected_id: int) -> dict[str, Any]:
            nonlocal stdout_eof
            deadline = time.monotonic() + self.timeout
            while True:
                while True:
                    nl = stdout_buf.find(b"\n")
                    if nl < 0:
                        break
                    raw_line = bytes(stdout_buf[:nl])
                    del stdout_buf[: nl + 1]
                    try:
                        msg = orjson.loads(raw_line)
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

                if stdout_eof:
                    _drain_stderr_nowait()
                    err_tail = stderr_tail.decode("utf-8", errors="replace")[
                        -400:
                    ].strip()
                    raise CodexRpcError(
                        f"codex app-server closed before response id={expected_id}: {err_tail}".strip()
                    )

                rem = deadline - time.monotonic()
                if rem <= 0:
                    _drain_stderr_nowait()
                    err_tail = stderr_tail.decode("utf-8", errors="replace")[
                        -400:
                    ].strip()
                    raise CodexRpcError(
                        f"codex app-server timed out waiting for response id={expected_id}: {err_tail}".strip()
                    )

                events = sel.select(timeout=rem)
                if not events:
                    _drain_stderr_nowait()
                    err_tail = stderr_tail.decode("utf-8", errors="replace")[
                        -400:
                    ].strip()
                    raise CodexRpcError(
                        f"codex app-server timed out waiting for response id={expected_id}: {err_tail}".strip()
                    )

                for key, _ in events:
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        chunk = b""
                    if key.data == "stderr":
                        if not chunk:
                            with contextlib.suppress(KeyError, ValueError):
                                sel.unregister(stderr)
                        else:
                            _append_stderr(chunk)
                    else:
                        if not chunk:
                            stdout_eof = True
                            with contextlib.suppress(KeyError, ValueError):
                                sel.unregister(stdout)
                        else:
                            stdout_buf.extend(chunk)

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
            with contextlib.suppress(OSError):
                sel.close()
            for fh in (stdin, stdout, stderr):
                with contextlib.suppress(OSError):
                    fh.close()
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
