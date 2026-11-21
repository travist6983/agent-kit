import { expect, test } from 'claude-code/testing'

const PANE = {
  plugin: 'mods-panel',
  component: 'Pane',
  requestId: 'mods',
  props: {
    title: 'Mods',
    isFocused: false,
    bodyColumns: 48,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 200 },
    view: {},
  },
} as const

const NOW = Date.parse('2026-11-21T12:00:00Z')

type Body = Extract<Parameters<typeof test>[1], (...args: never[]) => unknown>
type Engine = Parameters<Body>[1]

const RUN = {
  root: '/repo',
  key: 'AK-142',
  recipe: 'dev-loop-defect',
  tier: null,
  stages: [
    { id: 'intake', label: 'Intake', outcome: 'ok', tone: 'good', attempts: 1, isCurrent: false, detail: null },
    { id: 'fix', label: 'Fix', outcome: null, tone: 'none', attempts: 2, isCurrent: true, detail: null },
    { id: 'verify', label: 'Verify', outcome: null, tone: 'none', attempts: 0, isCurrent: false, detail: null },
  ],
  verdicts: [{ stage: 'verify', result: 'fail', attempt: 2, max: 3, action: 're-fix', reason: '2 of 14 auth tests timed out', budgetSpent: 2.76, budgetCap: 10, at: NOW - 60_000 }],
  updatedAt: NOW - 60_000,
}

const BLOCKING = {
  id: 'q1',
  key: 'AK-142',
  from: 'fix',
  kind: 'blocking',
  question: 'Drop the legacy status column?',
  options: ['Drop it', 'Keep it'],
  default: null,
  context: null,
  askedAt: '2026-11-21T11:00:00Z',
}

// The engine and the other mods: recipe-run and burn-meter have state, the rest are off or absent.
// A value another mod never wrote reads undefined at version 0, as the real engine answers.
function fakeKit(on: Engine, others: Record<string, unknown> = {}) {
  const state: Record<string, unknown> = { 'recipe-run/run': RUN, 'recipe-run/open': [BLOCKING], 'burn-meter/settings': { budgetUsd: 5, isHard: false, isWorkflowOff: false }, ...others }
  const store = new Map<string, unknown>()
  const runs: string[][] = []
  const appended: string[] = []
  // pid -> port it listens on, for the lsof check before and after a Stop
  const listening = new Map<number, number>()
  for (const plugin of ['recipe-run', 'burn-meter', 'traffic-control', 'ship', 'dev-servers', 'pr', 'brain']) {
    on('state.get', { plugin } as never, (_: unknown, e: { plugin: string; key: string }) => {
      const value = state[`${e.plugin}/${e.key}`]
      return { value: { value, version: value === undefined ? 0 : 1 } }
    })
  }
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('session.usage', () => ({
    value: {
      startedAt: NOW - 3600_000,
      context: { tokens: 0, window: 200_000, percent: 0 },
      rateLimits: [
        { kind: 'five_hour', percentUsed: 54, resetsAt: new Date(NOW + 127 * 60_000).toISOString() },
        { kind: 'seven_day', percentUsed: 31 },
      ],
      cost: { usd: 3.18 },
    },
  }))
  on('settings.read', () => ({ value: { enabledPlugins: { 'recipe-run@agent-kit': true, 'burn-meter@agent-kit': true, 'pr@agent-kit': false } } }))
  on('command.register', () => ({ value: { command: 'mods' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.toast', () => ({ value: undefined }))
  on('clock.every', () => ({ value: undefined }))
  on('clock.now', () => ({ value: NOW }))
  on('clock.sleep', () => ({ value: undefined }))
  on('store.get', (_, e) => ({ value: store.get(e.key) }))
  on('store.set', (_, e) => {
    store.set(e.key, e.value)
    return { value: undefined }
  })
  const ran = { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false }
  on('process.run', (_, e) => {
    const argv = [...e.argv]
    runs.push(argv)
    if (argv[0] === 'git') return { value: { ...ran, stdout: '/repo\n' } }
    if (argv[0] === 'sh') {
      appended.push(`${argv[5] ?? ''} ${argv[4] ?? ''}`)
      return { value: ran }
    }
    if (argv[0] === 'claude') return { value: ran }
    if (argv[0] === 'open') return { value: ran }
    if (argv[0] === 'lsof') {
      const pid = Number(argv[argv.indexOf('-p') + 1])
      const port = Number((argv.find(a => a.startsWith('-iTCP:')) ?? '').slice(6))
      return { value: listening.get(pid) === port ? { ...ran, stdout: `${pid}\n` } : { ...ran, exitCode: 1 } }
    }
    if (argv[0] === 'kill') {
      listening.delete(Number(argv.at(-1)))
      return { value: ran }
    }
    return { value: { ...ran, exitCode: 1, stderr: 'not faked' } }
  })
  return { store, runs, appended, listening }
}

test('the strip, the cards and the run ladder read the other mods, on every surface', async ($, on) => {
  fakeKit(on)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ type: 'Text', text: /^2\/8$/ })).toBeTruthy()
    expect(await ui.find({ type: 'Text', text: /^\$3\.18$/ })).toBeTruthy()
    expect(await ui.find({ type: 'Text', text: /Blocked on you · 1 question/ })).toBeTruthy()
    expect(await ui.find({ type: 'Text', text: /\$3\.18 session · 5h 54% · wk 31%/ })).toBeTruthy()
    expect(await ui.find({ type: 'Text', text: /^Not installed$/ })).toBeTruthy()
    // recipe-run opens by itself while a run is live: the ladder and the router's call show.
    expect(await ui.find({ type: 'Text', text: /! Fix/ })).toBeTruthy()
    expect(await ui.find({ type: 'Text', text: /verify fail: retry the fix\. 2 of 14 auth tests timed out/ })).toBeTruthy()
    await ui.unmount()
  }
})

