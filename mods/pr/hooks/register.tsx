import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { PrCheck, PrInfo, PrSnapshot, PrThread } from '../types'
import { duration, isDone, passed, summary, waitingThreads } from './parse'

const PANE = 'pr'
const snapshot = atom({ plugin: 'pr', key: 'snapshot' } as const, null)
const tab = atom({ plugin: 'pr', key: 'tab' } as const, 'checks')
const isLoading = atom({ plugin: 'pr', key: 'isLoading' } as const, false)

const SLOW_MS = 90_000
const FAST_MS = 10_000
const THREADS_QUERY =
  'query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){reviewThreads(first:100){nodes{isResolved isOutdated path line comments(last:30){nodes{author{login} body url}}}}}}}'

const poll = { lastAt: 0, isBusy: false, me: '', seenThreads: new Set<string>(), isPrimed: false }

async function gh($: EngineInterface, args: string[], cwd: string) {
  try {
    return await $.process.run(['gh', ...args], { cwd, timeoutMs: 20_000 })
  } catch {
    return { exitCode: 1, stdout: '', stderr: 'gh did not answer in 20s', isStdoutTruncated: false, isStderrTruncated: false }
  }
}

function firstLine(text: string) {
  return text.trim().split('\n')[0]?.slice(0, 200) ?? ''
}

async function fetchSnapshot($: EngineInterface): Promise<PrSnapshot> {
  const cwd = await $.session.cwd()
  const now = await $.clock.now()
  const br = await $.process.run(['git', 'branch', '--show-current'], { cwd })
  const branch = br.stdout.trim()
  const empty: PrSnapshot = { branch, pr: null, checks: [], threads: [], error: null, fetchedAt: now }
  if (br.exitCode !== 0 || branch === '') return empty

  const view = await gh($, ['pr', 'view', '--json', 'number,title,url,isDraft,reviewDecision'], cwd)
  if (view.exitCode !== 0) {
    const why = firstLine(view.stderr)
    return /no pull requests found|not a git repository|none of the git remotes/i.test(why) ? empty : { ...empty, error: `gh pr view: ${why}` }
  }
  const pr = JSON.parse(view.stdout) as PrInfo

  let checks: PrCheck[] = []
  let error: string | null = null
  const ch = await gh($, ['pr', 'checks', String(pr.number), '--json', 'name,workflow,bucket,state,startedAt,completedAt,link'], cwd)
  try {
    checks = ch.stdout.trim() ? (JSON.parse(ch.stdout) as PrCheck[]) : []
  } catch {
    checks = []
  }
  if (checks.length === 0 && ch.exitCode !== 0 && !/no checks reported/i.test(ch.stderr)) error = `gh pr checks: ${firstLine(ch.stderr)}`

  let threads: PrThread[] = []
  const repo = await gh($, ['repo', 'view', '--json', 'owner,name', '-q', '.owner.login + "/" + .name'], cwd)
  if (poll.me === '') {
    const who = await gh($, ['api', 'user', '-q', '.login'], cwd)
    poll.me = who.stdout.trim()
  }
  const [owner, name] = repo.stdout.trim().split('/')
  if (owner && name) {
    const q = await gh($, ['api', 'graphql', '-f', `query=${THREADS_QUERY}`, '-F', `o=${owner}`, '-F', `r=${name}`, '-F', `n=${pr.number}`], cwd)
    try {
      const nodes = JSON.parse(q.stdout).data.repository.pullRequest.reviewThreads.nodes
      threads = waitingThreads(nodes, poll.me)
    } catch {
      error = error ?? `review threads: ${firstLine(q.stderr) || 'unreadable answer'}`
    }
  }
  return { branch, pr, checks, threads, error, fetchedAt: now }
}

async function isPaneOpen($: EngineInterface) {
  return (await $.ui.panes()).some(p => p.id === PANE)
}

