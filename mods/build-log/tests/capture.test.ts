import { expect, test } from 'claude-code/testing'

import { latestShots, originOf, parseMerge, parseRouter, parseShotArgs, routerAction, slugOf } from '../hooks/capture'

const config = { deny: { names: ['x'] }, safe_repos: ['sams-toys', 'sam/side-project'] }

test('only the kit and listed repos are the owner\'s own; anything else is work', async () => {
  expect(originOf({ name: 'agent-kit', slug: 'travist6983/agent-kit', loopProject: '' }, config)).toBe('own')
  expect(originOf({ name: 'sams-toys', slug: '', loopProject: '' }, config)).toBe('own')
  expect(originOf({ name: 'renamed', slug: 'sam/side-project', loopProject: '' }, config)).toBe('own')
  expect(originOf({ name: 'portal', slug: 'acme/portal', loopProject: '' }, config)).toBe('work')
  expect(originOf({ name: '', slug: '', loopProject: '' }, config)).toBe('work')
  // A fork or clone of the kit under another owner is not the kit.
  expect(originOf({ name: 'agent-kit', slug: 'acme/agent-kit', loopProject: '' }, config)).toBe('work')
})

test('a loop session in the kit counts as the project it works on', async () => {
  expect(originOf({ name: 'agent-kit', slug: 'travist6983/agent-kit', loopProject: 'acme-portal' }, config)).toBe('work')
  expect(originOf({ name: 'agent-kit', slug: 'travist6983/agent-kit', loopProject: 'sams-toys' }, config)).toBe('own')
})

test('remotes become owner/name slugs', async () => {
  expect(slugOf('git@github.com:travist6983/agent-kit.git')).toBe('travist6983/agent-kit')
  expect(slugOf('https://github.com/acme/portal\n')).toBe('acme/portal')
})

test('merges and notable router calls are recognized', async () => {
  expect(parseMerge('gh pr merge 42 --squash')).toEqual({ pr: 42 })
  expect(parseMerge('gh pr merge --merge')).toEqual({ pr: null })
  expect(parseMerge('gh pr view 42')).toBe(null)
  expect(parseRouter('python3 .claude/loops/lib/recipe_router.py --stage fix --result fail --attempt 2')).toEqual({ stage: 'fix', result: 'fail' })
  expect(parseRouter('python3 other.py --stage fix')).toBe(null)
  expect(routerAction('working\n{"action": "re-fix", "reason": "flaky"}\n')).toBe('re-fix')
  expect(routerAction('no verdict')).toBe('')
})

test('the shot ledger keeps the latest status and hides dropped shots', async () => {
  const lines = [
    JSON.stringify({ id: 's-1', file: '/a.png', caption: 'a', status: 'review', origin: 'work' }),
    JSON.stringify({ id: 's-2', file: '/b.png', caption: 'b', status: 'ok', origin: 'own' }),
    '{torn',
    JSON.stringify({ id: 's-1', status: 'ok' }),
    JSON.stringify({ id: 's-2', status: 'dropped' }),
  ]
  const shots = latestShots(lines)
  expect(shots.map(s => [s.id, s.status, s.file])).toEqual([['s-1', 'ok', '/a.png']])
})

test('shot arguments take a quoted path with spaces', async () => {
  expect(parseShotArgs('"/tmp/my shot.png" the mods pane')).toEqual({ path: '/tmp/my shot.png', caption: 'the mods pane' })
  expect(parseShotArgs('/tmp/a.png')).toEqual({ path: '/tmp/a.png', caption: '' })
})
