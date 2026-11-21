// Pure functions that turn what the other mods hold into what the Mods panel draws.
// No $ here, so the tests can feed them plain values.

import type { PanelAnswer, RateLimit, Usage } from '../types'

// ---- what the panel reads from the other mods (the fields it uses, as their contracts declare them)

export type StageIn = { id: string; label: string; outcome: string | null; tone: string; attempts: number; isCurrent: boolean; detail: string | null }
export type VerdictIn = {
  stage: string
  result: string
  attempt: number | null
  max: number | null
  action: string
  reason: string
  budgetSpent: number | null
  budgetCap: number | null
  at: number
}
export type RunIn = { root: string; key: string; recipe: string | null; tier: string | null; stages: StageIn[]; verdicts: VerdictIn[]; updatedAt: number }
export type QuestionIn = {
  id: string
  key: string
  from: string
  kind: 'blocking' | 'non-blocking'
  question: string
  options: string[]
  default: string | null
  context: string | null
  askedAt: string
}
export type PeerIn = { id: string; label: string; root: string; branch: string; lastTool: string; lastSeen: number }
export type LockIn = { kind: 'push' | 'deploy'; root: string; label: string; command: string; at: number }
export type RepoIn = {
  name: string
  branch: string
  changed: number
  ahead: number
  behind: number
  hasUpstream: boolean
  lastDeploy: { at: number; what: string } | null
  recipe: string | null
}
export type ServerIn = { pid: number; port: number; process: string; command: string; age: string; cwd?: string; tool?: string | null; project?: string | null }
export type RecipeEntryIn = { name: string; port: number }
export type PrIn = {
  branch: string
  pr: { number: number; title: string; isDraft: boolean; reviewDecision: string } | null
  checks: { name: string; bucket: string }[]
  threads: { isOutdated: boolean }[]
  error: string | null
}
export type BrainIn = { isOnline: boolean; reason: string; project: string; lessons: number; proposed: number; checkedAt: number }
export type RecallIn = { query: string; at: number; lessons: unknown[]; error: string | null }
export type BurnSettingsIn = { budgetUsd: number | null; isHard: boolean; isWorkflowOff: boolean }

export type Snapshot = {
  now: number
  enabled: Record<string, boolean>
  toggled: Record<string, boolean>
  usage: Usage | null
  turns: number[]
  recipe: { run: RunIn | null; open: QuestionIn[] }
  answered: PanelAnswer[]
  burn: BurnSettingsIn | null
  traffic: { me: string; peers: PeerIn[]; locks: LockIn[] } | null
  ship: RepoIn | null
  dev: { servers: ServerIn[]; recipe: RecipeEntryIn[] } | null
  pr: PrIn | null
  brain: { health: BrainIn | null; recall: RecallIn | null; review: unknown[] } | null
}

// ---- the eight mods, in the design's order

export const MODS = ['recipe-run', 'burn-meter', 'traffic-control', 'ship', 'dev-servers', 'secret-catcher', 'pr', 'brain'] as const
export type ModName = (typeof MODS)[number]
export const MARKETPLACE = 'agent-kit'

// A run touched in the last two hours counts as live, as recipe-run's own band does.
export const RECENT_MS = 2 * 60 * 60_000
const LIVE_PEER_MS = 90_000
export const HOT_PCT = 80
// The design's burn scale: a full SESSION BURN meter is $10. A burrito is $10, a McDouble $3.125.
export const BURN_SCALE_USD = 10
const BURRITO_USD = 10
const MCDOUBLE_USD = 3.125

// ---- small formatting

export const money = (usd: number) => `$${usd.toFixed(2)}`
export const burritos = (usd: number) => (usd / BURRITO_USD).toFixed(2)
export const mcdoubles = (usd: number) => (usd / MCDOUBLE_USD).toFixed(2)

export function span(ms: number) {
  const m = Math.max(0, Math.round(ms / 60_000))
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${m % 60}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}
export const ago = (now: number, at: number) => (now - at < 60_000 ? 'just now' : `${span(now - at)} ago`)

