export type Peer = {
  id: string
  label: string
  cwd: string
  root: string
  worktree: string
  branch: string
  lastTool: string
  lastSeen: number
}

export type Lock = {
  key: string
  kind: 'push' | 'deploy'
  root: string
  holder: string
  label: string
  command: string
  at: number
}

declare module 'claude-code' {
  interface PluginState {
    'traffic-control': { me: string; peers: Peer[]; locks: Lock[] }
  }
}
