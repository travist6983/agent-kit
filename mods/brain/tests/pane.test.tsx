import { expect, test } from 'claude-code/testing'

type Body = Extract<Parameters<typeof test>[1], (...args: never[]) => unknown>
type Engine = Parameters<Body>[1]

const ran = { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false }
const LESSON = { id: 12, project: 'global', kind: 'preference', text: 'Give multiple-choice options.', status: 'proposed', source: 'seed' }

function fakeKb(on: Engine) {
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('env.get', () => ({ value: undefined }))
  on('fs.exists', () => ({ value: true }))
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('tool.register', () => ({ value: undefined }))
  on('clock.every', () => ({ value: undefined }))
  on('clock.now', () => ({ value: 0 }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('process.run', (_, e) => {
    const argv = [...e.argv]
    if (argv[1] === 'status') return { value: { ...ran, stdout: JSON.stringify({ ok: true, reachable: true, project: 'global', scope: 'single-machine', target: 'localhost/personal_brain', row_counts_visible: { lessons: 6 } }) } }
    if (argv[1] === 'read') return { value: { ...ran, stdout: JSON.stringify({ ok: true, rows: argv.includes('proposed') ? [LESSON] : [] }) } }
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