// How many of n segments a fraction fills, never past n and never below 0.
export const filled = (n: number, frac: number) => Math.max(0, Math.min(n, Math.round((Number.isFinite(frac) ? frac : 0) * n)))

// ---- on / off

export type Power = 'on' | 'off' | 'missing'

// enabledPlugins keys are name@marketplace. A mod loaded some other way (a --plugin-dir) is not
// listed there, so a mod whose state is live counts as on even when settings do not name it.
export function power(name: string, enabled: Record<string, boolean>, hasState: boolean): Power {
  const ids = Object.keys(enabled).filter(id => id.split('@')[0] === name)
  if (ids.some(id => enabled[id] === true)) return 'on'
  if (hasState) return 'on'
  return ids.length > 0 ? 'off' : 'missing'
}

// ---- limits

export type LimitRow = { kind: string; label: string; pct: number; reset: string | null; isHot: boolean }

const LIMIT_LABEL: Record<string, string> = { five_hour: '5 hour', seven_day: 'weekly', spend_limit: 'spend' }

export function limitRows(limits: RateLimit[], now: number): LimitRow[] {
  return limits.map(l => {
    const at = l.resetsAt ? Date.parse(l.resetsAt) : NaN
    return {
      kind: l.kind,
      label: LIMIT_LABEL[l.kind] ?? l.kind,
      pct: l.percentUsed,
      reset: Number.isFinite(at) ? `in ${span(at - now)}` : null,
      isHot: l.percentUsed >= HOT_PCT,
    }
  })
}

const pctOf = (rows: LimitRow[], kind: string) => rows.find(r => r.kind === kind)

// ---- recipe-run

const ACTION_BADGE: Record<string, string> = {
  advance: 'Continue',
  're-fix': 'Retry',
  're-repro': 'Retry',
  reject: 'Rejected',
  escalate: 'Escalated',
  rollback: 'Roll back',
  'stop-budget': 'Budget stop',
  'halt-paused': 'Paused',
}
const ACTION_WORDS: Record<string, string> = {
  advance: 'advanced',
  're-fix': 'retry the fix',
  're-repro': 'retry the repro',
  reject: 'reject: not reproduced',
  escalate: 'escalated to a human',
  rollback: 'roll back and escalate',
  'stop-budget': 'stopped: budget cap',
  'halt-paused': 'halted: global pause',
}
export const badgeFor = (action: string) => ACTION_BADGE[action] ?? action
export const isAlarm = (action: string) => ['escalate', 'rollback', 'stop-budget', 'halt-paused', 'reject'].includes(action)

// Questions recipe-run still lists as open, less the ones this panel answered since its last refresh.
export function openQuestions(open: QuestionIn[], answered: PanelAnswer[]) {
  const done = new Set(answered.map(a => `${a.key}/${a.id}`))
  return open
    .filter(q => !done.has(`${q.key}/${q.id}`))
    .slice()
    .sort((a, b) => Number(b.kind === 'blocking') - Number(a.kind === 'blocking'))
}

export const isRecent = (run: RunIn | null, now: number): boolean => run !== null && now - run.updatedAt < RECENT_MS

export type StageView = { id: string; label: string; state: 'done' | 'failed' | 'run' | 'blocked' | 'wait'; glyph: string; meta: string }

export function ladder(run: RunIn, blocked: boolean): StageView[] {
  return run.stages.map(s => {
    const state: StageView['state'] = s.isCurrent ? (blocked ? 'blocked' : 'run') : s.outcome !== null ? (s.tone === 'bad' && s.id !== 'intake' ? 'failed' : 'done') : 'wait'
    const glyph = { done: '✓', failed: '✗', run: '•', blocked: '!', wait: '○' }[state]
    const tries = s.attempts > 1 ? `try ${s.attempts}` : ''
    const meta = state === 'wait' ? 'waiting' : state === 'blocked' ? ['waiting on you', tries].filter(Boolean).join(' · ') : [s.outcome ?? (state === 'run' ? 'running' : ''), tries].filter(Boolean).join(' · ')
    return { id: s.id, label: s.label, state, glyph, meta }
  })
}

