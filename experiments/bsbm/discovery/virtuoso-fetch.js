// Preload (node -r) for Node.js engines against the benchmark's Virtuoso services.
//
// - Every fetch is sent with "Connection: close": Virtuoso keeps at most MaxKeepAlives (10) idle
//   connections and silently drops the rest, which makes fetch fail on a reused socket.
// - At most VIRTUOSO_LIMIT (default 16) requests are in flight to Virtuoso at once: all services are
//   one server with 20 HTTP threads, and an engine that floods it gets connect timeouts.
// - A request whose connection timed out or was reset is retried (up to 4 times, with backoff).
// - Request counts (documents, .meta descriptions, service requests, retries) are written to stderr
//   as `COUNTS {...}` every 2 s and at exit, so a run that is killed still reports how far it got.
//
// Usage: node -r ./virtuoso-fetch.js <engine>/bin/query.js ...
const orig = globalThis.fetch;
const LIMIT = Number(process.env.VIRTUOSO_LIMIT ?? 16);
const SERVICE = /^http:\/\/localhost:(343\d\d|8891)\//u;
const DOCUMENT = /^http:\/\/localhost:3000\//u;
const RETRY = new Set([ 'UND_ERR_CONNECT_TIMEOUT', 'ECONNRESET', 'UND_ERR_SOCKET', 'ECONNREFUSED' ]);
const counts = { docs: 0, meta: 0, endpoint: 0, retries: 0 };

let active = 0;
const waiting = [];
const acquire = () => active < LIMIT ? (active++, Promise.resolve()) : new Promise(resolve => waiting.push(resolve));
const release = () => {
  const next = waiting.shift();
  if (next) {
    next();
  } else {
    active--;
  }
};

globalThis.fetch = async(input, init = {}) => {
  const url = String(input instanceof Request ? input.url : input);
  const headers = new Headers(init.headers ?? (input instanceof Request ? input.headers : undefined));
  headers.set('connection', 'close');
  if (DOCUMENT.test(url)) {
    counts[url.endsWith('.meta') ? 'meta' : 'docs']++;
  }
  if (!SERVICE.test(url)) {
    return orig(input, { ...init, headers });
  }
  counts.endpoint++;
  await acquire();
  try {
    for (let attempt = 0; ; attempt++) {
      try {
        const res = await orig(input, { ...init, headers });
        // Read the body while holding the slot, so the cap bounds open connections.
        const body = await res.arrayBuffer();
        return new Response(body, { status: res.status, statusText: res.statusText, headers: res.headers });
      } catch (error) {
        if (attempt >= 4 || !RETRY.has(error.cause?.code)) {
          throw error;
        }
        counts.retries++;
        await new Promise(resolve => setTimeout(resolve, 200 * 2 ** attempt));
      }
    }
  } finally {
    release();
  }
};

const report = () => process.stderr.write(`COUNTS ${JSON.stringify(counts)}\n`);
setInterval(report, 2_000).unref();
process.on('exit', report);
