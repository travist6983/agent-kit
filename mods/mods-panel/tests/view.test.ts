import { expect, test } from 'claude-code/testing'

import { answersContext, appLabel, band, cards, filled, ladder, limitRows, openQuestions, power, recipeView, serverRows, sessionRows, span, summary } from '../hooks/view'
import type { QuestionIn, RunIn, Snapshot } from '../hooks/view'

const NOW = Date.parse('2026-11-21T12:00:00Z')

const RUN: RunIn = {
  root: '/repo',
  key: 'AK-142',
  recipe: 'dev-loop-defect',
  tier: null,
  stages: [
    { id: 'intake', label: 'Intake', outcome: 'ok', tone: 'good', attempts: 1, isCurrent: false, detail: null },
    { id: 'fix', label: 'Fix', outcome: null, tone: 'none', attempts: 2, isCurrent: true, detail: null },
    { id: 'verify', label: 'Verify', outcome: null, tone: 'none', attempts: 0, isCurrent: false, detail: null },
  ],
  verdicts: [
    { stage: 'verify', result: 'fail', attempt: 2, max: 3, action: 're-fix', reason: '2 of 14 auth tests timed out', budgetSpent: 2.76, budgetCap: 10, at: NOW - 60_000 },
  ],
  updatedAt: NOW - 60_000,
}

const Q = (id: string, kind: QuestionIn['kind']): QuestionIn => ({
  id,
  key: 'AK-142',
  from: 'review',
  kind,
  question: `Question ${id}?`,
  options: ['Yes', 'No'],
  default: kind === 'non-blocking' ? 'Yes' : null,
  context: null,
  askedAt: '2026-11-21T11:00:00Z',
})

function snap(over: Partial<Snapshot> = {}): Snapshot {
  return {
    now: NOW,
    enabled: { 'recipe-run@agent-kit': true, 'burn-meter@agent-kit': true, 'pr@agent-kit': false },
    toggled: {},
    usage: {
      costUsd: 3.18,
      rateLimits: [
        { kind: 'five_hour', percentUsed: 54, resetsAt: new Date(NOW + 127 * 60_000).toISOString() },
        { kind: 'seven_day', percentUsed: 31 },
      ],
      at: NOW,
    },
    turns: [0.22, 0.41],
    recipe: { run: RUN, open: [] },
    answered: [],
    burn: null,
    traffic: null,
    ship: null,
    dev: null,
    pr: null,
    brain: null,
    ...over,
  }
}

test('spans and segments', () => {
  expect(span(38 * 60_000)).toBe('38m')
  expect(span(127 * 60_000)).toBe('2h 7m')
  expect(span((6 * 24 + 15) * 3600_000)).toBe('6d 15h')
  expect(filled(20, 0.276)).toBe(6)
  expect(filled(16, 2)).toBe(16)
  expect(filled(16, Number.NaN)).toBe(0)
})

test('power reads enabledPlugins, and live state counts as on', () => {
  expect(power('recipe-run', { 'recipe-run@agent-kit': true }, false)).toBe('on')
  expect(power('pr', { 'pr@agent-kit': false }, false)).toBe('off')
  expect(power('ship', {}, false)).toBe('missing')
  expect(power('ship', {}, true)).toBe('on')
})

test('limit rows name the windows and mark 80 percent as hot', () => {
  const rows = limitRows([{ kind: 'five_hour', percentUsed: 90, resetsAt: new Date(NOW + 38 * 60_000).toISOString() }, { kind: 'seven_day', percentUsed: 61 }], NOW)
  expect(rows[0]).toEqual({ kind: 'five_hour', label: '5 hour', pct: 90, reset: 'in 38m', isHot: true })
  expect(rows[1]?.label).toBe('weekly')
  expect(rows[1]?.reset).toBe(null)
  expect(rows[1]?.isHot).toBe(false)
})

test('the run card shows stage, attempt and spend; the ladder marks each stage', () => {
  const list = cards(snap())
  const rr = list.find(c => c.name === 'recipe-run')
  expect(rr?.status).toBe('Fix · attempt 2 of 3 · $2.76 of $10.00')
  expect(rr?.isWarn).toBe(false)
  const steps = ladder(RUN, false)
  expect(steps.map(s => s.glyph).join('')).toBe('✓•○')
  expect(steps[1]?.meta).toBe('running · try 2')
  const v = recipeView(RUN, false)
  expect(v.verdict?.badge).toBe('Retry')
  expect(v.verdict?.reason).toBe('verify fail: retry the fix. 2 of 14 auth tests timed out')
  expect(v.spend).toEqual({ spent: 2.76, cap: 10 })
})

test('a blocking question puts the run card, ladder and band on the accent', () => {
  const s = snap({ recipe: { run: RUN, open: [Q('c', 'non-blocking'), Q('a', 'blocking')] } })
  expect(openQuestions(s.recipe.open, []).map(q => q.id)).toEqual(['a', 'c'])
  const rr = cards(s).find(c => c.name === 'recipe-run')
  expect(rr?.status).toBe('Blocked on you · 1 question')
  expect(rr?.isWarn).toBe(true)
  expect(ladder(RUN, true)[1]?.glyph).toBe('!')
  expect(recipeView(RUN, true).verdict?.badge).toBe('Ask you')
  const b = band(s)
  expect(b.blocking).toBe(1)
  expect(b.meta).toBe('Fix · blocked · attempt 2/3')
  expect(summary(s, cards(s)).blocking).toBe(1)
})

test('an answer given in the panel takes the question out before recipe-run rescans', () => {
  const s = snap({
    recipe: { run: RUN, open: [Q('a', 'blocking')] },
    answered: [{ id: 'a', key: 'AK-142', question: 'Question a?', answer: 'Yes', answeredAt: '2026-11-21T12:00:00Z' }],
  })
  expect(summary(s, cards(s)).blocking).toBe(0)
  expect(answersContext(s.answered)).toContain('- AK-142 a "Question a?": Yes')
})

