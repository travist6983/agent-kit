import { expect, test } from 'claude-code/testing'

type Body = Extract<Parameters<typeof test>[1], (...args: never[]) => unknown>
type Engine = Parameters<Body>[1]

const ran = { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false }
const LESSON = { id: 12, project: 'global', kind: 'preference', text: 'Give multiple-choice options.', status: 'proposed', source: 'seed' }

function fakeKb(on: Engine, calls: string[][] = []) {
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('env.get', () => ({ value: undefined }))
  on('fs.exists', () => ({ value: true }))
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('tool.register', () => ({ value: { tool: 'mcp__brain__remember' } }))
  on('clock.every', () => ({ value: undefined }))
  on('clock.now', () => ({ value: 0 }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('process.run', (_, e) => {
    const argv = [...e.argv]
    calls.push(argv.slice(1))
    if (argv[1] === 'status') return { value: { ...ran, stdout: JSON.stringify({ ok: true, reachable: true, project: 'global', scope: 'single-machine', target: 'localhost/personal_brain', row_counts_visible: { lessons: 6 } }) } }
    if (argv[1] === 'read') return { value: { ...ran, stdout: JSON.stringify({ ok: true, rows: argv.includes('proposed') ? [LESSON] : [{ ...LESSON, id: 7, status: 'active', text: 'Run the API on 8010.' }] }) } }
    if (argv[1] === 'forget') return { value: { ...ran, stdout: JSON.stringify({ ok: true }) } }
    return { value: { ...ran, exitCode: 1 } }
  })
}

test('/brain review shows the proposed lessons', async ($, on) => {
  fakeKb(on)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'brain', args: 'review', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 120 } })
  const ui = await $.ui.mount({
    plugin: 'brain',
    component: 'Pane',
    requestId: 'brain',
    surface: 'terminal',
    props: { title: 'Brain', isFocused: false, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 50 }, view: {} },
  } as never)
  expect(await ui.find({ type: 'Text', text: /Give multiple-choice options/ })).toBeTruthy()
  await ui.unmount()
})

const pane = (surface: 'terminal' | 'desktop') =>
  ({
    plugin: 'brain',
    component: 'Pane',
    requestId: 'brain',
    surface,
    props: { title: 'Brain', isFocused: false, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 50 }, view: {} },
  }) as never

test('the desktop app draws the styled cards with their coloured bars', async ($, on) => {
  fakeKb(on)
  await $.session.start({ cwd: '/repo', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'brain', args: 'review', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 120 } })
  const ui = await $.ui.mount(pane('desktop'))
  expect(await ui.find({ type: 'Svg' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /To review/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /Give multiple-choice options/ })).toBeTruthy()
  await ui.unmount()
})

test('Forget takes two presses on the same button', async ($, on) => {
  const calls: string[][] = []
  fakeKb(on, calls)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'brain', args: 'lessons', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 120 } })
  const ui = await $.ui.mount(pane('terminal'))
  expect(await ui.find({ type: 'Svg' })).toBeFalsy()
  await ui.press({ key: 'fg7' })
  expect(calls.some(c => c[0] === 'forget')).toBe(false)
  expect(await ui.find({ type: 'Text', text: /Confirm forget/ })).toBeTruthy()
  await ui.press({ key: 'fg7' })
  expect(calls.some(c => c[0] === 'forget')).toBe(true)
  await ui.unmount()
})
