import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { BrainHealth, Lesson } from '../types'
import { MAX_RECALL, MIN_SCORE, parseRemember, recallContext, shorten, shouldRecall } from './format'

const PANE = 'brain'
const health = atom({ plugin: 'brain', key: 'health' } as const, null)
const recall = atom({ plugin: 'brain', key: 'recall' } as const, null)
const review = atom({ plugin: 'brain', key: 'review' } as const, [])
const active = atom({ plugin: 'brain', key: 'active' } as const, [])
const tab = atom({ plugin: 'brain', key: 'tab' } as const, 'recalled')

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

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e)
    const h = await read($, health)
    const r = await read($, recall)
    const proposed = await read($, review)
    const current = await read($, active)
    const which = await read($, tab)

    const header = (
      <Box key="tabs" marginBottom={1}>
        <Button key="t1" hotkey="1" label={`Recalled ${r?.lessons.length ?? 0}`} variant={which === 'recalled' ? 'primary' : undefined} onPress={() => update($, tab, () => 'recalled')} />
        <Button key="t2" hotkey="2" label={`Review ${proposed.length}`} variant={which === 'review' ? 'primary' : undefined} onPress={() => update($, tab, () => 'review')} />
        <Button key="t3" hotkey="3" label={`Lessons ${current.length}`} variant={which === 'lessons' ? 'primary' : undefined} onPress={() => update($, tab, () => 'lessons')} />
        <Button key="refresh" hotkey="r" label="Refresh" onPress={() => void refresh($)} />
      </Box>
    )
    const line = h === null
      ? <Text dimColor>Checking the brain…</Text>
      : h.isOnline
        ? <Text dimColor>{h.target} · {h.scope} · project {h.project}</Text>
        : <Text color="red">Offline: {h.reason}. Prompts go through without lessons.</Text>

    let body
    if (which === 'recalled') {
      body = r === null
        ? <Text dimColor>Nothing recalled yet. Lessons that match a prompt show here.</Text>
        : (
          <Box flexDirection="column">
            <Text dimColor>For: {r.query}</Text>
            {r.error && <Text color="red">{r.error}</Text>}
            {r.lessons.length === 0 && <Text dimColor>No lesson matched.</Text>}
            {r.lessons.map(l => (
              <Text key={`r${l.id}`}>
                <Text color={(l.similarity ?? 0) >= MIN_SCORE ? 'green' : undefined}>{(l.similarity ?? 0).toFixed(2)} </Text>
                <Text dimColor>[{l.project}] </Text>
                {shorten(l.text, 160)}
              </Text>
            ))}
          </Box>
        )
    } else if (which === 'review') {
      body = (
        <Box flexDirection="column">
          {proposed.length === 0 && <Text dimColor>Nothing waiting. Lessons Claude proposes land here.</Text>}
          {proposed.map(l => (
            <Box key={`p${l.id}`} flexDirection="column" marginBottom={1}>
              <Text>{shorten(l.text, 240)}</Text>
              <Box>
                <Text dimColor>#{l.id} · {l.project} · {l.kind} · from {l.source} </Text>
                <Button key={`ok${l.id}`} label="Approve" variant="primary" onPress={() => void setStatus($, l.id, 'bless')} />
                <Button key={`no${l.id}`} label="Reject" onPress={() => void setStatus($, l.id, 'reject')} />
              </Box>
            </Box>
          ))}
        </Box>
      )
    } else {
      body = (
        <Box flexDirection="column">
          {current.length === 0 && <Text dimColor>No active lessons for this project yet. /remember adds one.</Text>}
          {current.map(l => (
            <Box key={`a${l.id}`}>
              <Text dimColor>[{l.project}] </Text>
              <Text>{shorten(l.text, 150)} </Text>
              <Button key={`fg${l.id}`} label="Forget" plain onPress={() => void setStatus($, l.id, 'forget')} />
            </Box>
          ))}
        </Box>
      )
    }

    return (
      <Box flexDirection="column">
        {header}
        {line}
        <Box marginTop={1} flexDirection="column">{body}</Box>
      </Box>
    )
  })
}
