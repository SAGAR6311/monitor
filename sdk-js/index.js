// monitor-js: reports errors to your Monitor server from the browser, Node / Next.js and React Native / Expo.
// No dependencies.
const g = globalThis;
const isRN = typeof navigator !== 'undefined' && navigator.product === 'ReactNative';
const isBrowser = !isRN && typeof window !== 'undefined' && typeof document !== 'undefined';
// via globalThis so Next.js doesn't flag Node-only APIs when it also bundles this for the Edge runtime
const proc = g.process;
const isNode = !isRN && !isBrowser && !!(proc && proc.versions && proc.versions.node && proc.on);
const MAX_PER_MINUTE = 30; // stops an error inside a loop from flooding the server
const MAX_CRUMBS = 50;

let cfg = null;
let user = null;
let rawFetch = null;
const tags = {};
const crumbs = [];
const recent = [];
const pending = new Set();
const reported = new WeakSet();

const str = x => {
  if (typeof x === 'string') return x;
  if (x instanceof Error) return `${x.name}: ${x.message}`;
  try { return JSON.stringify(x) ?? String(x); } catch { return String(x); }
};

export function init(options) {
  if (cfg) return; // hot reload / imported twice
  const m = /^(https?):\/\/([^@]+)@([^/]+)(\/.*)?\/(\d+)$/.exec((options && options.dsn) || '');
  if (!m) throw new Error('monitor-js: invalid dsn, expected https://KEY@host/PROJECT_ID');
  const [, scheme, key, host, path = '', project] = m;
  cfg = { ...options, origin: `${scheme}://${host}`, url: `${scheme}://${host}${path}/ingest/${project}?key=${encodeURIComponent(key)}` };
  Object.assign(tags, options.tags);
  rawFetch = g.fetch.bind(g);
  if (isBrowser) installBrowser();
  else if (isRN) installReactNative();
  else if (isNode) installNode();
}

export function setUser(u) { user = u || null; }
export function setTag(key, value) { tags[key] = value; }

export function addBreadcrumb(crumb) {
  crumbs.push({ timestamp: Date.now() / 1000, ...crumb });
  if (crumbs.length > MAX_CRUMBS) crumbs.shift();
}

export function captureException(err, opts = {}) {
  if (err && typeof err === 'object') { // the same error object is often logged twice (e.g. by Next.js)
    if (reported.has(err)) return undefined;
    reported.add(err);
  }
  const values = [];
  for (let e = err, i = 0; e != null && i < 5; e = e && typeof e === 'object' ? e.cause : null, i++) {
    values.unshift(e instanceof Error || (e && e.stack)
      ? { type: e.name || 'Error', value: String(e.message ?? ''), stacktrace: { frames: parseStack(e.stack) } }
      : { type: 'Error', value: str(e) });
  }
  return send({ level: opts.level || 'error', exception: { values } }, opts);
}

export function captureMessage(message, level = 'info', opts = {}) {
  return send({ level, message: String(message) }, opts);
}

/** Resolves when queued events are sent (or after timeoutMs). */
export function flush(timeoutMs = 2000) {
  return Promise.race([Promise.all([...pending]), new Promise(r => setTimeout(r, timeoutMs))]).then(() => {});
}

/** Next.js server errors: `export { onRequestError } from 'monitor-js'` in instrumentation.ts. */
export async function onRequestError(err, request, context) {
  captureException(err, {
    request: request && { url: request.path, method: request.method },
    tags: context && { route: context.routePath, routeType: context.routeType },
  });
  await flush();
}

// ---------- internals ----------

const V8 = /^\s*at (?:(.+?) \()?(?:address at )?(.+?):(\d+):(\d+)\)?$/; // Chrome, Node, Hermes
const GECKO = /^\s*(.*?)@(.+?):(\d+):(\d+)$/; // Firefox, Safari

function parseStack(stack) {
  const frames = [];
  for (const line of String(stack || '').split('\n')) {
    const m = V8.exec(line) || GECKO.exec(line);
    if (m) frames.push({
      function: m[1] || '?', filename: m[2], lineno: +m[3], colno: +m[4],
      in_app: !/node_modules|^node:|^internal\/|\/native$/.test(m[2]),
    });
  }
  return frames.reverse(); // oldest call first, crash site last
}

function context() {
  if (isBrowser) {
    const ua = navigator.userAgent;
    const b = [['Edg', 'Edge'], ['OPR', 'Opera'], ['Firefox', 'Firefox'], ['Chrome', 'Chrome'], ['Version', 'Safari']]
      .map(([k, name]) => [name, new RegExp(`${k}/([\\d.]+)`).exec(ua)]).find(([, m]) => m);
    const os = /(Windows NT|Mac OS X|Android|iPhone OS|CPU OS|Linux)[ _]?([\d._]*)/.exec(ua);
    return {
      request: { url: location.href, headers: { 'User-Agent': ua } },
      contexts: { browser: b && { name: b[0], version: b[1][1] }, os: os && { name: os[1], version: os[2].replace(/_/g, '.') } },
    };
  }
  if (isNode) return { contexts: { runtime: { name: 'node', version: proc.versions.node }, os: { name: proc.platform } } };
  return {};
}

