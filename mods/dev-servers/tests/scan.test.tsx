import { expect, test } from 'claude-code/testing'

type Body = Extract<Parameters<typeof test>[1], (...args: never[]) => unknown>
type Engine = Parameters<Body>[1]

const ran = { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false }

// Two listeners: vite run from a package's .bin, and roughdraft from a global install whose folder has no package.json.
function fakeHost(on: Engine) {
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('tool.register', () => ({ value: undefined }))
  on('clock.every', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('store.get', () => ({ value: undefined }))
  on('process.run', (_, e) => {
    const argv = [...e.argv]
    if (argv[0] === 'git') {
      if (argv[2] === '/Users/me/notes') return { value: { ...ran, stdout: '/Users/me/notes\n' } }
      return { value: { ...ran, stdout: '/Users/me/pm-tool\n' } }
    }
    if (argv[0] === 'lsof' && argv.includes('-Fpcn')) return { value: { ...ran, stdout: 'p10\ncnode\nn127.0.0.1:5173\np20\ncnode\nn[::1]:7373\np30\ncpostgres\nn*:5432\n' } }
    if (argv[0] === 'lsof') return { value: { ...ran, stdout: 'p10\nfcwd\nn/repo/web\np20\nfcwd\nn/Users/me/notes\n' } }
    if (argv[0] === 'ps')
      return { value: { ...ran, stdout: '10 1:00 node /repo/web/node_modules/.bin/vite --port 5173\n20 2:00 /usr/local/bin/node /Users/me/.npm-global/lib/node_modules/roughdraft/dist/child.js\n' } }
    if (argv[0] === 'sh' && argv[4] === '/repo/web') return { value: { ...ran, stdout: '{"name":"web"}' } }
    if (argv[0] === 'sh') return { value: { ...ran, exitCode: 1 } }
    return { value: { ...ran, exitCode: 1 } }
  })
}

test('each server names the tool it runs and the project its folder belongs to', async ($, on) => {
  fakeHost(on)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({
    plugin: 'dev-servers',
    component: 'Pane',
    requestId: 'dev-servers',
    surface: 'terminal',
    props: { title: 'Dev servers', isFocused: false, bodyColumns: 120, placement: 'dock', scroll: { offset: 0, bodyRows: 50 }, view: {} },
  } as never)
  expect(await ui.find({ type: 'Text', text: /localhost:5173/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /vite in pm-tool\/web · \/repo\/web/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /roughdraft in notes · ~\/notes/ })).toBeTruthy()
  await ui.unmount()
})
