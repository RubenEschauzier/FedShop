// Preload (node -r): send every fetch with "Connection: close", so no connection to Virtuoso is reused.
// Virtuoso keeps at most MaxKeepAlives (default 10) idle connections and silently drops the rest,
// which makes Node's fetch fail with "other side closed" when it reuses one.
const orig = globalThis.fetch;
globalThis.fetch = (input, init = {}) => {
  const headers = new Headers(init.headers ?? (input instanceof Request ? input.headers : undefined));
  headers.set('connection', 'close');
  return orig(input, { ...init, headers });
};
