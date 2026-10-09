import { expect, test } from 'claude-code/testing'

import { parseRemember, recallContext, shouldRecall } from '../hooks/format'

const lesson = (id: number, similarity: number, project = 'demo') => ({
  id, project, kind: 'tooling', text: `lesson ${id}`, status: 'active', occurrences: 1, source: 'x', similarity,
})

test('only close matches are recalled, at most five', async () => {
  expect(recallContext([lesson(1, 0.2)], 'demo')).toBe(null)
  const text = recallContext([1, 2, 3, 4, 5, 6].map(i => lesson(i, 0.5)), 'demo') ?? ''
  expect(text.split('\n').length).toBe(6)
  expect(text).toContain('[lesson 1, demo, tooling]')
})

test('global lessons are labelled global', async () => {
  expect(recallContext([lesson(9, 0.6, 'global')], 'demo')).toContain('[lesson 9, global, tooling]')
})

test('remember parses the global prefix', async () => {
  expect(parseRemember('global: prefer short answers')).toEqual({ text: 'prefer short answers', isGlobal: true })
  expect(parseRemember('API runs on 8010')).toEqual({ text: 'API runs on 8010', isGlobal: false })
})

test('recall runs for typed prompts only', async () => {
  expect(shouldRecall('push it live please', 'composer')).toBe(true)
  expect(shouldRecall('a headless run prompt', 'sdk')).toBe(true)
  expect(shouldRecall('/ship', 'composer')).toBe(false)
  expect(shouldRecall('ok', 'composer')).toBe(false)
  expect(shouldRecall('a long background task notification', 'task-notification')).toBe(false)
})
