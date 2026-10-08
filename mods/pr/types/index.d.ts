export type PrCheck = {
  name: string
  workflow: string
  bucket: string
  state: string
  startedAt: string
  completedAt: string
  link: string
}

export type PrThread = {
  path: string
  line: number | null
  isOutdated: boolean
  author: string
  body: string
  url: string
  replies: number
}

export type PrInfo = {
  number: number
  title: string
  url: string
  isDraft: boolean
  reviewDecision: string
}

export type PrSnapshot = {
  branch: string
  pr: PrInfo | null
  checks: PrCheck[]
  threads: PrThread[]
  error: string | null
  fetchedAt: number
}

declare module 'claude-code' {
  interface PluginState {
    pr: { snapshot: PrSnapshot | null; tab: 'checks' | 'threads'; isLoading: boolean }
  }
}
