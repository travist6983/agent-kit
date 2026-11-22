import { expect, test } from 'claude-code/testing'

const PANE = {
  plugin: 'pr',
  component: 'Pane',
  requestId: 'pr',
  props: { title: 'PR', isFocused: false, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 200 }, view: {} },
} as const

const NOW = Date.parse('2026-11-21T12:00:00Z')

type Body = Extract<Parameters<typeof test>[1], (...args: never[]) => unknown>
type Engine = Parameters<Body>[1]

const CHECKS = [
  { name: 'lint', workflow: 'ci', bucket: 'pass', state: 'SUCCESS', startedAt: '2026-11-21T11:50:00Z', completedAt: '2026-11-21T11:51:05Z', link: 'https://x/lint' },
  { name: 'unit', workflow: 'ci', bucket: 'fail', state: 'FAILURE', startedAt: '2026-11-21T11:50:00Z', completedAt: '2026-11-21T11:52:00Z', link: 'https://x/unit' },
  { name: 'e2e', workflow: 'ci', bucket: 'pending', state: 'IN_PROGRESS', startedAt: '2026-11-21T11:50:00Z', completedAt: '', link: '' },
]
const THREADS = {
  data: {
    repository: {
      pullRequest: {
        reviewThreads: {
          nodes: [{ isResolved: false, isOutdated: false, path: 'site.js', line: 10, comments: { nodes: [{ author: { login: 'rev' }, body: 'Rename this please', url: 'https://x/t1' }] } }],
        },
      },
    },
  },
}

function fakeGh(on: Engine) {
  const store = new Map<string, unknown>()
  const ran = { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false }
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('tool.register', () => ({ value: undefined }) as never)
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.panes', () => ({ value: [] }) as never)
  on('ui.toast', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }) as never)
  on('clock.every', () => ({ value: undefined }))
  on('clock.now', () => ({ value: NOW }))
  on('store.get', (_, e) => ({ value: store.get(e.key) }))
  on('store.set', (_, e) => {
    store.set(e.key, e.value)
    return { value: undefined }
  })
  on('process.run', (_, e) => {
    const argv = [...e.argv].join(' ')
    if (argv.startsWith('git branch')) return { value: { ...ran, stdout: 'feature\n' } }
    if (argv.startsWith('gh pr view')) return { value: { ...ran, stdout: JSON.stringify({ number: 42, title: 'Add widgets', url: 'https://x/42', isDraft: true, reviewDecision: '' }) } }
    if (argv.startsWith('gh pr checks')) return { value: { ...ran, stdout: JSON.stringify(CHECKS) } }
    if (argv.startsWith('gh repo view')) return { value: { ...ran, stdout: 'me/repo\n' } }
    if (argv.startsWith('gh api user')) return { value: { ...ran, stdout: 'me\n' } }
    if (argv.startsWith('gh api graphql')) return { value: { ...ran, stdout: JSON.stringify(THREADS) } }
    return { value: { ...ran, exitCode: 1, stderr: 'not faked' } }
  })
}

test('the checks group into sections and the threads tab shows cards, on every surface', async ($, on) => {
  fakeGh(on)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'ci', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 120 } })

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ type: 'Text', text: /^Needs a look/ })).toBeTruthy()
    expect(await ui.find({ type: 'Text', text: /^Running/ })).toBeTruthy()
    expect(await ui.find({ type: 'Text', text: /^Passed/ })).toBeTruthy()
    expect(await ui.find({ type: 'Text', text: /PR #42 Add widgets/ })).toBeTruthy()
    await ui.press({ key: 'tab-threads' })
    expect(await ui.find({ type: 'Text', text: /^Waiting on you/ })).toBeTruthy()
    expect(await ui.find({ type: 'Text', text: /Rename this please/ })).toBeTruthy()
    expect(await ui.find({ key: 'fix' })).toBeTruthy()
    await ui.press({ key: 'tab-checks' })
    await ui.unmount()
  }
})

test('the desktop app draws the coloured card bars as Svg', async ($, on) => {
  fakeGh(on)
  await $.session.start({ cwd: '/repo', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'ci', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 120 } })
  const desk = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await desk.find({ type: 'Svg' })).toBeTruthy()
  await desk.unmount()
  const term = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await term.find({ type: 'Svg' })).toBeFalsy()
  await term.unmount()
})
