// Black-box tests of the counting proxy: it runs as a process in front of fake upstreams (a Solid
// server, query services, a cgroup) and is driven through its public ports and control API only, so
// the same tests check any version of proxy.js.
//
// Usage, from tools/proxy: node --test    (PROXY_SCRIPT=<path> tests another proxy.js)
"use strict";
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { after, before, test } = require("node:test");

const SCRIPT = process.env.PROXY_SCRIPT || path.join(__dirname, "..", "proxy.js");
const KINDS = ["document", "storage-root", "container", "server-root", "sparql"];
const SERVICE_DELAY_MS = 100;

let tmp;
let cgroupDir;
let logPath;
let documents;
let services = {};
let ports = {};
let sleeper;
let proxy;

function listen(handler) {
  return new Promise((resolve) => {
    const server = http.createServer(handler);
    server.listen(0, () => resolve(server));
  });
}

async function freePort() {
  const server = await listen(() => {});
  const { port } = server.address();
  await new Promise((resolve) => server.close(resolve));
  return port;
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// One request on a fresh connection; resolves with its status, headers and body
function request(port, pathname, { method = "GET", headers = {}, body = "" } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: "localhost", port, path: pathname, method, headers, agent: false }, (res) => {
      let data = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => {
        data += chunk;
      });
      res.on("end", () => {
        resolve({ status: res.statusCode, headers: res.headers, body: data });
      });
    });
    req.on("error", reject);
    req.end(body);
  });
}

async function control(method, pathname, body = "") {
  const res = await request(ports.control, pathname, { method, body });
  return { status: res.status, body: JSON.parse(res.body) };
}

function sparql(name) {
  return request(ports[name], `/${name}/sparql`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: "query=ASK%7B%7D",
  });
}

// Renamed into place, so the proxy's sampler never reads a half-written file
function writeAtomic(file, content) {
  fs.writeFileSync(`${file}.tmp`, content);
  fs.renameSync(`${file}.tmp`, file);
}

function writeCgroup(usageUsec, memoryCurrent) {
  writeAtomic(path.join(cgroupDir, "cpu.stat"), `usage_usec ${usageUsec}\nuser_usec 0\n`);
  writeAtomic(path.join(cgroupDir, "memory.current"), `${memoryCurrent}\n`);
  writeAtomic(path.join(cgroupDir, "memory.stat"), "active_file 5\ninactive_file 200000\n");
}

// A query service that answers after SERVICE_DELAY_MS and remembers how many requests overlapped
async function fakeService() {
  const service = { inflight: 0, peak: 0 };
  service.server = await listen((req, res) => {
    service.inflight += 1;
    service.peak = Math.max(service.peak, service.inflight);
    req.resume();
    setTimeout(() => {
      service.inflight -= 1;
      res.end(`sparql:${req.headers.host}`);
    }, SERVICE_DELAY_MS);
  });
  service.port = service.server.address().port;
  return service;
}

function closeServer(server) {
  server.closeAllConnections();
  return new Promise((resolve) => server.close(resolve));
}

function counter() {
  const c = { requests: 0, bytes: 0, errors: 0 };
  for (const kind of KINDS) {
    c[kind] = 0;
  }
  return c;
}

// The counts the proxy should report for these log entries
function expectedCounts(label, known, entries) {
  const counts = {
    label, known, total: counter(), known_sources: counter(), discovered_sources: counter(), other: counter(),
    publishers: {},
  };
  const groups = { known: counts.known_sources, discovered: counts.discovered_sources, other: counts.other };
  for (const entry of entries) {
    const targets = [counts.total, groups[entry.source]];
    if (entry.publisher) {
      counts.publishers[entry.publisher] = counts.publishers[entry.publisher] || counter();
      targets.push(counts.publishers[entry.publisher]);
    }
    for (const c of targets) {
      c.requests += 1;
      c[entry.kind] += 1;
      c.bytes += entry.bytes;
      c.errors += entry.status >= 500 ? 1 : 0;
    }
  }
  return counts;
}

