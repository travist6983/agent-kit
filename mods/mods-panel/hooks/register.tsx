import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { PanelAnswer, Usage } from '../types'
import {
  BURN_SCALE_USD,
  MARKETPLACE,
  answersContext,
  band,
  burritos,
  cards,
  filled,
  isRecent,
  ladder,
  limitRows,
  lockView,
  mcdoubles,
  money,
  openQuestions,
  recipeView,
  sessionRows,
  summary,
} from './view'
import { GLASS_LOOK, GLASS_SCENE_ID, SCENE } from './glass'
import type { Card, QuestionIn, Snapshot } from './view'

const PANE = 'mods'
const TITLE = 'Mods'
const ACCENT = '#FF9F0A' // the design's one warm accent, for spend and warnings
const ON_ACCENT = '#1a1206'
const MAX_TURNS = 12

// ---- this panel's own values

const openAtom = atom({ plugin: 'mods-panel', key: 'open' } as const, null)
const tabAtom = atom({ plugin: 'mods-panel', key: 'tab' } as const, 'run')
const usageAtom = atom({ plugin: 'mods-panel', key: 'usage' } as const, null)
const turnsAtom = atom({ plugin: 'mods-panel', key: 'turns' } as const, [])
const enabledAtom = atom({ plugin: 'mods-panel', key: 'enabled' } as const, {})
const toggledAtom = atom({ plugin: 'mods-panel', key: 'toggled' } as const, {})
const answeredAtom = atom({ plugin: 'mods-panel', key: 'answered' } as const, [])
const pendingAtom = atom({ plugin: 'mods-panel', key: 'pending' } as const, [])
const bandAtom = atom({ plugin: 'mods-panel', key: 'isBandOn' } as const, true)

// ---- the other mods' values, read only ("any plugin reads any value; its owner alone writes it")

const RR_RUN = { plugin: 'recipe-run', key: 'run' } as const
const RR_OPEN = { plugin: 'recipe-run', key: 'open' } as const
const RR_TARGET = { plugin: 'recipe-run', key: 'target' } as const
const BURN = { plugin: 'burn-meter', key: 'settings' } as const
const TC_ME = { plugin: 'traffic-control', key: 'me' } as const
const TC_PEERS = { plugin: 'traffic-control', key: 'peers' } as const
const TC_LOCKS = { plugin: 'traffic-control', key: 'locks' } as const
const SHIP = { plugin: 'ship', key: 'status' } as const
const DEV_SERVERS = { plugin: 'dev-servers', key: 'servers' } as const
const DEV_RECIPE = { plugin: 'dev-servers', key: 'recipe' } as const
const PR = { plugin: 'pr', key: 'snapshot' } as const
const BRAIN_HEALTH = { plugin: 'brain', key: 'health' } as const
const BRAIN_RECALL = { plugin: 'brain', key: 'recall' } as const
const BRAIN_REVIEW = { plugin: 'brain', key: 'review' } as const

