// Counting reverse proxy in front of the benchmark's documents (Solid server) and query services
// (Virtuoso). Engines only see the public ports, so every engine's requests are counted the same way.
//
// Usage: node proxy.js <map.json> [request-log.jsonl]
//
// map.json (written by `fedshop-discovery serve`):
//   documents: {port, target, host?}             public port of the Solid server, and the port (and
//                                                host, default localhost) it listens on
//   services: [{port, target, host?, publisher}]  one per query service
//   publishers: {name: {kind, catalog}}    every publisher, and whether it is in the catalog
//   control_port, max_inflight            control API; most requests in flight to Virtuoso at once
//   monitor: {sample_ms, components: [{name, cgroup} or {name, pid}]}   what to measure (see below)
//
// Every request is counted by source and by kind:
//   source: "known" (a publisher in the known set: by default the catalog), "discovered" (any other
//           publisher) or "other" (no publisher: the server root, unknown paths)
//   kind:   "document", "storage-root" (/<publisher>/), "container" (another path ending in /),
//           "server-root" (/) or "sparql" (a query service)
//
// CPU and memory of every component (the Virtuoso containers by their cgroup, the Solid server by
// its process, and the proxy itself), per reset:
//   cpu_seconds, cpu_percent.avg  exact: the CPU counters are read at the reset and at /stats
//                                 (cgroup usage_usec, the sum of the process's threads' schedstat)
//   cpu_percent.min/max           over the sampling intervals (sample_ms), and the last one up to /stats
//   memory_bytes min/max/avg      over the samples, including one at the reset and one at /stats;
//                                 a container's memory is memory.current - inactive_file (as docker
//                                 stats reports it), a process's its resident set
//   100% CPU is one core. A peak shorter than sample_ms can be missed by min/max, never by the totals.
//
// Control API on control_port:
//   GET  /stats            the counts and resources since the last reset (?samples=1 adds every sample)
//   GET  /health           200 when the Solid server and one service on every Virtuoso host answer, else 503
//   POST /reset            zero both; optional JSON body {"label": "q07-2", "known": ["vendor13", "taxonomy"]}
//                          sets the label written to the request log and the publishers counted as
//                          known (default: the catalog)
"use strict";
const fs = require("fs");
const http = require("http");

const map = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const log = process.argv[3] ? fs.createWriteStream(process.argv[3], { flags: "a" }) : null;
const catalog = Object.keys(map.publishers).filter((name) => map.publishers[name].catalog);
const KINDS = ["document", "storage-root", "container", "server-root", "sparql"];
const { performance } = require("perf_hooks");

let label = null;
let known = new Set(catalog);
let stats;

function counter() {
  const c = { requests: 0, bytes: 0, errors: 0 };
  for (const kind of KINDS) c[kind] = 0;
  return c;
}

// ----- resources -----
function readCgroup(dir) {
  const cpu = Number(/usage_usec (\d+)/.exec(fs.readFileSync(`${dir}/cpu.stat`, "utf8"))[1]) / 1e6;
  const current = Number(fs.readFileSync(`${dir}/memory.current`, "utf8"));
  const inactive = /^inactive_file (\d+)$/m.exec(fs.readFileSync(`${dir}/memory.stat`, "utf8"));
  return { cpu, memory: current - (inactive ? Number(inactive[1]) : 0) };
}

function readProcess(pid) {
  let cpu = 0;
  for (const tid of fs.readdirSync(`/proc/${pid}/task`)) {
    try { cpu += Number(fs.readFileSync(`/proc/${pid}/task/${tid}/schedstat`, "utf8").split(" ")[0]) / 1e9; } catch (e) { /* thread ended */ }
  }
  const rss = /VmRSS:\s+(\d+) kB/.exec(fs.readFileSync(`/proc/${pid}/status`, "utf8"));
  return { cpu, memory: rss ? Number(rss[1]) * 1024 : 0 };
}

const monitor = (map.monitor && map.monitor.components || []).map((c) => ({
  name: c.name,
  read: c.cgroup ? () => readCgroup(c.cgroup) : () => readProcess(c.pid),
}));
monitor.push({ name: "proxy", read: () => ({ cpu: (process.cpuUsage().user + process.cpuUsage().system) / 1e6,
                                             memory: process.memoryUsage().rss }) });

