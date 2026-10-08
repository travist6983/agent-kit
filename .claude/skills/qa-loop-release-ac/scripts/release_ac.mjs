#!/usr/bin/env node
/**
 * qa-loop-release-ac — the deterministic half of the release acceptance-criteria loop.
 *
 * The recipe sequences this script -> qa-run-case (browser scenarios) / qa-run-chatbot (chat).
 * This script owns only the glue those skills don't have: which work a release tag names,
 * whether the environment actually runs it, the Gherkin inside each ticket, the case files the
 * runners read, and the final per-scenario matrix. It never writes to the issue tracker or the
 * VCS host — every outward write belongs to the atomic skill that already guards it.
 *
 *   prs       merged PRs (vcs_host adapter) carrying the release label or "[X.Y.Z]" -> ticket keys
 *   tickets   the PR keys + the tracker's release search (issue_tracker adapter) -> tickets.json
 *   deployed  is each PR's merge commit on the env? each deploy track is checked against the
 *             commit THAT track's own deploy source names (deploy.tracks / deploy / ci_cd adapters)
 *   scope     reconcile PR keys against the tracker's release field -> in-scope ticket list
 *   extract   every Gherkin scenario in each in-scope ticket (+ comments), classified
 *   cases     one qa-run-case case.json per browser-testable scenario (<out-dir>/<KEY>-S<n>/)
 *   chatset   qa-run-chatbot SET_FILE skeleton for the chat-class scenarios
 *   matrix    join scenarios -> qa-run-case / chat results -> ac-matrix.json + .md
 *
 * Every external system is reached through `lib/adapters/cli.py`; every literal (repo path,
 * environment URL, default branch, ticket-key pattern, deploy tracks) comes from
 * `.claude/loops/stack.config.json`. A missing adapter is a structured stop, never a guess.
 *
 * Exit codes: 0 ok · 1 error · 2 config/input missing · 3 stop condition (see output `stops`)
 */
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SKILL_DIR = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const REPO_ROOT = process.env.LOOPS_REPO_ROOT
  ? path.resolve(process.env.LOOPS_REPO_ROOT)
  : path.resolve(SKILL_DIR, '..', '..', '..');
const ADAPTER_CLI = path.join(REPO_ROOT, 'lib', 'adapters', 'cli.py');
const FLAG_RE = /\b(enable|feature|ff)[-_][a-z0-9][a-z0-9-_]{2,}\b/gi;

// ---------------------------------------------------------------- plumbing

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith('--')) { out._.push(a); continue; }
    const k = a.slice(2);
    const next = argv[i + 1];
    if (next === undefined || next.startsWith('--')) out[k] = true;
    else { out[k] = next; i++; }
  }
  return out;
}
function fail(msg, code = 1) { process.stderr.write(`${msg}\n`); process.exit(code); }
function need(args, k, usage) { if (!args[k] || args[k] === true) fail(`missing --${k}\nusage: ${usage}`, 2); return args[k]; }
function readJson(f) {
  try { return JSON.parse(fs.readFileSync(f, 'utf8')); }
  catch (e) { fail(`cannot read ${f}: ${e.message}`, 2); }
}
function writeJson(f, data) {
  fs.mkdirSync(path.dirname(path.resolve(f)), { recursive: true });
  fs.writeFileSync(f, JSON.stringify(data, null, 2) + '\n');
}
function emit(obj) { process.stdout.write(JSON.stringify(obj, null, 2) + '\n'); }
function run(cmd, argv, opts = {}) {
  const r = spawnSync(cmd, argv, { encoding: 'utf8', maxBuffer: 256 * 1024 * 1024, ...opts });
  return { ok: r.status === 0, out: r.stdout || '', err: r.stderr || '', status: r.status };
}

// ---------------------------------------------------------------- stack.config

/** Same resolution as lib/adapters/config.py: $LOOPS_STACK_CONFIG, else <root>/.claude/loops/stack.config.json. */
let STACK = null;
function stripComments(node) {
  if (Array.isArray(node)) return node.map(stripComments);
  if (node && typeof node === 'object') {
    return Object.fromEntries(Object.entries(node).filter(([k]) => !k.startsWith('_comment')).map(([k, v]) => [k, stripComments(v)]));
  }
  return node;
}
function stack() {
  if (STACK) return STACK;
  const f = process.env.LOOPS_STACK_CONFIG || path.join(REPO_ROOT, '.claude', 'loops', 'stack.config.json');
  if (!fs.existsSync(f)) fail(`stack.config.json not found at ${f} (set LOOPS_STACK_CONFIG or create it from .claude/loops/stack.config.example.json)`, 2);
  STACK = stripComments(readJson(f));
  return STACK;
}
/** Dotted lookup with the LOOPS_CFG__A__B env override (JSON when it parses, else a string). */
function cfg(dotted, dflt = null) {
  const env = process.env[`LOOPS_CFG__${dotted.toUpperCase().replace(/\./g, '__').replace(/-/g, '_')}`];
  if (env !== undefined) { try { return JSON.parse(env); } catch { return env; } }
  let cur = stack();
  for (const part of dotted.split('.')) {
    if (cur && typeof cur === 'object' && part in cur) cur = cur[part];
    else return dflt;
  }
  return cur === undefined ? dflt : cur;
}
function resolvePath(p) { return path.isAbsolute(p) ? p : path.resolve(REPO_ROOT, p); }
function defaultBranch(args) { return args.base || cfg('project.default_branch', 'main'); }
function evidenceRoot() { return resolvePath(cfg('evidence.root', 'runs')); }

/** The target app's checkout: --repo, else app.repo_dir from stack.config. Never a guessed sibling. */
function appRepo(args, { optional = false } = {}) {
  const tried = [args.repo, cfg('app.repo_dir')].filter(Boolean).map(resolvePath);
  for (const p of tried) if (fs.existsSync(path.join(p, '.git'))) return p;
  if (optional) return null;
  fail(`target app repo not found. Pass --repo <path> or set app.repo_dir in stack.config.json. Tried:\n  ${tried.join('\n  ') || '(nothing configured)'}`, 2);
}

/**
 * Ticket keys are found with project.ticket_key_pattern. The pattern is anchored for intake
 * ("^[A-Z][A-Z0-9]+-[0-9]+$"); here it is unanchored and word-bounded so keys can be pulled out
 * of titles, branch names and descriptions.
 */
let KEY_RE = null;
function keyRe() {
  if (KEY_RE) return KEY_RE;
  const pat = String(cfg('project.ticket_key_pattern', '^[A-Z][A-Z0-9]+-[0-9]+$')).replace(/^\^/, '').replace(/\$$/, '');
  KEY_RE = new RegExp(`(?<![A-Za-z0-9-])(${pat})(?![A-Za-z0-9])`, 'g');
  return KEY_RE;
}
function keysIn(text) {
  const s = new Set();
  for (const m of String(text || '').matchAll(keyRe())) s.add(m[1].toUpperCase());
  return [...s];
}
/** "4.2.0", "Sparrow 4.2.0", "v4.2.0" -> "4.2.0" */
function versionOf(release) {
  const m = String(release).match(/(\d+\.\d+\.\d+)/);
  if (!m) fail(`--release must contain a version like 4.2.0 (got "${release}")`, 2);
  return m[1];
}

// ---------------------------------------------------------------- adapters

/**
 * One call into lib/adapters/cli.py. `unavailable: true` means the role is `none` in this stack
 * (CLI exit 3) — the caller turns that into a structured stop, never into a default.
 */
function adapter(role, op, kwargs = {}) {
  if (!fs.existsSync(ADAPTER_CLI)) return { ok: false, status: 2, error: `adapter CLI missing at ${ADAPTER_CLI}` };
  const r = run('python3', [ADAPTER_CLI, role, op, '--json', JSON.stringify(kwargs)]);
  let parsed = null;
  try { parsed = JSON.parse(r.out); } catch { /* handled below */ }
  if (r.status === 0 && parsed?.ok) return { ok: true, status: 0, result: parsed.result };
  const error = parsed?.error || (r.err || r.out).trim().slice(0, 400) || `exit ${r.status}`;
  return { ok: false, status: r.status, unavailable: r.status === 3, error, reason: parsed?.reason || null };
}

