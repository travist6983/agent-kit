import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, Register, RenderChildren } from 'claude-code'

import type { Answer, Question, RunView, Tone, Verdict } from '../types'
import {
  ALL_STAGE_FILES,
  answersContext,
  bandText,
  buildRun,
  checkAsk,
  describeAction,
  openQuestions,
  parseJsonl,
  parseRouterCall,
  parseVerdict,
  questionId,
  runRef,
  spendText,
  toAnswer,
  toQuestion,
} from './run'
import { C, PILL, SECTION_COLOR, SECTION_TITLE, barSvg, burnSvg } from './chrome'
import type { Pill } from './chrome'

const PANE = 'recipe-run'
const INBOX = '_inbox' // where a question with no run key goes
const RECENT_MS = 2 * 60 * 60_000 // the band shows a run touched in the last two hours
const MAX_RUN_DIRS = 25
const MAX_VERDICTS = 20

const runAtom = atom({ plugin: 'recipe-run', key: 'run' } as const, null)
const targetAtom = atom({ plugin: 'recipe-run', key: 'target' } as const, null)
const pinnedAtom = atom({ plugin: 'recipe-run', key: 'pinned' } as const, null)
const openAtom = atom({ plugin: 'recipe-run', key: 'open' } as const, [])
const tabAtom = atom({ plugin: 'recipe-run', key: 'tab' } as const, 'run')
const selectedAtom = atom({ plugin: 'recipe-run', key: 'selected' } as const, null)
const deliveredAtom = atom({ plugin: 'recipe-run', key: 'delivered' } as const, [])
const pendingAtom = atom({ plugin: 'recipe-run', key: 'pending' } as const, [])

const verdictKey = (root: string, key: string) => `verdicts:${root}:${key}`
const notifiedKey = (root: string) => `notified:${root}`
const lastRunKey = (root: string) => `last-run:${root}`

async function gitRoot($: EngineInterface) {
  const cwd = await $.session.cwd()
  const top = await $.process.run(['git', 'rev-parse', '--show-toplevel'], { cwd })
  return top.exitCode === 0 ? top.stdout.trim() : cwd
}

async function readJson($: EngineInterface, path: string): Promise<Record<string, unknown> | undefined> {
  try {
    const v = JSON.parse(await $.fs.read(path)) as unknown
    return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined
  } catch {
    return undefined
  }
}

const readText = ($: EngineInterface, path: string) => $.fs.read(path).catch(() => '')

// Appends one line with O_APPEND, so a loop writing the same file at the same moment loses nothing.
async function appendLine($: EngineInterface, path: string, line: string) {
  const r = await $.process.run(['sh', '-c', 'mkdir -p "$(dirname "$2")" && printf "%s\\n" "$1" >> "$2"', 'append', line, path])
  if (r.exitCode !== 0) throw new Error(r.stderr.trim() || `could not write ${path}`)
}

// Run folders under <root>/runs, newest first. Directory mtimes come from stat, since list gives 0.
async function runDirs($: EngineInterface, root: string) {
  const entries = await $.fs.list(`${root}/runs`).catch(() => [])
  const dirs = entries.filter(d => d.kind === 'dir' && !d.name.startsWith('.'))
  const stamped = await Promise.all(
    dirs.map(async d => ({ name: d.name, mtimeMs: (await $.fs.stat(`${root}/runs/${d.name}`).catch(() => undefined))?.mtimeMs ?? 0 })),
  )
  return stamped.sort((a, b) => b.mtimeMs - a.mtimeMs).slice(0, MAX_RUN_DIRS)
}

async function loadRun($: EngineInterface, root: string, key: string): Promise<RunView> {
  const dir = `${root}/runs/${key}`
  const docs: Record<string, Record<string, unknown> | undefined> = {}
  let touched = 0
  for (const s of [...ALL_STAGE_FILES, 'context.json']) {
    docs[s] = await readJson($, `${dir}/${s}`)
    if (docs[s]) touched = Math.max(touched, (await $.fs.stat(`${dir}/${s}`).catch(() => undefined))?.mtimeMs ?? 0)
  }
  const verdicts = ((await $.store.get(verdictKey(root, key))) ?? []) as Verdict[]
  for (const v of verdicts) touched = Math.max(touched, v.at)
  return buildRun(root, key, docs, verdicts, touched)
}

