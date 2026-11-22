import { expect, test } from 'claude-code/testing'

import { EMPTY, applyAsk, describe, parse, resolveStep } from '../hooks/gear'

type Body = Extract<Parameters<typeof test>[1], (...args: never[]) => unknown>
type Engine = Parameters<Body>[1]

const NOW = Date.parse('2026-11-21T12:00:00Z')
const CMD = { origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 120 } } as const

// ---- the pure rules

test('presets, single fields and reset combine as asked', () => {
  const deep = applyAsk(EMPTY, { preset: 'deep', at: 1 })
  expect(deep).toEqual({ preset: 'deep', model: 'opus', effort: 'high', subagentModel: 'sonnet', at: 1 })
  const tweaked = applyAsk(deep, { effort: 'max', at: 2 })
  expect(tweaked).toMatchObject({ preset: null, model: 'opus', effort: 'max' })
  expect(applyAsk(tweaked, { effort: 'high', at: 3 }).preset).toBe('deep')
  expect(applyAsk(tweaked, { preset: null, at: 4 })).toEqual({ ...EMPTY, at: 4 })
  expect(describe(EMPTY)).toBe('session defaults')
})

test('/throttle words parse into an ask, and bad words say why', () => {
  expect(parse('', 1)).toBeNull()
  expect(parse('saver', 1)).toEqual({ preset: 'saver', at: 1 })
  expect(parse('opus max', 1)).toEqual({ model: 'opus', effort: 'max', at: 1 })
  expect(parse('model sonnet effort default sub haiku', 1)).toEqual({ model: 'sonnet', effort: null, subagentModel: 'haiku', at: 1 })
  expect(parse('reset', 1)).toEqual({ preset: null, at: 1 })
  expect(parse('effort turbo', 1)).toEqual({ error: 'unknown effort "turbo" (low, medium, high, xhigh, max)' })
  expect(parse('model', 1)).toEqual({ error: 'model needs a value' })
})

test('a live recipe run only lets throttle raise', () => {
  const saver = applyAsk(EMPTY, { preset: 'saver', at: 1 })
  expect(resolveStep(saver, 'claude-opus-5-5', 'high', false)).toEqual({ model: 'sonnet', effort: 'medium', isHeld: false })
  expect(resolveStep(saver, 'claude-opus-5-5', 'high', true)).toEqual({ model: 'claude-opus-5-5', effort: 'high', isHeld: true })
  const max = applyAsk(EMPTY, { preset: 'max', at: 1 })
  expect(resolveStep(max, 'claude-sonnet-5-5', 'medium', true)).toEqual({ model: 'opus', effort: 'max', isHeld: false })
  // a model without effort gets none added
  expect(resolveStep(max, 'claude-haiku-5-5', undefined, false)).toEqual({ model: 'opus', effort: undefined, isHeld: false })
})

// ---- the hooks, against the engine

function fakeHost(on: Engine, others: Record<string, unknown> = {}) {
  const sent: { model: string; effort?: unknown }[] = []
  const spawned: (string | undefined)[] = []
  for (const plugin of ['mods-panel', 'recipe-run', 'burn-meter']) {
    on('state.get', { plugin } as never, (_: unknown, e: { plugin: string; key: string }) => {
      const value = others[`${e.plugin}/${e.key}`]
      return { value: { value, version: value === undefined ? 0 : 1 } }
    })
  }
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('clock.every', () => ({ value: undefined }))
  on('clock.now', () => ({ value: NOW }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('turn.step', async function* (_, e) {
    sent.push({ model: e.model, effort: e.effort })
    return { turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: 'end_turn', usage: null } as never
  })
  on('agent.spawn', (_, e) => {
    spawned.push(e.model)
    return { model: e.model ?? 'inherit', agentId: 'a1' } as never
  })
  return { sent, spawned }
}

async function step($: Parameters<Body>[0], over: Record<string, unknown> = {}) {
  const stream = $.turn.step({ turnId: 't1', index: 0, model: 'claude-opus-5-5', effort: 'high', messageCount: 3, ...over } as never)
  for await (const _ of stream) void _
}

const spawn = ($: Parameters<Body>[0], over: Record<string, unknown> = {}) =>
  $.agent.spawn({ prompt: 'p', description: 'd', subagentType: 'general-purpose', parentModel: 'claude-opus-5-5', background: false, fork: false, ...over } as never)

test('/throttle swaps the next request and subagents without a model', async ($, on) => {
  const host = fakeHost(on)
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })

  await step($)
  expect(host.sent.at(-1)).toEqual({ model: 'claude-opus-5-5', effort: 'high' })

  const r = await $.command.run({ command: 'throttle', args: 'saver', ...CMD })
  expect(r.text).toContain('Throttle: saver: sonnet · medium · subagents haiku.')
  await step($)
  expect(host.sent.at(-1)).toEqual({ model: 'sonnet', effort: 'medium' })

  await spawn($)
  await spawn($, { model: 'opus' })
  await spawn($, { fork: true })
  expect(host.spawned).toEqual(['haiku', 'opus', undefined])

  // a subagent's own requests are left alone
  await step($, { agentId: 'a1' })
  expect(host.sent.at(-1)).toEqual({ model: 'claude-opus-5-5', effort: 'high' })
})

test('a Mods panel ask newer than the setting applies at the next request', async ($, on) => {
  const host = fakeHost(on, { 'mods-panel/throttleAsk': { preset: 'max', at: NOW } })
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  await step($, { model: 'claude-sonnet-5-5', effort: 'medium' })
  expect(host.sent.at(-1)).toEqual({ model: 'opus', effort: 'max' })
})

test('during a live recipe run a lower setting is held and /throttle says so', async ($, on) => {
  const run = { stages: [{ isCurrent: true }], updatedAt: NOW - 60_000 }
  const host = fakeHost(on, { 'recipe-run/run': run })
  await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const r = await $.command.run({ command: 'throttle', args: 'saver', ...CMD })
  expect(r.text).toContain('throttle only raises')
  await step($)
  expect(host.sent.at(-1)).toEqual({ model: 'claude-opus-5-5', effort: 'high' })
  const status = await $.command.run({ command: 'throttle', args: '', ...CMD })
  expect(status.text).toContain('held while the recipe run is live')
})