function documentEntry(pathname, publisher, kind, source, status = 200, bytes = `doc:${pathname}`.length) {
  return { method: "GET", url: `http://localhost:${ports.documents}${pathname}`, publisher, kind, source, status, bytes };
}

function serviceEntry(name, source, status = 200, bytes = `sparql:localhost:${services[name].port}`.length) {
  return { method: "POST", url: `http://localhost:${ports[name]}/${name}/sparql`, publisher: name, kind: "sparql",
           source, status, bytes };
}

function logEntries(label) {
  const lines = fs.readFileSync(logPath, "utf8").trim().split("\n").map((line) => JSON.parse(line));
  return lines.filter((entry) => entry.label === label);
}

function byUrl(a, b) {
  return `${a.url} ${a.status}`.localeCompare(`${b.url} ${b.status}`);
}

before(async () => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "proxy-test-"));
  cgroupDir = path.join(tmp, "cgroup");
  fs.mkdirSync(cgroupDir);
  writeCgroup(2000000, 1000000);
  logPath = path.join(tmp, "requests.jsonl");

  documents = await listen((req, res) => {
    documents.seen.push({ url: req.url, headers: req.headers });
    if (req.url.endsWith("/slow")) {
      setTimeout(() => res.end("slow"), 2000);
      return;
    }
    if (req.url.endsWith("/fail")) {
      res.writeHead(500);
      res.end("fail");
      return;
    }
    res.end(`doc:${req.url}`);
  });
  documents.seen = [];
  services = { vendor1: await fakeService(), vendor2: await fakeService() };
  ports = {
    documents: await freePort(), vendor1: await freePort(), vendor2: await freePort(), vendor3: await freePort(),
    control: await freePort(), closed: await freePort(),
  };
  sleeper = spawn("sleep", ["60"], { stdio: "ignore" });

  const map = {
    documents: { port: ports.documents, target: documents.address().port },
    services: [
      { port: ports.vendor1, target: services.vendor1.port, publisher: "vendor1" },
      { port: ports.vendor2, target: services.vendor2.port, host: "localhost", publisher: "vendor2" },
      { port: ports.vendor3, target: ports.closed, publisher: "vendor3" },
    ],
    publishers: {
      vendor1: { kind: "vendor", known: true },
      vendor2: { kind: "vendor", known: false },
      vendor3: { kind: "vendor", known: false },
    },
    control_port: ports.control,
    monitor: {
      sample_ms: 20,
      components: [
        { name: "css", pid: sleeper.pid },
        { name: "endpoints", cgroup: cgroupDir },
        { name: "missing", cgroup: path.join(tmp, "missing") },
      ],
    },
  };
  const mapPath = path.join(tmp, "proxy.json");
  fs.writeFileSync(mapPath, JSON.stringify(map));

  proxy = spawn(process.execPath, [SCRIPT, mapPath, logPath], { stdio: ["ignore", "pipe", "inherit"] });
  await new Promise((resolve, reject) => {
    proxy.stdout.once("data", resolve);
    proxy.once("exit", (code) => reject(new Error(`The proxy exited with code ${code}`)));
  });
});

after(async () => {
  proxy.kill();
  sleeper.kill();
  await Promise.all([documents, services.vendor1.server, services.vendor2.server].map(closeServer));
  fs.rmSync(tmp, { recursive: true, force: true });
});

