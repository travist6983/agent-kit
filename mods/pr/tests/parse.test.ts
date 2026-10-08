import { expect, test } from 'claude-code/testing'

import { summary, waitingThreads } from '../hooks/parse'

const thread = (isResolved: boolean, authors: string[]) => ({
  isResolved,
  isOutdated: false,
  path: 'site.js',
  line: 10,
  comments: { nodes: authors.map((a, i) => ({ author: { login: a }, body: `c${i}`, url: `u${a}${i}` })) },
})

test('only unresolved threads where someone else spoke last', async () => {
  const out = waitingThreads([thread(false, ['rev']), thread(false, ['rev', 'me']), thread(true, ['rev']), thread(false, ['me', 'rev'])], 'me')
  expect(out.length).toBe(2)
  expect(out.every(t => t.author === 'rev')).toBe(true)
})

test('status line reads like the article', async () => {
  const check = (bucket: string) => ({ name: bucket, workflow: '', bucket, state: '', startedAt: '', completedAt: '', link: '' })
  const text = summary({ number: 42, isDraft: true }, ['pass', 'pass', 'pass', 'pass', 'pending'].map(check), waitingThreads([thread(false, ['a']), thread(false, ['b'])], 'me'))
  expect(text).toBe('PR #42 draft · CI 4/5 …1 · 2 threads')
})
