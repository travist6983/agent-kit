export type Lesson = {
  id: number
  project: string
  kind: string
  text: string
  status: string
  occurrences: number
  source: string
  similarity?: number
}

export type BrainHealth = {
  isOnline: boolean
  reason: string
  project: string
  scope: string
  target: string
  lessons: number
  proposed: number
  checkedAt: number
}

export type Recall = {
  query: string
  at: number
  lessons: Lesson[]
  error: string | null
}

declare module 'claude-code' {
  interface PluginState {
    brain: {
      health: BrainHealth | null
      recall: Recall | null
      review: Lesson[]
      active: Lesson[]
      tab: 'recalled' | 'review' | 'lessons'
    }
  }
}