function send(body, opts) {
  if (!cfg) return undefined;
  const now = Date.now();
  while (recent.length && now - recent[0] > 60000) recent.shift();
  if (recent.length >= MAX_PER_MINUTE) return undefined;
  recent.push(now);

  const base = context();
  let event = {
    event_id: Array.from({ length: 32 }, () => ((Math.random() * 16) | 0).toString(16)).join(''),
    timestamp: now / 1000,
    platform: isRN ? 'react-native' : isBrowser ? 'javascript' : 'node',
    environment: cfg.environment,
    release: cfg.release,
    user: user || undefined,
    tags: { ...tags, ...opts.tags },
    extra: opts.extra,
    breadcrumbs: crumbs.slice(),
    ...base,
    request: opts.request || base.request,
    ...body,
  };
  if (cfg.beforeSend) {
    event = cfg.beforeSend(event);
    if (!event) return undefined;
  }
  let payload;
  try { payload = JSON.stringify(event); } catch { payload = JSON.stringify({ ...event, extra: undefined }); }
  const p = rawFetch(cfg.url, {
    method: 'POST',
    body: payload,
    headers: { 'Content-Type': 'text/plain' }, // simple request: no CORS preflight
    keepalive: isBrowser && payload.length < 60000, // survives page unload
  }).catch(() => {}).finally(() => pending.delete(p));
  pending.add(p);
  return event.event_id;
}

function wrapConsole() {
  for (const level of ['log', 'info', 'warn', 'error']) {
    const orig = console[level];
    console[level] = (...args) => {
      addBreadcrumb({ category: 'console', level, message: args.map(str).join(' ').slice(0, 500) });
      orig.apply(console, args);
    };
  }
}

function wrapFetch() {
  const orig = g.fetch;
  g.fetch = function (input, init) {
    const url = String((input && input.url) || input);
    const method = (init && init.method) || (input && input.method) || 'GET';
    const p = orig.apply(this, arguments);
    if (!url.startsWith(cfg.origin)) {
      p.then(r => addBreadcrumb({ category: 'fetch', level: r.ok ? 'info' : 'warning', message: `${method} ${url} → ${r.status}` }),
        () => addBreadcrumb({ category: 'fetch', level: 'error', message: `${method} ${url} → failed` }));
    }
    return p;
  };
}

function installBrowser() {
  window.addEventListener('error', e => {
    if (!e.error && e.message === 'Script error.') return; // cross-origin script, no details available
    captureException(e.error || e.message, { tags: { mechanism: 'window.onerror' } });
  });
  window.addEventListener('unhandledrejection', e => captureException(e.reason, { tags: { mechanism: 'unhandledrejection' } }));
  let last = location.href;
  const nav = () => { if (location.href !== last) { addBreadcrumb({ category: 'navigation', message: `${last} → ${location.href}` }); last = location.href; } };
  for (const k of ['pushState', 'replaceState']) {
    const orig = history[k];
    history[k] = function () { const r = orig.apply(this, arguments); nav(); return r; };
  }
  window.addEventListener('popstate', nav);
  wrapConsole();
  wrapFetch();
}

function installReactNative() {
  const EU = g.ErrorUtils;
  if (EU) {
    const prev = EU.getGlobalHandler();
    EU.setGlobalHandler((err, isFatal) => {
      captureException(err, { level: isFatal ? 'fatal' : 'error', tags: { mechanism: 'ErrorUtils' } });
      // a fatal error kills the app: give the report up to 2s to leave first
      if (isFatal) flush(2000).then(() => prev(err, isFatal));
      else prev(err, isFatal);
    });
  }
  // ponytail: Hermes-only rejection tracking (Expo default); JSC apps miss unhandled promise rejections
  if (!g.__DEV__ && g.HermesInternal && g.HermesInternal.enablePromiseRejectionTracker) {
    g.HermesInternal.enablePromiseRejectionTracker({ allRejections: true, onUnhandled: (_id, err) => captureException(err, { tags: { mechanism: 'unhandledrejection' } }) });
  }
  wrapConsole();
  wrapFetch();
}

function installNode() {
  proc.on('uncaughtException', err => {
    captureException(err, { level: 'fatal', tags: { mechanism: 'uncaughtException' } });
    // We're the only handler: Node would have crashed, so crash too, after sending.
    if (proc.listenerCount('uncaughtException') === 1) {
      console.error(err);
      flush(2000).then(() => proc.exit(1));
    }
  });
  proc.on('unhandledRejection', reason => captureException(reason, { tags: { mechanism: 'unhandledRejection' } }));
}