test('a blocking question is answered in the panel and reaches the next prompt', async ($, on) => {
  const kit = fakeKit(on)
  let seen: readonly string[] = []
  on('prompt.submit', (_, e) => {
    seen = e.context ?? []
    return { text: e.text }
  })
  await $.session.start({ cwd: '/repo', surface: 'desktop', isInteractive: true })

  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'answer-now' })
  expect(await ui.find({ type: 'Text', text: /Drop the legacy status column\?/ })).toBeTruthy()
  await ui.press({ key: 'a-AK-142-q1-1' })
  expect(kit.appended.some(l => l.startsWith('/repo/runs/AK-142/answers.jsonl') && l.includes('"answer":"Keep it"') && l.includes('"by":"human"'))).toBe(true)
  expect(await ui.find({ type: 'Text', text: /Answered AK-142/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /^0$/ })).toBeTruthy()
  await ui.unmount()

  await $.prompt.submit({ text: 'carry on', wait: false, origin: { kind: 'composer' } })
  expect(seen.join('\n')).toContain('AK-142 q1 "Drop the legacy status column?": Keep it')
})

test('the switch runs claude plugin and waits for a reload', async ($, on) => {
  const kit = fakeKit(on)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'switch-burn-meter' })
  expect(kit.runs).toContainEqual(['claude', 'plugin', 'disable', 'burn-meter@agent-kit'])
  expect(await ui.find({ type: 'Text', text: /off after \/reload-plugins/ })).toBeTruthy()
  await ui.press({ key: 'switch-pr' })
  expect(kit.runs).toContainEqual(['claude', 'plugin', 'enable', 'pr@agent-kit'])
  await ui.unmount()
})

