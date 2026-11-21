import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Answer, Question, RunView, Tone, Verdict } from '../types'
import {
  STAGES,
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
  for (const s of [...STAGES.map(st => st.file), 'context.json']) {
    if (!s) continue
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

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Input, Text } = $.ui.resolve(e)
    const run = await read($, runAtom)
    const open = await read($, openAtom)
    const tab = await read($, tabAtom)
    const pinned = await read($, pinnedAtom)
    const selected = await read($, selectedAtom)
    const blocking = open.filter(q => q.kind === 'blocking').length

    const header = (
      <Box key="tabs">
        <Button key="tab-run" label="Run" variant={tab === 'run' ? 'primary' : undefined} onPress={() => void update($, tabAtom, () => 'run')} />
        <Text> </Text>
        <Button
          key="tab-questions"
          label={`Questions (${open.length}${blocking > 0 ? `, ${blocking} blocking` : ''})`}
          variant={tab === 'questions' ? 'primary' : undefined}
          onPress={() => void update($, tabAtom, () => 'questions')}
        />
        <Text> </Text>
        <Button key="refresh" label="Refresh" plain onPress={() => void refresh($)} />
      </Box>
    )

    if (tab === 'questions') {
      return (
        <Box flexDirection="column">
          {header}
          {open.length === 0 && <Text dimColor>No open questions. Loops leave them with the ask_human tool or in runs/KEY/questions.jsonl.</Text>}
          {open.map(q => (
            <Box key={`q-${q.key}-${q.id}`} flexDirection="column" marginTop={1}>
              <Text color={q.kind === 'blocking' ? 'red' : 'yellow'} bold>
                {q.kind === 'blocking' ? 'Blocking' : 'Non-blocking'} · {q.key} · from {q.from}
              </Text>
              <Text>{q.question}</Text>
              {q.context && <Text dimColor>{q.context}</Text>}
              {q.default && <Text dimColor>If nobody answers: {q.default}</Text>}
              {q.options.length > 0 && (
                <Box>
                  {q.options.map((o, i) => (
                    <Button key={`a-${q.key}-${q.id}-${i}`} label={o} onPress={() => void answer($, q, o)} />
                  ))}
                </Box>
              )}
              <Input key={`in-${q.key}-${q.id}`} placeholder="Type an answer and press Enter" onSubmit={value => void answer($, q, value)} />
            </Box>
          ))}
        </Box>
      )
    }

    if (!run) {
      return (
        <Box flexDirection="column">
          {header}
          <Text dimColor>No run under runs/ yet. Start a recipe, or /recipe KEY to follow one.</Text>
        </Box>
      )
    }

    const sel = run.stages.find(s => s.id === selected) ?? run.stages.find(s => s.isCurrent) ?? null
    const spend = spendText(run.verdicts)
    return (
      <Box flexDirection="column">
        {header}
        <Text bold>
          {run.key}
          {run.recipe ? ` · ${run.recipe}` : ''}
          {run.tier ? ` · ${run.tier}` : ''}
          {pinned ? ' · pinned' : ''}
        </Text>
        {run.stages.map(s => (
          <Box key={`st-${s.id}`}>
            <Text color={TONE_COLOR[s.tone]}>{s.isCurrent ? '▸' : TONE_MARK[s.tone]} </Text>
            <Button
              key={`stage-${s.id}`}
              plain
              label={`${s.label}${s.outcome ? `: ${s.outcome}` : ''}${s.attempts > 1 ? ` (try ${s.attempts})` : ''}`}
              onPress={() => void update($, selectedAtom, () => s.id)}
            />
          </Box>
        ))}
        {sel?.detail && (
          <Box flexDirection="column" marginTop={1}>
            <Text bold>{sel.label}</Text>
            <Text dimColor>{sel.detail}</Text>
          </Box>
        )}
        {run.verdicts.length > 0 && (
          <Box flexDirection="column" marginTop={1}>
            <Text bold>Router</Text>
            {run.verdicts.slice(-6).map((v, i) => (
              <Text key={`v-${i}-${v.at}`} color={v.action === 'advance' ? undefined : v.action.startsWith('re-') ? 'yellow' : 'red'}>
                {verdictLine(v)}
              </Text>
            ))}
          </Box>
        )}
        {spend && <Text dimColor>Spend: {spend}</Text>}
        {open.length > 0 && (
          <Text color={blocking > 0 ? 'red' : 'yellow'}>
            {open.length} open question{open.length === 1 ? '' : 's'}. Open the Questions tab to answer.
          </Text>
        )}
      </Box>
    )
  })
}

