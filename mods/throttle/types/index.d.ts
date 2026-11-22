export type Effort = 'low' | 'medium' | 'high' | 'xhigh' | 'max'
export type PresetName = 'saver' | 'normal' | 'deep' | 'max'

// What throttle applies. A null field leaves the session's own choice alone.
export type Setting = {
  preset: PresetName | null
  model: string | null
  effort: Effort | null
  subagentModel: string | null
  at: number
}

// A change asked from somewhere other than /throttle (the Mods panel writes one of its own):
// a field left out stays as it is, null clears it back to the session's choice.
export type Ask = {
  preset?: PresetName | null
  model?: string | null
  effort?: Effort | null
  subagentModel?: string | null
  at: number
}

// What the last main-loop request actually went out with, after throttle, and why.
export type Seen = {
  model: string
  effort: Effort | null
  sessionModel: string
  sessionEffort: Effort | null
  // a lower setting held back while a recipe run is live (escalate, never de-escalate)
  isHeld: boolean
  at: number
}

declare module 'claude-code' {
  interface PluginState {
    throttle: { setting: Setting; seen: Seen | null }
  }
}
