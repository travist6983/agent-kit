// What build-log notices on its own, and how a moment is shaped. Pure functions only.

import type { BuildLogConfig } from './check'

export type Origin = 'own' | 'work'

export type Moment = {
  id: string
  at: string
  kind: string
  title: string
  problem?: string
  kit_did?: string
  broke?: string
  changed?: string
  repo: string
  origin: Origin
  source: 'claude' | 'user' | 'auto'
}

export type ShotStatus = 'ok' | 'review' | 'dropped'

export type Shot = {
  id: string
  at: string
  file: string
  caption: string
  repo: string
  origin: Origin
  status: ShotStatus
}

export const KINDS = ['recipe-run', 'pr-merged', 'mod-catch', 'spend', 'before-after', 'bug', 'lesson', 'note'] as const

export const SHOT_EXT = /\.(png|jpe?g|gif|webp|mp4|mov)$/i

export const newId = (prefix: string, now: number, salt: number) => `${prefix}-${now.toString(36)}${salt.toString(36).padStart(2, '0')}`

// The repo a session works in, as build-log sees it: the git root's folder name, the remote's
// owner/name slug, and the project a kit loop session was started for.
export type Where = { name: string; slug: string; loopProject: string }

export function slugOf(remote: string) {
  const m = remote.trim().match(/[:/]([^/:]+\/[^/]+?)(?:\.git)?\/?$/)
  return m?.[1] ?? ''
}

// Only the kit itself and repos the owner listed as their own are "own". Anything else, an
// unknown folder included, is work: its material gets the strict treatment.
// A loop session runs inside the kit checkout but works on AGENT_KIT_PROJECT, so that project decides.
export function originOf(where: Where, config: BuildLogConfig | null): Origin {
  const safe = new Set((config?.safe_repos ?? []).map(s => s.trim().toLowerCase()).filter(Boolean))
  const isOwn = (name: string) => name !== '' && safe.has(name.toLowerCase())
  if (where.loopProject) return isOwn(where.loopProject) ? 'own' : 'work'
  const isKit = where.slug.toLowerCase() === 'travist6983/agent-kit' || (where.slug === '' && where.name === 'agent-kit')
  return isKit || isOwn(where.name) || isOwn(where.slug) ? 'own' : 'work'
}

// `gh pr merge 42 --squash` (or `gh pr merge` on the current branch) that succeeded.
export function parseMerge(command: string) {
  const m = command.match(/\bgh\s+pr\s+merge\b([^;&|]*)/)
  if (!m) return null
  const num = (m[1] ?? '').match(/(?:^|\s)#?(\d+)\b/)
  return { pr: num ? Number(num[1]) : null }
}

// The kit's recipe router: `recipe_router.py --stage fix --result fail ...`.
export function parseRouter(command: string) {
  if (!/recipe_router\.py\b/.test(command)) return null
  const flag = (name: string) => command.match(new RegExp(`--${name}(?:=|\\s+)("[^"]*"|'[^']*'|\\S+)`))?.[1]?.replace(/^["']|["']$/g, '') ?? ''
  const stage = flag('stage')
  if (!stage) return null
  return { stage, result: flag('result') }
}

// The router prints its verdict as the last JSON line: {"action": "re-fix", "reason": "..."}.
export function routerAction(stdout: string) {
  for (const line of stdout.split('\n').map(l => l.trim()).reverse()) {
    if (!line.startsWith('{')) continue
    try {
      const v = JSON.parse(line) as { action?: unknown }
      if (typeof v.action === 'string') return v.action
    } catch {
      // not the verdict line
    }
  }
  return ''
}

// The verdicts worth a post: a retry, an escalation, a rollback, a budget stop, a pause.
// Plain "advance" is the loop doing its job and is not saved.
export const NOTABLE_ACTIONS = new Set(['re-fix', 're-repro', 'reject', 'escalate', 'rollback', 'stop-budget', 'halt-paused'])

export function latestShots(lines: string[]) {
  const byId = new Map<string, Shot>()
  for (const line of lines) {
    try {
      const row = JSON.parse(line) as Partial<Shot> & { id?: string }
      if (!row.id) continue
      const prev = byId.get(row.id)
      byId.set(row.id, { ...(prev ?? {}), ...row } as Shot)
    } catch {
      // a torn line from a crash is skipped, never fatal
    }
  }
  return [...byId.values()].filter(s => s.status !== 'dropped')
}

export function parseMoments(lines: string[]) {
  const out: Moment[] = []
  for (const line of lines) {
    try {
      const row = JSON.parse(line) as Moment
      if (row.id && row.title) out.push(row)
    } catch {
      // skipped
    }
  }
  return out
}

export function since(moments: Moment[], fromIso: string) {
  return fromIso ? moments.filter(m => m.at >= fromIso) : moments
}

export const slugify = (s: string) =>
  s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48) || 'post'

// `/buildlog <verb> <rest>`: the verb, then the rest as one string.
export function parseArgs(args: string) {
  const text = args.trim()
  const at = text.search(/\s/)
  return at < 0 ? { verb: text, rest: '' } : { verb: text.slice(0, at), rest: text.slice(at + 1).trim() }
}

// `<path> [caption]`, with the path optionally quoted.
export function parseShotArgs(rest: string) {
  const q = rest.match(/^(["'])(.+?)\1\s*(.*)$/)
  if (q) return { path: q[2] ?? '', caption: (q[3] ?? '').trim() }
  const at = rest.search(/\s/)
  return at < 0 ? { path: rest, caption: '' } : { path: rest.slice(0, at), caption: rest.slice(at + 1).trim() }
}
