export type BurnSettings = {
  budgetUsd: number | null
  isHard: boolean
  isWorkflowOff: boolean
}

declare module 'claude-code' {
  interface PluginState {
    'burn-meter': { settings: BurnSettings }
  }
}
