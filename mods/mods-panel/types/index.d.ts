export type RateLimit = { kind: string; percentUsed: number; resetsAt?: string }

export type Usage = { costUsd: number | null; rateLimits: RateLimit[]; at: number }

// A question this panel answered in this session, kept so the card can show "Answered" until
// recipe-run's next refresh drops it from its open list.
// A throttle change pressed in the panel. throttle reads it and applies anything newer than what
// it has: a field left out stays, null clears it back to the session's own choice.
export type ThrottleAsk = {
  preset?: 'saver' | 'normal' | 'deep' | 'max' | null
  model?: string | null
  effort?: 'low' | 'medium' | 'high' | 'xhigh' | 'max' | null
  subagentModel?: string | null
  at: number
}

export type PanelAnswer = { id: string; key: string; question: string; answer: string; answeredAt: string }

declare module 'claude-code' {
  interface PluginState {
    'mods-panel': {
      // expanded cards by mod name; null until the person opens or closes one (then the panel picks)
      open: string[] | null
      tab: 'run' | 'questions'
      usage: Usage | null
      // cost of each finished main turn this session, oldest first
      turns: number[]
      // enabledPlugins as settings read it: name -> on, off; a name missing is not installed
      enabled: Record<string, boolean>
      // a toggle pressed this session that waits for /reload-plugins: name -> wanted state
      toggled: Record<string, boolean>
      answered: PanelAnswer[]
      pending: PanelAnswer[]
      isBandOn: boolean
      // the pid whose Stop was pressed once and waits for a second press to confirm
      stopArmed: number | null
      // pids stopped from the panel, hidden until dev-servers' next scan drops them
      stopped: number[]
      // the last throttle change pressed here; throttle owns the setting itself
      throttleAsk: ThrottleAsk | null
    }
  }
}