test('the burn card warns at 80 percent of the 5 hour window', () => {
  const quiet = cards(snap()).find(c => c.name === 'burn-meter')
  expect(quiet?.status).toBe('$3.18 session · 5h 54% · wk 31%')
  const hot = cards(
    snap({ usage: { costUsd: 7.84, rateLimits: [{ kind: 'five_hour', percentUsed: 90, resetsAt: new Date(NOW + 38 * 60_000).toISOString() }], at: NOW } }),
  ).find(c => c.name === 'burn-meter')
  expect(hot?.status).toBe('5h at 90% · resets in 38m')
  expect(hot?.isWarn).toBe(true)
})

test('mods that are off or missing say so, with no invented numbers', () => {
  const list = cards(snap())
  expect(list.map(c => c.name)).toEqual(['recipe-run', 'burn-meter', 'traffic-control', 'ship', 'dev-servers', 'secret-catcher', 'pr', 'brain'])
  expect(list.find(c => c.name === 'pr')?.status).toBe('Off')
  expect(list.find(c => c.name === 'ship')?.status).toBe('Not installed')
  const toggled = cards(snap({ toggled: { pr: true, 'burn-meter': false } }))
  expect(toggled.find(c => c.name === 'pr')?.status).toBe('Off · on after /reload-plugins')
  expect(toggled.find(c => c.name === 'burn-meter')?.status).toContain('off after /reload-plugins')
})

test('ship, dev-servers, pr, brain and traffic read the mods own state', () => {
  const s = snap({
    enabled: {},
    ship: { name: 'agent-kit', branch: 'main', changed: 0, ahead: 3, behind: 0, hasUpstream: true, lastDeploy: { at: NOW - 3 * 3600_000, what: 'v0.14.2' }, recipe: 'make deploy' },
    dev: { servers: [{ pid: 1, port: 6006, process: 'storybook', command: '', age: '48:00' }, { pid: 2, port: 3000, process: 'node', command: '', age: '1:12:00' }], recipe: [{ name: 'api', port: 8080 }] },
    pr: { branch: 'fix', pr: { number: 218, title: 'Fix flaky auth test', isDraft: true, reviewDecision: 'APPROVED' }, checks: [{ name: 'a', bucket: 'pass' }, { name: 'b', bucket: 'pending' }], threads: [{ isOutdated: false }, { isOutdated: true }], error: null },
    brain: { health: { isOnline: true, reason: '', project: 'agent-kit', lessons: 412, proposed: 2, checkedAt: NOW - 4 * 60_000 }, recall: null, review: [] },
    traffic: {
      me: 'me',
      peers: [
        { id: 'me', label: 'agent-kit-aaaa', root: '/repo', branch: 'main', lastTool: 'Bash', lastSeen: NOW },
        { id: 'x', label: 'agent-kit-bbbb', root: '/repo', branch: 'recipe/AK-142', lastTool: 'Edit', lastSeen: NOW - 5_000 },
      ],
      locks: [{ kind: 'push', root: '/repo', label: 'agent-kit-bbbb', command: 'git push', at: NOW - 120_000 }],
    },
  })
  const by = (n: string) => cards(s).find(c => c.name === n)
  expect(by('ship')?.status).toBe('main · clean · 3 to push · deployed 3h 0m ago')
  expect(by('dev-servers')?.status).toBe('2 running · :3000 :6006')
  expect(by('dev-servers')?.details.at(-1)).toEqual(['api', ':8080 · stopped'])
  expect(by('pr')?.status).toBe('#218 draft · CI 1/2 · 1 thread')
  expect(by('brain')?.status).toBe('412 lessons · 2 to review')
  expect(by('traffic-control')?.status).toBe('1 other session · push lock held')
  expect(sessionRows(s.traffic!, NOW)).toEqual([{ label: 'agent-kit-bbbb', branch: 'recipe/AK-142', state: 'Edit', isBusy: true }])
  expect(by('secret-catcher')?.status).toBe('Not installed')
})

test('the band falls back to session spend and limits when no run is live', () => {
  const s = snap({ recipe: { run: { ...RUN, updatedAt: NOW - 3 * 3600_000 }, open: [] } })
  const b = band(s)
  expect(b.title).toBe('No recipe running')
  expect(b.meta).toBe('5h 54% · weekly 31%')
  expect(b.spend).toBe('$3.18')
  expect(b.limit).toBe(null)
})

test('server rows carry the app, the folder and the full command, and skip stopped pids', () => {
  const dev = {
    servers: [
      { pid: 2, port: 7373, process: 'node', command: '/usr/local/bin/node /Users/me/.npm-global/lib/node_modules/roughdraft/dist/child.js', age: '1:00', cwd: '/Users/me/notes', tool: 'roughdraft', project: null },
      { pid: 1, port: 5173, process: 'node', command: 'node vite', age: '2:00', cwd: '?' },
    ],
    recipe: [],
  }
  const rows = serverRows(dev)
  expect(rows.map(r => r.port)).toEqual([5173, 7373])
  expect(rows[0]).toMatchObject({ app: 'node', folder: 'folder unknown' })
  expect(rows[1]).toMatchObject({ app: 'roughdraft', folder: '~/notes', command: '/usr/local/bin/node ~/.npm-global/lib/node_modules/roughdraft/dist/child.js' })
  expect(appLabel({ ...dev.servers[0]!, project: 'travis-terry' })).toBe('roughdraft in travis-terry')
  expect(serverRows(dev, [2]).map(r => r.pid)).toEqual([1])
})