export type RecipeView = {
  name: string
  key: string
  stage: string | null
  attempt: number | null
  max: number
  verdict: { badge: string; reason: string; isAlarm: boolean } | null
  spend: { spent: number; cap: number | null } | null
}

export function recipeView(run: RunIn, blocked: boolean): RecipeView {
  const cur = run.stages.find(s => s.isCurrent) ?? null
  const last = run.verdicts.at(-1) ?? null
  const withSpend = [...run.verdicts].reverse().find(v => v.budgetSpent !== null) ?? null
  const max = last?.max ?? 3
  const attempt = last?.attempt ?? (cur && cur.attempts > 0 ? cur.attempts : null)
  return {
    name: run.recipe ?? run.key,
    key: run.key,
    stage: cur ? cur.label : null,
    attempt,
    max,
    verdict: last
      ? {
          badge: blocked ? 'Ask you' : badgeFor(last.action),
          reason: last.reason ? `${last.stage} ${last.result}: ${ACTION_WORDS[last.action] ?? last.action}. ${last.reason}` : `${last.stage} ${last.result}: ${ACTION_WORDS[last.action] ?? last.action}.`,
          isAlarm: blocked || isAlarm(last.action),
        }
      : null,
    spend: withSpend && withSpend.budgetSpent !== null ? { spent: withSpend.budgetSpent, cap: withSpend.budgetCap } : null,
  }
}

// ---- the cards