/** The environment a run targets: a name from app.environments, or a URL that matches one. */
function resolveEnv(arg) {
  const envs = cfg('app.environments', {}) || {};
  const name = arg && arg !== true ? String(arg) : 'staging';
  let entry = null; let envName = null;
  if (/^https?:\/\//i.test(name)) {
    const want = name.replace(/\/+$/, '');
    for (const [n, e] of Object.entries(envs)) if (e?.url && e.url.replace(/\/+$/, '') === want) { entry = e; envName = n; }
    if (!entry) { entry = { url: want }; envName = want; }
  } else {
    entry = envs[name]; envName = name;
    if (!entry) fail(`no environment named "${name}" in app.environments (have: ${Object.keys(envs).join(', ') || 'none'}) — pass --env <name>`, 2);
  }
  // Production is refused outright, before anything else is read. The name is the signal; an
  // explicit is_production flag on the environment entry is honoured when the adopter sets one.
  if (entry.is_production === true || /^prod(uction)?$/i.test(envName)) {
    fail(`refusing ${envName}: this loop runs against test environments, never production`, 2);
  }
  if (!entry.url) fail(`app.environments.${envName}.url is null — set it, or pass --env <name> for an environment that exists`, 2);
  return { name: envName, url: String(entry.url).replace(/\/+$/, ''), entry };
}

// ---------------------------------------------------------------- prs

/** A PR's merge timestamp, from whatever the provider recorded; created_at as the last resort. */
function prClosedAt(p) {
  const raw = p.raw || {};
  return raw.merged_at || raw.closed_at || raw.closedDate || p.created_at || null;
}

/**
 * The merge commit of a PR. A provider that knows it puts it in raw.merge_commit (or
 * raw.merge_commit_sha); otherwise it is resolved from git, by the branch name or the PR number
 * in a merge-commit message on the base branch. null = unknown, which `deployed` reports.
 */
function mergeCommitOf(p, repo, base, since) {
  const raw = p.raw || {};
  const direct = raw.merge_commit || raw.merge_commit_sha || raw.merged_sha || null;
  if (direct) return String(direct);
  if (!repo) return null;
  const probes = [p.branch, p.id && `#${p.id}`, p.id && `PR ${p.id}`].filter(Boolean);
  for (const probe of probes) {
    const r = run('git', ['-C', repo, 'log', `origin/${base}`, `--since=${since}`, '--format=%H', '-F', `--grep=${probe}`, '-1']);
    const sha = r.out.trim();
    if (r.ok && /^[0-9a-f]{7,40}$/.test(sha)) return sha;
  }
  return null;
}

/**
 * A release label is applied by hand, so one forgotten label is one ticket nobody tests. Teams
 * also prefix titles with "[X.Y.Z]" — a PR carrying either signal is in; a PR carrying only one
 * is reported so the label can be fixed at the source.
 */
