import { expect, test } from 'claude-code/testing'

const PANE = {
  plugin: 'recipe-run',
  component: 'Pane',
  requestId: 'recipe-run',
  props: {
    title: 'Recipe run',
    isFocused: false,
    bodyColumns: 60,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 40 },
    view: {},
  },
} as const

const ROUTER = 'python3 .claude/skills/dev-loop-defect/scripts/recipe_router.py'

type Body = Extract<Parameters<typeof test>[1], (...args: never[]) => unknown>
type Engine = Parameters<Body>[1]

// A small repo on a fake disk: one run with intake, repro and fix written, and one blocking
// question a headless loop left. Every hook here stands for the engine.
function fakeRepo(on: Engine, extra: Record<string, string> = {}) {
  const files: Record<string, string> = {
    '/repo/runs/PROJ-123/intake.json': JSON.stringify({ key: 'PROJ-123', recipe: 'dev-loop-defect' }),
    '/repo/runs/PROJ-123/repro.json': JSON.stringify({ status: 'reproduced', symptom_quote: 'Badge still reads Draft after Send.' }),
    '/repo/runs/PROJ-123/fix.json': JSON.stringify({ status: 'fixed', root_cause: 'Badge copied props into state on mount.' }),
    '/repo/runs/PROJ-123/questions.jsonl':
      JSON.stringify({ id: 'q1', kind: 'blocking', from: 'fix', question: 'Drop the legacy status column?', options: ['Drop it', 'Keep it'], asked_at: '2026-11-21T10:00:00Z' }) + '\n',
    ...extra,
  }
  const store = new Map<string, unknown>()
  const appended: string[] = []
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('command.register', () => ({ value: { command: 'recipe' } }))
  on('tool.register', () => ({ value: { tool: 'mcp__recipe-run__ask_human' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.notify', () => ({ value: { isSent: true, channel: 'kitty' } }))
  on('clock.every', () => ({ value: undefined }))
  on('clock.now', () => ({ value: Date.parse('2026-11-21T12:00:00Z') }))
  on('store.get', (_, e) => ({ value: store.get(e.key) }))
  on('store.set', (_, e) => {
    store.set(e.key, e.value)
    return { value: undefined }
  })
  on('fs.read', (_, e) => {
    const text = files[e.path]
    return text === undefined ? { deny: `ENOENT ${e.path}` } : { value: text }
  })
  on('fs.list', (_, e) => {
    const prefix = `${e.path.replace(/\/$/, '')}/`
    const names = new Set(Object.keys(files).filter(p => p.startsWith(prefix)).map(p => p.slice(prefix.length).split('/')[0] ?? ''))
    return { value: [...names].map(name => ({ name, kind: 'dir' as const, size: 0, mtimeMs: 0, isLink: false })) }
  })
  on('fs.stat', () => ({ value: { kind: 'file' as const, size: 1, mtimeMs: 1_000, isLink: false } }))
  const ran = { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false }
  on('process.run', (_, e) => {
    const argv = e.argv
    if (argv[0] === 'git') return { value: { ...ran, stdout: '/repo\n' } }
    if (argv[0] === 'sh') {
      const line = argv[4] ?? ''
      const path = argv[5] ?? ''
      appended.push(`${path} ${line}`)
      files[path] = `${files[path] ?? ''}${line}\n`
      return { value: ran }
    }
    return { value: { ...ran, exitCode: 1, stderr: 'not faked' } }
  })
  return { files, store, appended }
}

test('the ladder shows each stage and the router verdict, on every surface', async ($, on) => {
  fakeRepo(on)
  on('tool.call', (_, e) => {
    if (e.tool === 'Bash') return { result: { stdout: '{"action": "re-fix", "reason": "verify fail"}\n', stderr: '', interrupted: false } }
    return { result: {} }
  })
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  await $.tool.call({ tool: 'Bash', command: `${ROUTER} --stage verify --result fail --attempt 1 --max 3 --budget-spent 3.5 --budget-cap 10` })

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /^PROJ-123/ }))?.text).toBe('PROJ-123 · dev-loop-defect')
    expect(await ui.find({ type: 'Text', text: /verify 1\/3 fail: retry the fix\. verify fail/ })).toBeTruthy()
    expect(await ui.find({ type: 'Text', text: /Spend: \$3\.50 of \$10\.00/ })).toBeTruthy()
    expect(await ui.find({ type: 'Text', text: /1 open question/ })).toBeTruthy()
    await ui.unmount()
  }
})