// A value another mod never wrote reads undefined at version 0: that mod is off, or not loaded yet.
async function snapshot($: EngineInterface): Promise<Snapshot> {
  const g = async <T,>(p: Promise<{ value?: unknown; version: number }>) => {
    const r = await p
    return r.version > 0 ? (r.value as T) : undefined
  }
  const [run, open, burn, me, peers, locks, ship, servers, devRecipe, pr, health, recall, review] = await Promise.all([
    g<Snapshot['recipe']['run']>($.state.get(RR_RUN as never)),
    g<QuestionIn[]>($.state.get(RR_OPEN as never)),
    g<Snapshot['burn']>($.state.get(BURN as never)),
    g<string>($.state.get(TC_ME as never)),
    g<NonNullable<Snapshot['traffic']>['peers']>($.state.get(TC_PEERS as never)),
    g<NonNullable<Snapshot['traffic']>['locks']>($.state.get(TC_LOCKS as never)),
    g<Snapshot['ship']>($.state.get(SHIP as never)),
    g<NonNullable<Snapshot['dev']>['servers']>($.state.get(DEV_SERVERS as never)),
    g<NonNullable<Snapshot['dev']>['recipe']>($.state.get(DEV_RECIPE as never)),
    g<Snapshot['pr']>($.state.get(PR as never)),
    g<NonNullable<Snapshot['brain']>['health']>($.state.get(BRAIN_HEALTH as never)),
    g<NonNullable<Snapshot['brain']>['recall']>($.state.get(BRAIN_RECALL as never)),
    g<unknown[]>($.state.get(BRAIN_REVIEW as never)),
  ])
  return {
    now: await $.clock.now(),
    enabled: await read($, enabledAtom),
    toggled: await read($, toggledAtom),
    usage: await read($, usageAtom),
    turns: await read($, turnsAtom),
    recipe: { run: run ?? null, open: open ?? [] },
    answered: await read($, answeredAtom),
    burn: burn ?? null,
    traffic: peers !== undefined || locks !== undefined ? { me: me ?? '', peers: peers ?? [], locks: locks ?? [] } : null,
    ship: ship ?? null,
    dev: servers !== undefined || devRecipe !== undefined ? { servers: servers ?? [], recipe: devRecipe ?? [] } : null,
    pr: pr ?? null,
    brain: health !== undefined || recall !== undefined ? { health: health ?? null, recall: recall ?? null, review: review ?? [] } : null,
  }
}

// ---- reading the session and settings into this panel's own state

async function measure($: EngineInterface, u?: { rateLimits: Usage['rateLimits']; cost?: { usd?: number } }) {
  const now = await $.clock.now()
  const usage = u ?? (await $.session.usage())
  const next: Usage = { costUsd: usage.cost?.usd ?? null, rateLimits: usage.rateLimits.map(r => ({ ...r })), at: now }
  await update($, usageAtom, () => next)
}

async function readEnabled($: EngineInterface) {
  const raw = (await $.settings.read({ key: 'enabledPlugins' })) as unknown
  const map: Record<string, boolean> = {}
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    for (const [id, v] of Object.entries(raw as Record<string, unknown>)) if (typeof v === 'boolean') map[id] = v
  }
  await update($, enabledAtom, () => map)
}

async function refresh($: EngineInterface) {
  await Promise.all([measure($).catch(() => undefined), readEnabled($).catch(() => undefined)])
}

// ---- acts

async function toggle($: EngineInterface, c: Card) {
  if (c.power === 'missing') return
  const want = c.pending !== null ? c.pending === false : c.power !== 'on'
  const r = await $.process.run(['claude', 'plugin', want ? 'enable' : 'disable', `${c.name}@${MARKETPLACE}`])
  if (r.exitCode !== 0) {
    $.ui.toast(`Could not turn ${c.name} ${want ? 'on' : 'off'}: ${(r.stderr || r.stdout).trim().slice(0, 160)}`, { timeoutMs: 10_000 })
    return
  }
  await update($, toggledAtom, t => ({ ...t, [c.name]: want }))
  await readEnabled($).catch(() => undefined)
  $.ui.toast(`${c.name} turned ${want ? 'on' : 'off'}. Run /reload-plugins to apply it.`, { timeoutMs: 8000 })
}

async function gitRoot($: EngineInterface) {
  const cwd = await $.session.cwd()
  const top = await $.process.run(['git', 'rev-parse', '--show-toplevel'], { cwd })
  return top.exitCode === 0 ? top.stdout.trim() : cwd
}

// Appends one line with O_APPEND, as recipe-run does, so a loop writing the same file loses nothing.
async function appendLine($: EngineInterface, path: string, line: string) {
  const r = await $.process.run(['sh', '-c', 'mkdir -p "$(dirname "$2")" && printf "%s\\n" "$1" >> "$2"', 'append', line, path])
  if (r.exitCode !== 0) throw new Error(r.stderr.trim() || `could not write ${path}`)
}