async function loadQuestions($: EngineInterface, root: string, keys: string[]) {
  const questions: Question[] = []
  const answers: Answer[] = []
  for (const key of [...keys, INBOX]) {
    const dir = `${root}/runs/${key}`
    for (const raw of parseJsonl(await readText($, `${dir}/questions.jsonl`))) {
      const q = toQuestion(raw, key)
      if (q) questions.push(q)
    }
    for (const raw of parseJsonl(await readText($, `${dir}/answers.jsonl`))) {
      const a = toAnswer(raw, key)
      if (a) answers.push(a)
    }
  }
  return { questions, answers }
}

async function refresh($: EngineInterface) {
  const target = await read($, targetAtom)
  const pinned = await read($, pinnedAtom)
  const root = target?.root ?? (await gitRoot($))
  const dirs = await runDirs($, root)
  const names = dirs.map(d => d.name).filter(n => n !== INBOX)
  const key = pinned ?? target?.key ?? names[0] ?? null
  const run = key ? await loadRun($, root, key) : null
  await update($, runAtom, () => run)

  const { questions, answers } = await loadQuestions($, root, names)
  const open = openQuestions(questions, answers)
  await update($, openAtom, () => open)

  // One native notification per new blocking question, remembered across sessions.
  const notified = new Set(((await $.store.get(notifiedKey(root))) ?? []) as string[])
  const fresh = open.filter(q => q.kind === 'blocking' && !notified.has(`${q.key}/${q.id}`))
  if (fresh.length > 0) {
    for (const q of fresh) notified.add(`${q.key}/${q.id}`)
    await $.store.set(notifiedKey(root), [...notified].slice(-200))
    const first = fresh[0]
    if (first) {
      const more = fresh.length > 1 ? ` (+${fresh.length - 1} more)` : ''
      void $.ui.notify(`${first.key}: ${first.question}${more}`, { title: 'A loop is waiting on you' }).catch(() => undefined)
    }
  }
  return { root, run, open, questions }
}

// ---- recording what the loops do ----------------------------------------------------------

async function recordVerdict($: EngineInterface, root: string, key: string, v: Verdict) {
  const k = verdictKey(root, key)
  const list = ((await $.store.get(k)) ?? []) as Verdict[]
  await $.store.set(k, [...list, v].slice(-MAX_VERDICTS))
  await $.store.set(lastRunKey(root), key)
}

// The run key the model is working on: a runs/<KEY>/ path in the command, the pinned run, the
// run the last tool call touched, the last run a verdict went to, else the newest run folder.
async function keyFor($: EngineInterface, text: string, root: string) {
  const ref = runRef(text)
  if (ref && ref.key !== INBOX) return { key: ref.key, root: ref.root ?? root }
  const pinned = await read($, pinnedAtom)
  if (pinned) return { key: pinned, root }
  const target = await read($, targetAtom)
  if (target) return target
  const last = (await $.store.get(lastRunKey(root))) as string | undefined
  if (last) return { key: last, root }
  const newest = (await runDirs($, root)).find(d => d.name !== INBOX)
  return newest ? { key: newest.name, root } : null
}

// ---- answering -----------------------------------------------------------------------------

async function answer($: EngineInterface, q: Question, text: string) {
  const value = text.trim()
  if (value === '') return
  const { root } = await refresh($)
  const a: Answer = { id: q.id, key: q.key, answer: value, answeredAt: new Date(await $.clock.now()).toISOString() }
  try {
    await appendLine($, `${root}/runs/${q.key}/answers.jsonl`, JSON.stringify({ id: a.id, key: a.key, answer: a.answer, answered_at: a.answeredAt, by: 'human' }))
  } catch (err) {
    $.ui.toast(`Could not save the answer: ${String(err)}`, { timeoutMs: 8000 })
    return
  }
  await update($, pendingAtom, list => [...list, a])
  await refresh($)
  $.ui.toast(`Answered ${q.key}. Claude reads it with your next message.`, { timeoutMs: 6000 })
}

