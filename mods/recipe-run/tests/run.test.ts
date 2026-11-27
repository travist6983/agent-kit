import { expect, test } from 'claude-code/testing'

import {
  answersContext,
  bandText,
  buildRun,
  checkAsk,
  describeAction,
  openQuestions,
  parseJsonl,
  parseRouterCall,
  parseVerdict,
  runRef,
  spendText,
  toAnswer,
  toQuestion,
  toneOf,
} from '../hooks/run'
import type { Verdict } from '../types'

const ROUTER = 'python3 .claude/skills/dev-loop-defect/scripts/recipe_router.py'

const verdict = (over: Partial<Verdict>): Verdict => ({
  stage: 'fix', result: 'fixed', attempt: 1, max: 3, action: 'advance', reason: '',
  budgetSpent: null, budgetCap: null, at: 1, ...over,
})

test('router calls are read from their flags, and other commands are ignored', async () => {
  expect(parseRouterCall('git status')).toBe(null)
  expect(parseRouterCall(`${ROUTER} --stage fix --result could-not-fix --attempt 2 --max 3 --budget-spent 4.2 --budget-cap 10`)).toEqual({
    stage: 'fix', result: 'could-not-fix', attempt: 2, max: 3, budgetSpent: 4.2, budgetCap: 10,
  })
  // code-review is the router's name for the review stage; flags may use =, and switches have no value
  expect(parseRouterCall(`${ROUTER} --paused --stage=code-review --result="request-changes" --attempt 3`)?.stage).toBe('review')
  expect(parseRouterCall(`${ROUTER} --stage=code-review --result="request-changes"`)?.result).toBe('request-changes')
})

test('the verdict is the last JSON line with an action', async () => {
  expect(parseVerdict('{"action": "escalate", "reason": "fix=could-not-fix"}\n')).toEqual({ action: 'escalate', reason: 'fix=could-not-fix' })
  expect(parseVerdict('noise\n{"x":1}\n{"action":"advance","reason":"verify pass"}\n')).toEqual({ action: 'advance', reason: 'verify pass' })
  expect(parseVerdict('Traceback (most recent call last):')).toBe(null)
})

test('run folders are found in commands and paths', async () => {
  expect(runRef('cat runs/PROJ-123/fix.json')).toEqual({ key: 'PROJ-123', root: null })
  expect(runRef('/repo/app/runs/PROJ-9/verify.json')).toEqual({ key: 'PROJ-9', root: '/repo/app' })
  expect(runRef('ls runs/')).toBe(null)
  expect(runRef('cat ../runs/../etc')).toBe(null)
})

test('tones follow the stage enums', async () => {
  for (const good of ['fixed', 'reproduced', 'pass', 'green', 'approve', 'ready']) expect(toneOf(good)).toBe('good')
  for (const warn of ['warnings', 'flaky', 'partial', 'inconclusive']) expect(toneOf(warn)).toBe('warn')
  for (const bad of ['could-not-fix', 'fail', 'same-area-regression', 'request-changes', 'blocked']) expect(toneOf(bad)).toBe('bad')
  expect(toneOf(null)).toBe('none')
})

test('the ladder marks what ran and where the run stands', async () => {
  const docs = {
    'intake.json': { key: 'PROJ-123', recipe: 'dev-loop-defect' },
    'repro.json': { status: 'reproduced', symptom_quote: 'Badge still reads Draft after Send.' },
    'fix.json': { status: 'fixed', root_cause: 'Badge copied props into state on mount.' },
  }
  const run = buildRun('/repo', 'PROJ-123', docs, [verdict({ stage: 'fix', attempt: 2 })], 5)
  expect(run.recipe).toBe('dev-loop-defect')
  expect(run.stages.find(s => s.id === 'fix')).toEqual({
    id: 'fix', label: 'fix', outcome: 'fixed', tone: 'good', attempts: 2, isCurrent: false, detail: 'Badge copied props into state on mount.',
  })
  // the last verdict advanced, so the next stage without an outcome is current
  expect(run.stages.find(s => s.isCurrent)?.id).toBe('verify')

  // a retry verdict holds the run on its own stage
  const held = buildRun('/repo', 'PROJ-123', docs, [verdict({ stage: 'verify', result: 'fail', action: 're-fix' })], 5)
  expect(held.stages.find(s => s.isCurrent)?.id).toBe('verify')
  expect(held.stages.find(s => s.id === 'verify')?.tone).toBe('bad')
})