// Writes the answer where recipe-run reads it (runs/<KEY>/answers.jsonl, its format), then hands
// it to Claude with the next message. recipe-run's own pane drops the question on its next scan.
async function answer($: EngineInterface, q: QuestionIn, text: string) {
  const value = text.trim()
  if (value === '') return
  const run = (await $.state.get(RR_RUN as never)).value as { root?: string } | null | undefined
  const target = (await $.state.get(RR_TARGET as never)).value as { root?: string } | null | undefined
  const root = run?.root ?? target?.root ?? (await gitRoot($))
  const a: PanelAnswer = { id: q.id, key: q.key, question: q.question, answer: value, answeredAt: new Date(await $.clock.now()).toISOString() }
  try {
    await appendLine($, `${root}/runs/${q.key}/answers.jsonl`, JSON.stringify({ id: a.id, key: a.key, answer: a.answer, answered_at: a.answeredAt, by: 'human' }))
  } catch (err) {
    $.ui.toast(`Could not save the answer: ${String(err)}`, { timeoutMs: 8000 })
    return
  }
  await update($, answeredAtom, list => [...list, a].slice(-50))
  await update($, pendingAtom, list => [...list, a])
  $.ui.toast(`Answered ${q.key}. Claude reads it with your next message.`, { timeoutMs: 6000 })
}

async function openPane($: EngineInterface) {
  await refresh($)
  return $.ui.open({ id: PANE, title: TITLE })
}

// Which cards start expanded: the run while one is live or asking, else the burn meter.
function defaultOpen(s: Snapshot) {
  const live = isRecent(s.recipe.run, s.now) || openQuestions(s.recipe.open, s.answered).length > 0
  return live ? ['recipe-run'] : ['burn-meter']
}

// ---- drawing helpers

type El = ReturnType<EngineInterface['ui']['resolve']>