test("POST /reset without a body restores the defaults and restarts the resource measurements", async () => {
  assert.deepEqual(await control("POST", "/reset"), { status: 200, body: { label: null, known: ["vendor1"] } });
  writeCgroup(2500000, 1200000);
  await wait(150);

  const { status, body } = await control("GET", "/stats?samples=1");
  assert.equal(status, 200);
  assert.deepEqual(Object.keys(body.resources), ["css", "endpoints", "missing", "proxy"]);

  const endpoints = body.resources.endpoints;
  assert.deepEqual(Object.keys(endpoints),
                   ["wall_seconds", "cpu_seconds", "cpu_percent", "memory_bytes", "samples", "error", "series"]);
  assert.equal(endpoints.cpu_seconds, 0.5);
  assert.ok(endpoints.cpu_percent.max > 0);
  assert.equal(endpoints.memory_bytes.min, 800000);
  assert.equal(endpoints.memory_bytes.max, 1000000);
  assert.equal(endpoints.error, null);
  assert.equal(endpoints.series.length, endpoints.samples);
  assert.deepEqual(endpoints.series[0], { t_ms: 0, cpu_percent: null, memory_bytes: 800000 });

  assert.deepEqual(Object.keys(body.resources.missing), ["error"]);
  assert.match(body.resources.missing.error, /ENOENT/);
  for (const name of ["css", "proxy"]) {
    assert.equal(body.resources[name].error, null);
    assert.ok(body.resources[name].memory_bytes.min > 0);
    assert.ok(body.resources[name].cpu_seconds >= 0);
  }
});

test("counts and logs every request by source, kind and publisher", async () => {
  await control("POST", "/reset", JSON.stringify({ label: "phase1" }));

  for (const pathname of ["/", "/vendor1/", "/vendor2/a/", "/unknown/x", "/vendor%31/x"]) {
    assert.deepEqual(await request(ports.documents, pathname).then((r) => [r.status, r.body]), [200, `doc:${pathname}`]);
  }
  const forwarded = await request(ports.documents, "/vendor2/doc", {
    headers: { "x-test": "1", "proxy-connection": "keep-alive" },
  });
  assert.equal(forwarded.body, "doc:/vendor2/doc");
  const seen = documents.seen.at(-1).headers;
  assert.equal(seen["x-test"], "1");
  assert.equal(seen["proxy-connection"], undefined);
  assert.equal(seen.host, `localhost:${ports.documents}`);

  assert.equal((await request(ports.documents, "/vendor2/fail")).status, 500);

  // The client gives up before the upstream answers
  await new Promise((resolve) => {
    const req = http.request({ host: "localhost", port: ports.documents, path: "/vendor2/slow", agent: false });
    req.on("error", () => {});
    req.end();
    setTimeout(() => {
      req.destroy();
      resolve();
    }, 100);
  });

  // Requests are forwarded as they arrive: nothing waits in the proxy
  const answers = await Promise.all([sparql("vendor2"), sparql("vendor2"), sparql("vendor2"), sparql("vendor1")]);
  for (const [i, name] of ["vendor2", "vendor2", "vendor2", "vendor1"].entries()) {
    assert.deepEqual([answers[i].status, answers[i].body], [200, `sparql:localhost:${services[name].port}`]);
  }
  assert.equal(services.vendor2.peak, 3);

  const refused = await sparql("vendor3");
  assert.deepEqual([refused.status, refused.body], [502, "Bad gateway"]);
  await wait(50);

  const expected = [
    documentEntry("/", null, "server-root", "other"),
    documentEntry("/vendor1/", "vendor1", "storage-root", "known"),
    documentEntry("/vendor2/a/", "vendor2", "container", "discovered"),
    documentEntry("/unknown/x", "unknown", "document", "other"),
    documentEntry("/vendor%31/x", "vendor1", "document", "known"),
    documentEntry("/vendor2/doc", "vendor2", "document", "discovered"),
    documentEntry("/vendor2/fail", "vendor2", "document", "discovered", 500, 4),
    documentEntry("/vendor2/slow", "vendor2", "document", "discovered", 499, 0),
    serviceEntry("vendor2", "discovered"),
    serviceEntry("vendor2", "discovered"),
    serviceEntry("vendor2", "discovered"),
    serviceEntry("vendor1", "known"),
    serviceEntry("vendor3", "discovered", 502, 0),
  ];

  const log = logEntries("phase1");
  for (const entry of log) {
    assert.deepEqual(Object.keys(entry),
                     ["label", "method", "url", "publisher", "kind", "source", "t", "status", "bytes", "ms"]);
    assert.equal(typeof entry.t, "number");
    assert.ok(entry.ms >= 0);
  }
  const withoutTimes = log.map(({ t, ms, ...rest }) => rest);
  assert.deepEqual(withoutTimes.sort(byUrl), expected.map((e) => ({ label: "phase1", ...e })).sort(byUrl));

  const { body: stats } = await control("GET", "/stats");
  assert.deepEqual(Object.keys(stats), ["label", "since", "known", "total", "known_sources", "discovered_sources",
                                        "other", "publishers", "resources"]);
  assert.deepEqual(Object.keys(stats.total), ["requests", "bytes", "errors", ...KINDS]);
  assert.equal("series" in stats.resources.endpoints, false);
  const { since, resources, ...counts } = stats;
  assert.deepEqual(counts, expectedCounts("phase1", ["vendor1"], expected));
});