function cmdPrs(args) {
  const usage = 'prs --release 4.2.0 [--since-days 90] [--base <project.default_branch>] [--repo <app repo>] [--out prs.json]';
  const version = versionOf(need(args, 'release', usage));
  const sinceDays = Number(args['since-days'] || 90);
  const base = defaultBranch(args);
  const cutoff = Date.now() - sinceDays * 864e5;
  const a = adapter('vcs_host', 'list_prs', { filter: { state: 'merged', base, since_days: sinceDays } });
  if (!a.ok) {
    const stop = { code: a.unavailable ? 'blocked-config' : 'blocked-vcs', reason: a.unavailable ? 'adapter-unavailable:vcs_host' : a.error,
      detail: a.unavailable ? 'the vcs_host adapter is `none` — configure a provider that can list merged PRs' : `vcs_host list_prs failed: ${a.error}` };
    emit({ release_version: version, prs: [], keys: [], stops: [stop] });
    process.exit(3);
  }
  const repo = appRepo(args, { optional: true });
  if (repo) run('git', ['-C', repo, 'fetch', '-q', 'origin']);
  const since = `${sinceDays} days ago`;
  const titleTag = new RegExp(`\\[\\s*v?${version.replace(/\./g, '\\.')}\\s*\\]`);
  const prs = [];
  for (const p of a.result || []) {
    const closed = prClosedAt(p);
    if (closed && Date.parse(closed) < cutoff) continue;
    const labels = (p.labels || []).map((l) => (typeof l === 'string' ? l : l?.name || '')).filter(Boolean);
    const labelled = labels.some((l) => l.trim().replace(/^v/, '') === version);
    const titled = titleTag.test(p.title || '');
    if (!labelled && !titled) continue;
    const branch = String(p.branch || '').replace(/^refs\/heads\//, '');
    const body = String(p.body || '');
    prs.push({
      id: p.id,
      title: p.title,
      closed,
      source_branch: branch,
      merge_commit: mergeCommitOf({ ...p, branch }, repo, base, since),
      labels,
      signal: labelled && titled ? 'label+title' : labelled ? 'label-only' : 'title-only',
      // Title + branch name the work the PR delivers. A key only in the description is often
      // "relates to" / "follow-up to" — kept, but never enough on its own to scope a ticket.
      keys: [...new Set([...keysIn(p.title), ...keysIn(branch)])],
      mentioned_keys: keysIn(body).filter((k) => !keysIn(p.title).includes(k) && !keysIn(branch).includes(k)),
      // The PR description is where scope cuts ("AC4 needs a product decision"), AC moved to a
      // commit message, and feature flags get written down. The agent reads it; the script keeps it.
      description: body.slice(0, 4000),
      flags: [...new Set(body.match(FLAG_RE) || [])],
      url: p.url || null,
    });
  }
  const out = {
    release_version: version,
    target_branch: base,
    since_days: sinceDays,
    generated: new Date().toISOString(),
    prs,
    keys: [...new Set(prs.flatMap((p) => p.keys))].sort(),
    prs_without_key: prs.filter((p) => !p.keys.length).map((p) => ({ id: p.id, title: p.title })),
    prs_without_merge_commit: prs.filter((p) => !p.merge_commit).map((p) => ({ id: p.id, title: p.title })),
    // The label is the scoping signal; a PR found only by its "[X.Y.Z]" title is missing it.
    missing_label: prs.filter((p) => p.signal === 'title-only').map((p) => ({ id: p.id, title: p.title })),
  };
  if (args.out) writeJson(args.out, out);
  emit({ ...out, prs: `${prs.length} PRs${args.out ? ` -> ${args.out}` : ''}` });
}

// ---------------------------------------------------------------- tickets

/**
 * Fetch every ticket the release could contain: each key the PRs name (fetch_ticket) plus the
 * tracker's own release search (search). The union is written in the adapter's normalized
 * Ticket shape — the recipe never re-types ticket text, because a summary loses the Gherkin.
 */
function cmdTickets(args) {
  const usage = 'tickets --prs prs.json --release 4.2.0 [--release-query "<tracker query>"] [--out tickets.json]';
  const prs = readJson(need(args, 'prs', usage));
  const version = versionOf(need(args, 'release', usage));
  const query = args['release-query'] && args['release-query'] !== true ? String(args['release-query']) : version;
  const byKey = new Map(); const missing = []; const stops = [];
  const probe = adapter('issue_tracker', 'search', { query, limit: 200 });
  if (probe.unavailable) {
    stops.push({ code: 'blocked-tracker', reason: 'adapter-unavailable:issue_tracker', detail: 'the issue_tracker adapter is `none` — configure a provider before scoping a release' });
    emit({ release_version: version, tickets: [], missing: [], stops });
    process.exit(3);
  }
  if (!probe.ok) stops.push({ code: 'blocked-tracker', reason: probe.error, detail: `issue_tracker search "${query}" failed — release membership cannot be read` });
  for (const t of probe.ok ? probe.result || [] : []) if (t?.key) byKey.set(t.key, t);
  for (const key of prs.keys || []) {
    if (byKey.has(key)) continue;
    const a = adapter('issue_tracker', 'fetch_ticket', { key });
    if (a.ok && a.result?.key) byKey.set(a.result.key, a.result);
    else missing.push({ key, error: a.error });
  }
  if (missing.length) stops.push({ code: 'blocked-tracker', detail: `PR keys the tracker could not return: ${missing.map((m) => m.key).join(', ')}` });
  const tickets = [...byKey.values()].sort((x, y) => String(x.key).localeCompare(String(y.key)));
  const out = { release_version: version, query, generated: new Date().toISOString(), tickets, missing, stops };
  if (args.out) writeJson(args.out, out);
  emit({ ...out, tickets: `${tickets.length} tickets${args.out ? ` -> ${args.out}` : ''}` });
  if (stops.length) process.exitCode = 3;
}

// ---------------------------------------------------------------- deployed

/** Minimal glob -> RegExp: `**` any depth, `*` within a segment, `?` one char. Anchored. */
function globToRe(glob) {
  let re = '';
  const g = String(glob);
  for (let i = 0; i < g.length; i++) {
    const c = g[i];
    if (c === '*') {
      if (g[i + 1] === '*') { re += g[i + 2] === '/' ? '(?:.*/)?' : '.*'; i += g[i + 2] === '/' ? 2 : 1; }
      else re += '[^/]*';
    } else if (c === '?') re += '[^/]';
    else re += c.replace(/[.+^${}()|[\]\\]/g, '\\$&');
  }
  return new RegExp(`^${re}$`);
}

/**
 * The deploy tracks: independently deployed parts of the app, from deploy.tracks in stack.config
 * ([{name, path_globs, version_endpoint, deployed_version_command}]). Ordered, first match wins,
 * so a catch-all track ("**") goes last. With no tracks configured the whole app is one track
 * whose deployed commit can only come from the deploy / ci_cd adapters.
 */
function deployTracks() {
  const raw = cfg('deploy.tracks', []) || [];
  const tracks = raw.filter((t) => t && t.name).map((t) => ({
    name: String(t.name),
    globs: (t.path_globs || []).map(globToRe),
    version_endpoint: t.version_endpoint || null,
    deployed_version_command: t.deployed_version_command || null,
  }));
  return tracks.length ? tracks : [{ name: 'app', globs: [globToRe('**')], version_endpoint: null, deployed_version_command: null, implicit: true }];
}

/** Which tracks a commit touched (first parent diff). [] = only inert paths. null = not in the repo. */
function tracksOf(repo, sha, tracks) {
  const r = run('git', ['-C', repo, 'diff', '--name-only', `${sha}^1`, sha]);
  if (!r.ok) return null;
  const touched = new Set();
  for (const f of r.out.split('\n').map((x) => x.trim()).filter(Boolean)) {
    const t = tracks.find((tr) => tr.globs.some((g) => g.test(f)));
    if (t) touched.add(t.name);          // a path matching no track deploys nowhere (docs, e2e, …)
  }
  return tracks.map((t) => t.name).filter((n) => touched.has(n));
}

/** `merge-base --is-ancestor` exits 1 for "no"; any other failure is "can't tell", never "no". */
function ancestry(repo, sha, target) {
  const s = run('git', ['-C', repo, 'merge-base', '--is-ancestor', sha, target]).status;
  return s === 0 ? 'deployed' : s === 1 ? 'not-deployed' : 'unverified';
}

/**
 * One commit's state on the env. Each track it touched is checked against the commit THAT
 * track's deploy source names. One track's build proves nothing about another — they deploy
 * separately and can diverge. Deployed only when every touched track is; one track not deployed
 * makes the whole not-deployed. A commit touching only inert paths is judged by the first track.
 */
function deployStateOf(repo, sha, sources, tracks) {
  if (!sha) return { track: null, tracks: [], state: 'unverified', why: 'no merge commit recorded' };
  const touched = tracksOf(repo, sha, tracks);
  if (touched === null) return { track: null, tracks: [], state: 'unverified', why: `${sha.slice(0, 9)} is not in ${repo} — fetch, then re-run` };
  const judged = touched.length ? touched : [tracks[0].name];
  const checks = {}; const why = [];
  for (const name of judged) {
    const src = sources[name];
    checks[name] = src?.commit ? ancestry(repo, sha, src.commit) : 'unverified';
    if (!src?.commit) why.push(`${name}: ${src?.why || 'no deployed commit known'}`);
    else if (checks[name] === 'unverified') why.push(`${name}: git could not compare ${sha.slice(0, 9)} with ${src.commit.slice(0, 9)}`);
  }
  const s = Object.values(checks);
  const state = s.includes('not-deployed') ? 'not-deployed' : s.includes('unverified') ? 'unverified' : 'deployed';
  return { track: judged.join('+'), tracks: judged, state, checks, ...(why.length ? { why: why.join('; ') } : {}) };
}

/** Pull a commit-looking value out of a version endpoint / command response. */
function commitFromPayload(text) {
  const t = String(text || '').trim();
  if (/^[0-9a-f]{7,40}$/i.test(t)) return { commit: t, build: null, timestamp: null };
  let j = null;
  try { j = JSON.parse(t); } catch { return null; }
  if (!j || typeof j !== 'object') return null;
  const pick = (keys) => { for (const k of keys) if (j[k] !== undefined && j[k] !== null && j[k] !== '') return String(j[k]); return null; };
  const commit = pick(['commit', 'sha', 'git_sha', 'gitSha', 'git_commit', 'revision', 'version']);
  return commit ? { commit, build: pick(['build', 'build_number', 'buildNumber', 'build_id']), timestamp: pick(['timestamp', 'built_at', 'deployed_at', 'date']) } : null;
}

/**
 * The commit a track runs on the env, from the first source that exists:
 *   1. the track's version_endpoint (GET <env url><endpoint>, JSON or a bare sha)
 *   2. the track's deployed_version_command (template vars {env} {url} {track})
 *   3. the deploy adapter's deployed_version(env)
 *   4. the ci_cd adapter's deploy_status(env).version
 * A source that exists but fails is a stop (`stop: true`), never an answer — reading it as
 * "nothing deployed" or skipping the check would both put a guess in the matrix. No source at
 * all (adapters `none`, nothing configured) is the same stop with reason adapter-unavailable.
 */
function deployedCommitFor(track, env, memo) {
  if (track.version_endpoint) {
    const url = `${env.url}${track.version_endpoint.startsWith('/') ? '' : '/'}${track.version_endpoint}`;
    const r = run('curl', ['-s', '-m', '15', url]);
    const v = r.ok ? commitFromPayload(r.out) : null;
    return v ? { ...v, source: `version_endpoint ${url}` }
      : { commit: null, stop: true, why: `${url} did not return a commit${r.ok ? '' : ` (curl exit ${r.status})`}` };
  }
  if (track.deployed_version_command) {
    const cmd = String(track.deployed_version_command).replace(/\{env\}/g, env.name).replace(/\{url\}/g, env.url).replace(/\{track\}/g, track.name);
    const r = run('bash', ['-lc', cmd], { env: { ...process.env, ENV_NAME: env.name, ENV_URL: env.url, TRACK: track.name } });
    const v = r.ok ? commitFromPayload(r.out) : null;
    return v ? { ...v, source: `deployed_version_command` }
      : { commit: null, stop: true, why: `deployed_version_command for ${track.name} ${r.ok ? 'returned no commit' : `failed (exit ${r.status}): ${(r.err || r.out).trim().slice(0, 300)}`}` };
  }
  // Env-wide sources are asked once and shared by every track without its own source.
  if (!memo.envWide) {
    const d = adapter('deploy', 'deployed_version', { env: env.name });
    if (d.ok && d.result) memo.envWide = { ...(commitFromPayload(typeof d.result === 'string' ? d.result : JSON.stringify(d.result)) || { commit: String(d.result) }), source: 'deploy.deployed_version' };
    else if (d.ok) memo.envWide = { commit: null, stop: true, why: `deploy.deployed_version(${env.name}) returned nothing` };
    else if (!d.unavailable) memo.envWide = { commit: null, stop: true, why: `deploy adapter failed: ${d.error}` };
    else {
      const c = adapter('ci_cd', 'deploy_status', { env: env.name });
      if (c.ok && c.result?.version) memo.envWide = { commit: String(c.result.version), build: c.result.raw?.build || null, timestamp: c.result.deployed_at || null, url: c.result.url || null, source: 'ci_cd.deploy_status' };
      else if (c.ok) memo.envWide = { commit: null, stop: true, why: `ci_cd.deploy_status(${env.name}) names no version` };
      else if (!c.unavailable) memo.envWide = { commit: null, stop: true, why: `ci_cd adapter failed: ${c.error}` };
      else memo.envWide = { commit: null, stop: true, reason: 'adapter-unavailable:deploy', why: `no deploy source for track "${track.name}" on ${env.name}: deploy.tracks[].version_endpoint / deployed_version_command unset and the deploy + ci_cd adapters are none` };
    }
  }
  return memo.envWide;
}

/**
 * Testing a ticket whose code is not on the environment produces a FAIL that is really
 * "not deployed" — noise that teaches people to ignore the matrix. Each PR is classed by the
 * deploy tracks its merge commit touched and each track is checked against its own deploy
 * source. A PR is deployed when its merge commit is an ancestor of every relevant deployed
 * commit. A track with no signal is `unverified` with a reason, never assumed deployed.
 */
function cmdDeployed(args) {
  const usage = 'deployed --prs prs.json [--env <app.environments name, default staging>] [--repo <app repo>] [--out deployed.json]';
  const prs = readJson(need(args, 'prs', usage));
  const env = resolveEnv(args.env);
  const repo = appRepo(args);
  const tracks = deployTracks();
  const out = { env: env.name, env_url: env.url, generated: new Date().toISOString(), tracks: {}, prs: [], stops: [] };
  run('git', ['-C', repo, 'fetch', '-q', 'origin'], {});
  const known = (sha) => run('git', ['-C', repo, 'cat-file', '-t', sha]).out.trim() === 'commit';
  const sources = {};
  const memo = {};
  // Which tracks does this release actually touch? Only those sources are read — a release that
  // never touches a track never depends on that track's deploy record.
  const touchedByRelease = new Set();
  const tracked = prs.prs.map((p) => {
    const t = p.merge_commit ? tracksOf(repo, p.merge_commit, tracks) : null;
    for (const n of (t && t.length ? t : t ? [tracks[0].name] : [])) touchedByRelease.add(n);
    return { p, tracks: t };
  });
  for (const track of tracks) {
    if (!touchedByRelease.has(track.name)) { sources[track.name] = { commit: null, why: `no PR in this release touches ${track.name} — not read` }; continue; }
    const d = deployedCommitFor(track, env, memo);
    if (d.commit && known(d.commit)) {
      sources[track.name] = { commit: d.commit };
      out.tracks[track.name] = { commit: d.commit, build: d.build || null, timestamp: d.timestamp || null, source: d.source, ...(d.url ? { url: d.url } : {}) };
    } else {
      sources[track.name] = { commit: null, why: d.commit ? `deployed commit ${d.commit} is not in ${repo} — fetch, or the env runs an unpushed build` : d.why };
      out.stops.push({ code: 'deploy-unknown', ...(d.reason ? { reason: d.reason } : {}), detail: `${track.name}: ${sources[track.name].why}` });
    }
  }
  for (const { p } of tracked) {
    out.prs.push({ id: p.id, keys: p.keys, merge_commit: p.merge_commit, ...deployStateOf(repo, p.merge_commit, sources, tracks) });
  }
  out.summary = {
    deployed: out.prs.filter((p) => p.state === 'deployed').length,
    not_deployed: out.prs.filter((p) => p.state === 'not-deployed').map((p) => ({ id: p.id, keys: p.keys })),
    unverified: out.prs.filter((p) => p.state === 'unverified').length,
  };
  if (args.out) writeJson(args.out, out);
  emit(out);
  if (out.stops.length) process.exitCode = 3;
}

// ---------------------------------------------------------------- ticket normalisation

/**
 * Reads tickets.json (from `tickets`), or any array / {tickets|issues: [...]} of adapter-shaped
 * Tickets: {key, type, title, body, status, labels[], components[], comments[{author, at, body}],
 * raw}. Release membership is read from labels and from the common release-ish keys a provider
 * leaves in `raw` (releases, fix_versions, milestone, versions…), each a string or {name}.
 */
function releaseNamesOf(t) {
  const names = [...(t.labels || [])];
  const raw = t.raw || {};
  for (const k of ['releases', 'release', 'fix_versions', 'fix_version', 'fixVersions', 'milestone', 'milestones', 'versions', 'version']) {
    const v = raw[k] ?? (raw.fields || {})[k];
    for (const x of Array.isArray(v) ? v : v ? [v] : []) names.push(typeof x === 'string' ? x : x?.name || x?.title || '');
  }
  return [...new Set(names.map((n) => String(n || '').trim()).filter(Boolean))];
}
function loadTickets(file) {
  const raw = readJson(file);
  const nodes = Array.isArray(raw) ? raw
    : Array.isArray(raw.tickets) ? raw.tickets
      : Array.isArray(raw.issues) ? raw.issues : null;
  if (!nodes) fail(`${file}: no tickets found (expected tickets.json from \`tickets\`, {tickets:[...]}, or an array of adapter Tickets)`, 2);
  const text = (v) => (typeof v === 'string' ? v : v ? JSON.stringify(v) : '');
  return nodes.filter((n) => n && n.key).map((n) => ({
    key: n.key,
    summary: n.title || n.summary || '',
    type: n.type || null,
    status: n.status || null,
    releases: releaseNamesOf(n),
    components: (n.components || []).map((c) => (typeof c === 'string' ? c : c?.name || '')).filter(Boolean),
    description: text(n.body ?? n.description),
    reporter: n.reporter || null,
    comments: (n.comments || []).map((c) => ({
      created: c.at || c.created || null,
      author: typeof c.author === 'string' ? c.author : c.author?.displayName || c.author?.name || null,
      body: text(c.body),
    })),
  }));
}

/** Does a release name denote this version? integrations.release_ac.release_name_pattern when set ("{name} {version}"), else contains-the-version. */
function releaseMatcher(version) {
  const pat = cfg('integrations.release_ac.release_name_pattern');
  if (!pat) return (name) => name.includes(version);
  const re = new RegExp(`^${String(pat).split(/(\{[a-z_]+\})/i).map((seg) => (seg === '{version}' ? version.replace(/\./g, '\\.') : /^\{[a-z_]+\}$/i.test(seg) ? '.+?' : seg.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))).join('')}$`, 'i');
  return (name) => re.test(name) || name.includes(version);
}

// ---------------------------------------------------------------- scope

function cmdScope(args) {
  const usage = 'scope --prs prs.json --tickets tickets.json [--deployed deployed.json] [--release-name "<tracker release name>"] [--repo <app repo>] [--base <branch>] [--out scope.json]';
  const prs = readJson(need(args, 'prs', usage));
  const tickets = loadTickets(need(args, 'tickets', usage));
  const deployed = args.deployed ? readJson(args.deployed) : null;
  const version = prs.release_version;
  const byKey = new Map(tickets.map((i) => [i.key, i]));
  const matches = releaseMatcher(version);

  const relNames = [...new Set(tickets.flatMap((i) => i.releases).filter(matches))];
  const releaseName = args['release-name'] && args['release-name'] !== true ? String(args['release-name']) : (relNames.length === 1 ? relNames[0] : null);
  const stops = [];
  if (!releaseName) stops.push({ code: 'blocked-tracker', detail: relNames.length ? `several release names contain ${version}: ${relNames.join(', ')} — pass --release-name` : `no ticket in the tickets file carries a release name containing ${version}` });

  const onRelease = new Set(tickets.filter((i) => releaseName && i.releases.includes(releaseName)).map((i) => i.key));
  const prKeys = new Set(prs.keys);
  const prsByKey = new Map();
  for (const p of prs.prs) for (const k of p.keys) prsByKey.set(k, [...(prsByKey.get(k) || []), p.id]);
  const deployState = new Map();
  for (const d of deployed?.prs || []) for (const k of d.keys) {
    // a key shipped by several PRs is only deployed when all of them are
    const prev = deployState.get(k);
    deployState.set(k, prev === 'not-deployed' || d.state === 'not-deployed' ? 'not-deployed'
      : prev === 'unverified' || d.state === 'unverified' ? 'unverified' : 'deployed');
  }

  // A release-field ticket with no labelled PR may still have shipped under a commit that names it.
  const repo = appRepo(args, { optional: true });
  const base = prs.target_branch || defaultBranch(args);
  const since = `${Number(prs.since_days || 90)} days ago`;
  const commitNaming = (key) => (repo
    ? run('git', ['-C', repo, 'log', `origin/${base}`, `--since=${since}`, '--format=%H', '-i', `--grep=${key}\\b`, '-1']).out.trim() : '') || null;
  // Same per-track rule as `deployed`: an unlabelled commit is checked against the deploy source
  // of each track it touched. An older deployed.json with no `tracks` leaves it unverified.
  const tracks = deployTracks();
  const sources = {};
  for (const t of tracks) sources[t.name] = { commit: deployed?.tracks?.[t.name]?.commit || null, why: 'deployed.json names no deploy for this track' };
  const commitDeployState = (sha) => (!repo ? 'unverified' : deployStateOf(repo, sha, sources, tracks).state);
  // A multi-story feature often ships under ONE key while its PR description names the siblings
  // ("also delivers PROJ-124/125") — the release-field ticket shipped even though no title names it.
  const mentionedBy = new Map();
  for (const p of prs.prs) for (const k of p.mentioned_keys || []) mentionedBy.set(k, [...(mentionedBy.get(k) || []), p.id]);

  const out_tickets = [];
  for (const key of [...new Set([...prKeys, ...onRelease])].sort()) {
    const i = byKey.get(key);
    const set = prKeys.has(key) && onRelease.has(key) ? 'both' : prKeys.has(key) ? 'pr-only' : 'release-only';
    let inScope = true;
    let why = null;
    let deploy = null;
    if (!i) { inScope = false; why = 'not in the tickets file — re-run `tickets` so the tracker returns it'; }
    else if (set === 'release-only') {
      const viaPr = mentionedBy.get(key);
      const sha = viaPr ? null : commitNaming(key);
      if (viaPr) {
        why = `delivered inside PR ${viaPr.join(', ')} (named in its description, not its title) — confirm, then key the PR`;
        const states = (deployed?.prs || []).filter((d) => viaPr.includes(d.id)).map((d) => d.state);
        deploy = states.includes('not-deployed') ? 'not-deployed' : states.length ? states[0] : (deployed ? 'unverified' : 'unchecked');
      } else if (sha) {
        why = `shipped under an unlabelled commit ${sha.slice(0, 9)} — label its PR`;
        deploy = deployed ? commitDeployState(sha) : 'unchecked';
      } else { inScope = false; why = `no merged PR or commit names it (status: ${i.status}) — not shipped yet`; }
    } else if (set === 'pr-only') why = `merged under the ${version} label but not on release ${releaseName || '?'} — fix the ticket's release field`;
    if (!deploy) deploy = !inScope ? 'n/a' : deployState.get(key) || (deployed ? 'unverified' : 'unchecked');
    if (inScope && deploy === 'not-deployed') {
      const builds = Object.entries(deployed.tracks || {}).map(([n, t]) => `${n} ${t.build || (t.commit || '').slice(0, 9) || '?'}`).join(', ');
      why = [why, `code not on ${deployed.env} yet (${builds || 'unknown build'})`].filter(Boolean).join('; ');
    }
    out_tickets.push({
      key, set, in_scope: inScope, deploy, why,
      prs: prsByKey.get(key) || [],
      summary: i?.summary || null, type: i?.type || null, status: i?.status || null,
      components: i?.components || [], reporter: i?.reporter || null,
    });
  }
  const missingFromTracker = out_tickets.filter((t) => !t.summary).map((t) => t.key);
  if (missingFromTracker.length) stops.push({ code: 'blocked-tracker', detail: `PR keys missing from the tickets file: ${missingFromTracker.join(', ')} — re-run \`tickets\`` });

  const out = {
    release_version: version,
    release_name: releaseName,
    generated: new Date().toISOString(),
    env: deployed?.env || null,
    deployed_tracks: deployed?.tracks || null,
    counts: {
      both: out_tickets.filter((t) => t.set === 'both').length,
      pr_only: out_tickets.filter((t) => t.set === 'pr-only').length,
      release_only: out_tickets.filter((t) => t.set === 'release-only').length,
      in_scope: out_tickets.filter((t) => t.in_scope).length,
      testable_now: out_tickets.filter((t) => t.in_scope && t.deploy !== 'not-deployed').length,
    },
    tickets: out_tickets,
    stops,
  };
  out.flags = [...new Set(prs.prs.flatMap((p) => p.flags || []))];
  if (args.out) writeJson(args.out, out);
  emit(out);
  if (stops.length) process.exitCode = 3;
}

// ---------------------------------------------------------------- extract

const STEP_RE = /^(given|when|then|and|but)\b\s*/i;
const ACTUAL_STEP_RE = /✗|\((actual|actual result|actual behaviou?r|bug)\)\s*$|^✗/i;
const ACTUAL_SCENARIO_RE = /\b(actual|current|observed) (behaviou?r|result)\b|\(actual\)|✗/i;
const SCENARIO_RE = /^(scenario outline|scenario template|scenario|example)\s*:\s*(.*)$/i;
const BACKGROUND_RE = /^background\s*:/i;
const FEATURE_RE = /^(feature|rule)\s*:/i;
// A bug template carries TWO Given/When/Then blocks: Expected and Actual. The Actual block asserts
// the bug; testing it would turn a fixed defect into a FAIL. Anything under such a heading is skipped.
const IGNORE_HEADING_RE = /\b(actual (behaviou?r|result)s?|current behaviou?r|observed|steps to reproduce|repro(duction)? steps|root cause|evidence|workaround|out of scope|technical notes?)\b/i;

function clean(line) {
  return line
    // Only real HTML goes — a Scenario Outline's <system_email> placeholder is content.
    .replace(/<\/?(custom|br|p|span|div|a|strong|em|b|i|u|s|sup|sub|img|table|tr|td|th|ul|ol|li|code|pre)\b[^>]*>/gi, ' ')
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&')
    .replace(/\*\*|__|`/g, '')         // bold / code marks
    .replace(/^\s*(>\s*)+/, '')        // quotes
    .replace(/^\s*([-*+]|\d+[.)])\s+/, '') // bullets / numbering
    .replace(/\\([[\]()#*_-])/g, '$1')
    .replace(/\s+$/, '')
    .trim();
}

/**
 * Pull scenarios out of one markdown body. Handles the three shapes ticket templates commonly
 * use: ```gherkin fenced Feature/Scenario blocks, "### AC1: title" + bold Given/When/Then, and a
 * bug template's "Expected Behavior" block (whose "Actual Behavior" twin is skipped).
 */
function parseScenarios(md, source) {
  const lines = String(md || '').split(/\r?\n/);
  const out = [];
  let heading = null; let ignoring = false;
  let background = [];
  let inBackground = false;
  let cur = null;
  let lastKind = null;
  let feature = null;
  let pendingClose = false;   // a blank line after a Then: closes, unless Examples/a table follows
  const close = () => {
    if (cur && cur.steps.length && !ACTUAL_SCENARIO_RE.test(cur.title || '')) out.push(cur);
    cur = null; lastKind = null; pendingClose = false;
  };
  const open = (title, implicit) => {
    close();
    cur = { title: title || heading || null, implicit, heading, feature, source, steps: background.map((s) => ({ ...s, from_background: true })), notes: [], examples: [], dropped: [] };
  };
  let inFence = false;
  for (const rawLine of lines) {
    if (/^\s*```/.test(rawLine)) { close(); inFence = !inFence; inBackground = false; background = []; continue; }
    // Inside a fence "# ..." is a Gherkin comment; outside it is a markdown heading.
    const hm = !inFence && rawLine.match(/^\s*(#{1,6})\s+(.*)$/);
    if (hm) {
      close(); inBackground = false;
      heading = clean(hm[2]);
      // "Expected vs. Actual Behavior" is kept — its Actual lines are dropped step by step below.
      ignoring = IGNORE_HEADING_RE.test(heading) && !/expected/i.test(heading);
      background = [];
      continue;
    }
    const line = clean(rawLine);
    if (!line) { if (cur && lastKind === 'then') pendingClose = true; continue; }
    if (ignoring) continue;
    if (pendingClose && !/^examples\s*:/i.test(line) && !line.startsWith('|')) close();
    pendingClose = false;
    if (/^@\S/.test(line)) continue;                   // gherkin tags
    // A Feature resets the Background; a Rule inherits the Feature's.
    if (/^feature\s*:/i.test(line)) { close(); background = []; feature = line.replace(/^feature\s*:\s*/i, ''); continue; }
    if (FEATURE_RE.test(line)) { close(); continue; }
    if (BACKGROUND_RE.test(line)) { close(); inBackground = true; background = []; continue; }
    const sm = line.match(SCENARIO_RE);
    if (sm) { inBackground = false; open(sm[2] || heading, false); continue; }
    if (/^examples\s*:/i.test(line)) { if (cur) cur.in_examples = true; continue; }
    if (line.startsWith('|')) { if (cur?.in_examples) cur.examples.push(line); else if (cur?.steps.length) cur.steps.at(-1).table = [...(cur.steps.at(-1).table || []), line]; continue; }
    if (line.startsWith('#')) { if (cur) cur.notes.push(line.replace(/^#+\s*/, '')); continue; }
    const st = line.match(STEP_RE);
    if (st) {
      let kw = st[1].toLowerCase();
      const text = line.slice(st[0].length).trim();
      if (inBackground) { background.push({ kw: kw === 'and' || kw === 'but' ? 'given' : kw, text }); continue; }
      if (!cur) {
        // Without a Scenario: header only a Given opens one — a summary sentence that happens to
        // begin "When an invoice is retrieved…" is prose, not an AC.
        if (kw !== 'given') continue;
        open(heading, true);
      } else if (kw === 'given' && lastKind === 'then') {
        open(cur.implicit ? heading : null, true);      // a new Given after a Then starts the next AC
      }
      if (kw === 'and' || kw === 'but') kw = lastKind || 'given';
      lastKind = kw;
      // The bug template writes expected and actual side by side in ONE scenario:
      // "Then X (expected)" / "Then ✗ Y (actual)". The actual line asserts the bug — keep it as
      // a note for the reader, never as a step a case would check.
      if (ACTUAL_STEP_RE.test(text)) { cur.dropped.push(`${kw} ${text}`); continue; }
      cur.steps.push({ kw, text: text.replace(/\s*\((expected|expected and actual|expected result)\)\s*$/i, '') });
      continue;
    }
    // A continuation line of the previous step (wrapped prose) — only while a scenario is open.
    if (cur && cur.steps.length && lastKind && line.length < 300 && !/^(as a|i want|so that)\b/i.test(line)) {
      cur.steps.at(-1).text += ` ${line}`;
    } else close();
  }
  close();
  return out.filter((s) => s.steps.some((x) => !x.from_background));
}

/** Explicit acceptance-criteria bullets with no Given/When/Then — testable, but a human must read the conversion. */
function parseProseAc(md) {
  const lines = String(md || '').split(/\r?\n/);
  const out = []; let inAc = false;
  for (const raw of lines) {
    const hm = raw.match(/^\s*#{1,6}\s+(.*)$/);
    if (hm) { inAc = /acceptance criteria|definition of done|expected (result|behaviou?r)s?/i.test(hm[1]) && !/actual/i.test(hm[1]); continue; }
    if (!inAc) continue;
    // Bullets anywhere under the heading; plain paragraphs too (a bug's prose Expected Result).
    const t = clean(raw).replace(/^\[[ x]\]\s*/i, '');
    if (t.length > 12 && !STEP_RE.test(t) && !/^\|/.test(t)) out.push(t);
  }
  return out;
}

/**
 * Does this stack have a chat assistant at all? integrations.chatbot.entry_path names the chat
 * surface qa-run-chatbot drives. Without one there is no chat lane: a scenario that talks to "the
 * assistant" is classed by the other rules (usually `ui`) and the reviewer decides.
 */
function hasChatLane() { return Boolean(cfg('integrations.chatbot.entry_path')); }
/** The in-app assistant's name, when the adopter configured one (integrations.chatbot.assistant_name), for the chat rule. */
function assistantNames() {
  const n = cfg('integrations.chatbot.assistant_name');
  const names = ['the assistant', 'the chatbot', 'the chat assistant', 'the ai assistant', 'the copilot'];
  if (n) names.unshift(String(n).toLowerCase());
  return names.map((x) => x.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
}

// Classification is a first pass the agent reviews (`class_override`), never the last word.
// Order matters: the first class that matches wins.
const CLASS_RULES = [
  { cls: 'dev-check', where: 'all', re: /\b(typecheck|type-?check(s|ing)?|lint(er|ing)?|unit tests?|build (is )?(clean|passes|green)|code review|in the bundle|a developer (sends|calls|adds)|compile[sd]?)\b/i,
    why: 'a developer gate, not user behaviour — qa-regression / dev-pre-pr own it' },
  // Any step that needs an email in hand (received, clicked, absent) — not "email address" fields,
  // and not the in-app notification inbox, which is plain UI.
  { cls: 'manual-email', where: 'all', re: /\b(e-?mail(s|ed)?\b(?!\s+address)|magic link|unsubscribe|suppress(ed|ion)|digest is sent|receiv(es?|ing) the digest|inbox)\b/i,
    why: 'needs an email in hand — manual until a readable test inbox exists' },
  { cls: 'manual-mobile', where: 'all', re: /\b(mobile app|native app|ios|android|push notification)\b/i,
    why: 'native mobile — the browser driver covers the web app only' },
  { cls: 'manual-voice', where: 'all', re: /\b(voice|microphone|mic\b|speak|spoken|speech|audio|webrtc|tts|transcri(be|ption))\b/i,
    why: 'needs a microphone / audio output' },
  { cls: 'gated-destructive', where: 'all', re: /\b(cancel(s|led|ling)?\s+(the\s+|their\s+|a\s+)?(subscription|plan)|delete[sd]?\s+(the\s+|their\s+|an?\s+)?(workspace|organi[sz]ation|org|tenant|account)|payment|charge[sd]?|refund|card details|billing details)\b/i,
    why: 'billing / tenant deletion — only on a throwaway workspace a human provisions' },
  { cls: 'manual-time', where: 'all', re: /\b(date arrives|days? later|after \d+ (days|weeks)|at (the )?period end|window (ends|closes|expires)|(once|when) (it|the \w+) goes live|remains? open for \d+ days|\d+-day window)\b/i,
    why: 'needs calendar time to pass — seed the state or run it by hand' },
  { cls: 'backend', where: 'all', re: /\b(migration|database|db record|stored procedure|api (call|request|response)|endpoint|payload|event (fires|is (published|raised|emitted))|websocket|webhook|background job|cleanup runs|analytics event|telemetry|log entry|tool results?|serializ(ed|es|ation)|model-visible|(is|are) (stored|persisted)|customer record)\b/i,
    why: 'the outcome lives server-side — needs a data/API read, not a UI walk' },
  // "assistant" as a dropdown option or a confirmation "prompt" is not a chat test — the rule
  // needs someone talking to the assistant.
  { cls: 'chat', where: 'all', re: null, // built lazily: needs the configured assistant name
    why: 'chat assistant — nondeterministic answers, graded by qa-run-chatbot' },
];
function chatRe() {
  const who = assistantNames();
  return new RegExp(`\\b(ask(s|ed|ing)? (${who}) (to|for|about|what|how|when|which|whether)|(${who}) (answers?|respon(ds|se)|replies|says|returns|reports|tells)|chatbot|(in|from|via|through) (the )?(assistant )?chat|chat (window|panel|thread|client|session|history)|global chat)\\b`, 'i');
}

const MOBILE_TICKET_RE = /^\s*mobile\b|\bnative app\b|\bios\b|\bandroid\b/i;

function classify(sc, ticketSummary = '') {
  const thens = sc.steps.filter((s) => s.kw === 'then');
  // Then-only is fine when a Feature or heading supplies the context; only no Then is too thin.
  if (!thens.length) return { cls: 'too-thin', why: 'no Then — nothing to assert' };
  const all = [...sc.steps.map((s) => s.text), sc.title, sc.feature, sc.heading].filter(Boolean).join(' ');
  if (MOBILE_TICKET_RE.test(ticketSummary)) {
    const dev = all.match(CLASS_RULES[0].re);
    return dev ? { cls: 'dev-check', why: CLASS_RULES[0].why, matched: dev[0] }
      : { cls: 'manual-mobile', why: 'the ticket is a native-mobile story — the browser driver covers the web app only', matched: ticketSummary.match(MOBILE_TICKET_RE)[0].trim() };
  }
  const outcome = thens.map((s) => s.text).join(' ');
  for (const r of CLASS_RULES) {
    if (r.cls === 'chat' && !hasChatLane()) continue;   // no chat surface configured — no chat lane
    const re = r.re || chatRe();
    const hay = r.where === 'outcome' ? outcome : all;
    const m = hay.match(re);
    if (m) return { cls: r.cls, why: r.why, matched: m[0] };
  }
  return { cls: 'ui', why: 'browser-observable outcome' };
}

function proseClass(text, t) {
  if (t.deploy === 'not-deployed') return { class: 'not-deployed', class_why: 'code not on the environment yet' };
  const c = classify({ title: '', steps: [{ kw: 'then', text }] }, t.summary);
  return c.cls === 'ui'
    ? { class: 'prose-ac', class_why: 'an acceptance-criteria bullet with no Given/When/Then — convert only what it states' }
    : { class: c.cls, class_why: c.why, class_matched: c.matched };
}

// A heading like "Expected Behavior" names the template, not the behaviour — the case title
// would read "[PROJ-123] Expected Behavior" in the test-management tool forever. Fall back to
// the ticket summary.
const GENERIC_TITLE_RE = /^(expected (behaviou?r|results?)|acceptance criteria|scenarios?|summary|ac|definition of done)$/i;
function scenarioTitle(title, summary, n, total) {
  const t = String(title || '').replace(/^\d+\s*[·.)-]\s*/, '').trim();
  if (t && !GENERIC_TITLE_RE.test(t)) return t;
  return total > 1 ? `${summary} (AC ${n})` : summary;
}

const AMEND_RE = /\b(updated? (the )?(ac|acceptance criteria|scenarios?)|scope (change|reduced|cut)|descoped|out of scope|no longer|instead of|revised (ac|requirements?)|new ac|decision|decided|agreed|additional ac|also (needs|must|should))\b/i;

function cmdExtract(args) {
  const usage = 'extract --scope scope.json --tickets tickets.json [--out ac.json]';
  const scope = readJson(need(args, 'scope', usage));
  const tickets = new Map(loadTickets(need(args, 'tickets', usage)).map((i) => [i.key, i]));
  const scenarios = []; const outTickets = [];
  for (const t of scope.tickets.filter((x) => x.in_scope)) {
    const i = tickets.get(t.key);
    if (!i) continue;
    const found = [...parseScenarios(i.description, 'description')];
    const amendments = [];
    for (const c of i.comments) {
      const fromComment = parseScenarios(c.body, `comment ${c.created || ''} ${c.author || ''}`.trim());
      found.push(...fromComment);
      if (fromComment.length || AMEND_RE.test(c.body)) amendments.push({ created: c.created, author: c.author, scenarios: fromComment.length, excerpt: clean(c.body).slice(0, 240) });
    }
    const prose = found.length ? [] : parseProseAc(i.description);
    let n = 0;
    for (const sc of found) {
      n++;
      const c = classify(sc, i.summary);
      scenarios.push({
        id: `${t.key}#S${n}`, key: t.key, n, title: scenarioTitle(sc.title, i.summary, n, found.length),
        source: sc.source, implicit_title: sc.implicit, feature: sc.feature || undefined, heading: sc.heading || undefined,
        dropped_actual: sc.dropped.length ? sc.dropped : undefined,
        steps: sc.steps.map(({ kw, text, table, from_background }) => ({ kw, text, ...(table ? { table } : {}), ...(from_background ? { background: true } : {}) })),
        examples: sc.examples.length ? sc.examples : undefined,
        notes: sc.notes.length ? sc.notes : undefined,
        class: t.deploy === 'not-deployed' ? 'not-deployed' : c.cls,
        class_why: t.deploy === 'not-deployed' ? `code not on ${scope.env} yet` : c.why,
        class_matched: c.matched,
      });
    }
    for (const p of prose) {
      n++;
      scenarios.push({ id: `${t.key}#S${n}`, key: t.key, n, title: p.slice(0, 120), source: 'description (prose AC)',
        steps: [{ kw: 'then', text: p }], ...proseClass(p, t) });
    }
    // One destructive step makes the whole flow destructive: a "Keep my plan" dialog is harmless
    // on its own, but it is one click from cancelling on the shared test workspace.
    const mine = scenarios.filter((x) => x.key === t.key);
    const destructive = mine.filter((x) => x.class === 'gated-destructive');
    if (destructive.length) {
      for (const x of mine.filter((y) => y.class === 'ui' || y.class === 'prose-ac')) {
        x.class = 'gated-destructive';
        x.class_why = `walks the same flow as ${destructive.map((d) => `S${d.n}`).join(', ')} — throwaway workspace only`;
      }
    }
    outTickets.push({
      key: t.key, summary: t.summary, type: t.type, status: t.status, set: t.set, deploy: t.deploy,
      prs: t.prs, pr_note: t.why || undefined, reporter: t.reporter || i.reporter || undefined,
      scenarios: n, gherkin: found.length, prose_ac: prose.length,
      verdict: n ? null : 'no-ac', // a ticket with nothing to test is a finding, never a silent skip
      ac_amended_in_comments: amendments.length ? amendments : undefined,
      duplicate_ids_from_comments: found.some((s) => s.source !== 'description') && found.some((s) => s.source === 'description')
        ? 'description AND comments carry scenarios — decide which is current before drafting' : undefined,
    });
  }
  const byClass = {};
  for (const s of scenarios) byClass[s.class] = (byClass[s.class] || 0) + 1;
  const out = { release_version: scope.release_version, release_name: scope.release_name, env: scope.env,
    generated: new Date().toISOString(), counts: { tickets: outTickets.length, scenarios: scenarios.length, by_class: byClass,
      tickets_without_ac: outTickets.filter((t) => t.verdict === 'no-ac').map((t) => t.key) },
    tickets: outTickets, scenarios };
  if (args.out) writeJson(args.out, out);
  emit({ ...out, scenarios: `${scenarios.length} scenarios${args.out ? ` -> ${args.out}` : ''}` });
}

// ---------------------------------------------------------------- cases / chatset

/** Class -> the runner skill that executes it. The chat lane exists only when integrations.chatbot.entry_path is set. */
function automatedRunners() {
  const m = { ui: 'qa-run-case', 'prose-ac': 'qa-run-case' };
  if (hasChatLane()) m.chat = 'qa-run-chatbot';
  return m;
}
const AUTOMATED = automatedRunners();
/** Classes a person runs: they get no case file, and the matrix lists them under "needs a person". */
const MANUAL_RE = /^manual|gated|backend/;

function effectiveClass(s) { return s.class_override || s.class; }
/** PROJ-123#S2 -> PROJ-123-S2: the scenario's directory under the cases out-dir (qa-run-case's KEY). */
const caseDirName = (scenarioId) => String(scenarioId).replace('#', '-');

/** A scenario -> qa-run-case's CASE_FILE shape: {title, preconditions, steps_structured, expected_result, hints}. */
function toCase(s, ticket) {
  const preconditions = []; const steps = []; let when = null;
  for (const st of s.steps) {
    const table = st.table ? `\n${st.table.join('\n')}` : '';
    if (st.kw === 'given') preconditions.push(st.text);
    else if (st.kw === 'when') when = when ? `${when}, then ${st.text}` : st.text;
    else if (st.kw === 'then') {
      if (when) { steps.push({ n: steps.length + 1, action: when, expected: `${st.text}${table}` }); when = null; }
      else if (steps.length) steps.at(-1).expected += `\nAnd ${st.text}${table}`;
      // Then-only (context from the Feature/heading): name that context, don't invent an action.
      else steps.push({ n: 1, action: `View: ${s.feature || s.heading || s.title}`, expected: `${st.text}${table}` });
    }
  }
  if (s.examples?.length && steps.length) steps.at(-1).expected += `\nExamples:\n${s.examples.join('\n')}`;
  return {
    title: `[${s.key}] ${s.title}`.replace(/\s+/g, ' ').slice(0, 250),
    preconditions,
    steps_structured: steps,
    expected_result: s.steps.filter((x) => x.kw === 'then').map((x) => x.text).join(' AND '),
    hints: {
      scenario_id: s.id, ticket: s.key, source: s.source,
      component: ticket?.components?.[0] || null,
      not_expected: s.dropped_actual || [],
      depends_on: s.depends_on || null, runner_note: s.runner_note || null,
      prose_ac: effectiveClass(s) === 'prose-ac',
    },
  };
}

function cmdCases(args) {
  const usage = 'cases --ac ac.json --out-dir <dir> [--force]';
  const ac = readJson(need(args, 'ac', usage));
  const outDir = need(args, 'out-dir', usage);
  const tickets = new Map(ac.tickets.map((t) => [t.key, t]));
  const written = []; const kept = []; const manual = []; const skipped = [];
  for (const s of ac.scenarios) {
    const cls = effectiveClass(s);
    if (AUTOMATED[cls] === 'qa-run-case') {
      const file = path.join(outDir, caseDirName(s.id), 'case.json');
      const entry = { scenario_id: s.id, key: caseDirName(s.id), case_file: file };
      // A case file already on disk was finished by hand; a rerun must not reset it to the skeleton.
      if (fs.existsSync(file) && !args.force) { kept.push(entry); continue; }
      writeJson(file, toCase(s, tickets.get(s.key)));
      written.push(entry);
    } else if (MANUAL_RE.test(cls)) manual.push({ scenario_id: s.id, class: cls });
    else skipped.push({ scenario_id: s.id, why: cls });
  }
  emit({ out_dir: outDir, cases: written.length + kept.length, written, kept, manual, skipped });
}

function cmdChatset(args) {
  const usage = 'chatset --ac ac.json [--out set.json]';
  const ac = readJson(need(args, 'ac', usage));
  const cases = ac.scenarios.filter((s) => effectiveClass(s) === 'chat').map((s) => ({
    id: s.id,
    name: `[${s.key}] ${s.title}`,
    targets: [s.key],
    chat: 'fresh',
    setup: s.steps.filter((x) => x.kw === 'given').map((x) => x.text),
    // The When is the user's intent in the ticket's words; the agent rewrites it into what a real
    // user would type before the run — a Gherkin clause pasted into the assistant tests the
    // parser, not the assistant.
    turns: s.steps.filter((x) => x.kw === 'when').map((x) => x.text),
    pass: s.steps.filter((x) => x.kw === 'then').map((x) => x.text).join(' AND '),
    fail: [],
    verify_in_ui: null,
    _review: 'rewrite turns as natural user messages; set verify_in_ui where the Then names app state',
  }));
  const out = { id: `release-${ac.release_version}-chat`, cases };
  if (args.out) writeJson(args.out, out);
  emit({ id: out.id, cases: cases.length });
}

// ---------------------------------------------------------------- matrix

/** qa-run-case's case-run.json status -> matrix result. */
const CASE_RUN_RESULT = { PASS: 'pass', FAIL: 'fail', PARTIAL: 'partial', BLOCKED: 'blocked', MANUAL: 'manual' };

function cmdMatrix(args) {
  const usage = 'matrix --ac ac.json [--results-dir <cases out-dir>] [--chatbot chatbot-run.json] --out-dir <dir>';
  const ac = readJson(need(args, 'ac', usage));
  const outDir = need(args, 'out-dir', usage);
  const chat = args.chatbot ? readJson(args.chatbot) : { cases: [] };
  const chatById = new Map((chat.cases || []).map((c) => [c.id, c]));
  // qa-run-case writes <results-dir>/<KEY>-S<n>/case-run.json next to the case.json `cases` wrote.
  const runById = new Map();
  if (args['results-dir'] && fs.existsSync(args['results-dir'])) {
    for (const dir of fs.readdirSync(args['results-dir'])) {
      const f = path.join(args['results-dir'], dir, 'case-run.json');
      if (!fs.existsSync(f)) continue;
      runById.set(dir.replace(/-S(\d+)$/, '#S$1'), { ...readJson(f), dir: path.join(args['results-dir'], dir) });
    }
  }

  const rows = ac.scenarios.map((s) => {
    const cls = effectiveClass(s);
    const caseDir = args['results-dir'] && fs.existsSync(path.join(args['results-dir'], caseDirName(s.id), 'case.json'))
      ? path.join(args['results-dir'], caseDirName(s.id)) : null;
    let result = null; let evidence = null;
    if (cls === 'not-deployed') result = 'not-deployed';
    else if (cls === 'too-thin') result = 'too-thin';
    else if (cls === 'dev-check') result = 'dev-gate';
    else if (cls === 'descoped') result = 'descoped';
    else if (runById.has(s.id)) {
      const r = runById.get(s.id);
      result = CASE_RUN_RESULT[String(r.status || '').toUpperCase()] || 'not-run';
      evidence = [r.summary, path.join(r.dir, 'transcript.md')].filter(Boolean).join(' — ');
    } else if (cls === 'chat' && chatById.has(s.id)) {
      const c = chatById.get(s.id);
      result = String(c.status || c.verdict || '').toLowerCase() || null;
      evidence = c.evidence || c.notes || null;
    } else if (MANUAL_RE.test(cls)) result = 'manual';
    else result = caseDir ? 'not-run' : 'no-case';
    return { scenario_id: s.id, key: s.key, title: s.title, class: cls, class_why: s.class_override_why || s.class_why,
      case_dir: caseDir, result, evidence };
  });

  const count = (pred) => rows.filter(pred).length;
  const summary = {
    scenarios: rows.length,
    pass: count((r) => r.result === 'pass'),
    fail: count((r) => r.result === 'fail' || r.result === 'partial'),
    blocked: count((r) => r.result === 'blocked'),
    manual: count((r) => r.result === 'manual'),
    not_run: count((r) => r.result === 'not-run' || r.result === 'no-case'),
    not_deployed: count((r) => r.result === 'not-deployed'),
    too_thin: count((r) => r.result === 'too-thin'),
    dev_gate: count((r) => r.result === 'dev-gate'),
    descoped: count((r) => r.result === 'descoped'),
    tickets_without_ac: ac.counts.tickets_without_ac,
  };
  const automatable = rows.filter((r) => r.class in AUTOMATED);
  summary.automated_pct = rows.length ? Math.round((100 * automatable.length) / rows.length) : 0;
  // "All green" must mean every scenario was judged — not that the judged ones passed.
  summary.verdict = summary.fail ? 'failures'
    : summary.blocked || summary.not_run ? 'incomplete'
      : summary.manual || summary.not_deployed || summary.too_thin || summary.descoped || summary.tickets_without_ac.length ? 'automated-pass-with-gaps'
        : 'all-ac-pass';

  const out = { release_version: ac.release_version, release_name: ac.release_name ?? null, env: ac.env,
    generated: new Date().toISOString(), summary, rows };
  fs.mkdirSync(outDir, { recursive: true });
  writeJson(path.join(outDir, 'ac-matrix.json'), out);

  const icon = { pass: '✅ pass', fail: '❌ fail', blocked: '⛔ blocked', manual: '🖐 manual', 'not-run': '… not run',
    'no-case': '… no case', 'not-deployed': '🚫 not deployed', 'too-thin': '❔ too thin', 'dev-gate': '🔧 dev gate', descoped: '✂ descoped' };
  const md = [];
  md.push(`# ${ac.release_name || ac.release_version} — acceptance criteria`, '');
  md.push(`**Env:** ${ac.env || '?'} · **Verdict:** \`${summary.verdict}\``, '');
  md.push(`${summary.scenarios} scenarios: ${summary.pass} pass · ${summary.fail} fail · ${summary.blocked} blocked · ${summary.manual} manual · ${summary.not_run} not run · ${summary.not_deployed} not deployed · ${summary.too_thin} too thin · ${summary.descoped} descoped`, '');
  if (summary.tickets_without_ac.length) md.push(`**Tickets with no testable AC:** ${summary.tickets_without_ac.join(', ')}`, '');
  const byKey = new Map();
  for (const r of rows) byKey.set(r.key, [...(byKey.get(r.key) || []), r]);
  for (const t of ac.tickets) {
    const rs = byKey.get(t.key) || [];
    md.push(`## ${t.key} — ${t.summary}`, '');
    if (!rs.length) { md.push('_No Gherkin or acceptance criteria in the ticket — ask the reporter; nothing was invented._', ''); continue; }
    md.push('| # | Scenario | Class | Result |', '|---|---|---|---|');
    for (const r of rs) md.push(`| ${r.scenario_id.split('#')[1]} | ${r.title.replace(/\|/g, '\\|').slice(0, 90)} | ${r.class} | ${icon[r.result] || r.result} |`);
    md.push('');
  }
  fs.writeFileSync(path.join(outDir, 'ac-matrix.md'), md.join('\n'));
  emit({ written: [path.join(outDir, 'ac-matrix.json'), path.join(outDir, 'ac-matrix.md')], summary });
}

// ---------------------------------------------------------------- main

const HELP = `qa-loop-release-ac — the deterministic glue of the release AC loop

  prs      --release 4.2.0 [--since-days 90] [--base <project.default_branch>] [--repo <app repo>] [--out prs.json]
  tickets  --prs prs.json --release 4.2.0 [--release-query "<tracker query>"] [--out tickets.json]
  deployed --prs prs.json [--env <app.environments name>] [--repo <app repo>] [--out deployed.json]
  scope    --prs prs.json --tickets tickets.json [--deployed deployed.json] [--release-name "<name>"]
           [--out scope.json]
  extract  --scope scope.json --tickets tickets.json [--out ac.json]
  cases    --ac ac.json --out-dir <dir> [--force]                          qa-run-case case.json per browser scenario
  chatset  --ac ac.json [--out set.json]                                   qa-run-chatbot SET_FILE skeleton
  matrix   --ac ac.json [--results-dir <cases out-dir>] [--chatbot chatbot-run.json] --out-dir <dir>

Reads the vcs_host, issue_tracker, deploy and ci_cd adapters (lib/adapters/cli.py), the target
app repo (git, app.repo_dir) and each deploy track's version source (deploy.tracks). Writes only
local files. Config: .claude/loops/stack.config.json ($LOOPS_STACK_CONFIG, LOOPS_CFG__* overrides).
Exit codes: 0 ok · 1 error · 2 config/input missing · 3 stop condition`;

const [verb, ...rest] = process.argv.slice(2);
const args = parseArgs(rest);
const verbs = { prs: cmdPrs, tickets: cmdTickets, deployed: cmdDeployed, scope: cmdScope, extract: cmdExtract, cases: cmdCases, chatset: cmdChatset, matrix: cmdMatrix };
if (!verb || verb === '--help' || verb === 'help' || !verbs[verb]) { process.stdout.write(HELP + '\n'); process.exit(verb && !verbs[verb] && verb !== '--help' && verb !== 'help' ? 2 : 0); }
verbs[verb](args);

export { parseScenarios, classify, parseProseAc, toCase, globToRe, releaseNamesOf };