async function refresh($: EngineInterface) {
  if (poll.isBusy) return
  poll.isBusy = true
  await update($, isLoading, () => true)
  try {
    const before = await read($, snapshot)
    const next = await fetchSnapshot($)
    const isSamePr = before?.pr?.number === next.pr?.number && before?.branch === next.branch

    if (isSamePr && before) {
      for (const c of next.checks) {
        const old = before.checks.find(o => o.name === c.name && o.workflow === c.workflow)
        if (old && !isDone(old) && isDone(c)) {
          const mark = c.bucket === 'pass' ? '✓' : c.bucket === 'fail' ? '✗' : '–'
          $.ui.toast(`${mark} ${c.name} ${c.bucket}${duration(c) ? ` in ${duration(c)}` : ''}`, { timeoutMs: 8000 })
        }
      }
      const wasPending = before.checks.some(c => !isDone(c))
      if (wasPending && next.checks.length > 0 && next.checks.every(isDone)) {
        $.ui.toast(`CI finished: ${passed(next.checks)}/${next.checks.length} passed`, { timeoutMs: 12_000 })
      }
    }
    if (!isSamePr) {
      poll.seenThreads = new Set()
      poll.isPrimed = false
    }
    for (const t of next.threads) {
      if (!poll.seenThreads.has(t.url)) {
        if (poll.isPrimed) $.ui.toast(`New review comment from ${t.author} on ${t.path}`, { timeoutMs: 10_000 })
        poll.seenThreads.add(t.url)
      }
    }
    poll.isPrimed = true

    await update($, snapshot, () => next)
    $.ui.status(next.error && next.pr === null ? undefined : summary(next.pr, next.checks, next.threads))
  } finally {
    poll.lastAt = await $.clock.now()
    poll.isBusy = false
    await update($, isLoading, () => false)
  }
}

async function tick($: EngineInterface) {
  const now = await $.clock.now()
  const snap = await read($, snapshot)
  const isWatching = (await isPaneOpen($)) || (snap?.checks.some(c => !isDone(c)) ?? false)
  if (now - poll.lastAt >= (isWatching ? FAST_MS : SLOW_MS)) await refresh($)
}