test('each recipe shows its own spine', async () => {
  const feature = buildRun('/repo', 'VEU-63', { 'intake.json': { recipe: 'dev-loop-feature-complete' } }, [], 1)
  expect(feature.stages.map(s => s.label)).toEqual([
    'intake', 'spec', 'explore', 'ac-oracle', 'plan', 'implement', 'verify', 'regression', 'review', 'pre-pr', 'stage',
  ])
  // no repro on a feature: after intake the spec is next
  expect(feature.stages.find(s => s.isCurrent)?.id).toBe('spec')

  // a run from before dev-implement still shows its fix.json under implement
  const old = buildRun('/repo', 'VEU-63', { 'intake.json': { recipe: 'dev-loop-feature-complete' }, 'fix.json': { status: 'fixed' } }, [], 1)
  expect(old.stages.find(s => s.id === 'implement')?.outcome).toBe('fixed')

  const defect = buildRun('/repo', 'PROJ-1', { 'intake.json': { recipe: 'dev-loop-defect' } }, [], 1)
  expect(defect.stages.map(s => s.id)).not.toContain('review')
  const pre = buildRun('/repo', 'PROJ-2', { 'intake.json': { recipe: 'dev-loop-feature-prerelease' } }, [], 1)
  expect(pre.stages.at(-1)?.label).toBe('stage')
  expect(pre.stages.map(s => s.id)).not.toContain('prod-verify')
  // unknown or missing recipe falls back to the full defect spine
  expect(buildRun('/repo', 'X-1', {}, [], 1).stages).toHaveLength(9)
})

test('spend and the band read in plain words', async () => {
  const verdicts = [verdict({ budgetSpent: 2, budgetCap: 10 }), verdict({ stage: 'verify', result: 'fail', action: 're-fix', attempt: 1, budgetSpent: 4.25, budgetCap: 10 })]
  expect(spendText(verdicts)).toBe('$4.25 of $10.00')
  expect(spendText([verdict({})])).toBe(null)
  expect(describeAction('re-fix')).toBe('retry the fix')
  const run = buildRun('/repo', 'PROJ-7', { 'intake.json': { recipe: 'dev-loop-defect' } }, verdicts, 1)
  const q = toQuestion({ id: 'q1', kind: 'blocking', question: 'Ship it?' }, 'PROJ-7')
  expect(bandText(run, q ? [q] : [])).toBe('defect PROJ-7 · verify 1/3 · retry the fix · $4.25 of $10.00 · 1 question (1 blocking)')
  expect(bandText(null, [])).toBe(null)
})

test('questions need an id, a kind and text; answers close them', async () => {
  const lines = [
    JSON.stringify({ id: 'q1', kind: 'non-blocking', question: 'Use the v2 endpoint?', default: 'use v1', asked_at: '2026-11-21T10:00:00Z' }),
    '{half a line',
    JSON.stringify({ id: 'q2', kind: 'blocking', question: 'Drop the column?', from: 'fix', asked_at: '2026-11-21T11:00:00Z' }),
    JSON.stringify({ id: 'q3', kind: 'maybe', question: 'Not a kind' }),
    JSON.stringify({ id: 'q4', kind: 'blocking', question: 'Rotate the key?', asked_at: '2026-11-21T09:00:00Z' }),
  ].join('\n')
  const qs = parseJsonl(lines).map(r => toQuestion(r, 'PROJ-1')).filter(q => q !== null)
  expect(qs.map(q => q.id)).toEqual(['q1', 'q2', 'q4'])
  const answers = parseJsonl(JSON.stringify({ id: 'q4', answer: 'yes' })).map(r => toAnswer(r, 'PROJ-1')).filter(a => a !== null)
  // blocking first, oldest first; q4 is answered
  expect(openQuestions(qs, answers).map(q => q.id)).toEqual(['q2', 'q1'])
  // the same id under another run is a different question
  expect(openQuestions(qs.map(q => ({ ...q, key: 'PROJ-2' })), answers).length).toBe(3)
})

test('ask_human input is checked before anything is written', async () => {
  expect(checkAsk({ kind: 'blocking' })).toEqual({ error: 'question is required.' })
  expect('error' in checkAsk({ question: 'Go?', kind: 'urgent' })).toBe(true)
  expect(checkAsk({ question: 'Go?', kind: 'non-blocking' })).toEqual({ error: 'a non-blocking question needs a default: what you will do if nobody answers.' })
  expect('error' in checkAsk({ question: 'Go?', kind: 'blocking', options: ['a', 'b', 'c', 'd', 'e'] })).toBe(true)
  expect('error' in checkAsk({ question: 'Go?', kind: 'blocking', key: '../etc' })).toBe(true)
  expect(checkAsk({ question: 'Go?', kind: 'blocking', options: ['Yes', 'No'], key: 'PROJ-1', from: 'fix' })).toEqual({
    question: 'Go?', kind: 'blocking', options: ['Yes', 'No'], default: null, context: null, from: 'fix', key: 'PROJ-1',
  })
})

test('answers reach the model with the question they answer', async () => {
  const q = toQuestion({ id: 'q2', kind: 'blocking', question: 'Drop the column?' }, 'PROJ-1')
  const text = answersContext([{ id: 'q2', key: 'PROJ-1', answer: 'No, keep it', answeredAt: '' }], q ? [q] : []) ?? ''
  expect(text).toContain('- PROJ-1 q2 (blocking) "Drop the column?": No, keep it')
  expect(answersContext([], [])).toBe(null)
})
