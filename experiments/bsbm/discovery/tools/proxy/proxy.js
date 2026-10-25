// Counting reverse proxy in front of the benchmark's documents (Solid server) and query services
// (Virtuoso). Engines only see the public ports, so every engine's requests are counted the same way.
//
// Usage: node proxy.js <map.json> [request-log.jsonl]
//
// map.json (written by `fedshop-discovery serve`):
//   documents: {port, target}             public port of the Solid server and the port it listens on
//   services: [{port, target, publisher}]  one per query service
//   publishers: {name: {kind, catalog}}    every publisher, and whether it is in the catalog
//   control_port, max_inflight            control API; most requests in flight to Virtuoso at once
//
// Every request is counted by source and by kind:
//   source: "known" (a publisher in the known set: by default the catalog), "discovered" (any other
//           publisher) or "other" (no publisher: the server root, unknown paths)
//   kind:   "document", "storage-root" (/<publisher>/), "container" (another path ending in /),
//           "server-root" (/) or "sparql" (a query service)
//
// Control API on control_port:
//   GET  /stats  the counts since the last reset
//   POST /reset  zero the counts; optional JSON body {"label": "q07-2", "known": ["vendor13", "taxonomy"]}
//                sets the label written to the request log and the publishers counted as known
//                (default: the catalog)
"use strict";
const fs = require("fs");
const http = require("http");

const map = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const log = process.argv[3] ? fs.createWriteStream(process.argv[3], { flags: "a" }) : null;
const catalog = Object.keys(map.publishers).filter((name) => map.publishers[name].catalog);
const KINDS = ["document", "storage-root", "container", "server-root", "sparql"];

let label = null;
let known = new Set(catalog);
let stats;

function counter() {
  const c = { requests: 0, bytes: 0, errors: 0 };
  for (const kind of KINDS) c[kind] = 0;
  return c;
}

function reset() {
  stats = {
    label, since: new Date().toISOString(), known: [...known].sort(),
    total: counter(), known_sources: counter(), discovered_sources: counter(), other: counter(),
    publishers: {}, max_inflight_services: 0,
  };
}

function record(entry) {
  const group = { known: stats.known_sources, discovered: stats.discovered_sources, other: stats.other }[entry.source];
  const groups = [stats.total, group];
  if (entry.publisher) {
    stats.publishers[entry.publisher] = stats.publishers[entry.publisher] || counter();
    groups.push(stats.publishers[entry.publisher]);
  }
  for (const c of groups) {
    c.requests += 1;
    c[entry.kind] += 1;
    c.bytes += entry.bytes;
    c.errors += entry.status >= 500 ? 1 : 0;
  }
  if (log) log.write(JSON.stringify({ label, ...entry }) + "\n");
}

function source(publisher) {
  if (!publisher || !map.publishers[publisher]) return "other";
  return known.has(publisher) ? "known" : "discovered";
}

function documentKind(pathname) {
  if (pathname === "/") return "server-root";
  if (!pathname.endsWith("/")) return "document";
  return pathname.split("/").filter(Boolean).length === 1 ? "storage-root" : "container";
}

// Requests to Virtuoso wait here when max_inflight are already running.
let inflight = 0;
const waiting = [];
function acquire(start) {
  if (inflight < map.max_inflight) {
    inflight += 1;
    stats.max_inflight_services = Math.max(stats.max_inflight_services, inflight);
    start();
  } else {
    waiting.push(start);
  }
}
function release() {
  inflight -= 1;
  if (waiting.length) acquire(waiting.shift());
}

const HOP = new Set(["connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade"]);
const documentAgent = new http.Agent({ keepAlive: true, maxSockets: 64 });

function forward(req, res, target, entry, isService) {
  const began = Date.now();
  const headers = {};
  for (const [k, v] of Object.entries(req.headers)) if (!HOP.has(k)) headers[k] = v;
  if (isService) headers.host = `localhost:${target}`;     // Virtuoso's endpoints are defined on their own ports
  let finished = false;
  const done = (status, bytes) => {
    if (finished) return;
    finished = true;
    if (isService) release();
    record({ ...entry, t: began, status, bytes, ms: Date.now() - began });
  };
  const send = () => {
    const upstream = http.request({
      host: "localhost", port: target, method: req.method, path: req.url, headers,
      // Services: a fresh connection per request, so no reused socket can have been dropped by Virtuoso.
      agent: isService ? false : documentAgent,
    }, (up) => {
      const out = {};
      for (const [k, v] of Object.entries(up.headers)) if (!HOP.has(k)) out[k] = v;
      res.writeHead(up.statusCode, out);
      let bytes = 0;
      up.on("data", (chunk) => { bytes += chunk.length; });
      up.on("end", () => done(up.statusCode, bytes));
      up.on("error", () => done(502, bytes));
      up.pipe(res);
    });
    upstream.on("error", () => {
      if (!res.headersSent) res.writeHead(502, { "content-type": "text/plain" });
      res.end("Bad gateway");
      done(502, 0);
    });
    res.on("close", () => { if (!finished) { upstream.destroy(); done(499, 0); } });
    req.pipe(upstream);
  };
  if (isService) acquire(send); else send();
}

reset();

http.createServer((req, res) => {
  const pathname = new URL(req.url, "http://x").pathname;
  const publisher = decodeURIComponent(pathname.split("/").filter(Boolean)[0] || "") || null;
  const entry = { method: req.method, url: `http://${req.headers.host}${req.url}`, publisher,
                  kind: documentKind(pathname), source: source(publisher) };
  forward(req, res, map.documents.target, entry, false);
}).listen(map.documents.port);

for (const service of map.services) {
  http.createServer((req, res) => {
    const entry = { method: req.method, url: `http://${req.headers.host}${req.url}`, publisher: service.publisher,
                    kind: "sparql", source: source(service.publisher) };
    forward(req, res, service.target, entry, true);
  }).listen(service.port);
}

http.createServer((req, res) => {
  const reply = (status, body) => {
    res.writeHead(status, { "content-type": "application/json" });
    res.end(JSON.stringify(body, null, 2));
  };
  if (req.method === "GET" && req.url === "/stats") return reply(200, stats);
  if (req.method === "POST" && req.url === "/reset") {
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      let options = {};
      try { options = body.trim() ? JSON.parse(body) : {}; } catch (e) { return reply(400, { error: `Body is not JSON: ${e.message}` }); }
      label = options.label ?? null;
      known = new Set(options.known ?? catalog);
      reset();
      reply(200, { label, known: stats.known });
    });
    return;
  }
  reply(404, { error: "Use GET /stats or POST /reset" });
}).listen(map.control_port);

console.log(`Proxy: documents on :${map.documents.port}, ${map.services.length} services, control on :${map.control_port}`);