async function openPane($: EngineInterface, which: 'checks' | 'threads') {
  await update($, tab, () => which)
  await $.ui.open({ id: PANE, title: 'PR' })
  void refresh($)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'ci', description: 'PR checks for this branch, polled fast with a toast as each finishes' })
    await $.command.register({ name: 'threads', description: 'Unresolved review threads on this PR waiting on you' })
    await $.tool.register({
      name: 'review_threads',
      description:
        "The open review threads on the current branch's pull request where someone else spoke last: file, line, the latest comment and its URL. Use when the user asks to fix, address or answer review comments.",
      inputSchema: { type: 'object', properties: {} },
      isDeferred: false,
    })
    void refresh($)
    $.clock.every(FAST_MS, () => void tick($))
    return next(e)
  })

  on('command.run', { command: 'ci' }, async $ => {
    await openPane($, 'checks')
    return { text: 'Watching checks. A toast shows as each finishes.' }
  })

  on('command.run', { command: 'threads' }, async $ => {
    await openPane($, 'threads')
    return { text: 'Review threads pane opened.' }
  })

  on('tool.call', { tool: 'mcp__pr__review_threads' }, async $ => {
    const snap = await fetchSnapshot($)
    await update($, snapshot, () => snap)
    if (snap.pr === null) return { result: snap.error ?? `No pull request for branch ${snap.branch || '(none)'}.` }
    const lines = snap.threads.map(
      (t, i) =>
        `${i + 1}. ${t.path}${t.line ? `:${t.line}` : ''}${t.isOutdated ? ' (outdated)' : ''} · ${t.author} · ${t.replies} comment(s)\n   ${t.body.replace(/\s+/g, ' ').slice(0, 600)}\n   ${t.url}`,
    )
    return {
      result: [
        `PR #${snap.pr.number} ${snap.pr.title} (${snap.pr.url})`,
        snap.threads.length === 0 ? 'No unresolved threads are waiting on the user.' : `${snap.threads.length} unresolved thread(s) waiting on the user:`,
        ...lines,
        snap.error ? `Note: ${snap.error}` : '',
      ]
        .filter(Boolean)
        .join('\n'),
    }
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) void refresh($)
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Link, Text } = $.ui.resolve(e)
    const snap = await read($, snapshot)
    const which = await read($, tab)
    const loading = await read($, isLoading)
    const checks = snap?.checks ?? []
    const threads = snap?.threads ?? []
    const now = await $.clock.now()
    const age = snap ? Math.round((now - snap.fetchedAt) / 1000) : 0

    const header = (
      <Box key="tabs" marginBottom={1}>
        <Button key="tab-checks" hotkey="1" label={`Checks ${checks.length ? `${passed(checks)}/${checks.length}` : ''}`} variant={which === 'checks' ? 'primary' : undefined} onPress={() => update($, tab, () => 'checks')} />
        <Button key="tab-threads" hotkey="2" label={`Threads ${threads.length}`} variant={which === 'threads' ? 'primary' : undefined} onPress={() => update($, tab, () => 'threads')} />
        <Button key="refresh" hotkey="r" label="Refresh" onPress={() => void refresh($)} />
        <Text dimColor> {loading ? 'checking…' : snap ? `${age}s ago` : ''}</Text>
      </Box>
    )

    let body
    if (snap === null) body = <Text dimColor>Reading the PR…</Text>
    else if (snap.pr === null) body = <Text dimColor>{snap.error ?? `No pull request for ${snap.branch || 'this checkout'}.`}</Text>
    else if (which === 'checks')
      body = (
        <Box flexDirection="column">
          <Text bold>
            PR #{snap.pr.number}
            {snap.pr.isDraft ? ' (draft)' : ''} {snap.pr.title}
          </Text>
          {checks.length === 0 && <Text dimColor>No checks on this PR.</Text>}
          {checks.map(c => (
            <Box key={`${c.workflow}/${c.name}`}>
              <Text color={c.bucket === 'pass' ? 'green' : c.bucket === 'fail' ? 'red' : 'yellow'}>
                {c.bucket === 'pass' ? '✓' : c.bucket === 'fail' ? '✗' : c.bucket === 'pending' ? '…' : '–'}{' '}
              </Text>
              <Text>{c.workflow ? `${c.workflow} / ` : ''}{c.name} </Text>
              <Text dimColor>{duration(c) || c.state.toLowerCase()} </Text>
              {c.link ? <Link href={c.link} label="log" /> : null}
            </Box>
          ))}
        </Box>
      )
    else
      body = (
        <Box flexDirection="column">
          <Text dimColor>Unresolved threads whose last word is not yours.</Text>
          {threads.length === 0 && <Text dimColor>Nothing waiting on you.</Text>}
          {threads.map(t => (
            <Box key={t.url} flexDirection="column" marginTop={1}>
              <Text>
                <Text bold>{t.author}</Text> on {t.path}
                {t.line ? `:${t.line}` : ''}
                {t.isOutdated ? ' (outdated)' : ''}
              </Text>
              <Text dimColor>{t.body.replace(/\s+/g, ' ').slice(0, 240)}</Text>
              <Link href={t.url} label="open" />
            </Box>
          ))}
          {threads.length > 0 && (
            <Box marginTop={1}>
              <Button key="fix" label="Ask Claude to address these" variant="primary" onPress={() => void $.prompt.submit({ text: 'Address the open review comments on this PR (use the review_threads tool), push the fixes, then summarize what changed per thread.' })} />
            </Box>
          )}
        </Box>
      )

    return (
      <Box flexDirection="column">
        {header}
        {snap?.error && snap.pr !== null ? <Text color="red">{snap.error}</Text> : null}
        {body}
      </Box>
    )
  })
}
