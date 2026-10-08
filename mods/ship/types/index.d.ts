export type RepoStatus = {
  root: string
  name: string
  branch: string
  changed: number
  ahead: number
  behind: number
  hasUpstream: boolean
  lastDeploy: { at: number; what: string } | null
  recipe: string | null
}

declare module 'claude-code' {
  interface PluginState {
    ship: { status: RepoStatus | null; isHidden: boolean }
  }
}
