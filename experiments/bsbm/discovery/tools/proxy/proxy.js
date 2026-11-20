// Counting reverse proxy in front of the benchmark's documents (Solid server) and query services
// (Virtuoso).
// Usage: node proxy.js <map.json> [request-log.jsonl]
//
// map.json (written by `fedshop-discovery serve`, or from serving.json by the jbr experiment):
//   documents: {port, target, host?}               public port of the Solid server, and the port (and
//                                                  host, default localhost) it listens on
//   services: [{port, target, host?, publisher}]   one per query service
//   publishers: {name: {kind, known}}              every publisher, and whether it is known up front
//   control_port                                   control API
//   monitor: {sample_ms, components: [{name, cgroup} or {name, pid}]}   what to measure (see below)
//   log_query_chars                                how much of a query's text the request log keeps (default 4096)
//
// Request log, one JSON object per request, written when it finishes:
//   label, method, url, publisher, kind, source   what was asked, and of whom
//   t                                             epoch ms the proxy received it (and forwarded it: nothing waits)
//   status, bytes, ms                             the answer, and ms until it ended
//   headers_ms, first_byte_ms                     ms until the answer's headers and its first body byte (null if none):
//                                                 how long the server took before it began to answer
//   in_flight                                     requests to the same server (the Solid server or one Virtuoso
//                                                 container) still open when this one arrived
//   query_form, query, query_chars                SPARQL only: SELECT/CONSTRUCT/ASK/DESCRIBE, the query's text (cut
//                                                 at log_query_chars) and its full length
//
// Control API on control_port:
//   GET  /stats            the counts and resources since the last reset (?samples=1 adds every sample)
//   GET  /health           200 when the Solid server and one service on every Virtuoso host answer, else 503
//   POST /reset            zero the counts and resources; optional JSON body {"label": "q07-2", "known": ["vendor13", "taxonomy"]}
//                          sets the label written to the request log and the publishers counted as
//                          known (default: the known publishers)
"use strict";
const fs = require("fs");
const http = require("http");
const { performance } = require("perf_hooks");

