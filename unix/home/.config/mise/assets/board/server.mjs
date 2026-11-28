import { createHash, timingSafeEqual } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, realpathSync } from "node:fs";
import { createServer } from "node:http";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const MOUNT = "/api/board";
const BODY_MAX = 8 * 1024 * 1024;
const PROXY_HEADERS = [
  "forwarded",
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-proto",
  "x-real-ip",
];

function quietSqliteWarning() {
  const emit = process.emitWarning;
  process.emitWarning = (warning, ...rest) => {
    const text = typeof warning === "string" ? warning : (warning?.message ?? "");
    if (/SQLite is an experimental feature/.test(text)) return;
    return emit.call(process, warning, ...rest);
  };
}

function normalizeHandle(raw) {
  const handle = String(raw || "")
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^[^a-z0-9]+|[^a-z0-9]+$/g, "")
    .slice(0, 64)
    .replace(/[^a-z0-9]+$/, "");
  return handle || "me";
}

function resolveVbBinary() {
  // A host that pins vb outside its mise config (pc hub:up's npm prefix) names it here, so a
  // stale mise config cannot pick an older @vercel/board.
  const pinned = process.env.BOARD_VB_BIN;
  if (pinned) {
    if (existsSync(pinned)) return pinned;
    throw new Error(`BOARD_VB_BIN does not exist: ${pinned}`);
  }
  for (const [cmd, args] of [
    ["mise", ["which", "vb"]],
    ["which", ["vb"]],
  ]) {
    try {
      const out = execFileSync(cmd, args, {
        encoding: "utf8",
        stdio: ["ignore", "pipe", "ignore"],
      }).trim();
      if (out) return out.split(/\r?\n/)[0].trim();
    } catch {
      // Try next resolver.
    }
  }
  throw new Error("could not locate vb executable on PATH (run mise install)");
}

function findVercelBoardPkgRoot() {
  const candidates = [];
  try {
    let cur = dirname(realpathSync(resolveVbBinary()));
    for (let i = 0; i < 10; i += 1) {
      candidates.push(
        cur,
        join(cur, "@vercel", "board"),
        join(cur, "node_modules", "@vercel", "board"),
      );
      const parent = dirname(cur);
      if (parent === cur) break;
      cur = parent;
    }
  } catch {
    // Fall back to mise install layout below.
  }
  const dataDir = process.env.MISE_DATA_DIR || join(homedir(), ".local", "share", "mise");
  candidates.push(
    join(dataDir, "installs", "npm-vercel-board", "latest", "node_modules", "@vercel", "board"),
  );
  for (const candidate of candidates) {
    const pkgFile = join(candidate, "package.json");
    if (existsSync(pkgFile)) {
      try {
        const pkg = JSON.parse(readFileSync(pkgFile, "utf8"));
        if (pkg?.name === "@vercel/board") return realpathSync(candidate);
      } catch {
        // Keep checking next candidate.
      }
    }
  }
  throw new Error("could not locate @vercel/board package directory from vb executable");
}

function sha256(value) {
  return createHash("sha256").update(value, "utf8").digest();
}

function parseBearer(header) {
  if (typeof header !== "string") return null;
  const match = /^Bearer\s+(.+)$/i.exec(header.trim());
  return match ? match[1].trim() : null;
}

function parseTailscaleLogin(header) {
  if (typeof header !== "string") return null;
  const trimmed = header.trim().toLowerCase();
  if (!trimmed || /\s/.test(trimmed)) return null;
  const at = trimmed.indexOf("@");
  if (at <= 0 || at === trimmed.length - 1) return null;
  const local = trimmed.slice(0, at);
  const domain = trimmed.slice(at + 1);
  if (!/^[a-z0-9._-]+$/.test(local) || !/^[a-z0-9.-]+$/.test(domain)) return null;
  return { login: trimmed, handle: normalizeHandle(local) };
}

