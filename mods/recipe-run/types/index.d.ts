export type Tone = 'good' | 'bad' | 'warn' | 'none'

export type StageRow = {
  id: string
  label: string
  outcome: string | null
  tone: Tone
  attempts: number
  isCurrent: boolean
  detail: string | null
}

export type Verdict = {
  stage: string
  result: string
  attempt: number | null
  max: number | null
  action: string
  reason: string
  budgetSpent: number | null
  budgetCap: number | null
  at: number
}

export type RunView = {
  root: string
  key: string
  recipe: string | null
  tier: string | null
  stages: StageRow[]
  verdicts: Verdict[]
  updatedAt: number
}

export type QuestionKind = 'blocking' | 'non-blocking'

export type Question = {
  id: string
  key: string
  from: string
  kind: QuestionKind
  question: string
  options: string[]
  default: string | null
  context: string | null
  askedAt: string
}

export type Answer = {
  id: string
  key: string
  answer: string
  answeredAt: string
}

export type Target = { root: string; key: string }

declare module 'claude-code' {
  interface PluginState {
    'recipe-run': {
      run: RunView | null
      target: Target | null
      pinned: string | null
      open: Question[]
      tab: 'run' | 'questions'
      selected: string | null
      delivered: string[]
      pending: Answer[]
    }
  }
}
