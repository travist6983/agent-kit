import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, Register } from 'claude-code'

import type { BrainHealth, Lesson } from '../types'
import { C, PILL, SECTION_COLOR, SECTION_TITLE, barSvg } from './chrome'
import type { Pill, Section } from './chrome'
import { MAX_RECALL, MIN_SCORE, parseRemember, recallContext, shorten, shouldRecall } from './format'

const PANE = 'brain'
const health = atom({ plugin: 'brain', key: 'health' } as const, null)
const recall = atom({ plugin: 'brain', key: 'recall' } as const, null)
const review = atom({ plugin: 'brain', key: 'review' } as const, [])
const active = atom({ plugin: 'brain', key: 'active' } as const, [])
const tab = atom({ plugin: 'brain', key: 'tab' } as const, 'recalled')
const forgetArmed = atom({ plugin: 'brain', key: 'forgetArmed' } as const, null)

const REFRESH_MS = 5 * 60_000
const kit = { kb: '' as string, isWarned: false }

type KbAnswer = { ok?: boolean; error?: string; [key: string]: unknown }

// The kb CLI ships with the kit: $AGENT_KIT_HOME, else the kit this mod is read from (mods/brain → kit).
async function kbPath($: EngineInterface) {
  if (kit.kb) return kit.kb
  const home = (await $.env.get('AGENT_KIT_HOME')) ?? ''
  const roots = [home, `${$.plugin.root}/../..`].filter(Boolean)
  for (const root of roots) {
    const candidate = `${root}/.claude/skills/qa-kb/scripts/kb`
    if (await $.fs.exists(candidate)) {
      kit.kb = candidate
      return candidate
    }
  }
  return ''
}

// `/agent-kit init|doctor|list` from inside a project: runs the kit's scripts/agent-kit in the session's folder.
async function agentKit($: EngineInterface, args: string[]) {
  const path = await kbPath($)
  if (!path) return 'agent-kit not found: set AGENT_KIT_HOME to the agent-kit checkout.'
  const script = path.replace(/\/\.claude\/skills\/qa-kb\/scripts\/kb$/, '/scripts/agent-kit')
  const overlay = (await $.env.get('AGENT_KIT_OVERLAY')) ?? ''
  try {
    const r = await $.process.run([script, ...args], {
      cwd: await $.session.cwd(),
      timeoutMs: 120_000,
      env: overlay ? { AGENT_KIT_OVERLAY: overlay } : {},
    })
    return [r.stdout.trim(), r.stderr.trim()].filter(Boolean).join('\n') || `agent-kit exited ${r.exitCode}`
  } catch (err) {
    return `agent-kit failed: ${err instanceof Error ? err.message : String(err)}`
  }
}

async function kb($: EngineInterface, args: string[], timeoutMs = 8000): Promise<KbAnswer> {
  const path = await kbPath($)
  if (!path) return { ok: false, error: 'kb CLI not found: set AGENT_KIT_HOME to the agent-kit checkout' }
  const overlay = (await $.env.get('AGENT_KIT_OVERLAY')) ?? ''
  try {
    const r = await $.process.run([path, ...args], {
      cwd: await $.session.cwd(),
      timeoutMs,
      env: overlay ? { AGENT_KIT_OVERLAY: overlay } : {},
    })
    const out = r.stdout.trim()
    if (!out) return { ok: false, error: r.stderr.trim().split('\n').pop() || `kb exited ${r.exitCode}` }
    return JSON.parse(out) as KbAnswer
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : String(err) }
  }
}

function firstLine(text: unknown) {
  return shorten(String(text ?? '').split('\n')[0] ?? '', 120)
}