function resolveRequestOrigin(req, ownOrigin, tailscaleHost) {
  const host = String(req.headers.host ?? "").toLowerCase();
  const proxied = PROXY_HEADERS.some((h) => req.headers[h] !== undefined);
  if (proxied) {
    if (!tailscaleHost || host !== tailscaleHost) return null;
    const proto = String(req.headers["x-forwarded-proto"] ?? "")
      .split(",")[0]
      .trim()
      .toLowerCase();
    return `${proto === "http" ? "http" : "https"}://${host}`;
  }
  if (host === new URL(ownOrigin).host || host === `localhost:${new URL(ownOrigin).port}`) {
    return `${new URL(ownOrigin).protocol}//${host}`;
  }
  if (tailscaleHost && host === tailscaleHost) {
    return `https://${host}`;
  }
  return null;
}

async function readBody(req) {
  if (req.method === "GET" || req.method === "HEAD") return undefined;
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > BODY_MAX) {
      const err = new Error("request body too large");
      err.status = 413;
      throw err;
    }
    chunks.push(chunk);
  }
  if (chunks.length === 0) return undefined;
  return new Uint8Array(Buffer.concat(chunks));
}

async function main() {
  const port = Number.parseInt(process.env.BOARD_PORT || "4100", 10);
  const defaultDir = join(
    homedir(),
    ".local",
    "state",
    process.platform === "darwin" ? "vercel-board.noindex" : "vercel-board",
  );
  const dir = resolve(process.env.BOARD_DIR || defaultDir);
  const tokenFile = resolve(
    process.env.BOARD_TOKEN_FILE || join(homedir(), ".config", "board", "agent-token"),
  );
  const tailscaleHost = (process.env.BOARD_TAILSCALE_HOST || "")
    .trim()
    .toLowerCase()
    .replace(/\.$/, "");
  const localUserHandle = normalizeHandle(process.env.BOARD_HANDLE || process.env.USER || "me");

  const rawToken = readFileSync(tokenFile, "utf8").trim();
  if (!rawToken) throw new Error(`empty board token in ${tokenFile}`);
  const expectedDigest = sha256(rawToken);

  const pkgRoot = findVercelBoardPkgRoot();
  const { board, HttpError } = await import(
    pathToFileURL(join(pkgRoot, "dist/index.node.mjs")).href
  );
  const { local } = await import(pathToFileURL(join(pkgRoot, "dist/backend-local.mjs")).href);
  const { inspector } = await import(pathToFileURL(join(pkgRoot, "dist/inspect.mjs")).href);

  quietSqliteWarning();
  const backend = local({ dir });

  const team = board({
    backend,
    basePath: MOUNT,
    authenticate: async (request) => {
      const bearer = parseBearer(request.headers.get("authorization"));
      if (bearer !== null) {
        if (timingSafeEqual(sha256(bearer), expectedDigest)) {
          return { id: `local:user:${localUserHandle}`, kind: "person", handle: localUserHandle };
        }
        return null;
      }
      if (tailscaleHost && new URL(request.audience).host.toLowerCase() === tailscaleHost) {
        const tsCaller = parseTailscaleLogin(request.headers.get("tailscale-user-login"));
        if (tsCaller !== null) {
          const site = request.headers.get("sec-fetch-site");
          const reqOrigin = request.headers.get("origin");
          if (
            (site !== null && site !== "same-origin" && site !== "none") ||
            (reqOrigin !== null && reqOrigin !== new URL(request.audience).origin)
          ) {
            throw new HttpError(
              403,
              "forbidden",
              "cross-site requests cannot authenticate via Tailscale identity",
            );
          }
          return {
            id: `tailscale:user:${tsCaller.login}`,
            kind: "person",
            handle: tsCaller.handle,
          };
        }
      }
      return null;
    },
  });

  // Ensure local SQLite schema initializes before reporting healthy.
  await team.channels({ limit: 1 });
  const inspect = inspector({ board: team, basePath: "" });
  const ownOrigin = `http://127.0.0.1:${port}`;

  const server = createServer(async (req, res) => {
    const rawUrl = req.url ?? "/";
    const pathOnly = rawUrl.split("?")[0] || "/";

    if (req.method === "GET" && pathOnly === "/healthz") {
      res.writeHead(200, { "content-type": "application/json", "cache-control": "no-store" });
      res.end(JSON.stringify({ ok: true, mount: MOUNT, tailscale_host: tailscaleHost }));
      return;
    }

    const origin = resolveRequestOrigin(req, ownOrigin, tailscaleHost);
    if (origin === null) {
      res.writeHead(421, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: { code: "misdirected", message: "untrusted host header" } }));
      return;
    }

    const controller = new AbortController();
    res.on("close", () => controller.abort());

    try {
      const body = await readBody(req);
      const headers = new Headers();
      for (const [k, v] of Object.entries(req.headers)) {
        if (typeof v === "string") headers.set(k, v);
        else if (Array.isArray(v)) for (const item of v) headers.append(k, item);
      }
      const webReq = new Request(`${origin}${rawUrl}`, {
        method: req.method ?? "GET",
        headers,
        ...(body && body.length > 0 ? { body } : {}),
        signal: controller.signal,
      });
      const isApi = pathOnly === MOUNT || pathOnly.startsWith(`${MOUNT}/`);
      if (!isApi) {
        const bearer = parseBearer(webReq.headers.get("authorization"));
        const bearerOk = bearer !== null && timingSafeEqual(sha256(bearer), expectedDigest);
        const isTailnet = Boolean(
          tailscaleHost && new URL(origin).host.toLowerCase() === tailscaleHost,
        );
        if (isTailnet && !bearerOk) {
          const tsCaller = parseTailscaleLogin(webReq.headers.get("tailscale-user-login"));
          if (tsCaller === null) {
            res.writeHead(401, { "content-type": "application/json" });
            res.end(
              JSON.stringify({
                error: { code: "unauthorized", message: "authentication required" },
              }),
            );
            return;
          }
          const site = webReq.headers.get("sec-fetch-site");
          const reqOrigin = webReq.headers.get("origin");
          if (
            (site !== null && site !== "same-origin" && site !== "none") ||
            (reqOrigin !== null && reqOrigin !== origin)
          ) {
            res.writeHead(403, { "content-type": "application/json" });
            res.end(
              JSON.stringify({
                error: { code: "forbidden", message: "cross-site request forbidden" },
              }),
            );
            return;
          }
        }
      }
      const handler = isApi ? team.fetch : inspect;
      const webRes = await handler(webReq);
      const outHeaders = {};
      webRes.headers.forEach((value, key) => {
        if (key === "set-cookie") return;
        outHeaders[key] = value;
      });
      res.writeHead(webRes.status, outHeaders);
      if (webRes.body) {
        try {
          for await (const chunk of webRes.body) res.write(chunk);
        } catch {
          // Stream aborted by client disconnect.
        }
      }
      res.end();
    } catch (error) {
      if (controller.signal.aborted || res.writableEnded) return;
      if (res.headersSent) {
        res.end();
        return;
      }
      const status = error?.status === 413 ? 413 : 500;
      res.writeHead(status, { "content-type": "application/json" });
      res.end(
        JSON.stringify({
          error: {
            code: status === 413 ? "too_large" : "internal",
            message: error?.message ?? "internal error",
          },
        }),
      );
    }
  });

  await new Promise((resolveListen, rejectListen) => {
    server.once("error", rejectListen);
    server.listen(port, "127.0.0.1", () => {
      server.off("error", rejectListen);
      resolveListen();
    });
  });

  let closing = false;
  const shutdown = async () => {
    if (closing) return;
    closing = true;
    await new Promise((done) => {
      server.close(() => done());
      server.closeAllConnections?.();
    });
    await backend.close?.();
    process.exit(0);
  };
  process.once("SIGTERM", () => void shutdown());
  process.once("SIGINT", () => void shutdown());
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