test('a blocking question is answered from the pane and reaches the next prompt', async ($, on) => {
  const repo = fakeRepo(on)
  let seen: readonly string[] = []
  on('prompt.submit', (_, e) => {
    seen = e.context ?? []
    return { text: e.text }
  })
  await $.session.start({ cwd: '/repo', surface: 'desktop', isInteractive: true })

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'tab-questions' })
  expect(await ui.find({ type: 'Text', text: /Drop the legacy status column\?/ })).toBeTruthy()
  await ui.press({ key: 'a-PROJ-123-q1-1' })
  expect(repo.appended.some(l => l.startsWith('/repo/runs/PROJ-123/answers.jsonl') && l.includes('"answer":"Keep it"'))).toBe(true)
  expect(await ui.find({ type: 'Text', text: /No open questions/ })).toBeTruthy()
  await ui.unmount()

  await $.prompt.submit({ text: 'carry on', wait: false, origin: { kind: 'composer' } })
  expect(seen.join('\n')).toContain('PROJ-123 q1 (blocking) "Drop the legacy status column?": Keep it')
})

test('ask_human saves a blocking question when nobody can answer', async ($, on) => {
  const repo = fakeRepo(on, { '/repo/runs/PROJ-123/questions.jsonl': '' })
  // No one there: the question dialog is dismissed.
  on('tool.call', { tool: 'AskUserQuestion' }, () => ({ deny: 'no one to ask' }))
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: false })

  const blocked = await $.tool.call({ tool: 'mcp__recipe-run__ask_human', question: 'Rotate the leaked key?', kind: 'blocking', key: 'PROJ-123', from: 'pre-pr' })
  expect(String(blocked.result)).toContain('Stop this stage, do not guess')
  expect(repo.files['/repo/runs/PROJ-123/questions.jsonl']).toContain('"question":"Rotate the leaked key?"')

  const later = await $.tool.call({ tool: 'mcp__recipe-run__ask_human', question: 'Rename the flag?', kind: 'non-blocking', default: 'keep the old name', key: 'PROJ-123' })
  expect(String(later.result)).toContain('Carry on with your default: keep the old name')

  const bad = await $.tool.call({ tool: 'mcp__recipe-run__ask_human', question: 'Rename it?', kind: 'non-blocking' })
  expect(String(bad.result)).toContain('needs a default')
})

test('the desktop app draws the styled sections, card bars and the spend-vs-cap meter', async ($, on) => {
  fakeRepo(on)
  on('tool.call', (_, e) => {
    if (e.tool === 'Bash') return { result: { stdout: '{"action": "advance", "reason": "ok"}\n', stderr: '', interrupted: false } }
    return { result: {} }
  })
  await $.session.start({ cwd: '/repo', surface: 'desktop', isInteractive: true })
  await $.tool.call({ tool: 'Bash', command: `${ROUTER} --stage fix --result pass --attempt 1 --max 3 --budget-spent 9 --budget-cap 10` })

  const desk = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await desk.find({ type: 'Svg' })).toBeTruthy()
  expect(await desk.find({ type: 'Text', text: /^Needs a look/ })).toBeTruthy()
  expect(await desk.find({ type: 'Text', text: /^Live/ })).toBeTruthy()
  await desk.press({ key: 'answer-now' })
  expect(await desk.find({ type: 'Text', text: /Drop the legacy status column\?/ })).toBeTruthy()
  await desk.unmount()

  const term = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await term.find({ type: 'Svg' })).toBeFalsy()
  await term.unmount()
})
