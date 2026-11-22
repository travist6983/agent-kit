import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, Register, RenderChildren } from 'claude-code'

import type { PrCheck, PrInfo, PrSnapshot, PrThread } from '../types'
import { C, PILL, SECTION_COLOR, barSvg } from './chrome'
import type { Pill, Section } from './chrome'
import { duration, isDone, passed, summary, waitingThreads } from './parse'

const PANE = 'pr'
const snapshot = atom({ plugin: 'pr', key: 'snapshot' } as const, null)
const tab = atom({ plugin: 'pr', key: 'tab' } as const, 'checks')
const isLoading = atom({ plugin: 'pr', key: 'isLoading' } as const, false)

const SLOW_MS = 90_000
const FAST_MS = 10_000
const THREADS_QUERY =
  'query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){reviewThreads(first:100){nodes{isResolved isOutdated path line comments(last:30){nodes{author{login} body url}}}}}}}'

// Failed or cancelled checks need a look, pending ones are live, passed and skipped are idle.
const CHECK_SECTIONS: [Section, string][] = [
  ['look', 'Needs a look'],
  ['live', 'Running'],
  ['idle', 'Passed'],
]
function sectionOf(c: PrCheck): Section {
  return c.bucket === 'pending' ? 'live' : c.bucket === 'pass' || c.bucket === 'skipping' ? 'idle' : 'look'
}

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

  // ---- the pane

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e)
    const { Box, Button, Link, Text } = els
    // Desktop, VS Code and mobile draw SVG and the palette; the terminal keeps its own colours.
    const isGui = e.surface !== 'terminal'
    const Svg = isGui ? ((els as Partial<Elements['desktop']>).Svg ?? undefined) : undefined
    const gui = <T,>(v: T) => (isGui ? v : undefined)
    const tight = isGui ? ('xs' as const) : 0
    const snap = await read($, snapshot)
    const which = await read($, tab)
    const loading = await read($, isLoading)
    const checks = snap?.checks ?? []
    const threads = snap?.threads ?? []
    const now = await $.clock.now()
    const age = snap ? Math.round((now - snap.fetchedAt) / 1000) : 0
    const failing = checks.filter(c => sectionOf(c) === 'look')

    const header = (
      <Box key="head" flexDirection="row" justifyContent="space-between" alignItems="center">
        <Text wrap="truncate-end">
          <Text bold size="heading">
            PR
          </Text>
          <Text dimColor> {snap?.pr ? `#${snap.pr.number}` : snap?.branch || ''}</Text>
        </Text>
        <Box flexDirection="row" gap={1} alignItems="center">
          <Text dimColor size="footnote">
            {loading ? '↻ checking…' : snap ? `↻ ${age}s ago` : ''}
          </Text>
          <Button key="refresh" variant="secondary" hotkey="r" label="Refresh" onPress={() => void refresh($)} />
          <Button key="close" role="dismiss" plain dimColor label="✕" onPress={() => $.ui.close({ id: PANE })} />
        </Box>
      </Box>
    )

    const tile = (k: string, label: string, value: RenderChildren) => (
      <Box key={k} flexDirection="column" width="33%" flexGrow={1} flexShrink={1} borderStyle="round" borderColor={C.line} backgroundColor={gui(C.card)} paddingX={1}>
        <Text dimColor size="caption">
          {label}
        </Text>
        {value}
      </Box>
    )

    const strip = (
      <Box key="strip" flexDirection="row" gap={1}>
        {tile(
          'tile-passed',
          'Checks passed',
          <Text bold size="title" tabular>
            <Text color={checks.length > 0 && passed(checks) === checks.length ? C.green : undefined}>{passed(checks)}</Text>
            <Text dimColor>/{checks.length}</Text>
          </Text>,
        )}
        {tile(
          'tile-failing',
          'Failing',
          <Text bold size="title" tabular color={failing.length > 0 ? C.amber : undefined} dimColor={failing.length === 0}>
            {String(failing.length)}
          </Text>,
        )}
        {tile(
          'tile-threads',
          'Open threads',
          <Text bold size="title" tabular color={threads.length > 0 ? PILL.attn.fg : undefined} dimColor={threads.length === 0}>
            {String(threads.length)}
          </Text>,
        )}
      </Box>
    )

    const tabs = (
      <Box key="tabs" flexDirection="row" gap={1}>
        <Button key="tab-checks" hotkey="1" label={`Checks ${checks.length ? `${passed(checks)}/${checks.length}` : ''}`} variant={which === 'checks' ? 'primary' : 'secondary'} onPress={() => update($, tab, () => 'checks')} />
        <Button key="tab-threads" hotkey="2" label={`Threads ${threads.length}`} variant={which === 'threads' ? 'primary' : 'secondary'} onPress={() => update($, tab, () => 'threads')} />
      </Box>
    )

    // ---- small parts

    const pill = (p: Pill) => (
      <Text bold size="caption" color={PILL[p.tone].fg} backgroundColor={gui(PILL[p.tone].bg)}>
        {` ${p.text.toUpperCase()} `}
      </Text>
    )

    const heading = (sec: Section, title: string, n: number) => (
      <Text bold color={SECTION_COLOR[sec]}>
        {title} <Text dimColor>{n}</Text>
      </Text>
    )

    // The PR itself: a card with a coloured top bar, row 1 the number and title, row 2 its state.
    const prCard = (pr: PrInfo) => {
      const sec: Section = failing.length > 0 ? 'look' : checks.some(c => !isDone(c)) ? 'live' : 'idle'
      const color = SECTION_COLOR[sec]
      return (
        <Box key="pr-card" flexDirection="column" borderStyle="round" borderColor={isGui ? C.line : color} backgroundColor={gui(C.card)} paddingX={1} gap={tight}>
          {Svg && (
            <Box position="absolute" top={0} left={1} right={1}>
              <Svg source={barSvg(color, 4, 2)} alt="" height={4} />
            </Box>
          )}
          <Box flexDirection="row" justifyContent="space-between" alignItems="center" gap={1}>
            <Box flexDirection="row" alignItems="center" gap={1} flexShrink={1}>
              {pr.isDraft && pill({ text: 'draft', tone: 'live' })}
              <Text bold color={gui(C.name)} wrap="truncate-end">
                PR #{pr.number} {pr.title}
              </Text>
            </Box>
            <Link href={pr.url} label="open" />
          </Box>
          <Text color={gui(C.dim)} dimColor={!isGui} wrap="truncate-end" tabular>
            {snap?.branch ?? ''}
            {pr.reviewDecision ? ` · ${pr.reviewDecision.toLowerCase().replace(/_/g, ' ')}` : ''}
          </Text>
        </Box>
      )
    }

    // ---- checks: inset rows grouped as Needs a look (failed), Running, Passed

    const checkRow = (c: PrCheck) => {
      const sec = sectionOf(c)
      const glyph = c.bucket === 'pass' ? '✓' : c.bucket === 'fail' ? '✗' : c.bucket === 'pending' ? '…' : '–'
      return (
        <Box key={`${c.workflow}/${c.name}`} flexDirection="row" justifyContent="space-between" alignItems="center" gap={1} backgroundColor={gui(C.inset)} paddingX={isGui ? 1 : 0}>
          <Text wrap="truncate-end" tabular>
            <Text color={SECTION_COLOR[sec]}>{glyph} </Text>
            {c.workflow ? <Text dimColor>{c.workflow} / </Text> : null}
            <Text weight="semibold" bold={!isGui}>
              {c.name}
            </Text>
          </Text>
          <Box flexDirection="row" gap={1} alignItems="center">
            {c.bucket === 'fail' && pill({ text: 'fail', tone: 'warn' })}
            {c.bucket === 'pending' && pill({ text: 'running', tone: 'live' })}
            <Text dimColor tabular>
              {duration(c) || c.state.toLowerCase()}
            </Text>
            {c.link ? <Link href={c.link} label="log" /> : null}
          </Box>
        </Box>
      )
    }

    const checksBody = () => {
      if (checks.length === 0) return <Text key="no-checks" dimColor>No checks on this PR.</Text>
      return CHECK_SECTIONS.map(([sec, title]) => {
        const items = checks.filter(c => sectionOf(c) === sec)
        if (items.length === 0) return null
        return (
          <Box key={`sec-${sec}`} flexDirection="column" gap={isGui ? 'xs' : 0}>
            {heading(sec, title, items.length)}
            {items.map(checkRow)}
          </Box>
        )
      })
    }

    // ---- threads: one card each, coloured bar, row 1 author and file | comment count + open

    const threadCard = (t: PrThread) => {
      const color = t.isOutdated ? SECTION_COLOR.idle : PILL.attn.fg
      return (
        <Box key={t.url} flexDirection="column" borderStyle="round" borderColor={isGui ? C.line : t.isOutdated ? 'subtle' : SECTION_COLOR.look} backgroundColor={gui(C.card)} paddingX={1} gap={tight}>
          {Svg && (
            <Box position="absolute" top={0} left={1} right={1}>
              <Svg source={barSvg(color, 4, 2)} alt="" height={4} />
            </Box>
          )}
          <Box flexDirection="row" justifyContent="space-between" alignItems="center" gap={1}>
            <Box flexDirection="row" alignItems="center" gap={1} flexShrink={1}>
              {t.isOutdated ? pill({ text: 'outdated', tone: 'warn' }) : pill({ text: 'your turn', tone: 'attn' })}
              <Text wrap="truncate-end">
                <Text bold color={gui(C.name)}>
                  {t.author}
                </Text>{' '}
                on {t.path}
                {t.line ? `:${t.line}` : ''}
                {t.isOutdated ? ' (outdated)' : ''}
              </Text>
            </Box>
            <Box flexDirection="row" alignItems="center" gap={1}>
              <Text bold tabular>
                {t.replies}
              </Text>
              <Link href={t.url} label="open" />
            </Box>
          </Box>
          <Text color={gui(C.dim)} dimColor={!isGui}>
            {t.body.replace(/\s+/g, ' ').slice(0, 240)}
          </Text>
        </Box>
      )
    }

    const threadsBody = () => (
      <Box key="sec-threads" flexDirection="column" gap={isGui ? 'sm' : 0}>
        {heading('look', 'Waiting on you', threads.length)}
        <Text dimColor size="footnote">
          Unresolved threads whose last word is not yours.
        </Text>
        {threads.length === 0 && <Text dimColor>Nothing waiting on you.</Text>}
        {threads.map(threadCard)}
        {threads.length > 0 && (
          <Box marginTop={1}>
            <Button key="fix" label="Ask Claude to address these" variant="primary" onPress={() => void $.prompt.submit({ text: 'Address the open review comments on this PR (use the review_threads tool), push the fixes, then summarize what changed per thread.' })} />
          </Box>
        )}
      </Box>
    )

    let body
    if (snap === null) body = <Text dimColor>Reading the PR…</Text>
    else if (snap.pr === null) body = <Text dimColor>{snap.error ?? `No pull request for ${snap.branch || 'this checkout'}.`}</Text>
    else
      body = (
        <Box flexDirection="column" gap={1}>
          {prCard(snap.pr)}
          {which === 'checks' ? checksBody() : threadsBody()}
        </Box>
      )

    return (
      <Box flexDirection="column" gap={1} backgroundColor={gui(C.pane)} paddingX={isGui ? 2 : 0} paddingY={isGui ? 1 : 0}>
        {header}
        {snap?.pr ? strip : null}
        {tabs}
        {snap?.error && snap.pr !== null ? <Text color="error">{snap.error}</Text> : null}
        {body}
      </Box>
    )
  })
}