function measure(component) {
  const t = performance.now() / 1000;
  try {
    return { t, ...component.read() };
  } catch (e) {
    component.error = e.message;
    return null;
  }
}

function startResources() {
  for (const c of monitor) {
    c.error = null;
    const r = measure(c);
    c.base = c.last = r;
    c.cpuMin = c.cpuMax = null;
    c.memory = r ? [r.memory] : [];
    c.samples = r ? [{ t_ms: 0, cpu_percent: null, memory_bytes: r.memory }] : [];
  }
}

function sampleResources() {
  for (const c of monitor) {
    const r = measure(c);
    if (!r || !c.last) { c.last = c.last || r; c.base = c.base || r; continue; }
    const dt = r.t - c.last.t;
    if (dt < 0.001) continue;
    const pct = 100 * (r.cpu - c.last.cpu) / dt;
    c.cpuMin = c.cpuMin === null ? pct : Math.min(c.cpuMin, pct);
    c.cpuMax = c.cpuMax === null ? pct : Math.max(c.cpuMax, pct);
    c.memory.push(r.memory);
    c.samples.push({ t_ms: Math.round((r.t - c.base.t) * 1000), cpu_percent: +pct.toFixed(1), memory_bytes: r.memory });
    c.last = r;
  }
}

function resources(withSamples) {
  sampleResources();
  const out = {};
  for (const c of monitor) {
    if (!c.base || !c.last) { out[c.name] = { error: c.error }; continue; }
    const wall = c.last.t - c.base.t;
    const cpu = c.last.cpu - c.base.cpu;
    const sum = c.memory.reduce((a, b) => a + b, 0);
    out[c.name] = {
      wall_seconds: +wall.toFixed(4),
      cpu_seconds: +cpu.toFixed(6),
      cpu_percent: { avg: wall > 0 ? +(100 * cpu / wall).toFixed(1) : null,
                     min: c.cpuMin === null ? null : +c.cpuMin.toFixed(1), max: c.cpuMax === null ? null : +c.cpuMax.toFixed(1) },
      memory_bytes: { avg: Math.round(sum / c.memory.length), min: Math.min(...c.memory), max: Math.max(...c.memory) },
      samples: c.samples.length,
      error: c.error,
    };
    if (withSamples) out[c.name].series = c.samples;
  }
  return out;
}

setInterval(sampleResources, (map.monitor && map.monitor.sample_ms) || 50);

function reset() {
  startResources();
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

function forward(req, res, { target, host = "localhost" }, entry, isService) {
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
      host, port: target, method: req.method, path: req.url, headers,
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
  forward(req, res, map.documents, entry, false);
}).listen(map.documents.port);

for (const service of map.services) {
  http.createServer((req, res) => {
    const entry = { method: req.method, url: `http://${req.headers.host}${req.url}`, publisher: service.publisher,
                    kind: "sparql", source: source(service.publisher) };
    forward(req, res, service, entry, true);
  }).listen(service.port);
}

// One upstream per server: the Solid server, and the first service on every Virtuoso host.
function health() {
  const targets = { documents: { ...map.documents, path: "/" } };
  for (const s of map.services) {
    const key = s.host || "localhost";
    if (!targets[key]) targets[key] = { ...s, path: `/${s.publisher}/sparql` };
  }
  return Promise.all(Object.entries(targets).map(([name, t]) => new Promise((resolve) => {
    const r = http.get({ host: t.host || "localhost", port: t.target, path: t.path, timeout: 2000,
                         headers: { host: t.publisher ? `localhost:${t.target}` : `localhost:${map.documents.port}` } },
                       (up) => { up.resume(); resolve([name, true]); });
    r.on("timeout", () => r.destroy());
    r.on("error", () => resolve([name, false]));
  }))).then(Object.fromEntries);
}

http.createServer((req, res) => {
  const reply = (status, body) => {
    res.writeHead(status, { "content-type": "application/json" });
    res.end(JSON.stringify(body, null, 2));
  };
  const url = new URL(req.url, "http://x");
  if (req.method === "GET" && url.pathname === "/health") {
    health().then((upstreams) => reply(Object.values(upstreams).every(Boolean) ? 200 : 503, upstreams));
    return;
  }
  if (req.method === "GET" && url.pathname === "/stats") {
    return reply(200, { ...stats, resources: resources(url.searchParams.get("samples") === "1") });
  }
  if (req.method === "POST" && url.pathname === "/reset") {
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