test('/mods band off persists and hands the row back to recipe-run', async ($, on) => {
  const kit = fakeKit(on)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const off = await $.command.run({ command: 'mods', args: 'band off', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 120 } })
  expect(off.text).toBe('Mods band off. recipe-run draws its own row again.')
  expect(kit.store.get('isBandOn')).toBe(false)
  const open = await $.command.run({ command: 'mods', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 120 } })
  expect(open.text).toBe('Mods panel open: 2 of 8 mods on, 1 blocking questions, $3.18 this session.')
})

test('glass is on by default, stays off once turned off, and comes back on with /mods glass on', async ($, on) => {
  const kit = fakeKit(on)
  const looks: unknown[] = []
  let cleared = 0
  on('theme.set', (_, e) => {
    looks.push(e)
    return { value: { isDrawn: true as const, requestId: 'backdrop-1' } }
  })
  on('theme.clear', () => {
    cleared += 1
    return { value: { isDrawn: false as const, reason: 'cleared' } }
  })
  const cmd = (args: string) => $.command.run({ command: 'mods', args, origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 120 } })

  // A first session with nothing saved: the look is set as the mod loads.
  await $.session.start({ cwd: '/repo', surface: 'desktop', isInteractive: true })
  expect(looks).toHaveLength(1)
  expect(looks[0]).toMatchObject({ backdrop: { scene: 'mods-glass', glass: { blur: 28, opacity: 0.55 } }, overrideColorScheme: 'dark' })

  const offText = await cmd('glass off')
  expect(offText.text).toContain('Mods glass off')
  expect(cleared).toBe(1)
  expect(kit.store.get('isGlassOn')).toBe(false)

  // Off is remembered: the next session sets no look.
  await $.session.start({ cwd: '/repo', surface: 'desktop', isInteractive: true })
  expect(looks).toHaveLength(1)

  const onText = await cmd('glass on')
  expect(onText.text).toContain('Mods glass on')
  expect(looks).toHaveLength(2)
  expect(kit.store.get('isGlassOn')).toBe(true)
})

test('the glass backdrop draws its scene in the desktop app', async ($, on) => {
  fakeKit(on)
  const BACKDROP = { plugin: 'mods-panel', component: 'Backdrop', requestId: 'backdrop-1', props: { scene: 'mods-glass', plugin: 'mods-panel', glass: { blur: 28, tint: '#16161c', opacity: 0.55 } } } as const
  const desk = await $.ui.mount({ ...BACKDROP, surface: 'desktop' })
  expect(await desk.find({ type: 'Svg' })).toBeTruthy()
  await desk.unmount()
})

test('the dev-servers card names the app and folder, and Stop asks once before it kills', async ($, on) => {
  const server = { pid: 82373, port: 5173, process: 'node', command: '/Users/me/repos/web/node_modules/.bin/vite --port 5173', age: '09-01:49:02', cwd: '/Users/me/repos/pm-tool/web', tool: 'vite', project: 'pm-tool-web' }
  const kit = fakeKit(on, { 'dev-servers/servers': [server], 'dev-servers/recipe': [] })
  kit.listening.set(82373, 5173)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'expand-dev-servers' })
  expect(await ui.find({ type: 'Text', text: /vite in pm-tool-web/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /^~\/repos\/pm-tool\/web$/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /\$ ~\/repos\/web\/node_modules\/\.bin\/vite --port 5173/ })).toBeTruthy()

  await ui.press({ key: 'dev-stop-82373-5173' })
  expect(kit.runs.some(a => a[0] === 'kill')).toBe(false)
  await ui.press({ key: 'dev-stop-no-82373-5173' })
  await ui.press({ key: 'dev-stop-82373-5173' })
  await ui.press({ key: 'dev-stop-yes-82373-5173' })
  expect(kit.runs).toContainEqual(['kill', '82373'])
  expect(kit.runs).not.toContainEqual(['kill', '-9', '82373'])
  expect(await ui.find({ type: 'Text', text: /No dev servers are listening/ })).toBeTruthy()
  await ui.unmount()
})
