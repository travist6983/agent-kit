// Pure helpers for the recipe-run mod: no `$`, so the tests can call them directly.
import type { Answer, Question, QuestionKind, RunView, StageRow, Tone, Verdict } from '../types'

// The spine every dev-loop recipe walks, with the file each stage writes under runs/<KEY>/.
// ship and prod-verify write no stage file of their own; they show up through router verdicts.
export const STAGES: readonly { id: string; label: string; file: string | null }[] = [
  { id: 'intake', label: 'intake', file: 'intake.json' },
  { id: 'repro', label: 'repro', file: 'repro.json' },
  { id: 'fix', label: 'fix', file: 'fix.json' },
  { id: 'verify', label: 'verify', file: 'verify.json' },
  { id: 'regression', label: 'regression', file: 'regression.json' },
  { id: 'review', label: 'review', file: 'review.json' },
  { id: 'pre-pr', label: 'pre-pr', file: 'pre-pr.json' },
  { id: 'ship', label: 'ship', file: null },
  { id: 'prod-verify', label: 'prod-verify', file: null },
]

// recipe_router.py calls the review stage code-review.
export const stageId = (routerStage: string) => (routerStage === 'code-review' ? 'review' : routerStage)

const GOOD = new Set(['fixed', 'reproduced', 'pass', 'green', 'approve', 'ready', 'safe', 'ship-ok', 'prod-confirmed', 'advance'])
const WARN = new Set(['warnings', 'partial', 'flaky', 'inconclusive', 'risky', 'unproven'])

