import { expect, test } from 'claude-code/testing'

type Body = Extract<Parameters<typeof test>[1], (...args: never[]) => unknown>
type Engine = Parameters<Body>[1]

const NOW = Date.parse('2026-11-22T12:00:00Z')
const DATA = '/home/sam/.claude/build-log'
const CONFIG = JSON.stringify({ deny: { names: ['Acme Widgets'] }, allow_urls: ['sam.example.dev'], safe_repos: ['sams-toys'] })

// An engine with an in-memory disk. `repo` is the session's git root; `answer` is what the user
// picks in an ask dialog, or null when nobody answers (a headless run).
function fakeEngine(on: Engine, opts: { repo: string; remote?: string; files?: Record<string, string>; answer?: string | null }) {
  const files = new Map(Object.entries(opts.files ?? {}))
  const asked: string[] = []
  const store = new Map<string, unknown>()
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('tool.register', (_, e) => ({ value: { tool: e.name } }))
  on('session.cwd', () => ({ value: opts.repo }))
  on('clock.now', () => ({ value: NOW }))
  on('ui.toast', () => ({ value: undefined }))
  // $.ui.ask is a call of the AskUserQuestion tool; here the "user" answers it.
  on('tool.call', { tool: 'AskUserQuestion' }, (_, e) => {
    const question = e.questions[0]?.question ?? ''
    asked.push(question)
    if (opts.answer === null || opts.answer === undefined) return { deny: 'no one to ask' }
    return { result: { questions: e.questions, answers: { [question]: opts.answer } } }
  })
  on('store.get', (_, e) => ({ value: store.get(e.key) }))
  on('store.set', (_, e) => {
    store.set(e.key, e.value)
    return { value: undefined }
  })
  on('env.get', (_, e) => ({ value: e.name === 'HOME' ? '/home/sam' : undefined }))
  on('fs.exists', (_, e) => ({ value: files.has(e.path) }))
  on('fs.read', (_, e) => (files.has(e.path) ? { value: files.get(e.path) ?? '' } : { deny: `ENOENT ${e.path}` }))
  on('process.run', (_, e) => {
    const argv = e.argv
    const ok = (stdout = '') => ({ value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
    if (argv[0] === 'git' && argv[1] === 'rev-parse') return ok(`${opts.repo}\n`)
    if (argv[0] === 'git' && argv[1] === 'config') return opts.remote ? ok(`${opts.remote}\n`) : { value: { exitCode: 1, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    if (argv[0] === 'sh') {
      const script = argv[2] ?? ''
      const [a = '', b = ''] = argv.slice(4)
      if (script.includes('>>')) files.set(b, `${files.get(b) ?? ''}${a}\n`)
      else if (script.includes('cp ')) files.set(b, files.get(a) ?? '')
      else if (script.includes('> "$2"')) files.set(b, a)
      return ok()
    }
    return ok()
  })
  return { files, asked }
}

const start = ($: Parameters<Body>[0], cwd: string) => $.session.start({ cwd, surface: 'terminal', isInteractive: true })

const draft = (site: string) => ({
  tool: 'mcp__build-log__save_draft' as const,
  title: 'A retry has to change something',
  site,
  linkedin: 'Short version. More at https://sam.example.dev/logs/retry',
  x_thread: ['One', 'Two', 'Three', 'Four'],
})

const resultText = (r: unknown) => String((r as { result?: unknown }).result ?? '')

test('with no denylist, save_draft refuses and writes nothing', async ($, on) => {
  const eng = fakeEngine(on, { repo: '/home/sam/sams-toys' })
  await start($, '/home/sam/sams-toys')
  const r = await $.tool.call(draft('## Problem\n\nall fine'))
  expect(resultText(r)).toContain('Not saved: no config')
  expect([...eng.files.keys()].some(k => k.includes('/drafts/'))).toBe(false)
})

test('a draft naming the denylist is blocked; a clean one is written', async ($, on) => {
  const eng = fakeEngine(on, { repo: '/home/sam/sams-toys', files: { [`${DATA}/config.json`]: CONFIG } })
  await start($, '/home/sam/sams-toys')
  const bad = await $.tool.call(draft('## Problem\n\nAt Acme Widgets the loop kept retrying.'))
  expect(resultText(bad)).toContain('denylist:names: Acme Widgets')
  expect([...eng.files.keys()].some(k => k.includes('/drafts/'))).toBe(false)

  const good = await $.tool.call(draft('## Problem\n\nOn a production codebase the loop kept retrying.'))
  expect(resultText(good)).toContain('Nothing was posted')
  expect(eng.files.get(`${DATA}/drafts/2026-11-22-a-retry-has-to-change-something/site.md`)).toContain('production codebase')
})

test('a screenshot from a work repo needs the user\'s yes; with none it is not kept', async ($, on) => {
  const eng = fakeEngine(on, {
    repo: '/home/sam/portal',
    remote: 'git@github.com:acme/portal.git',
    files: { [`${DATA}/config.json`]: CONFIG, '/home/sam/portal/shot.png': 'png' },
    answer: null,
  })
  await start($, '/home/sam/portal')
  const r = await $.tool.call({ tool: 'mcp__build-log__add_screenshot', path: 'shot.png', caption: 'the pane' })
  expect(resultText(r)).toContain('Not kept')
  expect(eng.asked.length).toBe(1)
  expect(eng.files.has(`${DATA}/shots.jsonl`)).toBe(false)
})

test('a kept work screenshot is flagged and blocks drafts until reviewed', async ($, on) => {
  const eng = fakeEngine(on, {
    repo: '/home/sam/portal',
    remote: 'git@github.com:acme/portal.git',
    files: { [`${DATA}/config.json`]: CONFIG, '/home/sam/portal/shot.png': 'png' },
    answer: 'Keep it, flag for review',
  })
  await start($, '/home/sam/portal')
  const kept = resultText(await $.tool.call({ tool: 'mcp__build-log__add_screenshot', path: 'shot.png', caption: 'the pane' }))
  expect(kept).toContain('flagged for review')
  const id = kept.match(/Kept (s-[\w]+)/)?.[1] ?? ''
  const blocked = resultText(await $.tool.call(draft(`## Problem\n\nA retry.\n\n![pane](shot:${id})`)))
  expect(blocked).toContain('shot-needs-review')
  const ok = await $.command.run({ command: 'buildlog', args: `ok ${id}`, origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 120 } })
  expect(ok.text).toContain('marked reviewed')
  expect(resultText(await $.tool.call(draft(`## Problem\n\nA retry.\n\n![pane](shot:${id})`)))).toContain('look at each one')
  expect(eng.files.has(`${DATA}/drafts/2026-11-22-a-retry-has-to-change-something/check.json`)).toBe(true)
})

test('a successful gh pr merge is saved as a moment, and pausing stops it', async ($, on) => {
  const eng = fakeEngine(on, { repo: '/home/sam/sams-toys', files: { [`${DATA}/config.json`]: CONFIG } })
  on('tool.call', { tool: 'Bash' }, () => ({ result: { stdout: 'merged', stderr: '', interrupted: false } }))
  await start($, '/home/sam/sams-toys')
  await $.tool.call({ tool: 'Bash', command: 'gh pr merge 7 --squash' })
  expect(eng.files.get(`${DATA}/moments.jsonl`) ?? '').toContain('Merged PR 7')
  await $.command.run({ command: 'buildlog', args: 'off', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 120 } })
  await $.tool.call({ tool: 'Bash', command: 'gh pr merge 8 --squash' })
  expect(eng.files.get(`${DATA}/moments.jsonl`) ?? '').not.toContain('Merged PR 8')
})
