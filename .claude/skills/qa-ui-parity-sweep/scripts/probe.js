// qa-ui-parity-sweep in-page probe. Served by `sweep.py serve`; load it in the page with
//   const P = await import('http://127.0.0.1:<port>/probe.js?v=' + Date.now());
// The collector's origin is taken from this module's own URL, so the port is whatever `serve`
// bound. Every dump is POSTed to the collector (never returned inline — a page's worth of
// computed styles would flood the transcript). Functions:
//   P.guard()                      block every write request (non-GET) to the app, for the page's life
//   P.status()                     visibility / focus / url — measure only when visible
//   await P.settle(ms)             wait, then until no enter/leave transition classes remain
//   P.pin(sel, px)                 pin a content column to a fixed width (so both apps lay out alike)
//   await P.capture({run, route, env, root})              buttons + headings + open dialogs → dumps/<route>/<env>/base.json
//   await P.modal({run, route, env, name, trigger})      open ONE dialog via a trigger, dump it, close it
//   await P.closeAll()             Escape / outside-click until no dialog is open
//
// The probe knows nothing about the app's UI toolkit. Toolkit-specific selectors, the identity
// provider's hosts, utility-class prefixes and icon-class patterns come from the routes file's
// `probe` block, served by the collector at /config.json and loaded on the first capture()/modal().

const COLLECTOR = new URL(import.meta.url).origin;

// The visible-style properties a parity reviewer can see. Box size comes from the rect.
export const PROPS = [
  'display', 'visibility', 'opacity', 'color', 'background-color', 'background-image',
  'border-top-width', 'border-right-width', 'border-bottom-width', 'border-left-width',
  'border-top-style', 'border-top-color', 'border-right-color', 'border-bottom-color', 'border-left-color',
  'border-top-left-radius', 'border-top-right-radius', 'border-bottom-right-radius', 'border-bottom-left-radius',
  'box-shadow', 'outline-style', 'outline-width', 'outline-color',
  'font-family', 'font-size', 'font-weight', 'font-style', 'line-height', 'letter-spacing',
  'text-transform', 'text-decoration-line', 'text-align', 'white-space',
  'padding-top', 'padding-right', 'padding-bottom', 'padding-left',
  'margin-top', 'margin-right', 'margin-bottom', 'margin-left',
  'column-gap', 'row-gap', 'justify-content', 'align-items', 'cursor',
];

// ---- per-stack configuration (from the collector) ------------------------------------------
// Defaults are native HTML + ARIA only. A toolkit that renders buttons as <a class="btn"> or
// dialogs as <div class="modal show"> declares those under `probe` in the routes file.
const CFG = (window.__paritySweepCfg ||= {
  loaded: false,
  guard_allow_hosts: [],
  settle_selectors: ['[class*="-enter-active"]', '[class*="-leave-active"]', '[class*="-enter-from"]'],
  buttons_extra: [], dialogs_extra: [], dialog_close_extra: [],
  utility_class_prefixes: [], icon_class_patterns: [],
});
const toRegex = (s) => { try { return new RegExp(s, 'i'); } catch (e) { return null; } };
let cfgPromise = null;
export function loadConfig() {
  if (CFG.loaded) return Promise.resolve(CFG);
  if (!cfgPromise) {
    cfgPromise = fetch(`${COLLECTOR}/config.json`, { cache: 'no-store' }).then((r) => r.json()).then((c) => {
      for (const k of Object.keys(CFG)) if (k !== 'loaded' && Array.isArray(c[k])) CFG[k] = c[k];
      CFG.allowRe = CFG.guard_allow_hosts.map(toRegex).filter(Boolean);
      CFG.utilRe = CFG.utility_class_prefixes.map((p) => toRegex('^(' + p + ')')).filter(Boolean);
      CFG.iconRe = CFG.icon_class_patterns.map(toRegex).filter(Boolean);
      CFG.loaded = true;
      return CFG;
    }).catch(() => { CFG.loaded = true; return CFG; });   // no config: the neutral defaults stand
  }
  return cfgPromise;
}
const sel = (base, extra) => [base, ...(extra || [])].filter(Boolean).join(',');
const BUTTONS_BASE = 'button,[role=button],input[type=button],input[type=submit],input[type=reset],[role=menuitem],[role=tab],[role=switch]';
const HEADINGS = 'h1,h2,h3,h4,h5,h6,[role=heading]';
const DIALOGS_BASE = '[role=dialog],[role=alertdialog],dialog[open]';
const BUTTONS = () => sel(BUTTONS_BASE, CFG.buttons_extra);
const DIALOGS = () => sel(DIALOGS_BASE, CFG.dialogs_extra);
const CLOSERS = () => sel('[aria-label="Close"],[aria-label="close"]', CFG.dialog_close_extra);
// Generic state/grid class prefixes that never identify a component; the toolkit's own
// utility prefixes (spacing, display, router state…) are added from config.
const UTIL_BASE = /^(is-|has-|active$|show$|fade$|col$|row$)/;