export function toneOf(outcome: string | null): Tone {
  if (outcome === null || outcome === '') return 'none'
  if (GOOD.has(outcome)) return 'good'
  if (WARN.has(outcome)) return 'warn'
  return 'bad'
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
export const describeAction = (action: string) => ACTION_WORDS[action] ?? action

type RouterCall = Omit<Verdict, 'action' | 'reason' | 'at'>

const num = (v: string | undefined) => {
  if (v === undefined) return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

// Reads the flags of a `python3 .../recipe_router.py --stage fix --result fixed ...` command.
// Returns null for any command that does not run the router.
export function parseRouterCall(command: string): RouterCall | null {
  if (!/recipe_router\.py\b/.test(command)) return null
  const flags: Record<string, string> = {}
  const re = /--([a-z][\w-]*)(?:=|\s+)("[^"]*"|'[^']*'|[^\s-][^\s]*|-?\d[\d.]*)/g
  for (const m of command.matchAll(re)) {
    const name = m[1] ?? ''
    const raw = m[2] ?? ''
    flags[name] = raw.replace(/^["']|["']$/g, '')
  }
  if (flags.stage === undefined) return null
  return {
    stage: stageId(flags.stage),
    result: flags.result ?? '',
    attempt: num(flags.attempt),
    max: num(flags.max),
    budgetSpent: num(flags['budget-spent']),
    budgetCap: num(flags['budget-cap']),
  }
}

// The router prints one JSON line, {"action": ..., "reason": ...}. Take the last such line.
export function parseVerdict(stdout: string): { action: string; reason: string } | null {
  const lines = stdout.split('\n').map(l => l.trim()).filter(Boolean).reverse()
  for (const line of lines) {
    if (!line.startsWith('{')) continue
    try {
      const v = JSON.parse(line) as { action?: unknown; reason?: unknown }
      if (typeof v.action === 'string') return { action: v.action, reason: typeof v.reason === 'string' ? v.reason : '' }
    } catch {
      // not the verdict line
    }
  }
  return null
}

// Finds a run folder named in a command or a path: `runs/PROJ-123/fix.json`, `/repo/runs/PROJ-123`.
// root is the folder that holds runs/ when the text gives an absolute path, else null.
export function runRef(text: string): { key: string; root: string | null } | null {
  const m = text.match(/(^|[\s"'=:(])((\/[^\s"']*?)\/)?runs\/([A-Za-z0-9][\w.-]*)(\/|\s|$|["'])/)
  if (!m) return null
  const key = m[4] ?? ''
  if (key === '' || key === '_inbox' || key.includes('..')) return key === '_inbox' ? { key, root: m[3] ?? null } : null
  return { key, root: m[3] ?? null }
}

const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s)

// The one line a person reads first for a stage: its summary, root cause or what was observed.
export function stageDetail(doc: Record<string, unknown> | undefined): string | null {
  if (!doc) return null
  for (const field of ['summary', 'root_cause', 'observed_quote', 'symptom_quote', 'detail']) {
    const v = doc[field]
    if (typeof v === 'string' && v.trim() !== '') return clip(v.trim(), 280)
  }
  return null
}

export function stageOutcome(id: string, doc: Record<string, unknown> | undefined): string | null {
  if (!doc) return null
  if (id === 'intake') return typeof doc.recipe === 'string' ? 'routed' : 'done'
  for (const field of ['status', 'verdict']) {
    const v = doc[field]
    if (typeof v === 'string') return v
  }
  return 'written'
}

export function buildRun(
  root: string,
  key: string,
  docs: Record<string, Record<string, unknown> | undefined>,
  verdicts: Verdict[],
  now: number,
): RunView {
  const rows: StageRow[] = STAGES.map(s => {
    const doc = s.file ? docs[s.file] : undefined
    const mine = verdicts.filter(v => v.stage === s.id)
    const last = mine[mine.length - 1]
    const outcome = stageOutcome(s.id, doc) ?? (last ? last.result || null : null)
    const attempts = mine.reduce((n, v) => Math.max(n, v.attempt ?? 1), 0)
    return { id: s.id, label: s.label, outcome, tone: toneOf(outcome), attempts, isCurrent: false, detail: stageDetail(doc) }
  })
  const lastVerdict = verdicts[verdicts.length - 1]
  let current = -1
  if (lastVerdict && lastVerdict.action !== 'advance') {
    current = rows.findIndex(r => r.id === lastVerdict.stage)
  } else {
    const lastDone = rows.map(r => r.outcome !== null).lastIndexOf(true)
    current = lastDone + 1 < rows.length ? lastDone + 1 : -1
  }
  const stages = rows.map((r, i) => ({ ...r, isCurrent: i === current }))
  const intake = docs['intake.json']
  const context = docs['context.json']
  return {
    root,
    key,
    recipe: typeof intake?.recipe === 'string' ? intake.recipe : null,
    tier: typeof context?.tier === 'string' ? context.tier : null,
    stages,
    verdicts,
    updatedAt: now,
  }
}

const money = (n: number) => `$${n.toFixed(2)}`

export function spendText(verdicts: Verdict[]): string | null {
  const last = [...verdicts].reverse().find(v => v.budgetSpent !== null)
  if (!last || last.budgetSpent === null) return null
  return last.budgetCap !== null ? `${money(last.budgetSpent)} of ${money(last.budgetCap)}` : money(last.budgetSpent)
}

// One line for the band above the prompt.
export function bandText(run: RunView | null, open: Question[]): string | null {
  const parts: string[] = []
  if (run) {
    const recipe = run.recipe ? run.recipe.replace(/^dev-loop-|^qa-loop-/, '') : 'run'
    parts.push(`${recipe} ${run.key}`)
    const cur = run.stages.find(s => s.isCurrent)
    if (cur) parts.push(cur.attempts > 0 ? `${cur.label} ${cur.attempts}/${run.verdicts.at(-1)?.max ?? 3}` : cur.label)
    const last = run.verdicts.at(-1)
    if (last) parts.push(describeAction(last.action))
    const spend = spendText(run.verdicts)
    if (spend) parts.push(spend)
  }
  if (open.length > 0) {
    const blocking = open.filter(q => q.kind === 'blocking').length
    parts.push(`${open.length} question${open.length === 1 ? '' : 's'}${blocking > 0 ? ` (${blocking} blocking)` : ''}`)
  }
  return parts.length > 0 ? parts.join(' · ') : null
}

// ---- Open questions ------------------------------------------------------------------------
// runs/<KEY>/questions.jsonl: one question per line, appended by a loop (or by the ask_human tool).
// runs/<KEY>/answers.jsonl: one answer per line, appended by the pane. Two files, so the writer of
// one never rewrites the other's lines.

export function parseJsonl(text: string): unknown[] {
  const out: unknown[] = []
  for (const line of text.split('\n')) {
    const t = line.trim()
    if (t === '') continue
    try {
      out.push(JSON.parse(t))
    } catch {
      // a half-written last line, or junk: skip it
    }
  }
  return out
}

const str = (v: unknown) => (typeof v === 'string' && v.trim() !== '' ? v.trim() : null)

export function toQuestion(raw: unknown, key: string): Question | null {
  if (typeof raw !== 'object' || raw === null) return null
  const r = raw as Record<string, unknown>
  const id = str(r.id)
  const question = str(r.question)
  const kind = r.kind === 'blocking' || r.kind === 'non-blocking' ? (r.kind as QuestionKind) : null
  if (!id || !question || !kind) return null
  const options = Array.isArray(r.options) ? r.options.map(str).filter((o): o is string => o !== null).slice(0, 4) : []
  return {
    id,
    key: str(r.key) ?? key,
    from: str(r.from) ?? 'a loop',
    kind,
    question: clip(question, 600),
    options,
    default: str(r.default),
    context: r.context === undefined ? null : clip(str(r.context) ?? '', 600) || null,
    askedAt: str(r.asked_at) ?? '',
  }
}

export function toAnswer(raw: unknown, key: string): Answer | null {
  if (typeof raw !== 'object' || raw === null) return null
  const r = raw as Record<string, unknown>
  const id = str(r.id)
  const answer = str(r.answer)
  if (!id || !answer) return null
  return { id, key: str(r.key) ?? key, answer, answeredAt: str(r.answered_at) ?? '' }
}

// Unanswered questions, blocking first, oldest first within each kind.
export function openQuestions(questions: Question[], answers: Answer[]): Question[] {
  const answered = new Set(answers.map(a => `${a.key}/${a.id}`))
  const seen = new Set<string>()
  const open = questions.filter(q => {
    const id = `${q.key}/${q.id}`
    if (answered.has(id) || seen.has(id)) return false
    seen.add(id)
    return true
  })
  return open.sort((a, b) => (a.kind === b.kind ? a.askedAt.localeCompare(b.askedAt) : a.kind === 'blocking' ? -1 : 1))
}

export type AskInput = {
  question?: unknown
  kind?: unknown
  options?: unknown
  default?: unknown
  context?: unknown
  key?: unknown
  from?: unknown
}

// Checks what a loop handed the ask_human tool. A non-blocking question must say what the loop
// does if nobody answers, or it is not really non-blocking.
export function checkAsk(input: AskInput): { error: string } | Omit<Question, 'id' | 'askedAt' | 'key'> & { key: string | null } {
  const question = str(input.question)
  if (!question) return { error: 'question is required.' }
  if (input.kind !== 'blocking' && input.kind !== 'non-blocking') return { error: 'kind must be "blocking" or "non-blocking".' }
  const options = Array.isArray(input.options) ? input.options.map(str).filter((o): o is string => o !== null) : []
  if (options.length > 4) return { error: 'give at most 4 options.' }
  const dflt = str(input.default)
  if (input.kind === 'non-blocking' && !dflt) return { error: 'a non-blocking question needs a default: what you will do if nobody answers.' }
  const key = str(input.key)
  if (key && !/^[A-Za-z0-9][\w.-]*$/.test(key)) return { error: 'key must be a run key such as PROJ-123.' }
  return {
    question,
    kind: input.kind,
    options,
    default: dflt,
    context: str(input.context),
    from: str(input.from) ?? 'claude',
    key,
  }
}

export const questionId = (now: number, salt: number) => `q-${now.toString(36)}-${salt.toString(36)}`

// What the model is told, on the next prompt, about answers the person gave in the pane.
export function answersContext(answers: Answer[], questions: Question[]): string | null {
  if (answers.length === 0) return null
  const byId = new Map(questions.map(q => [`${q.key}/${q.id}`, q]))
  const lines = answers.map(a => {
    const q = byId.get(`${a.key}/${a.id}`)
    return `- ${a.key} ${a.id}${q ? ` (${q.kind}) "${q.question}"` : ''}: ${a.answer}`
  })
  return [
    'The user answered open questions in the recipe-run pane (also saved to runs/<KEY>/answers.jsonl):',
    ...lines,
    'A blocking question that stopped a run can now be resumed with this answer.',
  ].join('\n')
}