export type Card = {
  name: ModName
  power: Power
  // a toggle pressed this session, waiting for /reload-plugins
  pending: boolean | null
  status: string
  isWarn: boolean
  isActive: boolean
  details: [string, string][]
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

function recipeStatus(s: Snapshot): Pick<Card, 'status' | 'isWarn' | 'isActive'> {
  const open = openQuestions(s.recipe.open, s.answered)
  const blocking = open.filter(q => q.kind === 'blocking').length
  const run = s.recipe.run
  if (blocking > 0) return { status: `Blocked on you · ${plural(blocking, 'question')}`, isWarn: true, isActive: true }
  if (run === null || !isRecent(run, s.now)) {
    const idle = run ? `Idle · last run ${run.key} ${ago(s.now, run.updatedAt)}` : 'Idle · no runs yet'
    return { status: open.length > 0 ? `${idle} · ${plural(open.length, 'question')}` : idle, isWarn: false, isActive: false }
  }
  const v = recipeView(run, false)
  const parts = [v.stage ?? 'between stages']
  if (v.attempt !== null) parts.push(`attempt ${v.attempt} of ${v.max}`)
  if (v.spend) parts.push(v.spend.cap !== null ? `${money(v.spend.spent)} of ${money(v.spend.cap)}` : money(v.spend.spent))
  return { status: parts.join(' · '), isWarn: v.verdict?.isAlarm ?? false, isActive: true }
}

function burnStatus(s: Snapshot): Pick<Card, 'status' | 'isWarn' | 'isActive' | 'details'> {
  const rows = limitRows(s.usage?.rateLimits ?? [], s.now)
  const h5 = pctOf(rows, 'five_hour')
  const wk = pctOf(rows, 'seven_day')
  const spend = s.usage?.costUsd ?? null
  const details: [string, string][] = []
  if (s.burn) {
    details.push(['Turn budget', typeof s.burn.budgetUsd === 'number' ? `${money(s.burn.budgetUsd)}${s.burn.isHard ? ' hard' : ''}` : 'off'])
    details.push(['Workflows', s.burn.isWorkflowOff ? 'blocked' : 'allowed'])
  }
  if (h5?.isHot) return { status: `5h at ${h5.pct}%${h5.reset ? ` · resets ${h5.reset}` : ''}`, isWarn: true, isActive: true, details }
  const parts = [spend !== null ? `${money(spend)} session` : 'no cost yet']
  if (h5) parts.push(`5h ${h5.pct}%`)
  if (wk) parts.push(`wk ${wk.pct}%`)
  return { status: parts.join(' · '), isWarn: false, isActive: false, details }
}

export type SessionRow = { label: string; branch: string; state: string; isBusy: boolean }

export function sessionRows(t: NonNullable<Snapshot['traffic']>, now: number): SessionRow[] {
  return t.peers
    .filter(p => p.id !== t.me)
    .map(p => {
      const isBusy = now - p.lastSeen < LIVE_PEER_MS && p.lastTool !== ''
      return { label: p.label, branch: p.branch, state: isBusy ? p.lastTool : `idle ${ago(now, p.lastSeen)}`, isBusy }
    })
}

export function lockView(t: NonNullable<Snapshot['traffic']>, now: number) {
  const held = t.locks[0]
  return held
    ? { state: 'Held', detail: `${held.kind} by ${held.label} · ${ago(now, held.at)}${t.locks.length > 1 ? ` · ${plural(t.locks.length - 1, 'more lock')}` : ''}` }
    : { state: 'Free', detail: 'No push or deploy lock is held.' }
}

function trafficStatus(s: Snapshot): Pick<Card, 'status' | 'isWarn' | 'isActive'> {
  if (!s.traffic) return { status: 'Waiting for its first scan', isWarn: false, isActive: false }
  const others = sessionRows(s.traffic, s.now).length
  const lock = s.traffic.locks.length > 0
  return { status: `${plural(others, 'other session')} · push lock ${lock ? 'held' : 'free'}`, isWarn: false, isActive: lock }
}

function shipCard(s: Snapshot): Pick<Card, 'status' | 'isWarn' | 'isActive' | 'details'> {
  const r = s.ship
  if (!r) return { status: 'No repo status yet', isWarn: false, isActive: false, details: [] }
  const state = r.changed > 0 ? `${r.changed} changed` : 'clean'
  const push = r.ahead > 0 ? ` · ${r.ahead} to push` : ''
  return {
    status: `${r.branch} · ${state}${push}${r.lastDeploy ? ` · deployed ${ago(s.now, r.lastDeploy.at)}` : ''}`,
    isWarn: false,
    isActive: r.changed > 0 || r.ahead > 0,
    details: [
      ['Branch', `${r.branch}${r.hasUpstream ? '' : ' · no upstream'}`],
      ['Changes', r.changed > 0 ? `${r.changed} files` : 'clean'],
      ['Commits', `${r.ahead} ahead · ${r.behind} behind`],
      ['Last deploy', r.lastDeploy ? `${r.lastDeploy.what} · ${ago(s.now, r.lastDeploy.at)}` : 'none recorded'],
      ['Deploy recipe', r.recipe ? 'saved' : 'not saved yet (first /ship works it out)'],
    ],
  }
}

// "vite in pm-tool", "roughdraft", or the bare process name when nothing better is known.
export function appLabel(x: ServerIn) {
  const what = x.tool || x.process || `pid ${x.pid}`
  return x.project && x.project !== x.tool ? `${what} in ${x.project}` : what
}

export const shortPath = (path: string) => path.replace(/^\/(Users|home)\/[^/]+/, '~').replace(/ \/(Users|home)\/[^/ ]+/g, ' ~')

// One row per listening port, with what the card's one-liner leaves out: the project folder,
// the full command and the pid. `hidden` holds pids the panel just stopped, so they drop out
// before dev-servers' next scan.
export type ServerRow = { key: string; pid: number; port: number; process: string; app: string; age: string; folder: string; command: string }
export function serverRows(d: NonNullable<Snapshot['dev']>, hidden: number[] = []): ServerRow[] {
  return d.servers
    .filter(x => !hidden.includes(x.pid))
    .map(x => ({
      key: `${x.pid}-${x.port}`,
      pid: x.pid,
      port: x.port,
      process: x.process || `pid ${x.pid}`,
      app: appLabel(x),
      age: x.age,
      folder: x.cwd && x.cwd !== '?' ? shortPath(x.cwd) : 'folder unknown',
      command: shortPath(x.command || x.process),
    }))
    .sort((a, b) => a.port - b.port)
}

function devCard(s: Snapshot): Pick<Card, 'status' | 'isWarn' | 'isActive' | 'details'> {
  const d = s.dev
  if (!d) return { status: 'No scan yet', isWarn: false, isActive: false, details: [] }
  const ports = [...new Set(d.servers.map(x => x.port))].sort((a, b) => a - b)
  const running = new Set(ports)
  const details: [string, string][] = d.servers.map(x => [appLabel(x), `:${x.port}${x.cwd && x.cwd !== '?' ? ` · ${shortPath(x.cwd)}` : ''}${x.age ? ` · up ${x.age}` : ''}`])
  for (const r of d.recipe) if (!running.has(r.port)) details.push([r.name, `:${r.port} · stopped`])
  return {
    status: ports.length > 0 ? `${ports.length} running · ${ports.map(p => `:${p}`).join(' ')}` : 'Nothing running',
    isWarn: false,
    isActive: ports.length > 0,
    details,
  }
}

function prCard(s: Snapshot): Pick<Card, 'status' | 'isWarn' | 'isActive' | 'details'> {
  const p = s.pr
  if (!p) return { status: 'No PR data yet', isWarn: false, isActive: false, details: [] }
  if (p.error) return { status: p.error, isWarn: true, isActive: false, details: [] }
  if (!p.pr) return { status: `No PR for ${p.branch}`, isWarn: false, isActive: false, details: [] }
  const pass = p.checks.filter(c => c.bucket === 'pass' || c.bucket === 'skipping').length
  const fail = p.checks.filter(c => c.bucket === 'fail').length
  const pending = p.checks.filter(c => c.bucket === 'pending').length
  const threads = p.threads.filter(t => !t.isOutdated).length
  const ci = p.checks.length > 0 ? ` · CI ${pass}/${p.checks.length}` : ''
  return {
    status: `#${p.pr.number} ${p.pr.isDraft ? 'draft' : 'open'}${ci} · ${plural(threads, 'thread')}`,
    isWarn: fail > 0,
    isActive: true,
    details: [
      [`#${p.pr.number}`, p.pr.title],
      ['Reviews', p.pr.reviewDecision ? p.pr.reviewDecision.toLowerCase().replace(/_/g, ' ') : 'none yet'],
      ['Checks', p.checks.length > 0 ? `${pass} passed · ${fail} failed · ${pending} running` : 'none'],
      ['Threads', `${threads} open`],
    ],
  }
}

function brainCard(s: Snapshot): Pick<Card, 'status' | 'isWarn' | 'isActive' | 'details'> {
  const h = s.brain?.health ?? null
  if (!h) return { status: 'No health check yet', isWarn: false, isActive: false, details: [] }
  if (!h.isOnline) return { status: `Offline · ${h.reason}`, isWarn: true, isActive: false, details: [['Project', h.project]] }
  const recall = s.brain?.recall ?? null
  const review = s.brain?.review.length ?? 0
  return {
    status: `${plural(h.lessons, 'lesson')} · ${h.proposed} to review`,
    isWarn: false,
    isActive: review > 0,
    details: [
      ['Project', h.project],
      ['Last recall', recall ? `${plural(recall.lessons.length, 'lesson')} · ${ago(s.now, recall.at)}` : 'none yet'],
      ['Checked', ago(s.now, h.checkedAt)],
    ],
  }
}

const STATE_HELD: Record<ModName, (s: Snapshot) => boolean> = {
  'recipe-run': s => s.recipe.run !== null || s.recipe.open.length > 0,
  'burn-meter': s => s.burn !== null,
  'traffic-control': s => s.traffic !== null,
  ship: s => s.ship !== null,
  'dev-servers': s => s.dev !== null,
  'secret-catcher': () => false,
  pr: s => s.pr !== null,
  brain: s => s.brain !== null,
}

export function cards(s: Snapshot): Card[] {
  return MODS.map(name => {
    const p = power(name, s.enabled, STATE_HELD[name](s))
    const pending = name in s.toggled ? (s.toggled[name] ?? null) : null
    const base = { name, power: p, pending }
    if (p !== 'on') {
      return {
        ...base,
        status: p === 'missing' ? 'Not installed' : pending === true ? 'Off · on after /reload-plugins' : 'Off',
        isWarn: false,
        isActive: false,
        details: p === 'missing' ? [['Install', `/plugin install ${name} --marketplace travist6983/agent-kit`]] : [],
      }
    }
    const off = pending === false ? ' · off after /reload-plugins' : ''
    const body = (() => {
      switch (name) {
        case 'recipe-run':
          return { ...recipeStatus(s), details: [] }
        case 'burn-meter':
          return burnStatus(s)
        case 'traffic-control':
          return { ...trafficStatus(s), details: [] }
        case 'ship':
          return shipCard(s)
        case 'dev-servers':
          return devCard(s)
        case 'secret-catcher':
          return {
            status: 'Watching prompts for API keys',
            isWarn: false,
            isActive: false,
            details: [['Catch counts', 'not shared yet: secret-catcher keeps no state the panel can read'] as [string, string]],
          }
        case 'pr':
          return prCard(s)
        case 'brain':
          return brainCard(s)
      }
    })()
    return { ...base, ...body, status: `${body.status}${off}` }
  })
}

// ---- the summary strip and the band

export function summary(s: Snapshot, list: Card[]) {
  const blocking = openQuestions(s.recipe.open, s.answered).filter(q => q.kind === 'blocking').length
  return { on: list.filter(c => c.power === 'on').length, total: list.length, blocking, spend: s.usage?.costUsd ?? null }
}

export type Band = { title: string; meta: string; spend: string; spendSub: string; limit: string | null; blocking: number; isBlocked: boolean }

export function band(s: Snapshot): Band {
  const rows = limitRows(s.usage?.rateLimits ?? [], s.now)
  const h5 = pctOf(rows, 'five_hour')
  const wk = pctOf(rows, 'seven_day')
  const blocking = openQuestions(s.recipe.open, s.answered).filter(q => q.kind === 'blocking').length
  const limit = h5?.isHot ? `5h ${h5.pct}%${h5.reset ? ` · ${h5.reset.replace(/^in /, '')}` : ''}` : null
  const run = s.recipe.run
  const session = s.usage?.costUsd ?? null
  if (run !== null && isRecent(run, s.now)) {
    const v = recipeView(run, blocking > 0)
    const stage = blocking > 0 ? `${v.stage ?? 'Run'} · blocked` : (v.stage ?? 'between stages')
    return {
      title: v.name === v.key ? v.key : `${v.name} ${v.key}`,
      meta: v.attempt !== null ? `${stage} · attempt ${v.attempt}/${v.max}` : stage,
      spend: v.spend ? money(v.spend.spent) : session !== null ? money(session) : '',
      spendSub: v.spend ? (v.spend.cap !== null ? `of ${money(v.spend.cap)} cap` : 'run spend') : 'this session',
      limit,
      blocking,
      isBlocked: blocking > 0,
    }
  }
  const meta = [h5 ? `5h ${h5.pct}%` : null, wk ? `weekly ${wk.pct}%` : null].filter(Boolean).join(' · ')
  return {
    title: 'No recipe running',
    meta: meta || 'no limit readings yet',
    spend: session !== null ? money(session) : '',
    spendSub: 'this session',
    limit,
    blocking,
    isBlocked: blocking > 0,
  }
}

// What the person's next message tells Claude about answers given in the panel.
export function answersContext(list: PanelAnswer[]): string | null {
  if (list.length === 0) return null
  return [
    'The user answered open questions in the Mods panel (also saved to runs/<KEY>/answers.jsonl):',
    ...list.map(a => `- ${a.key} ${a.id} "${a.question}": ${a.answer}`),
    'A blocking question that stopped a run can now be resumed with this answer.',
  ].join('\n')
}
