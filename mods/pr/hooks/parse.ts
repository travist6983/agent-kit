import type { PrCheck, PrThread } from '../types'

type RawThread = {
  isResolved: boolean
  isOutdated: boolean
  path: string
  line: number | null
  comments: { nodes: { author: { login: string } | null; body: string; url: string }[] }
}

// Unresolved threads whose last word is someone else's.
export function waitingThreads(raw: RawThread[], me: string): PrThread[] {
  const out: PrThread[] = []
  for (const t of raw) {
    if (t.isResolved) continue
    const comments = t.comments.nodes
    const last = comments[comments.length - 1]
    if (!last) continue
    const author = last.author?.login ?? 'ghost'
    if (author === me) continue
    out.push({ path: t.path, line: t.line, isOutdated: t.isOutdated, author, body: last.body, url: last.url, replies: comments.length })
  }
  return out
}

export function passed(checks: PrCheck[]) {
  return checks.filter(c => c.bucket === 'pass' || c.bucket === 'skipping').length
}

export function isDone(c: PrCheck) {
  return c.bucket !== 'pending'
}

export function duration(c: PrCheck) {
  const start = Date.parse(c.startedAt)
  const end = c.completedAt && !c.completedAt.startsWith('0001') ? Date.parse(c.completedAt) : NaN
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return ''
  const s = Math.round((end - start) / 1000)
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s`
}

export function summary(pr: { number: number; isDraft: boolean } | null, checks: PrCheck[], threads: PrThread[]) {
  if (pr === null) return undefined
  const parts = [`PR #${pr.number}${pr.isDraft ? ' draft' : ''}`]
  if (checks.length > 0) {
    const failed = checks.filter(c => c.bucket === 'fail').length
    const pending = checks.filter(c => c.bucket === 'pending').length
    parts.push(`CI ${passed(checks)}/${checks.length}${failed ? ` ✗${failed}` : ''}${pending ? ` …${pending}` : ''}`)
  }
  if (threads.length > 0) parts.push(`${threads.length} thread${threads.length === 1 ? '' : 's'}`)
  return parts.join(' · ')
}