// ---- drawing -------------------------------------------------------------------------------

const TONE_COLOR: Record<Tone, string | undefined> = { good: 'green', bad: 'red', warn: 'yellow', none: undefined }
const TONE_MARK: Record<Tone, string> = { good: '●', bad: '●', warn: '●', none: '○' }
const TONE_GUI: Record<Tone, string | undefined> = { good: C.green, bad: '#E58A8A', warn: C.amber, none: undefined }

function clockTime(at: number) {
  try {
    return new Date(at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
  } catch {
    return ''
  }
}

function verdictLine(v: Verdict) {
  const tries = v.attempt !== null ? ` ${v.attempt}/${v.max ?? 3}` : ''
  return `${v.stage}${tries} ${v.result}: ${describeAction(v.action)}${v.reason ? `. ${v.reason}` : ''}`
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'recipe',
      description: 'Recipe run pane: /recipe, /recipe questions, /recipe <KEY> to pin a run, /recipe auto',
    })
    await $.tool.register({
      name: 'ask_human',
      description: [
        'Leave a question for the human running this recipe or loop.',
        'kind "blocking": you cannot go on safely without the answer (a risky change, a missing fact, a judgement call the recipe reserves for a person). In an interactive session the user is asked now; otherwise the question is saved, and you must stop this stage and escalate rather than guess.',
        'kind "non-blocking": you can go on with a stated default; the question is saved for later and you carry on with the default.',
        'Questions land in runs/<KEY>/questions.jsonl, where the recipe-run pane lists them and the answer is written to runs/<KEY>/answers.jsonl.',
      ].join(' '),
      inputSchema: {
        type: 'object',
        properties: {
          question: { type: 'string', description: 'One clear question, ending in a question mark.' },
          kind: { type: 'string', enum: ['blocking', 'non-blocking'] },
          options: { type: 'array', items: { type: 'string' }, maxItems: 4, description: 'Up to 4 short answers to pick from.' },
          default: { type: 'string', description: 'Required for non-blocking: what you will do if nobody answers.' },
          context: { type: 'string', description: 'One or two sentences the human needs to answer well.' },
          key: { type: 'string', description: 'The run key, e.g. PROJ-123. Leave out when there is no run.' },
          from: { type: 'string', description: 'Which stage, agent or loop is asking, e.g. "fix" or "qa-dream".' },
        },
        required: ['question', 'kind'],
      },
      isDeferred: false,
    })
    await refresh($).catch(() => undefined)
    $.clock.every(20_000, () => void refresh($).catch(() => undefined))
    return next(e)
  })

  on('command.run', { command: 'recipe' }, async ($, e) => {
    const arg = (e.args ?? '').trim()
    if (arg === 'auto') {
      await update($, pinnedAtom, () => null)
      await refresh($)
      return { text: 'Following the newest run again.' }
    }
    if (arg === 'questions') {
      await update($, tabAtom, () => 'questions')
    } else if (arg !== '') {
      if (!/^[A-Za-z0-9][\w.-]*$/.test(arg)) return { text: `"${arg}" is not a run key. Try /recipe PROJ-123.` }
      await update($, pinnedAtom, () => arg)
      await update($, tabAtom, () => 'run')
    }
    const { run, open } = await refresh($)
    await $.ui.open({ id: PANE, title: 'Recipe run' })
    if (arg !== '' && arg !== 'questions' && run && run.stages.every(s => s.outcome === null)) {
      return { text: `Pinned ${arg}, but runs/${arg}/ has no stage files yet.` }
    }
    return { text: bandText(run, open) ?? 'No recipe run found under runs/ yet.' }
  })

  // Watch every tool call for the router and for runs/<KEY>/ paths. Never block the call.
  on('tool.call', async ($, e, next) => {
    const input = e as unknown as { tool: string; command?: unknown; file_path?: unknown }
    const command = input.tool === 'Bash' && typeof input.command === 'string' ? input.command : ''
    const path = typeof input.file_path === 'string' ? input.file_path : ''
    const call = command ? parseRouterCall(command) : null
    const ref = runRef(command || path)
    const res = await next(e)
    if (!call && !ref) return res
    try {
      const root = (await read($, targetAtom))?.root ?? (await gitRoot($))
      if (ref && ref.key !== INBOX) await update($, targetAtom, () => ({ root: ref.root ?? root, key: ref.key }))
      if (call && res.deny === undefined) {
        const r = res.result as { stdout?: unknown } | undefined
        const out = typeof r?.stdout === 'string' ? r.stdout : (res.text ?? '')
        const verdict = parseVerdict(out)
        const at = await keyFor($, command, root)
        if (verdict && at) {
          await recordVerdict($, at.root, at.key, { ...call, ...verdict, at: await $.clock.now() })
          if (verdict.action === 'escalate' || verdict.action === 'rollback' || verdict.action === 'stop-budget') {
            $.ui.toast(`${at.key}: ${describeAction(verdict.action)}. /recipe shows why.`, { timeoutMs: 10_000 })
          }
        }
      }
      await refresh($)
    } catch {
      // the pane is a viewer: a failure here never touches the tool's own result
    }
    return res
  }).catch(($, e, next) => next(e))

  on('tool.call', { tool: 'mcp__recipe-run__ask_human' }, async ($, e) => {
    const checked = checkAsk(e as Record<string, unknown>)
    if ('error' in checked) return { result: `Not recorded: ${checked.error}` }
    const { root } = await refresh($)
    const at = checked.key ? { key: checked.key, root } : await keyFor($, '', root)
    const key = at?.key ?? INBOX
    const now = await $.clock.now()
    const q: Question = {
      ...checked,
      key,
      id: questionId(now, Math.floor(Math.random() * 1296)),
      askedAt: new Date(now).toISOString(),
    }
    const file = `${at?.root ?? root}/runs/${key}/questions.jsonl`
    await appendLine(
      $,
      file,
      JSON.stringify({ id: q.id, key, from: q.from, kind: q.kind, question: q.question, options: q.options, default: q.default, context: q.context, asked_at: q.askedAt }),
    )
    const where = `runs/${key}/questions.jsonl (id ${q.id})`

    if (q.kind === 'non-blocking') {
      await refresh($)
      return { result: `Saved to ${where}. Carry on with your default: ${q.default}. If the human answers later, the answer arrives with their next message.` }
    }

    // Blocking: ask now when someone is there. $.ui.ask rejects in a -p run or when dismissed.
    const choices = q.options.length >= 2 ? q.options : undefined
    const reply = await $.ui.ask(q.question.endsWith('?') ? q.question : `${q.question}?`, choices).catch(() => null)
    if (reply !== null && reply.trim() !== '') {
      const a: Answer = { id: q.id, key, answer: reply.trim(), answeredAt: new Date(await $.clock.now()).toISOString() }
      await appendLine($, `${at?.root ?? root}/runs/${key}/answers.jsonl`, JSON.stringify({ id: a.id, key, answer: a.answer, answered_at: a.answeredAt, by: 'human' }))
      await update($, deliveredAtom, list => [...list, `${key}/${q.id}`])
      await refresh($)
      return { result: `The human answered: ${a.answer}` }
    }
    await refresh($)
    return {
      result: `Nobody answered now. Saved as blocking in ${where}. Stop this stage, do not guess: escalate with the question as the reason. The human answers in the recipe-run pane, and the run resumes from that answer.`,
    }
  })

  // Answers given in the pane reach the model with the person's next message.
  on('prompt.submit', async ($, e, next) => {
    const pending = await read($, pendingAtom)
    const delivered = new Set(await read($, deliveredAtom))
    const todo = pending.filter(a => !delivered.has(`${a.key}/${a.id}`))
    if (todo.length === 0) return next(e)
    const { questions } = await refresh($)
    const text = answersContext(todo, questions)
    await update($, pendingAtom, () => [])
    await update($, deliveredAtom, list => [...list, ...todo.map(a => `${a.key}/${a.id}`)].slice(-200))
    return text ? next({ ...e, context: [...(e.context ?? []), text] }) : next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const run = await read($, runAtom)
    const open = await read($, openAtom)
    const now = await $.clock.now()
    const recent = run !== null && now - run.updatedAt < RECENT_MS
    const text = bandText(recent ? run : null, open)
    if (!text) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    const blocking = open.some(q => q.kind === 'blocking')
    return (
      <Box>
        <Text color={blocking ? 'red' : undefined} dimColor={!blocking}>
          {text}
        </Text>
      </Box>
    )
  })

  // ---- the pane: header, stat tiles, then sections. Blocking questions sit in "Needs a look"
  // (amber), the run in "Live" (blue) while it moves, or dim "Last run" once it has gone quiet.

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e)
    const { Box, Button, Input, Text } = els
    // Desktop, VS Code and mobile draw SVG and the palette; the terminal keeps its own colours.
    const isGui = e.surface !== 'terminal'
    const Svg = isGui ? ((els as Partial<Elements['desktop']>).Svg ?? undefined) : undefined
    const gui = <T,>(v: T) => (isGui ? v : undefined)
    const tight = isGui ? ('xs' as const) : 0
    const run = await read($, runAtom)
    const open = await read($, openAtom)
    const tab = await read($, tabAtom)
    const pinned = await read($, pinnedAtom)
    const selected = await read($, selectedAtom)
    const now = await $.clock.now()
    const blocking = open.filter(q => q.kind === 'blocking').length
    const cols = Math.max(30, e.props.bodyColumns ?? 44)
    const wide = Math.max(8, Math.min(24, cols - 14))
    const toneColor = (t: Tone) => (isGui ? TONE_GUI[t] : TONE_COLOR[t])

    // ---- small parts

    const pill = (p: Pill) => (
      <Text bold size="caption" color={PILL[p.tone].fg} backgroundColor={gui(PILL[p.tone].bg)}>
        {` ${p.text.toUpperCase()} `}
      </Text>
    )

    const hairline = (k: string) => (Svg ? <Svg key={k} source={barSvg(C.divider, 1)} alt="" height={1} /> : null)

    const heading = (k: string, title: string, color: string | undefined, count: number | null) => (
      <Text key={k} bold color={color} dimColor={color === undefined}>
        {title}
        {count !== null && <Text dimColor> {count}</Text>}
      </Text>
    )

    // A card: round border, a coloured bar along the top on GUI, then its rows.
    const card = (k: string, color: string, children: RenderChildren) => (
      <Box
        key={k}
        flexDirection="column"
        borderStyle="round"
        borderColor={isGui ? C.line : color}
        backgroundColor={gui(C.card)}
        paddingX={1}
        gap={tight}
      >
        {Svg && (
          <Box position="absolute" top={0} left={1} right={1}>
            <Svg source={barSvg(color, 4, 2)} alt="" height={4} />
          </Box>
        )}
        {children}
      </Box>
    )

    const header = (
      <Box key="head" flexDirection="row" justifyContent="space-between" alignItems="center">
        <Text>
          <Text bold size="heading">
            Recipe run
          </Text>
          <Text dimColor> {pinned ? `pinned to ${pinned}` : 'following the newest run'}</Text>
        </Text>
        <Box flexDirection="row" gap={1} alignItems="center">
          {run && run.updatedAt > 0 && (
            <Text dimColor size="footnote">
              ↻ {clockTime(run.updatedAt)}
            </Text>
          )}
          <Button key="refresh" variant="secondary" hotkey="r" label="Refresh" onPress={() => void refresh($)} />
          <Button key="close" role="dismiss" plain dimColor label="✕" onPress={() => $.ui.close({ id: PANE })} />
        </Box>
      </Box>
    )

    const tabs = (
      <Box key="tabs" flexDirection="row" gap={1}>
        <Button key="tab-run" label="Run" variant={tab === 'run' ? 'primary' : 'secondary'} onPress={() => void update($, tabAtom, () => 'run')} />
        <Button
          key="tab-questions"
          variant={tab === 'questions' ? 'primary' : 'secondary'}
          onPress={() => void update($, tabAtom, () => 'questions')}
        >
          <Text color={blocking > 0 && tab !== 'questions' ? C.amber : undefined}>
            {`Questions (${open.length}${blocking > 0 ? `, ${blocking} blocking` : ''})`}
          </Text>
        </Button>
      </Box>
    )

    const frame = (...body: RenderChildren[]) => (
      <Box flexDirection="column" gap={1} backgroundColor={gui(C.pane)} paddingX={isGui ? 2 : 0} paddingY={isGui ? 1 : 0}>
        {header}
        {tabs}
        {body}
      </Box>
    )

    // ---- the Questions tab

    const questionCard = (q: Question) => {
      const isBlocking = q.kind === 'blocking'
      return card(
        `q-${q.key}-${q.id}`,
        isBlocking ? SECTION_COLOR.look : C.line,
        <Box flexDirection="column" gap={tight}>
          <Box flexDirection="row" alignItems="center" gap={1}>
            {pill({ text: isBlocking ? 'Blocking' : 'Can wait', tone: isBlocking ? 'warn' : 'live' })}
            <Text color={gui(C.dim)} dimColor={!isGui} wrap="truncate-end">
              {q.key} · from {q.from}
            </Text>
          </Box>
          <Text bold color={gui(C.name)}>
            {q.question}
          </Text>
          {q.context && (
            <Text color={gui(C.dim)} dimColor={!isGui}>
              {q.context}
            </Text>
          )}
          {q.default && (
            <Box backgroundColor={gui(C.inset)} paddingX={1}>
              <Text dimColor>If nobody answers: {q.default}</Text>
            </Box>
          )}
          {q.options.length > 0 && (
            <Box flexDirection="row" flexWrap="wrap" gap={1}>
              {q.options.map((o, i) => (
                <Button key={`a-${q.key}-${q.id}-${i}`} variant="secondary" label={o} onPress={() => void answer($, q, o)} />
              ))}
            </Box>
          )}
          <Input key={`in-${q.key}-${q.id}`} placeholder="Type an answer and press Enter" onSubmit={value => void answer($, q, value)} />
        </Box>,
      )
    }

    if (tab === 'questions') {
      const must = open.filter(q => q.kind === 'blocking')
      const can = open.filter(q => q.kind !== 'blocking')
      return frame(
        open.length === 0 && (
          <Text key="no-q" dimColor>
            No open questions. Loops leave them with the ask_human tool or in runs/KEY/questions.jsonl.
          </Text>
        ),
        must.length > 0 && (
          <Box key="sec-look" flexDirection="column" gap={isGui ? 'sm' : 0}>
            {heading('h-look', SECTION_TITLE.look, SECTION_COLOR.look, must.length)}
            {must.map(questionCard)}
          </Box>
        ),
        can.length > 0 && (
          <Box key="sec-can" flexDirection="column" gap={isGui ? 'sm' : 0}>
            {heading('h-can', 'Can wait', undefined, can.length)}
            {can.map(questionCard)}
          </Box>
        ),
      )
    }

    if (!run) {
      return frame(
        <Text key="no-run" dimColor>
          No run under runs/ yet. Start a recipe, or /recipe KEY to follow one.
        </Text>,
      )
    }

    // ---- the Run tab

    const cur = run.stages.find(s => s.isCurrent) ?? null
    const sel = run.stages.find(s => s.id === selected) ?? cur
    const last = run.verdicts.at(-1) ?? null
    const max = last?.max ?? 3
    const budget = [...run.verdicts].reverse().find(v => v.budgetSpent !== null) ?? null
    const spend = spendText(run.verdicts)
    const pct = budget?.budgetSpent != null && budget.budgetCap ? Math.round((budget.budgetSpent / budget.budgetCap) * 100) : null
    const isHot = pct !== null && pct >= 80
    const isLive = now - run.updatedAt < RECENT_MS

    const tile = (k: string, label: string, value: RenderChildren, below?: RenderChildren) => (
      <Box key={k} flexDirection="column" width="33%" flexGrow={1} flexShrink={1} borderStyle="round" borderColor={C.line} backgroundColor={gui(C.card)} paddingX={1}>
        <Text dimColor size="caption">
          {label}
        </Text>
        {value}
        {below}
      </Box>
    )

    const meter = (k: string, n: number, frac: number, color: string) => {
      const lit = Math.max(0, Math.min(n, Math.round((Number.isFinite(frac) ? frac : 0) * n)))
      return (
        <Box key={k} flexDirection="row">
          <Text color={color}>{'█'.repeat(lit)}</Text>
          <Text dimColor>{'░'.repeat(n - lit)}</Text>
        </Box>
      )
    }

    const strip = (
      <Box key="strip" flexDirection="row" gap={1}>
        {tile(
          'tile-stage',
          'Stage',
          <Text bold size="title" color={cur ? toneColor(cur.tone) ?? gui(C.name) : undefined} dimColor={!cur} wrap="truncate-end">
            {cur ? cur.label : 'done'}
          </Text>,
        )}
        {tile(
          'tile-attempt',
          'Attempt',
          <Text bold size="title" tabular color={cur && cur.attempts > 1 ? C.amber : undefined} dimColor={!cur || cur.attempts === 0}>
            {cur && cur.attempts > 0 ? `${cur.attempts}/${max}` : '–'}
          </Text>,
        )}
        {tile(
          'tile-spend',
          'Spend vs cap',
          <Text tabular>
            <Text bold size="title" color={budget ? C.orange : undefined} dimColor={!budget}>
              {budget?.budgetSpent != null ? `$${budget.budgetSpent.toFixed(2)}` : 'n/a'}
            </Text>
            {budget?.budgetCap != null && <Text dimColor size="footnote"> / ${budget.budgetCap.toFixed(2)}</Text>}
          </Text>,
          pct !== null &&
            (Svg ? (
              <Svg key="spend-bar" source={burnSvg(pct, isHot, 16)} alt={`${pct}% of the budget cap spent`} />
            ) : (
              meter('spend-bar', Math.max(6, Math.floor(wide / 2)), pct / 100, isHot ? C.orange : C.tan)
            )),
        )}
      </Box>
    )

    // Blocking questions first: one card with the main action that opens the Questions tab.
    const firstAsk = open.find(q => q.kind === 'blocking') ?? open[0]
    const asks =
      open.length > 0 && firstAsk ? (
        <Box key="sec-asks" flexDirection="column" gap={isGui ? 'sm' : 0}>
          {blocking > 0 ? heading('h-asks', SECTION_TITLE.look, SECTION_COLOR.look, blocking) : heading('h-asks', 'Can wait', undefined, open.length)}
          {card(
            'card-asks',
            blocking > 0 ? SECTION_COLOR.look : C.line,
            <Box flexDirection="column" gap={tight}>
              <Box flexDirection="row" justifyContent="space-between" alignItems="center" gap={1}>
                <Box flexDirection="row" alignItems="center" gap={1} flexShrink={1}>
                  {pill({ text: blocking > 0 ? 'Waiting on you' : 'Can wait', tone: blocking > 0 ? 'warn' : 'live' })}
                  <Text bold color={gui(C.name)} wrap="truncate-end">
                    {open.length} open question{open.length === 1 ? '' : 's'}
                  </Text>
                </Box>
                <Button key="answer-now" variant={blocking > 0 ? 'primary' : 'secondary'} label="Answer" onPress={() => void update($, tabAtom, () => 'questions')} />
              </Box>
              <Text color={gui(C.dim)} dimColor={!isGui} wrap="truncate-end">
                {firstAsk.question} · {firstAsk.key}
              </Text>
            </Box>,
          )}
        </Box>
      ) : null

    const verdictTone = (action: string): Pill['tone'] => (action === 'advance' ? 'live' : action.startsWith('re-') ? 'attn' : 'warn')
    const runColor = isLive ? SECTION_COLOR.live : C.missing
    const status = [cur ? `${cur.label}${cur.attempts > 0 ? ` ${cur.attempts}/${max}` : ''}` : 'every stage written', last ? describeAction(last.action) : null]
      .filter(Boolean)
      .join(' · ')

    const ladder = (
      <Box key="ladder" flexDirection="column">
        {run.stages.map(s => (
          <Box key={`st-${s.id}`} flexDirection="row" justifyContent="space-between" alignItems="center" gap={1} backgroundColor={gui(sel?.id === s.id ? C.inset : undefined)}>
            <Box flexDirection="row" alignItems="center">
              <Text color={toneColor(s.tone)}>{s.isCurrent ? '▸' : TONE_MARK[s.tone]} </Text>
              <Button
                key={`stage-${s.id}`}
                plain
                dimColor={s.outcome === null && !s.isCurrent}
                label={s.label}
                onPress={() => void update($, selectedAtom, () => s.id)}
              />
            </Box>
            <Text dimColor={s.tone !== 'bad' && s.tone !== 'warn'} color={s.tone === 'bad' || s.tone === 'warn' ? toneColor(s.tone) : undefined} wrap="truncate-end">
              {s.outcome ?? ''}
              {s.attempts > 1 ? ` · try ${s.attempts}` : ''}
            </Text>
          </Box>
        ))}
      </Box>
    )

    const detail = sel?.detail ? (
      <Box key="detail" flexDirection="column" backgroundColor={gui(C.inset)} paddingX={1}>
        <Text bold>{sel.label}</Text>
        <Text dimColor>{sel.detail}</Text>
      </Box>
    ) : null

    const router =
      run.verdicts.length > 0 ? (
        <Box key="router" flexDirection="column" gap={tight}>
          <Text bold>Router's last calls</Text>
          {run.verdicts.slice(-6).map((v, i) => (
            <Box key={`v-${i}-${v.at}`} flexDirection="row" alignItems="center" gap={1} backgroundColor={gui(C.inset)} paddingX={isGui ? 1 : 0}>
              {pill({ text: v.action, tone: verdictTone(v.action) })}
              <Text color={v.action === 'advance' ? undefined : PILL[verdictTone(v.action)].fg} wrap="wrap">
                {verdictLine(v)}
              </Text>
            </Box>
          ))}
        </Box>
      ) : null

    const spendRow = spend ? (
      <Box key="spend" flexDirection="column">
        <Text dimColor>Spend: {spend}</Text>
        {pct !== null && !Svg && meter('spend-meter', wide, pct / 100, isHot ? C.orange : C.tan)}
      </Box>
    ) : null

    const runCard = card(
      'card-run',
      runColor,
      <Box flexDirection="column" gap={tight}>
        <Box flexDirection="row" justifyContent="space-between" alignItems="center" gap={1}>
          <Box flexDirection="row" alignItems="center" gap={1} flexShrink={1}>
            {last && pill({ text: last.action, tone: verdictTone(last.action) })}
            <Text bold color={gui(C.name)} wrap="truncate-end">
              {run.key}
              {run.recipe ? ` · ${run.recipe}` : ''}
              {run.tier ? ` · ${run.tier}` : ''}
              {pinned ? ' · pinned' : ''}
            </Text>
          </Box>
          {budget?.budgetSpent != null && (
            <Text bold tabular color={isHot ? C.amber : undefined}>
              ${budget.budgetSpent.toFixed(2)}
            </Text>
          )}
        </Box>
        <Text color={gui(C.dim)} dimColor={!isGui} wrap="truncate-end">
          {status}
        </Text>
        <Box key="open-run" flexDirection="column" gap={tight} marginTop={isGui ? 0 : 1}>
          {hairline('hr-run')}
          {ladder}
          {detail}
          {router}
          {spendRow}
        </Box>
      </Box>,
    )

    return frame(
      strip,
      asks,
      <Box key="sec-run" flexDirection="column" gap={isGui ? 'sm' : 0}>
        {isLive ? heading('h-run', SECTION_TITLE.live, SECTION_COLOR.live, null) : heading('h-run', 'Last run', undefined, null)}
        {runCard}
      </Box>,
    )
  })
}

