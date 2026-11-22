import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Ask, Effort, Seen, Setting } from '../types'
import { EMPTY, PRESET_NAMES, applyAsk, describe, isEffort, parse, resolveStep } from './gear'

const setting = atom({ plugin: 'throttle', key: 'setting' } as const, EMPTY)
const seen = atom({ plugin: 'throttle', key: 'seen' } as const, null)

// Other mods' values, read only. The Mods panel writes its own `throttleAsk`; throttle takes
// any ask newer than what it applied, on a short timer and before every model request.
const PANEL_ASK = { plugin: 'mods-panel', key: 'throttleAsk' } as const
const RR_RUN = { plugin: 'recipe-run', key: 'run' } as const
const BURN = { plugin: 'burn-meter', key: 'settings' } as const

const ABSORB_MS = 1500
const RUN_LIVE_MS = 2 * 60 * 60_000
const BUDGET_WARN = 0.8
const mem = { isBudgetWarned: false }

// A value another mod never wrote reads undefined at version 0: that mod is off, or not loaded yet.
async function other<T>(p: Promise<unknown>) {
  const r = (await p) as { value?: unknown; version: number }
  return r.version > 0 ? (r.value as T) : undefined
}

async function absorb($: EngineInterface) {
  const ask = await other<Ask | null>($.state.get(PANEL_ASK as never))
  const cur = await read($, setting)
  if (!ask || ask.at <= cur.at) return cur
  const next = applyAsk(cur, ask)
  await update($, setting, () => next)
  drawStatus($, next)
  return next
}

async function isRunLive($: EngineInterface) {
  const run = await other<{ stages: { isCurrent: boolean }[]; updatedAt: number } | null>($.state.get(RR_RUN as never))
  if (!run) return false
  return run.stages.some(s => s.isCurrent) && (await $.clock.now()) - run.updatedAt < RUN_LIVE_MS
}

function drawStatus($: EngineInterface, s: Setting) {
  const isDefault = s.model === null && s.effort === null && s.subagentModel === null
  $.ui.status(isDefault ? undefined : `throttle: ${s.preset ?? [s.model, s.effort].filter(Boolean).join(' ')}`)
}

async function apply($: EngineInterface, ask: Ask) {
  const next = applyAsk(await read($, setting), ask)
  await update($, setting, () => next)
  drawStatus($, next)
  return next
}

function help(s: Setting, last: Seen | null) {
  const lines = [`Throttle: ${describe(s)}.`]
  if (last) lines.push(`Last request: ${last.model}${last.effort ? ` · ${last.effort}` : ''}${last.isHeld ? ' (a lower setting is held while the recipe run is live)' : ''}.`)
  lines.push(`Use: /throttle ${PRESET_NAMES.join('|')} · model <haiku|sonnet|opus> · effort <low…max> · sub <model> · reset`)
  return lines.join('\n')
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'throttle',
      description: 'Change model, effort and subagent model mid-run',
      argumentHint: `[${PRESET_NAMES.join('|')} | model <m> | effort <e> | sub <m> | reset]`,
      immediate: true,
    })
    drawStatus($, await read($, setting))
    $.clock.every(ABSORB_MS, () => void absorb($))
    return next(e)
  })

  on('command.run', { command: 'throttle' }, async ($, e) => {
    const ask = parse(e.args, await $.clock.now())
    if (ask === null) return { text: help(await absorb($), await read($, seen)) }
    if ('error' in ask) return { text: `Throttle: ${ask.error}.\n${help(await read($, setting), await read($, seen))}` }
    const next = await apply($, ask)
    const held = (await isRunLive($)) ? ' A recipe run is live: throttle only raises model or effort until it ends.' : ''
    return { text: `Throttle: ${describe(next)}. Takes effect at the next model request.${held}` }
  })

  // Every main-loop model request: swap model and effort for this one request.
  // It streams, so the hook is a generator; anything that fails before the request sends it unchanged.
  on('turn.step', async function* ($, e, next) {
    if (e.agentId !== undefined) return yield* next(e)
    let out: ReturnType<typeof resolveStep> | null = null
    const effort = typeof e.effort === 'string' && isEffort(e.effort) ? e.effort : undefined
    try {
      const s = await absorb($)
      out = resolveStep(s, e.model, effort, await isRunLive($))
      const last: Seen = {
        model: out.model,
        effort: (out.effort ?? null) as Effort | null,
        sessionModel: e.model,
        sessionEffort: effort ?? null,
        isHeld: out.isHeld,
        at: await $.clock.now(),
      }
      await update($, seen, () => last)
    } catch {
      out = null
    }
    if (out === null || (out.model === e.model && out.effort === effort)) return yield* next(e)
    return yield* next({ ...e, model: out.model, ...(out.effort !== undefined ? { effort: out.effort } : {}) })
  })

  // Subagents that name no model of their own take the throttle's. An explicit model (a recipe
  // stage's, a workflow's) is left alone, and forks always inherit.
  on('agent.spawn', async ($, e, next) => {
    const s = await read($, setting)
    if (s.subagentModel === null || e.model !== undefined || e.fork || e.workflow !== undefined || e.resume !== undefined) return next(e)
    return next({ ...e, model: s.subagentModel })
  }).catch(($, e, next) => next(e))

  // Near burn-meter's budget, offer the saver preset once. Never switches by itself.
  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined && !mem.isBudgetWarned) {
      const burn = await other<{ budgetUsd: number | null }>($.state.get(BURN as never))
      const s = await read($, setting)
      if (burn?.budgetUsd && s.preset !== 'saver') {
        const usd = (await $.session.usage()).cost?.usd ?? 0
        if (usd >= burn.budgetUsd * BUDGET_WARN) {
          mem.isBudgetWarned = true
          $.ui.toast(`Spend is at $${usd.toFixed(2)} of your $${burn.budgetUsd} budget. /throttle saver drops to Sonnet · low.`, { timeoutMs: 12_000 })
        }
      }
    }
    return next(e)
  })
}
