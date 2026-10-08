import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, Timer } from 'claude-code'

import type { BurnSettings } from '../types'

const DEFAULTS: BurnSettings = { budgetUsd: null, isHard: false, isWorkflowOff: false }
const settings = atom({ plugin: 'burn-meter', key: 'settings' } as const, DEFAULTS)
const HEARTBEAT_MS = 5 * 60_000
const FANOUT = new Set(['Workflow', 'Agent'])

const money = (usd: number) => `$${usd.toFixed(usd < 10 ? 2 : 0)}`
const clock = (ms: number) => {
  const s = Math.floor(ms / 1000)
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s`
}

const t = {
  turnId: null as string | null,
  startedAt: 0,
  costAtStart: 0,
  steps: 0,
  lastTool: '',
  warned: 0,
  ticker: null as Timer | null,
  heartbeat: null as Timer | null,
}

async function sessionCost($: EngineInterface) {
  return (await $.session.usage()).cost?.usd ?? 0
}

async function turnCost($: EngineInterface) {
  return Math.max(0, (await sessionCost($)) - t.costAtStart)
}

async function draw($: EngineInterface) {
  const s = await read($, settings)
  const usage = await $.session.usage()
  const total = usage.cost?.usd ?? 0
  const ctx = usage.context.percent !== undefined ? ` · ctx ${usage.context.percent}%` : ''
  const flags = [s.budgetUsd !== null ? `budget ${money(s.budgetUsd)}${s.isHard ? ' hard' : ''}` : null, s.isWorkflowOff ? 'no workflows' : null]
    .filter(Boolean)
    .join(' · ')
  if (t.turnId === null) {
    $.ui.status(`session ${money(total)}${ctx}${flags ? ` · ${flags}` : ''}`)
    return
  }
  const elapsed = (await $.clock.now()) - t.startedAt
  const spent = total - t.costAtStart
  $.ui.status(`⏱ ${clock(elapsed)} · ${t.steps} t.steps · turn ${money(spent)} · session ${money(total)}${ctx}${flags ? ` · ${flags}` : ''}`)

  if (s.budgetUsd !== null) {
    const ratio = spent / s.budgetUsd
    if (ratio >= 1 && t.warned < 3) {
      t.warned = 3
      if (s.isHard) {
        $.ui.toast(`Budget ${money(s.budgetUsd)} hit: stopping the turn.`, { timeoutMs: 10_000 })
        void $.turn.abort({ turnId: t.turnId })
      } else {
        $.ui.toast(`Budget ${money(s.budgetUsd)} hit: new workflows and subagents are blocked this turn.`, { timeoutMs: 10_000 })
      }
    } else if (ratio >= 0.8 && t.warned < 2) {
      t.warned = 2
      $.ui.toast(`80% of the ${money(s.budgetUsd)} turn budget used.`)
    } else if (ratio >= 0.5 && t.warned < 1) {
      t.warned = 1
      $.ui.toast(`Half of the ${money(s.budgetUsd)} turn budget used.`)
    }
  }
}

async function saveSettings($: EngineInterface, fn: (s: BurnSettings) => BurnSettings) {
  await update($, settings, fn)
  await $.store.set('settings', await read($, settings))
  await draw($)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const saved = (await $.store.get('settings')) as BurnSettings | undefined
    if (saved) await update($, settings, () => ({ ...DEFAULTS, ...saved }))
    await $.command.register({
      name: 'budget',
      description: 'Per-turn spend limit: /budget 5 (warn, block fan-out), /budget 5 hard (stop the turn), /budget off',
      argumentHint: '<usd> [hard] | off',
      immediate: true,
    })
    await $.command.register({
      name: 'workflows',
      description: 'Allow or block multi-agent workflows: /workflows off | on',
      argumentHint: 'on | off',
      immediate: true,
    })
    await draw($)
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    t.turnId = e.turnId
    t.startedAt = await $.clock.now()
    t.costAtStart = await sessionCost($)
    t.steps = 0
    t.warned = 0
    t.lastTool = ''
    t.ticker?.cancel()
    t.heartbeat?.cancel()
    t.ticker = $.clock.every(5000, () => void draw($))
    t.heartbeat = $.clock.every(HEARTBEAT_MS, () => {
      void (async () => {
        const elapsed = (await $.clock.now()) - t.startedAt
        $.ui.toast(`Still working · ${clock(elapsed)} · ${t.steps} t.steps · ${money(await turnCost($))}${t.lastTool ? ` · last: ${t.lastTool}` : ''}`, {
          timeoutMs: 8000,
        })
      })()
    })
    await draw($)
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    if (e.agentId === undefined) {
      t.steps += 1
      t.lastTool = e.tool === 'Bash' && typeof e.description === 'string' ? e.description : String(e.tool)
    }
    if (FANOUT.has(String(e.tool))) {
      const s = await read($, settings)
      if (s.isWorkflowOff && e.tool === 'Workflow') {
        return {
          deny: 'The user has switched off multi-agent workflows (/workflows off). Do this work directly in the main session, reading only what you need.',
        }
      }
      if (s.budgetUsd !== null && t.turnId !== null && (await turnCost($)) >= s.budgetUsd) {
        return {
          deny: `This turn has used its ${money(s.budgetUsd)} budget (/budget). Do not start workflows or subagents; finish with what you have and tell the user what is left.`,
        }
      }
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined && e.turnId === t.turnId) {
      t.ticker?.cancel()
      t.heartbeat?.cancel()
      const spent = await turnCost($)
      if (e.durationMs > 3 * 60_000) {
        $.ui.toast(`Done · ${clock(e.durationMs)} · ${t.steps} t.steps · ${money(spent)}`, { timeoutMs: 10_000 })
      }
      t.turnId = null
      await draw($)
    }
    return next(e)
  })

  on('command.run', { command: 'budget' }, async ($, e) => {
    const args = e.args.trim().toLowerCase()
    if (args === 'off' || args === '') {
      await saveSettings($, s => ({ ...s, budgetUsd: null, isHard: false }))
      return { text: 'Turn budget off.' }
    }
    const usd = Number(args.replace(/[$\s]|hard/g, ''))
    if (!Number.isFinite(usd) || usd <= 0) return { text: 'Usage: /budget 5 | /budget 5 hard | /budget off' }
    const isHard = /\bhard\b/.test(args)
    await saveSettings($, s => ({ ...s, budgetUsd: usd, isHard }))
    return { text: `Turn budget ${money(usd)}${isHard ? ': the turn stops when it is hit.' : ': warnings at 50% and 80%, then workflows and subagents are blocked.'}` }
  })

  on('command.run', { command: 'workflows' }, async ($, e) => {
    const isOff = e.args.trim().toLowerCase() !== 'on'
    await saveSettings($, s => ({ ...s, isWorkflowOff: isOff }))
    return { text: isOff ? 'Workflows blocked in every session until /workflows on.' : 'Workflows allowed.' }
  })
}
