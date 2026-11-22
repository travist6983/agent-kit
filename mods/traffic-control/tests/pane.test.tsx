import { expect, test } from 'claude-code/testing'

const PANE = {
  plugin: 'traffic-control',
  component: 'Pane',
  requestId: 'traffic',
  props: {
    title: 'Sessions',
    isFocused: false,
    bodyColumns: 60,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 80 },
    view: {},
  },
} as const

const NOW = Date.parse('2026-11-22T12:00:00Z')
const DIR = '/home/.claude/traffic'

type Body = Extract<Parameters<typeof test>[1], (...args: never[]) => unknown>
type Engine = Parameters<Body>[1]

// A fake disk with two other live sessions: one busy in this repo holding its push lock, one idle elsewhere.
function fakeHost(on: Engine) {
  const busy = { id: 'peer-busy', label: 'repo-wt-abcd', cwd: '/repo-wt', root: '/repo', worktree: '/repo-wt', branch: 'feat', lastTool: 'Bash', lastSeen: NOW - 5_000 }
  const idle = { id: 'peer-idle', label: 'notes-ef01', cwd: '/notes', root: '/notes', worktree: '/notes', branch: 'main', lastTool: '', lastSeen: NOW - 30_000 }
  const lock = { key: `${DIR}/locks/_repo__push.json`, kind: 'push', root: '/repo', holder: 'peer-busy', label: 'repo-wt-abcd', command: 'git push origin feat', at: NOW - 60_000 }
  const files: Record<string, string> = {
    [`${DIR}/sessions/peer-busy.json`]: JSON.stringify(busy),
    [`${DIR}/sessions/peer-idle.json`]: JSON.stringify(idle),
    [`${DIR}/locks/_repo__push.json`]: JSON.stringify(lock),
  }
  const closed: string[] = []
  const ran = { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false }
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('session.id', () => ({ value: 'me-1234' }))
  on('session.repo', () => ({ value: null }))
  on('env.get', (_, e) => ({ value: e.name === 'HOME' ? '/home' : undefined }))
  on('command.register', () => ({ value: { command: 'traffic' } }))
  on('clock.every', () => ({ value: undefined }))
  on('clock.now', () => ({ value: NOW }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.close', (_, e) => {
    closed.push(e.id)
    return { value: undefined }
  })
  on('fs.write', (_, e) => {
    files[e.path] = e.text
    return { value: undefined }
  })
  on('fs.read', (_, e) => {
    const text = files[e.path]
    return text === undefined ? { deny: `ENOENT ${e.path}` } : { value: text }
  })
  on('fs.list', (_, e) => {
    const prefix = `${e.path.replace(/\/$/, '')}/`
    const names = Object.keys(files).filter(p => p.startsWith(prefix)).map(p => p.slice(prefix.length))
    return { value: names.map(name => ({ name, kind: 'file' as const, size: 1, mtimeMs: 0, isLink: false })) }
  })
  on('process.run', (_, e) => {
    const argv = [...e.argv]
    if (argv[0] === 'git' && argv.includes('--show-toplevel')) return { value: { ...ran, stdout: '/repo\n' } }
    if (argv[0] === 'git') return { value: { ...ran, stdout: 'main\n' } }
    return { value: ran }
  })
  return { files, closed }
}

test('the terminal groups the held lock, busy and idle sessions, with no SVG', async ($, on) => {
  fakeHost(on)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /^Needs a look/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /^Live/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /^Idle/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /push held by repo-wt-abcd/ })).toBeTruthy()
  // held locks start open and show the command
  expect(await ui.find({ type: 'Text', text: /\$ git push origin feat/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /notes · main · idle/ })).toBeTruthy()
  expect(await ui.find({ type: 'Svg' })).toBeFalsy()
  await ui.unmount()
})

test('a card opens from its name, and the header closes the pane', async ($, on) => {
  const host = fakeHost(on)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /^\/notes$/ })).toBeFalsy()
  await ui.press({ key: 'expand-peer-idle' })
  expect(await ui.find({ type: 'Text', text: /^\/notes$/ })).toBeTruthy()
  await ui.press({ key: 'refresh' })
  await ui.press({ key: 'close' })
  expect(host.closed).toContain('traffic')
  await ui.unmount()
})

test('the desktop app draws the coloured card bars and the stat tiles', async ($, on) => {
  fakeHost(on)
  await $.session.start({ cwd: '/repo', surface: 'desktop', isInteractive: true })
  const desk = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await desk.find({ type: 'Svg' })).toBeTruthy()
  expect(await desk.find({ type: 'Text', text: /^Other sessions$/ })).toBeTruthy()
  expect(await desk.find({ type: 'Text', text: /^Held$/ })).toBeTruthy()
  await desk.unmount()
})