// ---- colour normalisation (oklch/color-mix/hex → rgb) -------------------------------------
const canvas = document.createElement('canvas');
canvas.width = canvas.height = 1;
const ctx = canvas.getContext('2d', { willReadFrequently: true });
const colorCache = new Map();
function normColor(c) {
  if (colorCache.has(c)) return colorCache.get(c);
  let out = c;
  try {
    ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = '#010203'; ctx.fillStyle = c; ctx.fillRect(0, 0, 1, 1);
    const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
    out = a === 0 ? 'transparent' : a === 255 ? `rgb(${r},${g},${b})` : `rgba(${r},${g},${b},${+(a / 255).toFixed(2)})`;
  } catch (e) { /* keep raw */ }
  colorCache.set(c, out);
  return out;
}
const COLOR_FN = /(?:oklch|oklab|lab|lch|color|rgba?|hsla?|hwb|color-mix)\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\)|#[0-9a-fA-F]{3,8}\b|\btransparent\b/g;
const normValue = (v) => (v && typeof v === 'string' && /[(#]|transparent/.test(v) ? v.replace(COLOR_FN, normColor) : v);
const r1 = (n) => Math.round(n * 10) / 10;
const squash = (s) => (s || '').replace(/\s+/g, ' ').trim();

// ---- read-only guard ------------------------------------------------------------------------
// A parity sweep must never write. Loading a page is a read; everything after the guard goes on
// is forced to be one. It goes on only AFTER the app has mounted (capture() installs it once the
// root exists): installed earlier it refuses the app's own boot requests (session / token POSTs)
// and the app sits on its splash screen forever. So: any non-GET/HEAD/OPTIONS fetch, XHR or beacon
// to the app (same origin, or a host sharing the app's registrable domain) is refused and logged.
// Auth traffic is let through — an identity provider refreshes its session with POSTs, and
// blocking it signs the user out and turns every later reading into an error page; its hosts come
// from `probe.guard_allow_hosts`. WebSocket sends are refused too: realtime hubs invoke server
// methods over them.
const READ = /^(GET|HEAD|OPTIONS)$/i;
// On window, not in module scope: every import uses a fresh ?v= URL, i.e. a fresh module
// instance — a module-level list would forget what an earlier instance refused.
const blocked = (window.__paritySweepBlocked ||= []);
function appHost(u) {
  let url; try { url = new URL(u, location.href); } catch (e) { return false; }
  if ((CFG.allowRe || []).some((re) => re.test(url.host))) return false;
  const base = (h) => h.split('.').slice(-2).join('.');
  return url.host === location.host || base(url.host) === base(location.host);
}
export function guard() {
  if (window.__paritySweepGuard) return { installed: true, already: true, blocked: blocked.length };
  window.__paritySweepGuard = true;
  const of = window.fetch;
  window.fetch = function (input, init) {
    const method = (init && init.method) || (input && input.method) || 'GET';
    const url = typeof input === 'string' ? input : input && input.url;
    if (!READ.test(method) && appHost(url)) {
      blocked.push({ kind: 'fetch', method, url: String(url).slice(0, 200), at: Date.now() });
      return Promise.reject(new TypeError('qa-ui-parity-sweep guard: write blocked (read-only sweep)'));
    }
    return of.apply(this, arguments);
  };
  const oOpen = XMLHttpRequest.prototype.open, oSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__ps = { m, u }; return oOpen.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    const r = this.__ps || {};
    if (!READ.test(r.m || 'GET') && appHost(r.u)) {
      blocked.push({ kind: 'xhr', method: r.m, url: String(r.u).slice(0, 200), at: Date.now() });
      try { this.abort(); } catch (e) { /* */ }
      return undefined;
    }
    return oSend.apply(this, arguments);
  };
  if (navigator.sendBeacon) {
    const ob = navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = (u, d) => { if (appHost(u)) { blocked.push({ kind: 'beacon', method: 'POST', url: String(u).slice(0, 200), at: Date.now() }); return false; } return ob(u, d); };
  }
  const ows = WebSocket.prototype.send;
  WebSocket.prototype.send = function (d) {
    if (appHost(this.url)) { blocked.push({ kind: 'websocket', method: 'SEND', url: String(this.url).slice(0, 200), at: Date.now() }); return undefined; }
    return ows.apply(this, arguments);
  };
  // In-page console capture from injection onward (the browser tool's console reader covers page load).
  const logs = (window.__paritySweepConsole ||= []);
  for (const level of ['error', 'warn']) {
    const o = console[level];
    console[level] = function (...a) { logs.push({ level, text: squash(a.map(String).join(' ')).slice(0, 400), at: Date.now() }); return o.apply(this, a); };
  }
  addEventListener('error', (e) => logs.push({ level: 'error', text: squash(String(e.message || e.error)).slice(0, 400), at: Date.now(), uncaught: true }));
  addEventListener('unhandledrejection', (e) => logs.push({ level: 'error', text: squash(String(e.reason && (e.reason.message || e.reason))).slice(0, 400), at: Date.now(), uncaught: true }));
  return { installed: true };
}

export function status() {
  return { visibility: document.visibilityState, hidden: document.hidden, focus: document.hasFocus(),
    url: location.href, title: document.title, w: innerWidth, h: innerHeight, ready: document.readyState,
    guard: !!window.__paritySweepGuard, blocked: blocked.length, loadedAtMs: Math.round(performance.timeOrigin),
    config: CFG.loaded };
}

// Wait, then until no enter/leave transition class remains. The `-enter-active` / `-leave-active`
// convention is shared by several transition libraries; a toolkit with its own names sets
// `probe.settle_selectors`.
export async function settle(ms = 1500) {
  const t0 = performance.now();
  await new Promise((r) => setTimeout(r, ms));
  const moving = CFG.settle_selectors.join(',');
  while (performance.now() - t0 < 12000) {
    const inFlight = moving && document.querySelector(moving);
    if (document.readyState === 'complete' && !inFlight) break;
    await new Promise((r) => setTimeout(r, 250));
  }
  return { settledMs: Math.round(performance.now() - t0), visibility: document.visibilityState };
}

export function pin(selector, width) {
  let st = document.getElementById('__parity_sweep_pin');
  if (!st) { st = document.createElement('style'); st.id = '__parity_sweep_pin'; document.head.appendChild(st); }
  st.textContent = width ? `${selector}{width:${width}px!important;max-width:${width}px!important;min-width:${width}px!important;flex:none!important;box-sizing:border-box!important;}` : '';
  const el = document.querySelector(selector);
  return el ? r1(el.getBoundingClientRect().width) : null;
}

// ---- element records --------------------------------------------------------------------------
function roleOf(el) {
  const explicit = el.getAttribute('role');
  if (explicit) return explicit.split(/\s+/)[0];
  const tag = el.tagName.toLowerCase();
  if (/^h[1-6]$/.test(tag)) return 'heading';
  if (tag === 'dialog') return 'dialog';
  if (CFG.dialogs_extra.length && el.matches(CFG.dialogs_extra.join(','))) return 'dialog';
  return 'button';
}
function nameOf(el) {
  const lb = el.getAttribute('aria-labelledby');
  if (lb) { const t = lb.split(/\s+/).map((id) => document.getElementById(id)?.innerText || '').join(' '); if (squash(t)) return squash(t); }
  const al = el.getAttribute('aria-label'); if (squash(al)) return squash(al);
  if (el.matches('input')) return squash(el.value || el.getAttribute('title'));
  const t = squash(el.innerText || el.textContent); if (t) return t.slice(0, 160);
  const ti = squash(el.getAttribute('title')); if (ti) return ti;
  // Icon-only controls are often named on a wrapper (a kebab menu whose aria-label sits on the
  // wrapping div, not the button) — take the nearest labelled ancestor within two levels.
  for (let p = el.parentElement, n = 0; p && n < 2; p = p.parentElement, n++) {
    const a = squash(p.getAttribute('aria-label')); if (a) return a;
  }
  return '';
}
function visibleOf(el, cs) {
  if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity < 0.05) return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}
function isUtility(x) {
  return UTIL_BASE.test(x) || (CFG.utilRe || []).some((re) => re.test(x));
}
function hints(el) {
  // Class chain for source location: own classes + up to 4 ancestors' classes (component root
  // classes are what `sweep.py locate` greps for; utility classes are dropped).
  const chain = [];
  for (let p = el, n = 0; p && p !== document.body && n < 5; p = p.parentElement, n++) {
    const c = typeof p.className === 'string' ? p.className.trim() : (p.getAttribute('class') || '');
    if (c) chain.push(c.split(/\s+/).filter((x) => !isUtility(x)).slice(0, 4));
  }
  return chain;
}
function iconOf(el) {
  const svg = el.querySelector('svg');
  if (!svg) return undefined;
  const cls = svg.getAttribute('class') || '';
  const names = [];
  for (const re of [/icon-[\w-]+/g, ...(CFG.iconRe || []).map((r) => new RegExp(r.source, 'g' + r.flags.replace('g', '')))]) {
    for (const m of cls.match(re) || []) names.push(m);
  }
  return (names.length ? names : ['svg']).join(' ');
}
function record(el, i, rootRect, extra = {}) {
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  const rec = {
    i, role: roleOf(el), name: nameOf(el), tag: el.tagName.toLowerCase(),
    text: squash(el.innerText || '').slice(0, 200),
    visible: visibleOf(el, cs),
    disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
    level: /^h[1-6]$/i.test(el.tagName) ? +el.tagName[1] : (+el.getAttribute('aria-level') || undefined),
    box: { x: r1(r.left - rootRect.left), y: r1(r.top - rootRect.top), w: r1(r.width), h: r1(r.height) },
    icon: iconOf(el),
    cls: hints(el),
    s: {},
    ...extra,
  };
  for (const p of PROPS) rec.s[p] = normValue(cs.getPropertyValue(p));
  return rec;
}
function collectIn(root, { inDialog = null } = {}) {
  const rootRect = root.getBoundingClientRect();
  const out = [];
  const seen = new Set();
  for (const el of root.querySelectorAll(`${BUTTONS()},${HEADINGS}`)) {
    if (seen.has(el)) continue;
    // a [role=button] wrapper around a real <button> is one control, not two
    if (el.matches('[role=button]') && el.querySelector('button')) continue;
    seen.add(el);
    if (!inDialog && el.closest(DIALOGS())) continue; // dialogs get their own dump
    out.push(record(el, out.length, rootRect, inDialog ? { dialog: inDialog } : {}));
  }
  return out;
}

async function post(path, data) {
  const res = await fetch(`${COLLECTOR}/save?name=${encodeURIComponent(path).replace(/%2F/g, '/')}`,
    { method: 'POST', body: JSON.stringify(data) });
  return res.json();
}

function openDialogs() {
  return [...document.querySelectorAll(DIALOGS())].filter((d) => visibleOf(d, getComputedStyle(d)))
    .filter((d, _, all) => !all.some((o) => o !== d && o.contains(d)));
}

// Base capture: every button and heading under `root` (visible or not — a hidden control is a
// finding), plus any dialog already open. Nothing is clicked.
export async function capture({ run, route, env, root = 'main', pinSel = null, pinWidth = null } = {}) {
  if (!run || !route || !env) throw new Error('capture needs run, route, env');
  await loadConfig();
  // An SPA mounts its content after auth + data, well after `load`: wait for the root itself
  // (up to 15s) before measuring, or a slow page reads as "did not render".
  const t0 = performance.now();
  while (!document.querySelector(root) && performance.now() - t0 < 15000) await new Promise((r) => setTimeout(r, 250));
  guard();   // after mount, before anything is clicked
  const st = await settle(1200);
  let pinned = null;
  if (pinSel && pinWidth) { pinned = pin(pinSel, pinWidth); await new Promise((r) => setTimeout(r, 300)); }
  scrollTo(0, 0);
  const rootEl = document.querySelector(root) || document.body;
  const items = collectIn(rootEl);
  const dialogs = openDialogs().map((d, k) => ({ name: nameOf(d) || `dialog-${k}`, items: collectIn(d, { inDialog: nameOf(d) || `dialog-${k}` }) }));
  const meta = { run, route, env, url: location.href, title: document.title, root, rootFound: !!document.querySelector(root),
    rootWidth: r1(rootEl.getBoundingClientRect().width), pinned, loadedAtMs: Math.round(performance.timeOrigin),
    waitedForRootMs: Math.round(performance.now() - t0), viewport: { w: innerWidth, h: innerHeight, dpr: devicePixelRatio }, visibility: document.visibilityState,
    focus: document.hasFocus(), fonts: document.fonts.status, settle: st, at: new Date().toISOString(),
    counts: { buttons: items.filter((x) => x.role !== 'heading').length, headings: items.filter((x) => x.role === 'heading').length, dialogs: dialogs.length },
    blocked: blocked.slice(), console: (window.__paritySweepConsole || []).slice() };
  const saved = await post(`${route}/${env}/base.json`, { meta, items, dialogs });
  return { ...saved, counts: meta.counts, visibility: meta.visibility, rootFound: meta.rootFound, rootWidth: meta.rootWidth,
    loadedAtMs: meta.loadedAtMs,
    pinned, dialogTriggers: dialogTriggers(rootEl) };
}

// Candidate dialog openers on the page: controls that DECLARE a dialog (aria-haspopup=dialog,
// or aria-controls naming a dialog) and whose name does not read like a write. The sweep opens
// a trigger only when the SAME name is a candidate in both environments.
export function dialogTriggers(rootEl = document.body) {
  return [...rootEl.querySelectorAll(BUTTONS())].filter((e) => visibleOf(e, getComputedStyle(e)) && !e.closest(DIALOGS()))
    .filter((e) => {
      const hp = (e.getAttribute('aria-haspopup') || '').toLowerCase();
      const ctl = e.getAttribute('aria-controls');
      const target = ctl && document.getElementById(ctl);
      return hp === 'dialog' || (target && target.matches(DIALOGS() + ',[role=dialog]'));
    })
    .map((e) => ({ role: roleOf(e), name: nameOf(e) }))
    .filter((t) => t.name && !WRITE_WORDS.test(t.name));
}

// Resolve a trigger to one element: {role?, name} (exact accessible name, or '/regex/i').
export function find(trigger) {
  const want = trigger.name || '';
  const re = want.startsWith('/') ? new RegExp(want.slice(1, want.lastIndexOf('/')), want.slice(want.lastIndexOf('/') + 1)) : null;
  const pool = [...document.querySelectorAll(trigger.role === 'heading' ? HEADINGS : BUTTONS())]
    .filter((e) => visibleOf(e, getComputedStyle(e)) && !e.closest(DIALOGS()));
  const hits = pool.filter((e) => { const n = nameOf(e); return re ? re.test(n) : n === want; });
  return hits[trigger.nth || 0] || null;
}

// Refuse triggers whose accessible name reads like a write. The routes file declares which
// triggers only OPEN a dialog; this is the second lock on that door.
const WRITE_WORDS = /\b(save|create|update|delete|remove|archive|send|invite|submit|start|join|end|complete|confirm|publish|duplicate|approve|pay|upgrade|sign out|log out)\b/i;

export async function modal({ run, route, env, name, trigger, wait = 900 } = {}) {
  await loadConfig();
  if (!window.__paritySweepGuard) guard();
  // A failed attempt is written too: "the baseline's dialog opens, the candidate's does not" is a
  // High finding, and a failure that lives only in a tool result is one the report can never see.
  const failed = async (reason, label) => {
    const saved = await post(`${route}/${env}/modal-${name}.json`, { meta: { run, route, env, name, url: location.href, trigger, label,
      visibility: document.visibilityState, at: new Date().toISOString() }, error: reason });
    return { ok: false, reason, label, ...saved };
  };
  const el = find(trigger);
  if (!el) return failed('trigger-not-found', null);
  const label = nameOf(el);
  if (WRITE_WORDS.test(label) && !trigger.allowWriteWord) return failed('trigger-looks-like-a-write', label);
  const before = new Set(openDialogs());
  const refusedBefore = blocked.length;
  el.scrollIntoView({ block: 'center' });
  el.click();
  await settle(wait);
  const fresh = openDialogs().filter((d) => !before.has(d));
  // If the guard refused a request during the attempt, the app may have needed that request to
  // open the dialog (a POST-shaped read): that is the guard's doing, not a candidate defect.
  if (!fresh.length) { await closeAll(); return failed(blocked.length > refusedBefore ? 'guard-blocked' : 'no-dialog-opened', label); }
  const d = fresh[0];
  const items = collectIn(d, { inDialog: name });
  const box = d.getBoundingClientRect();
  const dcs = getComputedStyle(d);
  const shell = { name, role: roleOf(d), label: nameOf(d), box: { w: r1(box.width), h: r1(box.height) }, s: Object.fromEntries(PROPS.map((p) => [p, normValue(dcs.getPropertyValue(p))])) };
  const saved = await post(`${route}/${env}/modal-${name}.json`, { meta: { run, route, env, name, url: location.href, trigger, label,
    visibility: document.visibilityState, at: new Date().toISOString(), blocked: blocked.slice(), console: (window.__paritySweepConsole || []).slice() }, shell, items });
  const stillOpen = await closeAll();
  return { ok: true, ...saved, items: items.length, stillOpen };
}

export async function closeAll() {
  const key = (t) => ['keydown', 'keyup'].forEach((type) => t.dispatchEvent(new KeyboardEvent(type, { key: 'Escape', code: 'Escape', bubbles: true, cancelable: true })));
  for (let k = 0; k < 3 && openDialogs().length; k++) {
    key(document.activeElement || document.body); key(document.body);
    await new Promise((r) => setTimeout(r, 300));
    const x = [...document.querySelectorAll(CLOSERS())].find((b) => visibleOf(b, getComputedStyle(b)));
    if (openDialogs().length && x) { x.click(); await new Promise((r) => setTimeout(r, 300)); }
  }
  await settle(300);
  return openDialogs().length;
}

window.__paritySweep = { guard, status, settle, pin, capture, modal, find, closeAll, dialogTriggers, loadConfig };