test("POST /reset sets the label and the publishers counted as known", async () => {
  const reset = await control("POST", "/reset", JSON.stringify({ label: "q1", known: ["vendor2", "taxonomy"] }));
  assert.deepEqual(reset, { status: 200, body: { label: "q1", known: ["taxonomy", "vendor2"] } });

  for (const pathname of ["/vendor2/doc", "/vendor1/doc", "/taxonomy/x"]) {
    await request(ports.documents, pathname);
  }
  const expected = [
    documentEntry("/vendor2/doc", "vendor2", "document", "known"),
    documentEntry("/vendor1/doc", "vendor1", "document", "discovered"),
    documentEntry("/taxonomy/x", "taxonomy", "document", "other"),
  ];
  const { body: stats } = await control("GET", "/stats");
  const { since, resources, ...counts } = stats;
  assert.deepEqual(counts, expectedCounts("q1", ["taxonomy", "vendor2"], expected));
  assert.equal(logEntries("q1").length, 3);

  // An explicit null is the same as leaving it out
  const restored = await control("POST", "/reset", JSON.stringify({ label: "q2", known: null }));
  assert.deepEqual(restored, { status: 200, body: { label: "q2", known: ["vendor1"] } });
});

test("the control API rejects a body that is not JSON and unknown routes", async () => {
  const previous = (await control("GET", "/stats")).body;

  const bad = await control("POST", "/reset", "{");
  assert.equal(bad.status, 400);
  assert.match(bad.body.error, /^Body is not JSON: /);

  const notFound = { status: 404, body: { error: "Use GET /stats, GET /health or POST /reset" } };
  assert.deepEqual(await control("GET", "/nope"), notFound);
  assert.deepEqual(await control("POST", "/stats"), notFound);

  const current = (await control("GET", "/stats")).body;
  assert.deepEqual([current.label, current.since], [previous.label, previous.since]);
});

test("a client that gives up on a query service holds up no later request", async () => {
  await control("POST", "/reset", JSON.stringify({ label: "abandoned" }));
  const clients = Array.from({ length: 3 }, () => http.request({
    host: "localhost", port: ports.vendor2, path: "/vendor2/sparql", method: "POST", agent: false,
  }));
  for (const req of clients) {
    req.on("error", () => {});
    req.end("query=ASK%7B%7D");
  }
  await wait(SERVICE_DELAY_MS / 2);
  for (const req of clients) {
    req.destroy();
  }

  const answer = await sparql("vendor2");
  assert.deepEqual([answer.status, answer.body], [200, `sparql:localhost:${services.vendor2.port}`]);
  await wait(50);
  assert.deepEqual(logEntries("abandoned").map((e) => e.status).sort(), [200, 499, 499, 499]);
});

test("GET /health checks the Solid server and one service per Virtuoso host", async () => {
  assert.deepEqual(await control("GET", "/health"), { status: 200, body: { documents: true, localhost: true } });
  await closeServer(documents);
  assert.deepEqual(await control("GET", "/health"), { status: 503, body: { documents: false, localhost: true } });
});