async function refresh($: EngineInterface) {
  const status = await kb($, ['status'])
  const now = await $.clock.now()
  const isOnline = status.ok === true && status.reachable === true
  let proposed: Lesson[] = []
  let current: Lesson[] = []
  if (isOnline) {
    const p = await kb($, ['read', 'lessons', '--status', 'proposed'])
    const a = await kb($, ['read', 'lessons', '--status', 'active'])
    proposed = (p.rows as Lesson[] | undefined) ?? []
    current = (a.rows as Lesson[] | undefined) ?? []
  }
  const visible = (status.row_counts_visible as Record<string, number> | undefined)?.lessons ?? 0
  const next: BrainHealth = {
    isOnline,
    reason: isOnline ? '' : firstLine(status.error ?? status.warning ?? 'unreachable'),
    project: String(status.project ?? '?'),
    scope: String(status.scope ?? '?'),
    target: String(status.target ?? status.brain ?? '?'),
    lessons: visible,
    proposed: proposed.length,
    checkedAt: now,
  }
  await update($, health, () => next)
  await update($, review, () => proposed)
  await update($, active, () => current)
  await drawStatus($)
  if (!isOnline && !kit.isWarned) {
    kit.isWarned = true
    $.ui.toast(`Brain offline: ${next.reason}. Prompts go through without lessons.`, { timeoutMs: 10_000 })
  }
  if (isOnline) kit.isWarned = false
}

async function drawStatus($: EngineInterface) {
  const h = await read($, health)
  const r = await read($, recall)
  if (h === null) return
  if (!h.isOnline) {
    $.ui.status(`brain: offline (${h.reason})`)
    return
  }
  const recalled = r ? ` · ${r.lessons.length} recalled` : ''
  const pending = h.proposed > 0 ? ` · ${h.proposed} to review` : ''
  $.ui.status(`brain: ${h.project} · ${h.lessons} lessons${recalled}${pending}`)
}

async function openPane($: EngineInterface, which: 'recalled' | 'review' | 'lessons') {
  await update($, tab, () => which)
  await $.ui.open({ id: PANE, title: 'Brain' })
  void refresh($)
}

