import type { Lesson } from '../types'

export const MIN_SCORE = 0.35
export const MAX_RECALL = 5

// The context block the model reads beside a prompt. Empty when nothing is worth recalling.
export function recallContext(lessons: Lesson[], project: string): string | null {
  const useful = lessons.filter(l => (l.similarity ?? 0) >= MIN_SCORE).slice(0, MAX_RECALL)
  if (useful.length === 0) return null
  const lines = useful.map(l => `- [lesson ${l.id}, ${l.project === 'global' ? 'global' : l.project}, ${l.kind}] ${l.text}`)
  return [
    `Lessons recalled from the brain for this prompt (project ${project}). They were learned in earlier sessions and may be out of date: apply the ones that fit, and if one is wrong, say so.`,
    ...lines,
  ].join('\n')
}

// `/remember global: text` stores a global lesson; anything else is for the current project.
export function parseRemember(args: string): { text: string; isGlobal: boolean } {
  const m = args.trim().match(/^global\s*:\s*(.+)$/is)
  return m ? { text: (m[1] ?? '').trim(), isGlobal: true } : { text: args.trim(), isGlobal: false }
}

export function shouldRecall(text: string, origin: string): boolean {
  if (origin !== 'composer' && origin !== 'bridge' && origin !== 'sdk') return false
  const t = text.trim()
  return t.length >= 8 && !t.startsWith('/')
}

export function shorten(text: string, n: number) {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > n ? `${flat.slice(0, n - 1)}…` : flat
}
