// Pure rules for throttle: presets, parsing /throttle's words, and what a request goes out with.
// No $ here, so the tests feed plain values.

import type { Ask, Effort, PresetName, Setting } from '../types'

export const EFFORTS: Effort[] = ['low', 'medium', 'high', 'xhigh', 'max']
export const MODELS = ['haiku', 'sonnet', 'opus'] as const

export const PRESETS: Record<PresetName, Omit<Setting, 'preset' | 'at'>> = {
  saver: { model: 'sonnet', effort: 'low', subagentModel: 'haiku' },
  normal: { model: null, effort: null, subagentModel: null },
  deep: { model: 'opus', effort: 'high', subagentModel: 'sonnet' },
  max: { model: 'opus', effort: 'max', subagentModel: 'opus' },
}
export const PRESET_NAMES = Object.keys(PRESETS) as PresetName[]

export const EMPTY: Setting = { preset: null, model: null, effort: null, subagentModel: null, at: 0 }

// A rank for comparing models by strength: an alias or any full id that names one.
export function modelRank(model: string) {
  const m = model.toLowerCase()
  if (m.includes('haiku')) return 0
  if (m.includes('sonnet')) return 1
  if (m.includes('opus') || m === 'best') return 2
  if (m.includes('fable')) return 3
  return 1
}

export const effortRank = (e: Effort | null | undefined) => (e ? EFFORTS.indexOf(e) : -1)
export const isEffort = (v: string): v is Effort => (EFFORTS as string[]).includes(v)
const isPreset = (v: string): v is PresetName => (PRESET_NAMES as string[]).includes(v)
const isModel = (v: string) => (MODELS as readonly string[]).includes(v) || /^claude-/.test(v)

// Apply an ask on top of a setting. A preset sets all three; the fields after it adjust.
export function applyAsk(cur: Setting, ask: Ask): Setting {
  let next: Setting = { ...cur, at: ask.at }
  if (ask.preset !== undefined) next = ask.preset === null ? { ...EMPTY, at: ask.at } : { ...PRESETS[ask.preset], preset: ask.preset, at: ask.at }
  const touched = ask.model !== undefined || ask.effort !== undefined || ask.subagentModel !== undefined
  if (ask.model !== undefined) next.model = ask.model
  if (ask.effort !== undefined) next.effort = ask.effort
  if (ask.subagentModel !== undefined) next.subagentModel = ask.subagentModel
  if (touched) next.preset = matchPreset(next)
  return next
}

export function matchPreset(s: Omit<Setting, 'preset' | 'at'>): PresetName | null {
  for (const name of PRESET_NAMES) {
    const p = PRESETS[name]
    if (p.model === s.model && p.effort === s.effort && p.subagentModel === s.subagentModel) return name
  }
  return null
}

// `/throttle` words into an ask, or an error to show. Words: a preset, `model <m>`, `effort <e>`,
// `sub <m>` / `subagents <m>`, `reset`; a bare model or effort name works too. `default` clears one field.
export function parse(args: string, at: number): Ask | { error: string } | null {
  const words = args.trim().toLowerCase().split(/\s+/).filter(Boolean)
  if (words.length === 0) return null
  const ask: Ask = { at }
  for (let i = 0; i < words.length; i++) {
    const w = words[i] ?? ''
    const val = words[i + 1]
    // `max` is a preset and an effort: right after a model ("opus max") it is the effort.
    const isEffortMax = w === 'max' && i > 0 && isModel(words[i - 1] ?? '')
    if (w === 'reset' || w === 'off') ask.preset = null
    else if (isPreset(w) && !isEffortMax) ask.preset = w
    else if (w === 'model' || w === 'effort' || w === 'sub' || w === 'subagents' || w === 'subagent') {
      if (!val) return { error: `${w} needs a value` }
      i++
      const v = val === 'default' ? null : val
      if (w === 'model') {
        if (v !== null && !isModel(v)) return { error: `unknown model "${val}" (haiku, sonnet, opus or a claude-… id)` }
        ask.model = v
      } else if (w === 'effort') {
        if (v !== null && !isEffort(v)) return { error: `unknown effort "${val}" (${EFFORTS.join(', ')})` }
        ask.effort = v
      } else {
        if (v !== null && !isModel(v)) return { error: `unknown model "${val}"` }
        ask.subagentModel = v
      }
    } else if (isEffort(w)) ask.effort = w
    else if (isModel(w)) ask.model = w
    else return { error: `didn't understand "${w}"` }
  }
  return ask
}

// What one main-loop request goes out with. While a recipe run is live, throttle only raises:
// a lower model or effort than the request already carries is held back.
export function resolveStep(s: Setting, model: string, effort: Effort | undefined, isRunLive: boolean) {
  let outModel = s.model ?? model
  let outEffort: Effort | undefined = effort !== undefined && s.effort !== null ? s.effort : effort
  let isHeld = false
  if (isRunLive) {
    if (modelRank(outModel) < modelRank(model)) {
      outModel = model
      isHeld = true
    }
    if (effort !== undefined && outEffort !== undefined && effortRank(outEffort) < effortRank(effort)) {
      outEffort = effort
      isHeld = true
    }
  }
  return { model: outModel, effort: outEffort, isHeld }
}

export function describe(s: Setting) {
  if (s.model === null && s.effort === null && s.subagentModel === null) return 'session defaults'
  const parts = [s.model ?? 'session model', s.effort ?? 'session effort', `subagents ${s.subagentModel ?? 'default'}`]
  return `${s.preset ? `${s.preset}: ` : ''}${parts.join(' · ')}`
}