async function setStatus($: EngineInterface, id: number, verb: 'bless' | 'reject' | 'forget') {
  const args = verb === 'forget' ? ['forget', 'lessons', '--id', String(id)] : [verb, 'lesson', '--id', String(id), '--by', 'user']
  const r = await kb($, args)
  $.ui.toast(r.ok ? `Lesson ${id} ${verb === 'bless' ? 'approved' : verb === 'reject' ? 'rejected' : 'forgotten'}` : `Could not ${verb} lesson ${id}: ${firstLine(r.error)}`)
  await refresh($)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'brain', description: 'Brain health, recalled lessons and the review queue', argumentHint: '[review | lessons]' })
    await $.command.register({ name: 'remember', description: 'Save a lesson to the brain for this project (or "global: …" for every project)', argumentHint: '[global:] <lesson>' })
    await $.command.register({ name: 'agent-kit', description: 'Connect this repo to agent-kit: init drafts its overlay config, doctor checks it', argumentHint: 'init | doctor | list' })
    await $.tool.register({
      name: 'remember',
      description:
        "Propose a durable lesson for the user's knowledge brain: a reusable fact, tool quirk, workflow step or user preference that will matter in future sessions (e.g. 'The API must run on port 8010 here because 8000 is taken'). One compressed sentence or two; never a transcript, a one-off detail or a secret. It lands as a proposal the user approves. scope 'global' is for lessons true across all of the user's projects.",
      inputSchema: {
        type: 'object',
        properties: {
          text: { type: 'string', description: 'The lesson, stated so it is useful with no other context' },
          kind: { type: 'string', enum: ['tooling', 'preference', 'workflow', 'project-fact'] },
          scope: { type: 'string', enum: ['project', 'global'] },
        },
        required: ['text', 'kind'],
      },
      isDeferred: false,
    })
    await refresh($)
    $.clock.every(REFRESH_MS, () => void refresh($))
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    const h = await read($, health)
    if (!shouldRecall(e.text, e.origin.kind) || h?.isOnline === false) return next(e)
    const r = await kb($, ['search', 'lessons', e.text.slice(0, 2000), '--k', String(MAX_RECALL), '--min-score', String(MIN_SCORE)], 6000)
    const lessons = r.ok ? ((r.results as Lesson[] | undefined) ?? []) : []
    const at = await $.clock.now()
    await update($, recall, () => ({ query: shorten(e.text, 200), at, lessons, error: r.ok ? null : firstLine(r.error) }))
    void drawStatus($)
    const block = recallContext(lessons, h?.project ?? '')
    return block ? next({ ...e, context: [...(e.context ?? []), block] }) : next(e)
  }).catch(($, e, next) => next(e))

  on('tool.call', { tool: 'mcp__brain__remember' }, async ($, e) => {
    const text = String(e.text ?? '').trim()
    if (text.length < 10) return { result: 'Not saved: a lesson needs a full sentence.' }
    const args = ['write', 'lesson', '--text', text, '--kind', String(e.kind ?? 'workflow'), '--source', 'claude']
    if (e.scope === 'global') args.push('--project', 'global')
    const r = await kb($, args)
    if (!r.ok) return { result: `Not saved: the brain answered ${firstLine(r.error)}` }
    const row = r.row as Lesson | undefined
    const isBump = r.action === 'bump'
    $.ui.toast(isBump ? `Brain: lesson ${row?.id} seen again` : `Claude proposed a lesson. /brain review to approve it`, { timeoutMs: 8000 })
    void refresh($)
    return {
      result: isBump
        ? `Already known (lesson ${row?.id}); its count went up.`
        : `Proposed as lesson ${row?.id} (${row?.project}). The user approves it before it is recalled.`,
    }
  })

  on('command.run', { command: 'remember' }, async ($, e) => {
    const { text, isGlobal } = parseRemember(e.args)
    if (text.length < 5) return { text: 'Usage: /remember <lesson>  or  /remember global: <lesson>' }
    const args = ['write', 'lesson', '--text', text, '--kind', 'workflow', '--status', 'active', '--source', 'user']
    if (isGlobal) args.push('--project', 'global')
    const r = await kb($, args)
    await refresh($)
    if (!r.ok) return { text: `Not saved: ${firstLine(r.error)}` }
    const row = r.row as Lesson | undefined
    return { text: r.action === 'bump' ? `Already known as lesson ${row?.id}.` : `Saved lesson ${row?.id} (${row?.project}).` }
  })

  on('command.run', { command: 'agent-kit' }, async ($, e) => {
    const [verb = 'doctor', ...rest] = e.args.trim().split(/\s+/).filter(Boolean)
    if (!['init', 'doctor', 'list'].includes(verb)) return { text: 'Usage: /agent-kit init [--ticket-prefix XX] | doctor | list' }
    const text = await agentKit($, verb === 'init' ? ['init', '.', ...rest] : [verb, ...rest])
    if (verb !== 'init') return { text }
    return {
      text,
      context: [
        'agent-kit init just drafted this project\'s overlay config. Read the ONBOARDING.md it names, fill in what you can verify from the repo (app URL and ports, health path, test accounts, blast-radius paths in conventions.json), ask the user for anything you cannot verify, then run `/agent-kit doctor` until it reports 0 blocking.',
      ],
    }
  })

  on('command.run', { command: 'brain' }, async ($, e) => {
    const arg = e.args.trim()
    await openPane($, arg === 'review' ? 'review' : arg === 'lessons' ? 'lessons' : 'recalled')
    const h = await read($, health)
    return { text: h?.isOnline ? `Brain ${h.target} (${h.scope}) · project ${h.project}` : `Brain offline: ${h?.reason ?? 'not checked yet'}` }
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined) void drawStatus($)
    return next(e)
  })


  // ---- the pane

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e)
    const { Box, Button, Text } = els
    // Desktop, VS Code and mobile draw SVG and the palette; the terminal keeps its own colours.
    const isGui = e.surface !== 'terminal'
    const Svg = isGui ? ((els as Partial<Elements['desktop']>).Svg ?? undefined) : undefined
    const gui = <T,>(v: T) => (isGui ? v : undefined)
    const tight = isGui ? ('xs' as const) : 0
    // not `h`: that name is the JSX factory, and shadowing it blanks the pane
    const hl = await read($, health)
    const r = await read($, recall)
    const proposed = await read($, review)
    const current = await read($, active)
    const which = await read($, tab)
    const armed = await read($, forgetArmed)
    const recalledCount = r?.lessons.length ?? 0

    const header = (
      <Box key="head" flexDirection="row" justifyContent="space-between" alignItems="center">
        <Text>
          <Text bold size="heading">
            Brain
          </Text>
          <Text dimColor> {hl?.isOnline ? `${hl.target} · ${hl.scope} · project ${hl.project}` : 'lessons'}</Text>
        </Text>
        <Box flexDirection="row" gap={1} alignItems="center">
          {hl && (
            <Text dimColor size="footnote">
              ↻ {clockTime(hl.checkedAt)}
            </Text>
          )}
          <Button key="refresh" variant="secondary" hotkey="r" label="Refresh" onPress={() => void refresh($)} />
          <Button key="close" role="dismiss" plain dimColor label="✕" onPress={() => $.ui.close({ id: PANE })} />
        </Box>
      </Box>
    )

    const tile = (k: string, label: string, value: string, color: string | undefined, action?: ReturnType<typeof Button>) => (
      <Box key={k} flexDirection="column" width="33%" flexGrow={1} flexShrink={1} borderStyle="round" borderColor={C.line} backgroundColor={gui(C.card)} paddingX={1}>
        <Text dimColor size="caption">
          {label}
        </Text>
        <Box flexDirection="row" justifyContent="space-between" alignItems="center">
          <Text bold size="title" tabular color={color} dimColor={color === undefined}>
            {value}
          </Text>
          {action}
        </Box>
      </Box>
    )

    const strip = (
      <Box key="strip" flexDirection="row" gap={1}>
        {tile('tile-lessons', 'Lessons', String(hl?.lessons ?? current.length), C.green)}
        {tile(
          'tile-review',
          'To review',
          String(proposed.length),
          proposed.length > 0 ? C.amber : undefined,
          proposed.length > 0 && which !== 'review' ? <Button key="review-now" variant="secondary" label="Review" onPress={() => update($, tab, () => 'review')} /> : undefined,
        )}
        {tile('tile-recalled', 'Recalled', String(recalledCount), recalledCount > 0 ? C.blue : undefined)}
      </Box>
    )

    // the one active tab is filled; the others are chips
    const tabs = (
      <Box key="tabs" flexDirection="row" gap={1}>
        <Button key="t1" hotkey="1" label={`Recalled ${recalledCount}`} variant={which === 'recalled' ? 'primary' : 'secondary'} onPress={() => update($, tab, () => 'recalled')} />
        <Button key="t2" hotkey="2" label={`Review ${proposed.length}`} variant={which === 'review' ? 'primary' : 'secondary'} onPress={() => update($, tab, () => 'review')} />
        <Button key="t3" hotkey="3" label={`Lessons ${current.length}`} variant={which === 'lessons' ? 'primary' : 'secondary'} onPress={() => update($, tab, () => 'lessons')} />
      </Box>
    )

    // ---- small parts

    const pill = (p: Pill) => (
      <Text bold size="caption" color={PILL[p.tone].fg} backgroundColor={PILL[p.tone].bg}>
        {` ${p.text.toUpperCase()} `}
      </Text>
    )

    const hairline = (k: string) => (Svg ? <Svg key={k} source={barSvg(C.divider, 1)} alt="" height={1} /> : null)

    const heading = (sec: Section, count: number | null, note?: string) => (
      <Text key={`h-${sec}`} bold color={SECTION_COLOR[sec]}>
        {SECTION_TITLE[sec]}
        {count !== null && <Text dimColor> {count}</Text>}
        {note && <Text dimColor> · {note}</Text>}
      </Text>
    )

    const empty = (k: string, text: string) => (
      <Box key={k} backgroundColor={gui(C.inset)} paddingX={1}>
        <Text dimColor>{text}</Text>
      </Box>
    )

    // A card: coloured top bar, row 1 (pill + id | key number + actions), the lesson, then a dim
    // meta line under a hairline.
    const card = (k: string, sec: Section, l: Lesson, keyNum: { text: string; color?: string } | null, actions: ReturnType<typeof Button>[], meta: string, max: number) => (
      <Box key={k} flexDirection="column" borderStyle="round" borderColor={isGui ? C.line : SECTION_COLOR[sec]} backgroundColor={gui(C.card)} paddingX={1} gap={tight}>
        {Svg && (
          <Box position="absolute" top={0} left={1} right={1}>
            <Svg source={barSvg(SECTION_COLOR[sec], 4, 2)} alt="" height={4} />
          </Box>
        )}
        <Box flexDirection="row" justifyContent="space-between" alignItems="center" gap={1}>
          <Box flexDirection="row" alignItems="center" gap={1} flexShrink={1}>
            {pill({ text: l.project === 'global' ? 'global' : l.kind || 'lesson', tone: l.project === 'global' ? 'live' : sec === 'review' ? 'warn' : 'attn' })}
            <Text bold color={gui(C.name)} tabular>
              #{l.id}
            </Text>
          </Box>
          <Box flexDirection="row" alignItems="center" gap={1}>
            {keyNum && (
              <Text bold tabular color={keyNum.color}>
                {keyNum.text}
              </Text>
            )}
            {actions}
          </Box>
        </Box>
        <Text>{shorten(l.text, max)}</Text>
        {hairline(`${k}-hr`)}
        <Text color={gui(C.dim)} dimColor={!isGui} size="footnote" wrap="truncate-end">
          {meta}
        </Text>
      </Box>
    )

    let body
    if (hl !== null && !hl.isOnline) {
      body = (
        <Box key="sec-offline" flexDirection="column" gap={isGui ? 'sm' : 0}>
          {heading('offline', null)}
          <Box backgroundColor={gui(C.inset)} paddingX={1}>
            <Text color={C.red}>Offline: {hl.reason}. Prompts go through without lessons.</Text>
          </Box>
        </Box>
      )
    } else if (which === 'recalled') {
      body = (
        <Box key="sec-recalled" flexDirection="column" gap={isGui ? 'sm' : 0}>
          {heading('recalled', r ? r.lessons.length : null)}
          {hl === null && <Text dimColor>Checking the brain…</Text>}
          {r === null ? (
            empty('none-recalled', 'Nothing recalled yet. Lessons that match a prompt show here.')
          ) : (
            <Box flexDirection="column" gap={isGui ? 'sm' : 0}>
              <Text color={gui(C.dim)} dimColor={!isGui} wrap="truncate-end">
                For: {r.query}
              </Text>
              {r.error && <Text color={isGui ? C.red : 'red'}>{r.error}</Text>}
              {r.lessons.length === 0 && empty('none-matched', 'No lesson matched.')}
              {r.lessons.map(l => {
                const score = l.similarity ?? 0
                return card(`r${l.id}`, 'recalled', l, { text: score.toFixed(2), color: score >= MIN_SCORE ? (isGui ? C.green : 'green') : undefined }, [], `${l.project} · ${l.kind}`, 160)
              })}
            </Box>
          )}
        </Box>
      )
    } else if (which === 'review') {
      body = (
        <Box key="sec-review" flexDirection="column" gap={isGui ? 'sm' : 0}>
          {heading('review', proposed.length)}
          {proposed.length === 0 && empty('none-review', 'Nothing waiting. Lessons Claude proposes land here.')}
          {proposed.map(l =>
            card(
              `p${l.id}`,
              'review',
              l,
              null,
              [
                <Button key={`ok${l.id}`} label="Approve" variant="primary" onPress={() => void setStatus($, l.id, 'bless')} />,
                <Button key={`no${l.id}`} label="Reject" variant="secondary" onPress={() => void setStatus($, l.id, 'reject')} />,
              ],
              `${l.project} · ${l.kind} · from ${l.source}`,
              240,
            ),
          )}
        </Box>
      )
    } else {
      body = (
        <Box key="sec-lessons" flexDirection="column" gap={isGui ? 'sm' : 0}>
          {heading('lessons', current.length)}
          {current.length === 0 && empty('none-lessons', 'No active lessons for this project yet. /remember adds one.')}
          {current.map(l =>
            card(
              `a${l.id}`,
              'lessons',
              l,
              l.occurrences > 1 ? { text: `×${l.occurrences}` } : null,
              [
                // two presses: Forget arms it, Confirm forget runs it
                <Button
                  key={`fg${l.id}`}
                  plain
                  dimColor={armed !== l.id}
                  onPress={async () => {
                    if (armed === l.id) {
                      await update($, forgetArmed, () => null)
                      await setStatus($, l.id, 'forget')
                    } else await update($, forgetArmed, () => l.id)
                  }}
                >
                  <Text color={armed === l.id ? C.amber : undefined}>{armed === l.id ? 'Confirm forget' : 'Forget'}</Text>
                </Button>,
              ],
              `${l.project} · ${l.kind} · from ${l.source}`,
              150,
            ),
          )}
        </Box>
      )
    }

    return (
      <Box flexDirection="column" gap={1} backgroundColor={gui(C.pane)} paddingX={isGui ? 2 : 0} paddingY={isGui ? 1 : 0}>
        {header}
        {strip}
        {tabs}
        {body}
      </Box>
    )
  })
}

function clockTime(at: number) {
  try {
    return new Date(at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
  } catch {
    return ''
  }
}
