import { expect, test } from 'claude-code/testing'

const NOW = Date.parse('2026-11-21T12:00:00Z')

type Body = Extract<Parameters<typeof test>[1], (...args: never[]) => unknown>
type Engine = Parameters<Body>[1]

// The engine: it refuses any command name in `taken`, as Claude Code refuses a built-in's name.
function fakeEngine(on: Engine, taken: string[]) {
  const registered: string[] = []
  const statuses: string[] = []
  const store = new Map<string, unknown>()
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('command.register', (_, e) => {
    if (taken.includes(e.name)) return { deny: `"/${e.name}" refused: it is the built-in /${e.name}` }
    registered.push(e.name)
    return { value: { command: e.name } }
  })
  on('session.usage', () => ({ value: { startedAt: NOW, context: { window: 200_000 }, rateLimits: [], cost: { usd: 1.5 } } }))
  on('ui.status', (_, e) => {
    statuses.push(String((e as { text?: unknown }).text ?? e))
    return { value: undefined }
  })
  on('ui.toast', () => ({ value: undefined }))
  on('clock.now', () => ({ value: NOW }))
  on('store.get', (_, e) => ({ value: store.get(e.key) }))
  on('store.set', (_, e) => {
    store.set(e.key, e.value)
    return { value: undefined }
  })
  return { registered, store, statuses }
}

test('a refused command name no longer stops burn-meter from starting', async ($, on) => {
  const eng = fakeEngine(on, ['workflows', 'fanout'])
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  expect(eng.registered).toContain('budget')
  // The status line is drawn last in session.start: it only shows when startup ran to the end.
  expect(eng.statuses.length).toBeGreaterThan(0)
})

test('/fanout off blocks the Workflow tool, and it is not registered as /workflows', async ($, on) => {
  const eng = fakeEngine(on, ['workflows'])
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  expect(eng.registered).toEqual(['budget', 'fanout'])
  const off = await $.command.run({ command: 'fanout', args: 'off', origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 120 } })
  expect(off.text).toBe('Workflows blocked in every session until /fanout on.')
  const call = await $.tool.call({ tool: 'Workflow', script: '' })
  expect(String(call.deny ?? '')).toContain('/fanout off')
})