const KINDS = ["document", "storage-root", "container", "server-root", "sparql"];
const HOP = new Set(["connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade"]);
const DEFAULT_LOG_QUERY_CHARS = 4096;
// A query body beyond this is not kept for the log (it is forwarded whole all the same)
const MAX_LOGGED_BODY_BYTES = 1 << 20;
// The keyword after the prologue (comments, BASE and PREFIX declarations)
const QUERY_FORM = /^(?:\s|#[^\n]*\n|BASE\s*<[^>]*>|PREFIX\s+[^\s:]*:\s*<[^>]*>)*(SELECT|CONSTRUCT|ASK|DESCRIBE)\b/i;
// Requests forwarded to each server and not finished yet, for the log's in_flight
const openPerServer = new Map();

function counter() {
  const c = { requests: 0, bytes: 0, errors: 0 };
  for (const kind of KINDS) {
    c[kind] = 0;
  }
  return c;
}

// Reads docker cgroup for resource usage
function readCgroup(dir) {
  const cpu = Number(/usage_usec (\d+)/.exec(fs.readFileSync(`${dir}/cpu.stat`, "utf8"))[1]) / 1e6;
  // Memory usage is given as total - inactive as os keeps previously accessed ram in memory as 'cache'
  const current = Number(fs.readFileSync(`${dir}/memory.current`, "utf8"));
  const inactive = /^inactive_file (\d+)$/m.exec(fs.readFileSync(`${dir}/memory.stat`, "utf8"));
  return { cpu, memory: current - (inactive ? Number(inactive[1]) : 0) };
}

function readProcess(pid) {
  let cpu = 0;
  for (const tid of fs.readdirSync(`/proc/${pid}/task`)) {
    try {
      cpu += Number(fs.readFileSync(`/proc/${pid}/task/${tid}/schedstat`, "utf8").split(" ")[0]) / 1e9;
    } catch (e) {
      /* thread ended */
    }
  }
  const rss = /VmRSS:\s+(\d+) kB/.exec(fs.readFileSync(`/proc/${pid}/status`, "utf8"));
  return { cpu, memory: rss ? Number(rss[1]) * 1024 : 0 };
}

function readSelf() {
  const usage = process.cpuUsage();
  return { cpu: (usage.user + usage.system) / 1e6, memory: process.memoryUsage().rss };
}

// CPU and memory of one component since the last start(). read() returns its counters {cpu, memory};
// when it throws, the error is kept and the measurement skipped.
class ComponentMonitor {
  constructor(name, read) {
    this.name = name;
    this.read = read;
    this.start();
  }

  measure() {
    const t = performance.now() / 1000;
    try {
      return { t, ...this.read() };
    } catch (e) {
      this.error = e.message;
      return null;
    }
  }

  start() {
    this.error = null;
    const r = this.measure();
    this.base = r;
    this.last = r;
    this.cpuMin = null;
    this.cpuMax = null;
    this.memory = r ? [r.memory] : [];
    this.samples = r ? [{ t_ms: 0, cpu_percent: null, memory_bytes: r.memory }] : [];
  }

  sample() {
    const r = this.measure();
    if (!r || !this.last) {
      this.last = this.last || r;
      this.base = this.base || r;
      return;
    }
    const dt = r.t - this.last.t;
    if (dt < 0.001) {
      return;
    }
    const pct = 100 * (r.cpu - this.last.cpu) / dt;
    this.cpuMin = this.cpuMin === null ? pct : Math.min(this.cpuMin, pct);
    this.cpuMax = this.cpuMax === null ? pct : Math.max(this.cpuMax, pct);
    this.memory.push(r.memory);
    this.samples.push({ t_ms: Math.round((r.t - this.base.t) * 1000), cpu_percent: +pct.toFixed(1), memory_bytes: r.memory });
    this.last = r;
  }

  summary(withSamples) {
    if (!this.base || !this.last) {
      return { error: this.error };
    }
    const wall = this.last.t - this.base.t;
    const cpu = this.last.cpu - this.base.cpu;
    const sum = this.memory.reduce((a, b) => a + b, 0);
    const out = {
      wall_seconds: +wall.toFixed(4),
      cpu_seconds: +cpu.toFixed(6),
      cpu_percent: {
        avg: wall > 0 ? +(100 * cpu / wall).toFixed(1) : null,
        min: this.cpuMin === null ? null : +this.cpuMin.toFixed(1),
        max: this.cpuMax === null ? null : +this.cpuMax.toFixed(1)
      },
      memory_bytes: {
        avg: Math.round(sum / this.memory.length),
        min: Math.min(...this.memory),
        max: Math.max(...this.memory)
      },
      samples: this.samples.length,
      error: this.error,
    };
    if (withSamples) {
      out.series = this.samples;
    }
    return out;
  }
}

// Request counts since the last reset, by source, kind and publisher. Every request is also written
// to the log (if any), with the label at the time it finished.
class RequestStats {
  constructor(publishers, log, logQueryChars = DEFAULT_LOG_QUERY_CHARS) {
    this.publishers = publishers;
    this.defaultKnown = Object.keys(publishers).filter((name) => publishers[name].known);
    this.log = log;
    this.logQueryChars = logQueryChars;
    this.reset();
  }

  reset(options = {}) {
    this.label = options.label ?? null;
    this.known = new Set(options.known ?? this.defaultKnown);
    this.counts = {
      label: this.label,
      since: new Date().toISOString(),
      known: [...this.known].sort(),
      total: counter(),
      known_sources: counter(),
      discovered_sources: counter(),
      other: counter(),
      publishers: {},
    };
  }

  source(publisher) {
    if (!publisher || !this.publishers[publisher]) {
      return "other";
    }
    return this.known.has(publisher) ? "known" : "discovered";
  }

  record(entry) {
    const counts = this.counts;
    const group = { known: counts.known_sources, discovered: counts.discovered_sources, other: counts.other }[entry.source];
    const groups = [counts.total, group];
    if (entry.publisher) {
      counts.publishers[entry.publisher] = counts.publishers[entry.publisher] || counter();
      groups.push(counts.publishers[entry.publisher]);
    }
    for (const c of groups) {
      c.requests += 1;
      c[entry.kind] += 1;
      c.bytes += entry.bytes;
      c.errors += entry.status >= 500 ? 1 : 0;
    }
    if (this.log) {
      this.log.write(JSON.stringify({ label: this.label, ...entry }) + "\n");
    }
  }
}

function documentKind(pathname) {
  if (pathname === "/") {
    return "server-root";
  }
  if (!pathname.endsWith("/")) {
    return "document";
  }
  return pathname.split("/").filter(Boolean).length === 1 ? "storage-root" : "container";
}

// The SPARQL query of a request (GET ?query=, a form-encoded POST or a sparql-query POST), described for the log
function describeQuery(req, body, maxChars) {
  let query = new URL(req.url, "http://x").searchParams.get("query");
  if (query === null && body) {
    const type = String(req.headers["content-type"] || "").split(";")[0].trim().toLowerCase();
    if (type === "application/sparql-query") {
      query = body;
    } else if (type === "application/x-www-form-urlencoded") {
      query = new URLSearchParams(body).get("query");
    }
  }
  if (query === null) {
    return { query_form: null, query: null, query_chars: 0 };
  }
  const form = QUERY_FORM.exec(query);
  return {
    query_form: form ? form[1].toUpperCase() : null,
    query: query.length > maxChars ? query.slice(0, maxChars) : query,
    query_chars: query.length,
  };
}

function withoutHop(headers) {
  const out = {};
  for (const [k, v] of Object.entries(headers)) {
    if (!HOP.has(k)) {
      out[k] = v;
    }
  }
  return out;
}

// Forwards a request to an upstream {server, host, port, agent, hostHeader?} as soon as it arrives, and records
// it in stats once the response has ended, the upstream failed (502) or the client went away (499).
function forward(req, res, upstream, entry, stats) {
  const began = Date.now();
  const headers = withoutHop(req.headers);
  if (upstream.hostHeader) {
    headers.host = upstream.hostHeader;
  }
  const inFlight = openPerServer.get(upstream.server) || 0;
  openPerServer.set(upstream.server, inFlight + 1);
  let headersMs = null;
  let firstByteMs = null;
  // The query is only read for the log, so without one a request costs nothing more than before
  const keepQuery = entry.kind === "sparql" && Boolean(stats.log);
  const body = [];
  let bodyBytes = 0;

  let finished = false;
  const done = (status, bytes) => {
    if (finished) {
      return;
    }
    finished = true;
    openPerServer.set(upstream.server, openPerServer.get(upstream.server) - 1);
    const record = { ...entry, t: began, status, bytes, ms: Date.now() - began,
                     headers_ms: headersMs, first_byte_ms: firstByteMs, in_flight: inFlight };
    if (!keepQuery) {
      stats.record(record);
      return;
    }
    // An upstream can fail before the client has sent all of its query: the entry waits for the rest
    let written = false;
    const write = () => {
      if (!written) {
        written = true;
        stats.record({ ...record, ...describeQuery(req, Buffer.concat(body).toString("utf8"), stats.logQueryChars) });
      }
    };
    if (req.readableEnded || req.destroyed) {
      write();
    } else {
      req.once("end", write);
      req.once("close", write);
    }
  };

  const outgoing = http.request({
    host: upstream.host, port: upstream.port, method: req.method, path: req.url, headers, agent: upstream.agent,
  }, (up) => {
    headersMs = Date.now() - began;
    res.writeHead(up.statusCode, withoutHop(up.headers));
    let bytes = 0;
    up.on("data", (chunk) => {
      if (firstByteMs === null) {
        firstByteMs = Date.now() - began;
      }
      bytes += chunk.length;
    });
    up.on("end", () => done(up.statusCode, bytes));
    up.on("error", () => done(502, bytes));
    // An upstream that drops its answer midway would leave the client waiting on the rest of it, which never
    // comes: the client's connection is cut off as well, so it sees the failure when it happens
    up.on("close", () => {
      if (!up.complete) {
        done(502, bytes);
        res.destroy();
      }
    });
    up.pipe(res);
  });
  outgoing.on("error", () => {
    done(502, 0);
    // Once the answer has begun, the failure can only be told by cutting it off: appending to it would
    // hand the client a corrupt answer instead
    if (res.headersSent) {
      res.destroy();
      return;
    }
    res.writeHead(502, { "content-type": "text/plain" });
    res.end("Bad gateway");
  });
  res.on("close", () => {
    if (!finished) {
      outgoing.destroy();
      done(499, 0);
    }
  });
  req.pipe(outgoing);
  if (keepQuery) {
    // A second reader beside the pipe: it sees the same chunks and holds nothing back
    req.on("data", (chunk) => {
      if (bodyBytes < MAX_LOGGED_BODY_BYTES) {
        body.push(chunk);
        bodyBytes += chunk.length;
      }
    });
  }
}

// A server that never closes a client's connection itself. Node's default does so 5 s after it has handed
// the last byte of an answer to the kernel, which can be before the client has read it: a client still reading
// a large answer then has it cut off. The client closes the connections it no longer needs instead.
function createServer(handler) {
  const server = http.createServer(handler);
  server.keepAliveTimeout = 0;
  return server;
}

// One upstream per server: the Solid server, and the first service on every Virtuoso host.
function health(map) {
  const targets = { documents: { ...map.documents, path: "/" } };
  for (const s of map.services) {
    const key = s.host || "localhost";
    if (!targets[key]) {
      targets[key] = { ...s, path: `/${s.publisher}/sparql` };
    }
  }
  return Promise.all(Object.entries(targets).map(([name, t]) => new Promise((resolve) => {
    const r = http.get({ host: t.host || "localhost", port: t.target, path: t.path, timeout: 2000,
                         headers: { host: t.publisher ? `localhost:${t.target}` : `localhost:${map.documents.port}` } },
                       (up) => { up.resume(); resolve([name, true]); });
    r.on("timeout", () => r.destroy());
    r.on("error", () => resolve([name, false]));
  }))).then(Object.fromEntries);
}

function main(mapPath, logPath) {
  const map = JSON.parse(fs.readFileSync(mapPath, "utf8"));
  const log = logPath ? fs.createWriteStream(logPath, { flags: "a" }) : null;
  const stats = new RequestStats(map.publishers, log, map.log_query_chars ?? DEFAULT_LOG_QUERY_CHARS);
  const monitors = (map.monitor && map.monitor.components || []).map((c) =>
    new ComponentMonitor(c.name, c.cgroup ? () => readCgroup(c.cgroup) : () => readProcess(c.pid)));
  monitors.push(new ComponentMonitor("proxy", readSelf));

  setInterval(() => {
    for (const m of monitors) {
      m.sample();
    }
  }, (map.monitor && map.monitor.sample_ms) || 50);

  const documents = {
    server: "documents",
    host: map.documents.host || "localhost",
    port: map.documents.target,
    // Connections are reused, but never limited: a cap would hold requests back in the proxy, out of sight of
    // both the engine and the Solid server, and the proxy must only count
    agent: new http.Agent({ keepAlive: true, maxSockets: Infinity }),
  };
  createServer((req, res) => {
    const pathname = new URL(req.url, "http://x").pathname;
    const publisher = decodeURIComponent(pathname.split("/").filter(Boolean)[0] || "") || null;
    const entry = { method: req.method, url: `http://${req.headers.host}${req.url}`, publisher,
                    kind: documentKind(pathname), source: stats.source(publisher) };
    forward(req, res, documents, entry, stats);
  }).listen(map.documents.port);

  for (const service of map.services) {
    const upstream = {
      // Every service on one Virtuoso host shares its server, and so its HTTP threads
      server: `virtuoso:${service.host || "localhost"}`,
      host: service.host || "localhost",
      port: service.target,
      // A fresh connection per request, so no reused socket can have been dropped by Virtuoso.
      agent: false,
      // Virtuoso's endpoints are defined on their own ports
      hostHeader: `localhost:${service.target}`,
    };
    createServer((req, res) => {
      const entry = { method: req.method, url: `http://${req.headers.host}${req.url}`, publisher: service.publisher,
                      kind: "sparql", source: stats.source(service.publisher) };
      forward(req, res, upstream, entry, stats);
    }).listen(service.port);
  }

  createServer((req, res) => {
    const reply = (status, body) => {
      res.writeHead(status, { "content-type": "application/json" });
      res.end(JSON.stringify(body, null, 2));
    };
    const url = new URL(req.url, "http://x");
    if (req.method === "GET" && url.pathname === "/health") {
      health(map).then((upstreams) => reply(Object.values(upstreams).every(Boolean) ? 200 : 503, upstreams));
      return;
    }
    if (req.method === "GET" && url.pathname === "/stats") {
      const withSamples = url.searchParams.get("samples") === "1";
      for (const m of monitors) {
        m.sample();
      }
      reply(200, {
        ...stats.counts,
        resources: Object.fromEntries(monitors.map((m) => [m.name, m.summary(withSamples)])),
      });
      return;
    }
    if (req.method === "POST" && url.pathname === "/reset") {
      let body = "";
      req.on("data", (chunk) => { body += chunk; });
      req.on("end", () => {
        let options = {};
        try {
          options = body.trim() ? JSON.parse(body) : {};
        } catch (e) {
          reply(400, { error: `Body is not JSON: ${e.message}` });
          return;
        }
        for (const m of monitors) {
          m.start();
        }
        stats.reset(options);
        reply(200, { label: stats.label, known: stats.counts.known });
      });
      return;
    }
    reply(404, { error: "Use GET /stats, GET /health or POST /reset" });
  }).listen(map.control_port);

  console.log(`Proxy: documents on :${map.documents.port}, ${map.services.length} services, control on :${map.control_port}`);
}

if (require.main === module) {
  main(process.argv[2], process.argv[3]);
}

module.exports = { ComponentMonitor, RequestStats, describeQuery, documentKind };