function Meter({ els, n, frac, color, k }: { els: El; n: number; frac: number; color: string; k: string }) {
  const { Box, Text } = els
  const on = filled(n, frac)
  return (
    <Box key={k} flexDirection="row">
      <Text color={color}>{'█'.repeat(on)}</Text>
      <Text dimColor>{'░'.repeat(n - on)}</Text>
    </Box>
  )
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const saved = (await $.store.get('isBandOn')) as boolean | undefined
    if (typeof saved === 'boolean') await update($, bandAtom, () => saved)
    // Glass is on by default: set at every session start (a look doesn't outlive its session)
    // until the person turns it off with /mods glass off, which is saved.
    if ((await $.store.get('isGlassOn')) !== false) await $.theme.set(GLASS_LOOK).catch(() => undefined)
    await $.command.register({
      name: 'mods',
      description: 'Mods panel: every agent-kit mod, its live status and an on/off switch. /mods band on|off for the row above the prompt, /mods glass on|off for the frosted look',
      argumentHint: '[band on|off] [glass on|off]',
    })
    await refresh($).catch(() => undefined)
    $.clock.every(30_000, () => void refresh($).catch(() => undefined))
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    await measure($, e).catch(() => undefined)
    return next(e)
  })

  // Per-turn cost: the session's cost when a main turn starts, against its cost when it ends.
  let costAtStart: number | null = null
  on('turn.start', async ($, e, next) => {
    costAtStart = (await $.session.usage()).cost?.usd ?? null
    return next(e)
  })
  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined && costAtStart !== null) {
      const end = (await $.session.usage()).cost?.usd ?? null
      if (end !== null) {
        const spent = Math.max(0, end - costAtStart)
        await update($, turnsAtom, list => [...list, spent].slice(-MAX_TURNS))
      }
      costAtStart = null
      await measure($).catch(() => undefined)
    }
    return next(e)
  })

  on('command.run', { command: 'mods' }, async ($, e) => {
    const arg = (e.args ?? '').trim().toLowerCase()
    if (arg === 'band off' || arg === 'band on') {
      const isOn = arg === 'band on'
      await update($, bandAtom, () => isOn)
      await $.store.set('isBandOn', isOn)
      return { text: isOn ? 'Mods band on above the prompt.' : 'Mods band off. recipe-run draws its own row again.' }
    }
    if (arg === 'glass on') {
      const set = await $.theme.set(GLASS_LOOK)
      await $.store.set('isGlassOn', true)
      return {
        text: set.isDrawn
          ? 'Mods glass on: frosted panels over a dark backdrop in the desktop app. /mods glass off clears it.'
          : `Mods glass is saved and shows in the desktop and mobile apps; this one draws no look (${set.reason}).`,
      }
    }
    if (arg === 'glass off') {
      // There is one theme per session, so this clears whatever look stands; it runs only on this ask.
      await $.theme.clear()
      await $.store.set('isGlassOn', false)
      return { text: 'Mods glass off: back to the app\'s own look.' }
    }
    if (arg !== '') return { text: 'Usage: /mods, /mods band on|off, /mods glass on|off' }
    const opened = await openPane($)
    const s = await snapshot($)
    const sum = summary(s, cards(s))
    const spend = sum.spend !== null ? `, ${money(sum.spend)} this session` : ''
    return { text: `${opened.isPlaced ? 'Mods panel open' : 'Mods panel waiting for room to open'}: ${sum.on} of ${sum.total} mods on, ${sum.blocking} blocking questions${spend}.` }
  })

  // Answers given in the panel reach the model with the person's next message.
  on('prompt.submit', async ($, e, next) => {
    const todo = await read($, pendingAtom)
    if (todo.length === 0) return next(e)
    const text = answersContext(todo)
    await update($, pendingAtom, () => [])
    return text ? next({ ...e, context: [...(e.context ?? []), text] }) : next(e)
  })

  // ---- the glass backdrop (desktop and mobile only; the terminal draws no look)

  on('ui.render', { component: 'Backdrop' }, async ($, e, next) => {
    if (e.props.scene !== GLASS_SCENE_ID || (e.surface !== 'desktop' && e.surface !== 'mobile')) return next(e)
    const { Svg } = $.ui.resolve(e)
    return <Svg source={SCENE} alt="Slow orange, blue and violet light drifting behind frosted glass" />
  })

  // ---- the band above the prompt

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || !(await read($, bandAtom))) return next(e)
    const els = $.ui.resolve(e)
    const { Box, Button, Text } = els
    const s = await snapshot($)
    const b = band(s)
    return (
      <Box flexDirection="row" justifyContent="space-between" gap={2}>
        <Box flexDirection="column" flexShrink={1}>
          <Text bold color={b.isBlocked ? ACCENT : undefined} wrap="truncate-end">
            {b.isBlocked ? '● ' : isRecent(s.recipe.run, s.now) ? '● ' : '○ '}
            {b.title}
          </Text>
          <Text dimColor wrap="truncate-end">
            {b.meta}
          </Text>
        </Box>
        {b.limit && (
          <Text bold color={ACCENT}>
            {b.limit}
          </Text>
        )}
        <Box flexDirection="column" alignItems="flex-end">
          <Text bold color={ACCENT}>
            {b.spend}
          </Text>
          <Text dimColor>{b.spendSub}</Text>
        </Box>
        {b.blocking > 0 ? (
          <Button
            key="band-blocking"
            variant="primary"
            label={`${b.blocking} blocking`}
            onPress={async () => {
              await update($, tabAtom, () => 'questions')
              await update($, openAtom, list => [...new Set([...(list ?? defaultOpen(s)), 'recipe-run'])])
              await openPane($)
            }}
          />
        ) : (
          <Button key="band-open" plain dimColor label="Mods" onPress={() => openPane($)} />
        )}
      </Box>
    )
  })

  // While the Mods band is drawn it carries recipe-run's run line, so recipe-run's own row is left
  // out of the band (recipe-run itself is untouched). /mods band off brings that row back.
  on('ui.render', { component: 'AbovePromptList' }, async ($, e, next) => {
    if (!e.props.rows.some(r => r.plugin === 'mods-panel')) return next(e)
    return next({ ...e, props: { ...e.props, rows: e.props.rows.filter(r => r.plugin !== 'recipe-run') } })
  }).catch(($, e, next) => next(e))

  // ---- the pane

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e)
    const { Box, Button, Input, Text } = els
    const s = await snapshot($)
    const list = cards(s)
    const sum = summary(s, list)
    const open = new Set((await read($, openAtom)) ?? defaultOpen(s))
    const tab = await read($, tabAtom)
    const cols = Math.max(30, e.props.bodyColumns ?? 44)
    const wide = Math.max(8, Math.min(24, cols - 14))

    const setOpen = (name: string) => update($, openAtom, cur => {
      const set = new Set(cur ?? defaultOpen(s))
      if (set.has(name)) set.delete(name)
      else set.add(name)
      return [...set]
    })

    const header = (
      <Box key="head" flexDirection="row" justifyContent="space-between" alignItems="center">
        <Text>
          <Text bold size="title">
            Mods
          </Text>
          <Text dimColor> agent-kit</Text>
        </Text>
        <Box flexDirection="row" gap={1}>
          <Button key="refresh" plain dimColor label="Refresh" onPress={() => refresh($)} />
          <Button key="close" role="dismiss" plain dimColor label="✕" onPress={() => $.ui.close({ id: PANE })} />
        </Box>
      </Box>
    )

    const tile = (k: string, value: string, caption: string, color?: string, action?: { key: string; label: string; onPress: () => unknown }) => (
      <Box key={k} flexDirection="column" flexGrow={1} borderStyle="round" borderColor={color === ACCENT && k === 'tile-blocking' ? ACCENT : 'subtle'} paddingX={1}>
        <Box flexDirection="row" justifyContent="space-between">
          <Text bold size="title" tabular color={color}>
            {value}
          </Text>
          {action && <Button key={action.key} plain label={action.label} onPress={action.onPress} />}
        </Box>
        <Text dimColor size="caption">
          {caption}
        </Text>
      </Box>
    )

    const strip = (
      <Box key="strip" flexDirection="row" gap={1}>
        {tile('tile-on', `${sum.on}/${sum.total}`, 'mods on')}
        {tile(
          'tile-blocking',
          String(sum.blocking),
          'blocking questions',
          sum.blocking > 0 ? ACCENT : undefined,
          sum.blocking > 0
            ? {
                key: 'answer-now',
                label: 'Answer',
                onPress: async () => {
                  await update($, tabAtom, () => 'questions')
                  await update($, openAtom, cur => [...new Set([...(cur ?? defaultOpen(s)), 'recipe-run'])])
                },
              }
            : undefined,
        )}
        {tile('tile-spend', sum.spend !== null ? money(sum.spend) : 'n/a', 'session spend', ACCENT)}
      </Box>
    )

    // ---- per-mod bodies

    const recipeBody = () => {
      const qs = openQuestions(s.recipe.open, s.answered)
      const blocking = qs.filter(q => q.kind === 'blocking').length
      const tabs = (
        <Box key="rr-tabs" flexDirection="row" gap={1}>
          <Button key="tab-run" label="Run" variant={tab === 'run' ? 'primary' : undefined} onPress={() => update($, tabAtom, () => 'run')} />
          <Button
            key="tab-questions"
            label={blocking > 0 ? `Questions (${blocking})` : qs.length > 0 ? `Questions · ${qs.length}` : 'Questions'}
            variant={tab === 'questions' ? 'primary' : undefined}
            onPress={() => update($, tabAtom, () => 'questions')}
          />
        </Box>
      )
      if (tab === 'questions') {
        const recent = s.answered.slice(-5)
        return (
          <Box key="rr-body" flexDirection="column" gap={1}>
            {tabs}
            {qs.length === 0 && recent.length === 0 && <Text dimColor>No questions. A run asks here when it needs a decision from you.</Text>}
            {qs.map(q => (
              <Box key={`q-${q.key}-${q.id}`} flexDirection="column" borderStyle="round" borderColor={q.kind === 'blocking' ? ACCENT : 'subtle'} paddingX={1}>
                <Text>
                  <Text bold color={q.kind === 'blocking' ? ACCENT : undefined}>
                    {q.kind === 'blocking' ? 'BLOCKING' : 'CAN WAIT'}
                  </Text>
                  <Text dimColor>
                    {' '}
                    · {q.key} · from {q.from}
                  </Text>
                </Text>
                <Text>{q.question}</Text>
                {q.context && <Text dimColor>{q.context}</Text>}
                {q.default && <Text dimColor>If nobody answers: {q.default}</Text>}
                {q.options.length > 0 && (
                  <Box flexDirection="row" flexWrap="wrap" gap={1}>
                    {q.options.map((o, i) => (
                      <Button key={`a-${q.key}-${q.id}-${i}`} label={o} onPress={() => answer($, q, o)} />
                    ))}
                  </Box>
                )}
                <Input key={`in-${q.key}-${q.id}`} placeholder="Or type an answer…" submitLabel="↑" onSubmit={value => void answer($, q, value)} />
              </Box>
            ))}
            {recent.map(a => (
              <Text key={`done-${a.key}-${a.id}`} dimColor>
                ✓ Answered {a.key}: <Text bold>{a.answer}</Text>
              </Text>
            ))}
          </Box>
        )
      }
      const run = s.recipe.run
      if (!run) {
        return (
          <Box key="rr-body" flexDirection="column" gap={1}>
            {tabs}
            <Text bold>No recipe running</Text>
            <Text dimColor>No run under runs/ yet.</Text>
          </Box>
        )
      }
      const v = recipeView(run, blocking > 0)
      const live = isRecent(run, s.now)
      return (
        <Box key="rr-body" flexDirection="column" gap={1}>
          {tabs}
          {!live && <Text bold>No recipe running · last run below</Text>}
          <Box key="rr-title" flexDirection="row" justifyContent="space-between">
            <Text bold>{v.name}</Text>
            <Text dimColor>{v.key}</Text>
          </Box>
          <Box key="rr-ladder" flexDirection="column">
            {ladder(run, blocking > 0).map(st => (
              <Box key={`st-${st.id}`} flexDirection="row" justifyContent="space-between">
                <Text
                  bold={st.state === 'run' || st.state === 'blocked'}
                  dimColor={st.state === 'wait'}
                  color={st.state === 'blocked' ? ACCENT : st.state === 'failed' ? 'error' : undefined}
                >
                  {st.glyph} {st.label}
                </Text>
                <Text dimColor={st.state !== 'blocked'} color={st.state === 'blocked' ? ACCENT : undefined}>
                  {st.meta}
                </Text>
              </Box>
            ))}
          </Box>
          {v.attempt !== null && (
            <Box key="rr-attempt" flexDirection="row" justifyContent="space-between">
              <Text>
                <Text bold>Attempt</Text>
                <Text dimColor>
                  {' '}
                  {v.attempt} of {v.max}
                </Text>
              </Text>
              <Text>
                {Array.from({ length: v.max }, (_, i) => (i + 1 < (v.attempt ?? 0) ? '▬' : i + 1 === v.attempt ? '▬' : '▭')).join(' ')}
              </Text>
            </Box>
          )}
          {v.verdict && (
            <Box key="rr-verdict" flexDirection="column" borderStyle="round" borderColor="subtle" paddingX={1}>
              <Box flexDirection="row" justifyContent="space-between">
                <Text bold>Router's last call</Text>
                <Text bold color={v.verdict.isAlarm ? ON_ACCENT : undefined} backgroundColor={v.verdict.isAlarm ? ACCENT : undefined}>
                  {` ${v.verdict.badge} `}
                </Text>
              </Box>
              <Text dimColor>{v.verdict.reason}</Text>
            </Box>
          )}
          {v.spend && (
            <Box key="rr-spend" flexDirection="column">
              <Box flexDirection="row" justifyContent="space-between">
                <Text bold>Spend</Text>
                <Text>
                  <Text bold color={ACCENT}>
                    {money(v.spend.spent)}
                  </Text>
                  <Text dimColor>{v.spend.cap !== null ? ` of ${money(v.spend.cap)} cap` : ''}</Text>
                </Text>
              </Box>
              {v.spend.cap !== null && <Meter els={els} k="rr-cap" n={Math.min(20, wide)} frac={v.spend.spent / v.spend.cap} color={ACCENT} />}
            </Box>
          )}
        </Box>
      )
    }

    const burnBody = (c: Card) => {
      const spend = s.usage?.costUsd ?? null
      const rows = limitRows(s.usage?.rateLimits ?? [], s.now)
      const h5 = rows.find(r => r.kind === 'five_hour')
      const turns = s.turns.map((cost, i) => ({ n: i + 1, cost })).slice(-6).reverse()
      const maxTurn = Math.max(0.0001, ...s.turns)
      return (
        <Box key="burn-body" flexDirection="column" gap={1}>
          <Box key="burn-session" flexDirection="column">
            <Text bold color={ACCENT} size="caption">
              SESSION BURN
            </Text>
            {spend !== null && (
              <Text tabular>
                <Text bold>{money(spend)}</Text>
                <Text dimColor> = {burritos(spend)} burritos</Text>
              </Text>
            )}
            {spend !== null && <Meter els={els} k="burn-meter-bar" n={wide} frac={spend / BURN_SCALE_USD} color={ACCENT} />}
            {spend === null && <Text dimColor>No cost reading yet.</Text>}
          </Box>
          <Box key="burn-limits" flexDirection="column">
            <Text bold>Limits</Text>
            {rows.length === 0 && <Text dimColor>No limit readings yet (they come with the first reply, on a subscription plan).</Text>}
            {rows.map(r => (
              <Box key={`lim-${r.kind}`} flexDirection="row" gap={1}>
                <Box width={7}>
                  <Text dimColor>{r.label}</Text>
                </Box>
                <Meter els={els} k={`lim-bar-${r.kind}`} n={Math.max(6, wide - 12)} frac={r.pct / 100} color={r.isHot ? ACCENT : 'text'} />
                <Text bold={r.isHot} color={r.isHot ? ACCENT : undefined} dimColor={!r.isHot} tabular>
                  {r.pct}%{r.reset ? ` · ${r.reset.replace(/^in /, '')}` : ''}
                </Text>
              </Box>
            ))}
            {h5?.isHot && (
              <Box key="burn-warn" borderStyle="round" borderColor={ACCENT} paddingX={1}>
                <Text>
                  The 5 hour limit is at {h5.pct}%.{h5.reset ? ` It resets ${h5.reset}.` : ''}
                </Text>
              </Box>
            )}
          </Box>
          <Box key="burn-turns" flexDirection="column">
            <Text bold>Per turn</Text>
            {turns.length === 0 && <Text dimColor>No finished turns yet this session.</Text>}
            {turns.map(t => (
              <Box key={`turn-${t.n}`} flexDirection="row" gap={1}>
                <Box width={4}>
                  <Text dimColor>#{t.n}</Text>
                </Box>
                <Meter els={els} k={`turn-bar-${t.n}`} n={Math.max(6, wide - 10)} frac={t.cost / maxTurn} color="text" />
                <Text dimColor tabular>
                  {money(t.cost)}
                </Text>
              </Box>
            ))}
          </Box>
          {spend !== null && (
            <Box key="burn-that" flexDirection="column">
              <Text bold>That is</Text>
              <Text color={ACCENT}>= {burritos(spend)} burritos</Text>
              <Text color={ACCENT}>= {mcdoubles(spend)} McDoubles</Text>
            </Box>
          )}
          {c.details.map(([k, val]) => (
            <Box key={`burn-d-${k}`} flexDirection="row" justifyContent="space-between">
              <Text dimColor>{k}</Text>
              <Text>{val}</Text>
            </Box>
          ))}
        </Box>
      )
    }

    const trafficBody = () => {
      if (!s.traffic) return <Text key="tc-body" dimColor>No sessions seen yet.</Text>
      const rows = sessionRows(s.traffic, s.now)
      const lock = lockView(s.traffic, s.now)
      return (
        <Box key="tc-body" flexDirection="column" gap={1}>
          <Box key="tc-sessions" flexDirection="column">
            <Text bold>Sessions</Text>
            {rows.length === 0 && <Text dimColor>No other Claude sessions are live.</Text>}
            {rows.map(r => (
              <Box key={`tc-${r.label}`} flexDirection="row" justifyContent="space-between" gap={1}>
                <Text wrap="truncate-end">
                  <Text dimColor={!r.isBusy}>{r.isBusy ? '● ' : '○ '}</Text>
                  <Text bold>{r.label}</Text>
                  <Text dimColor> {r.branch}</Text>
                </Text>
                <Text dimColor={!r.isBusy}>{r.state}</Text>
              </Box>
            ))}
          </Box>
          <Box key="tc-lock" flexDirection="column" borderStyle="round" borderColor="subtle" paddingX={1}>
            <Box flexDirection="row" justifyContent="space-between">
              <Text bold>Push lock</Text>
              <Text bold>{lock.state}</Text>
            </Box>
            <Text dimColor>{lock.detail}</Text>
          </Box>
        </Box>
      )
    }

    const genericBody = (c: Card) =>
      c.details.length === 0 ? (
        <Text key={`${c.name}-body`} dimColor>
          Nothing more to show yet.
        </Text>
      ) : (
        <Box key={`${c.name}-body`} flexDirection="column">
          {c.details.map(([k, val]) => (
            <Box key={`${c.name}-d-${k}`} flexDirection="row" justifyContent="space-between" gap={2}>
              <Text dimColor>{k}</Text>
              <Text wrap="truncate-end">{val}</Text>
            </Box>
          ))}
        </Box>
      )

    const card = (c: Card) => {
      const isOn = c.power === 'on'
      const isOpen = open.has(c.name)
      const dot = !isOn ? '○' : '●'
      const dotColor = !isOn ? undefined : c.isWarn ? ACCENT : c.isActive ? 'text' : undefined
      const switchLabel = c.power === 'missing' ? null : (c.pending ?? isOn) ? 'On' : 'Off'
      return (
        <Box key={`card-${c.name}`} flexDirection="column" borderStyle="round" borderColor={c.isWarn ? ACCENT : 'subtle'} paddingX={1}>
          <Box flexDirection="row" alignItems="center" gap={1}>
            <Text color={dotColor} dimColor={!isOn || (!c.isWarn && !c.isActive)}>
              {dot}
            </Text>
            <Box flexDirection="column" flexGrow={1} flexShrink={1}>
              <Button key={`expand-${c.name}`} plain dimColor={!isOn} label={`${c.name} ${isOpen ? '⌄' : '›'}`} onPress={() => setOpen(c.name)} />
              <Text color={isOn && c.isWarn ? ACCENT : undefined} dimColor={!(isOn && c.isWarn)} wrap="truncate-end" tabular>
                {c.status}
              </Text>
            </Box>
            {switchLabel && (
              <Button key={`switch-${c.name}`} label={switchLabel} variant={switchLabel === 'On' ? 'primary' : undefined} onPress={() => toggle($, c)} />
            )}
          </Box>
          {isOpen && (
            <Box flexDirection="column" marginTop={1}>
              {!isOn
                ? genericBody(c)
                : c.name === 'recipe-run'
                  ? recipeBody()
                  : c.name === 'burn-meter'
                    ? burnBody(c)
                    : c.name === 'traffic-control'
                      ? trafficBody()
                      : genericBody(c)}
            </Box>
          )}
        </Box>
      )
    }

    return (
      <Box flexDirection="column" gap={1}>
        {header}
        {strip}
        {list.map(card)}
      </Box>
    )
  })
}

