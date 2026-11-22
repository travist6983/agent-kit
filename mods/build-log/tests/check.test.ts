import { expect, test } from 'claude-code/testing'

import { checkFormat, checkText, configProblem, figuresIn } from '../hooks/check'
import type { BuildLogConfig } from '../hooks/check'

// Made-up names only: the real denylist never enters the repo.
const config: BuildLogConfig = {
  deny: {
    names: ['Acme Widgets', 'Jordan Placeholder'],
    products: ['WidgetHub'],
    domains: ['acmewidgets.com'],
    repos: ['acme-portal'],
    paths: ['/Users/sam/work'],
    words: ['dealership'],
  },
  allow_urls: ['sam.example.dev'],
  safe_repos: ['sams-toys'],
}

const rules = (text: string, c: BuildLogConfig | null = config, figures?: Set<string>) =>
  checkText(text, c, { allowedFigures: figures }).filter(h => h.severity === 'block').map(h => h.rule)

test('an unfilled or missing denylist is a problem, so drafts fail closed', async () => {
  expect(configProblem(null)).toBe('no config')
  expect(configProblem({ deny: {} })).toBe('the denylist is empty')
  const example: BuildLogConfig = { deny: { names: ['Example Employer Inc'], paths: ['/Users/your-name/work'], words: ['example-industry-word'] } }
  expect(configProblem(example)).toContain('placeholders')
  expect(configProblem(config)).toBe(null)
})

test('denylist names are caught however they are spelled', async () => {
  for (const s of ['We shipped it at Acme Widgets.', 'acme-widgets was slow', "AcmeWidgets's queue", 'a WidgetHub bug', 'the acme-portal repo']) {
    expect(rules(s)[0]?.startsWith('denylist')).toBe(true)
  }
  expect(rules('A dealership asked for texting')).toContain('denylist:words')
  expect(rules('The kit caught a widget bug.')).toEqual([])
})

test('links pass only for the owner site and the kit repo', async () => {
  expect(rules('Read it at https://sam.example.dev/logs/loops and https://github.com/travist6983/agent-kit/pull/9')).toEqual([])
  expect(rules('/plugin install build-log --marketplace travist6983/agent-kit')).toEqual([])
  expect(rules('see https://github.com/someone/private-app')).toEqual(['url'])
  expect(rules('hosted on internal-tools.io')).toEqual(['domain'])
  expect(rules('sam.example.dev.attacker.com')).toEqual(['domain'])
  expect(rules('mail me at sam@example.org')).toEqual(['email'])
})

test('file paths and branch names are blocked, kit paths are not', async () => {
  expect(rules('it lives in /Users/sam/work/app')).toContain('denylist:paths')
  expect(rules('open ~/code/thing')).toEqual(['path'])
  expect(rules('the bug was in src/billing/invoice.ts')).toEqual(['path'])
  expect(rules('see app.config.ts')).toEqual([])
  expect(rules('the hook is mods/build-log/hooks/check.ts')).toEqual([])
  expect(rules('on feat/new-thing')).toEqual(['branch'])
  expect(rules('and/or, on/off, 24/7')).toEqual([])
  expect(rules('C:\\work\\repo')).toEqual(['path'])
})

test('costs and hours from work are blocked, own-repo figures pass', async () => {
  expect(rules('the run cost $14.20')).toEqual(['money'])
  expect(rules('billed 12 hours')).toEqual(['hours'])
  expect(rules('at 90/hr')).toEqual(['hours'])
  const own = figuresIn('a kit run spent $2.40 over 3 hours')
  expect(rules('the kit run spent $2.40 over 3 hours', config, own)).toEqual([])
  expect(rules('the 5 hour limit and weekly limit')).toEqual([])
  expect(rules('it took 40 minutes and 12 steps')).toEqual([])
})

test('dashes, ticket keys, phones and tokens are blocked', async () => {
  expect(rules('fast \u2014 and cheap')).toEqual(['dash'])
  expect(rules('fixed ABC-1234 today')).toEqual(['ticket-key'])
  expect(rules('a file ticket like PROJ-123, built on GPT-4')).toEqual([])
  expect(rules('call 555-201-9876')).toEqual(['phone'])
  expect(rules('key sk-ant-abcdefghijklmnopqrstuv')).toEqual(['secret'])
})

test('hints at the day job warn without blocking', async () => {
  const hits = checkText('This week at work the loop escalated.', config)
  expect(hits.map(h => [h.rule, h.severity])).toEqual([['job-hint', 'warn']])
})

test('an X post over 280 characters blocks; lengths only warn', async () => {
  const site = ['## Problem', '## What the kit did', '## What broke', '## What I changed', '## Try it'].join('\n\nword\n\n')
  const hits = checkFormat({ site, linkedin: 'short', x_thread: ['x'.repeat(281), 'ok'] })
  expect(hits.filter(h => h.severity === 'block').map(h => h.part)).toEqual(['x 1'])
  expect(hits.some(h => h.rule === 'length' && h.part === 'site')).toBe(true)
  expect(hits.some(h => h.rule === 'template')).toBe(false)
  // A shot reference doesn't count against the limit.
  const withShot = checkFormat({ site, linkedin: '', x_thread: [`${'y'.repeat(270)} ![a](shot:s-abc123)`] })
  expect(withShot.some(h => h.rule === 'x-limit')).toBe(false)
})
