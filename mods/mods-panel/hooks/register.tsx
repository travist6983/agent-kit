import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, Register, RenderChildren } from 'claude-code'

import type { PanelAnswer, ThrottleAsk, Usage } from '../types'
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
  look,
  mcdoubles,
  money,
  openQuestions,
  recipeView,
  serverRows,
  sessionRows,
  summary,
  SECTIONS,
} from './view'
import { C, PILL, SECTION_COLOR, SECTION_TITLE, barSvg, burnSvg } from './chrome'
import type { Card, Look, Pill, QuestionIn, Snapshot } from './view'

const PANE = 'mods'
const TITLE = 'Mods'
const ACCENT = '#FF9F0A' // the design's one warm accent, for spend and warnings
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
const stopArmedAtom = atom({ plugin: 'mods-panel', key: 'stopArmed' } as const, null)
const stoppedAtom = atom({ plugin: 'mods-panel', key: 'stopped' } as const, [])
const throttleAskAtom = atom({ plugin: 'mods-panel', key: 'throttleAsk' } as const, null)

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
const THROTTLE_SETTING = { plugin: 'throttle', key: 'setting' } as const
const THROTTLE_SEEN = { plugin: 'throttle', key: 'seen' } as const

// A value another mod never wrote reads undefined at version 0: that mod is off, or not loaded yet.
async function snapshot($: EngineInterface): Promise<Snapshot> {
  const g = async <T,>(p: Promise<{ value?: unknown; version: number }>) => {
    const r = await p
    return r.version > 0 ? (r.value as T) : undefined
  }
  const [run, open, burn, me, peers, locks, ship, servers, devRecipe, pr, health, recall, review, tSetting, tSeen] = await Promise.all([
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
    g<NonNullable<Snapshot['throttle']>['setting']>($.state.get(THROTTLE_SETTING as never)),
    g<NonNullable<Snapshot['throttle']>['seen']>($.state.get(THROTTLE_SEEN as never)),
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
    throttle: tSetting !== undefined || tSeen !== undefined ? { setting: tSetting ?? null, seen: tSeen ?? null } : null,
    throttleAsk: await read($, throttleAskAtom),
  }
}

// ---- stopping a dev server from its card

// The pid still listening on this port, or null: dev-servers scans every 15 seconds, so the row
// can be stale, and a pid can be reused. Only a pid that still holds the port is signalled.
async function holder($: EngineInterface, pid: number, port: number) {
  const r = await $.process.run(['lsof', '-nP', '-a', '-p', String(pid), `-iTCP:${port}`, '-sTCP:LISTEN', '-t'])
  return r.exitCode === 0 && r.stdout.trim() !== '' ? pid : null
}

async function stopServer($: EngineInterface, pid: number, port: number) {
  await update($, stopArmedAtom, () => null)
  if ((await holder($, pid, port)) === null) {
    await $.ui.toast(`:${port} is already stopped.`, { timeoutMs: 5000 })
    await update($, stoppedAtom, cur => [...new Set([...cur, pid])])
    return
  }
  await $.process.run(['kill', String(pid)])
  await $.clock.sleep(1500)
  if ((await holder($, pid, port)) !== null) await $.process.run(['kill', '-9', String(pid)])
  const isGone = (await holder($, pid, port)) === null
  if (isGone) await update($, stoppedAtom, cur => [...new Set([...cur, pid])])
  await $.ui.toast(isGone ? `Stopped :${port} (pid ${pid}).` : `Could not stop :${port} (pid ${pid}).`, { timeoutMs: 6000 })
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

// Which cards start expanded: the live ones with controls (throttle, dev-servers, a live run),
// and the run whenever it is blocked on a question.
function defaultOpen(s: Snapshot) {
  return cards(s)
    .filter(c => {
      const sec = look(c, s).section
      return (sec === 'live' && ['recipe-run', 'throttle', 'dev-servers'].includes(c.name)) || (c.name === 'recipe-run' && sec === 'look')
    })
    .map(c => c.name)
}

// "2:58 PM" for the header's last refresh.
function clockTime(at: number) {
  try {
    return new Date(at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
  } catch {
    return ''
  }
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
    await $.command.register({
      name: 'mods',
      description: 'Mods panel: every agent-kit mod, its live status and an on/off switch. /mods band on|off for the row above the prompt',
      argumentHint: '[band on|off]',
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
    if (arg !== '') return { text: 'Usage: /mods, /mods band on|off' }
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
    // Desktop, VS Code and mobile draw SVG and the palette; the terminal keeps its own colours.
    const isGui = e.surface !== 'terminal'
    const Svg = isGui ? ((els as Partial<Elements['desktop']>).Svg ?? undefined) : undefined
    const gui = <T,>(v: T) => (isGui ? v : undefined)
    const s = await snapshot($)
    const list = cards(s)
    const looks = new Map(list.map(c => [c.name, look(c, s)]))
    const lookOf = (c: Card) => looks.get(c.name) ?? look(c, s)
    const sum = summary(s, list)
    const open = new Set((await read($, openAtom)) ?? defaultOpen(s))
    const tab = await read($, tabAtom)
    const armed = await read($, stopArmedAtom)
    // pids stopped here stay hidden only until dev-servers' scan stops listing them
    const listed = new Set((s.dev?.servers ?? []).map(x => x.pid))
    const stopped = (await read($, stoppedAtom)).filter(pid => listed.has(pid))
    const cols = Math.max(30, e.props.bodyColumns ?? 44)
    const wide = Math.max(8, Math.min(24, cols - 14))
    const tight = isGui ? ('xs' as const) : 0

    const setOpen = (name: string) => update($, openAtom, cur => {
      const set = new Set(cur ?? defaultOpen(s))
      if (set.has(name)) set.delete(name)
      else set.add(name)
      return [...set]
    })

    const header = (
      <Box key="head" flexDirection="row" justifyContent="space-between" alignItems="center">
        <Text>
          <Text bold size="heading">
            Mods
          </Text>
          <Text dimColor> agent-kit</Text>
        </Text>
        <Box flexDirection="row" gap={1} alignItems="center">
          {s.usage && (
            <Text dimColor size="footnote">
              ↻ {clockTime(s.usage.at)}
            </Text>
          )}
          <Button key="refresh" variant="secondary" hotkey="r" label="Refresh" onPress={() => refresh($)} />
          <Button key="close" role="dismiss" plain dimColor label="✕" onPress={() => $.ui.close({ id: PANE })} />
        </Box>
      </Box>
    )

    const tile = (k: string, label: string, value: RenderChildren, action?: RenderChildren) => (
      <Box key={k} flexDirection="column" width="33%" flexGrow={1} flexShrink={1} borderStyle="round" borderColor={C.line} backgroundColor={gui(C.card)} paddingX={1}>
        <Text dimColor size="caption">
          {label}
        </Text>
        <Box flexDirection="row" justifyContent="space-between" alignItems="center">
          {value}
          {action}
        </Box>
      </Box>
    )

    const strip = (
      <Box key="strip" flexDirection="row" gap={1}>
        {tile(
          'tile-on',
          'Mods on',
          <Text bold size="title" tabular>
            <Text color={C.green}>{sum.on}</Text>
            <Text dimColor>/{sum.total}</Text>
          </Text>,
        )}
        {tile(
          'tile-blocking',
          'Blocking questions',
          <Text bold size="title" tabular color={sum.blocking > 0 ? C.amber : undefined} dimColor={sum.blocking === 0}>
            {String(sum.blocking)}
          </Text>,
          sum.blocking > 0 ? (
            <Button
              key="answer-now"
              variant="secondary"
              label="Answer"
              onPress={async () => {
                await update($, tabAtom, () => 'questions')
                await update($, openAtom, cur => [...new Set([...(cur ?? defaultOpen(s)), 'recipe-run'])])
              }}
            />
          ) : undefined,
        )}
        {tile(
          'tile-spend',
          'Session spend',
          <Text tabular>
            <Text bold size="title" color={C.orange}>
              {sum.spend !== null ? money(sum.spend) : 'n/a'}
            </Text>
            {sum.spend !== null && (
              <Text color={C.tan} size="footnote">
                {' '}
                = {burritos(sum.spend)} burritos
              </Text>
            )}
          </Text>,
        )}
      </Box>
    )

    // ---- small parts

    const pill = (p: Pill) => (
      <Text bold size="caption" color={PILL[p.tone].fg} backgroundColor={PILL[p.tone].bg}>
        {` ${p.text.toUpperCase()} `}
      </Text>
    )

    const nameButton = (c: Card, isOpen: boolean) => (
      <Button key={`expand-${c.name}`} plain onPress={() => setOpen(c.name)}>
        <Text bold color={gui(C.name)}>
          {c.name}
        </Text>
        <Text dimColor>{isOpen ? ' ⌄' : ' ›'}</Text>
      </Button>
    )

    const onButton = (c: Card) => {
      const isOn = c.pending ?? true
      return (
        <Button key={`switch-${c.name}`} variant="secondary" onPress={() => toggle($, c)}>
          <Text color={isOn ? C.green : C.amber}>{isOn ? 'On' : 'Off'}</Text>
        </Button>
      )
    }

    const hairline = (k: string) => (Svg ? <Svg key={k} source={barSvg(C.divider, 1)} alt="" height={1} /> : null)

    // ---- per-mod bodies

    const recipeBody = () => {
      const qs = openQuestions(s.recipe.open, s.answered)
      const blocking = qs.filter(q => q.kind === 'blocking').length
      const tabs = (
        <Box key="rr-tabs" flexDirection="row" gap={1}>
          <Button key="tab-run" label="Run" variant={tab === 'run' ? 'primary' : 'secondary'} onPress={() => update($, tabAtom, () => 'run')} />
          <Button
            key="tab-questions"
            label={blocking > 0 ? `Questions (${blocking})` : qs.length > 0 ? `Questions · ${qs.length}` : 'Questions'}
            variant={tab === 'questions' ? 'primary' : 'secondary'}
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
              <Box key={`q-${q.key}-${q.id}`} flexDirection="column" borderStyle="round" borderColor={q.kind === 'blocking' ? C.amber : C.line} backgroundColor={gui(C.inset)} paddingX={1}>
                <Text>
                  <Text bold color={q.kind === 'blocking' ? C.amber : undefined}>
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
                      <Button key={`a-${q.key}-${q.id}-${i}`} variant="secondary" label={o} onPress={() => answer($, q, o)} />
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
                  color={st.state === 'blocked' ? C.amber : st.state === 'failed' ? 'error' : undefined}
                >
                  {st.glyph} {st.label}
                </Text>
                <Text dimColor={st.state !== 'blocked'} color={st.state === 'blocked' ? C.amber : undefined}>
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
              <Text>{Array.from({ length: v.max }, (_, i) => (i + 1 <= (v.attempt ?? 0) ? '▬' : '▭')).join(' ')}</Text>
            </Box>
          )}
          {v.verdict && (
            <Box key="rr-verdict" flexDirection="column" backgroundColor={gui(C.inset)} borderStyle={isGui ? undefined : 'round'} borderColor={C.line} paddingX={1}>
              <Box flexDirection="row" justifyContent="space-between">
                <Text bold>Router's last call</Text>
                {pill({ text: v.verdict.badge, tone: v.verdict.isAlarm ? 'warn' : 'live' })}
              </Box>
              <Text dimColor>{v.verdict.reason}</Text>
            </Box>
          )}
          {v.spend && (
            <Box key="rr-spend" flexDirection="column">
              <Box flexDirection="row" justifyContent="space-between">
                <Text bold>Spend</Text>
                <Text>
                  <Text bold color={C.orange}>
                    {money(v.spend.spent)}
                  </Text>
                  <Text dimColor>{v.spend.cap !== null ? ` of ${money(v.spend.cap)} cap` : ''}</Text>
                </Text>
              </Box>
              {v.spend.cap !== null && <Meter els={els} k="rr-cap" n={Math.min(20, wide)} frac={v.spend.spent / v.spend.cap} color={C.orange} />}
            </Box>
          )}
        </Box>
      )
    }

    // SESSION BURN and the limit rows, on the warning card's face.
    const burnBars = () => {
      const rows = limitRows(s.usage?.rateLimits ?? [], s.now)
      return (
        <Box key="burn-bars" flexDirection="column" gap={tight}>
          <Text bold size="footnote" color={C.orange}>
            SESSION BURN
          </Text>
          {rows.length === 0 && <Text dimColor>No limit readings yet (they come with the first reply, on a subscription plan).</Text>}
          {rows.map(r => (
            <Box key={`lim-${r.kind}`} flexDirection="row" gap={1} alignItems="center">
              <Box width={8}>
                <Text dimColor>{r.label}</Text>
              </Box>
              <Box flexGrow={1} flexShrink={1}>
                {Svg ? (
                  <Svg source={burnSvg(r.pct, r.isHot)} alt={`${r.label} ${r.pct}% used`} />
                ) : (
                  <Meter els={els} k={`lim-bar-${r.kind}`} n={Math.max(6, wide - 12)} frac={r.pct / 100} color={r.isHot ? C.orange : C.tan} />
                )}
              </Box>
              <Text tabular>
                <Text bold color={r.isHot ? C.amber : undefined}>
                  {r.pct}%
                </Text>
                <Text dimColor>{r.reset ? ` ${r.reset}` : ''}</Text>
              </Text>
            </Box>
          ))}
        </Box>
      )
    }

    const burnBody = (c: Card, hasBars: boolean) => {
      const spend = s.usage?.costUsd ?? null
      const turns = s.turns.map((cost, i) => ({ n: i + 1, cost })).slice(-6).reverse()
      const maxTurn = Math.max(0.0001, ...s.turns)
      return (
        <Box key="burn-body" flexDirection="column" gap={1}>
          {!hasBars && burnBars()}
          {spend !== null && (
            <Text tabular>
              <Text bold color={C.orange}>
                {money(spend)}
              </Text>
              <Text color={C.tan}>
                {' '}
                = {burritos(spend)} burritos = {mcdoubles(spend)} McDoubles
              </Text>
            </Text>
          )}
          {spend !== null && <Meter els={els} k="burn-meter-bar" n={wide} frac={spend / BURN_SCALE_USD} color={C.orange} />}
          <Box key="burn-turns" flexDirection="column">
            <Text bold>Per turn</Text>
            {turns.length === 0 && <Text dimColor>No finished turns yet this session.</Text>}
            {turns.map(t => (
              <Box key={`turn-${t.n}`} flexDirection="row" gap={1}>
                <Box width={4}>
                  <Text dimColor>#{t.n}</Text>
                </Box>
                <Meter els={els} k={`turn-bar-${t.n}`} n={Math.max(6, wide - 10)} frac={t.cost / maxTurn} color={C.tan} />
                <Text dimColor tabular>
                  {money(t.cost)}
                </Text>
              </Box>
            ))}
          </Box>
          {genericRows(c)}
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
          <Box key="tc-lock" flexDirection="row" justifyContent="space-between">
            <Text bold>Push lock</Text>
            <Text>
              <Text bold>{lock.state}</Text>
              <Text dimColor> · {lock.detail}</Text>
            </Text>
          </Box>
        </Box>
      )
    }

    const devBody = (c: Card) => {
      if (!s.dev) return genericBody(c)
      const rows = serverRows(s.dev, stopped)
      const running = new Set(rows.map(r => r.port))
      const down = s.dev.recipe.filter(r => !running.has(r.port))
      return (
        <Box key="dev-body" flexDirection="column" gap={tight}>
          {rows.length === 0 && <Text dimColor>No dev servers are listening.</Text>}
          {rows.map(r => (
            <Box key={`dev-${r.key}`} flexDirection="column" backgroundColor={gui(C.inset)} paddingX={1}>
              <Box flexDirection="row" justifyContent="space-between" alignItems="center" gap={1}>
                <Text wrap="truncate-end" tabular>
                  <Text weight="semibold" bold={!isGui}>
                    {r.tool}
                  </Text>
                  {r.where !== '' && <Text dimColor> {r.where}</Text>}
                  <Text color={C.blue}> :{r.port}</Text>
                  {r.age !== '' && <Text dimColor> {r.age}</Text>}
                </Text>
                <Box flexDirection="row" gap={1}>
                  <Button key={`dev-open-${r.key}`} variant="secondary" label="Open" onPress={() => $.process.run(['open', `http://localhost:${r.port}`])} />
                  <Button
                    key={`dev-stop-${r.key}`}
                    variant="secondary"
                    onPress={() => (armed === r.pid ? stopServer($, r.pid, r.port) : update($, stopArmedAtom, () => r.pid))}
                  >
                    <Text color={armed === r.pid ? C.amber : undefined}>{armed === r.pid ? 'Confirm stop' : 'Stop'}</Text>
                  </Button>
                </Box>
              </Box>
              <Text dimColor size="footnote" wrap="truncate-middle">
                {r.folder} · $ {r.command}
              </Text>
            </Box>
          ))}
          {down.map(r => (
            <Text key={`dev-down-${r.name}`} dimColor>
              ○ {r.name} :{r.port} · stopped
            </Text>
          ))}
        </Box>
      )
    }

    // Throttle's controls. A press writes this panel's own ask; throttle applies it at once and
    // before the next model request, mid-run included. Keys 1-4 pick a preset while the pane has focus.
    const throttleBody = (c: Card) => {
      const set = s.throttle?.setting ?? null
      const lk = lookOf(c)
      const ask = (change: Omit<ThrottleAsk, 'at'>) => async () => {
        const at = await $.clock.now()
        await update($, throttleAskAtom, () => ({ ...change, at }))
      }
      const label = (t: string) => (
        <Box width={11}>
          <Text dimColor size="footnote">
            {t}
          </Text>
        </Box>
      )
      // the chosen value is filled; the rest stay plain text
      const pick = (isOn: boolean) => (isOn ? { variant: 'primary' as const } : { plain: true as const, dimColor: true })
      const row = (name: string, field: 'model' | 'effort' | 'subagentModel', values: string[]) => (
        <Box key={`th-${field}`} flexDirection="row" gap={1} alignItems="center" flexWrap="wrap">
          {label(name)}
          {values.map(v => (
            <Button key={`th-${field}-${v}`} label={v} {...pick(set?.[field] === v)} onPress={ask({ [field]: v })} />
          ))}
          <Button key={`th-${field}-default`} label="default" {...pick((set?.[field] ?? null) === null)} onPress={ask({ [field]: null })} />
        </Box>
      )
      return (
        <Box key="th-body" flexDirection="column" gap={tight}>
          <Box key="th-presets" flexDirection="row" justifyContent="space-between" alignItems="center">
            <Box flexDirection="row" gap={1} alignItems="center">
              {label('Preset')}
              {(['saver', 'normal', 'deep', 'max'] as const).map((p, i) => (
                <Button
                  key={`th-preset-${p}`}
                  label={p}
                  hotkey={String(i + 1)}
                  variant={(set?.preset ?? 'normal') === p ? 'primary' : 'secondary'}
                  onPress={ask({ preset: p === 'normal' ? null : p })}
                />
              ))}
            </Box>
            <Text dimColor size="footnote">
              keys 1-4
            </Text>
          </Box>
          {row('Model', 'model', ['haiku', 'sonnet', 'opus'])}
          {row('Effort', 'effort', ['low', 'medium', 'high', 'xhigh', 'max'])}
          {row('Subagents', 'subagentModel', ['haiku', 'sonnet', 'opus'])}
          {lk.note && (
            <Text dimColor size="footnote">
              ▸ {lk.note}
            </Text>
          )}
        </Box>
      )
    }

    const genericRows = (c: Card) =>
      c.details.length === 0 ? null : (
        <Box key={`${c.name}-rows`} flexDirection="column">
          {c.details.map(([k, val]) => (
            <Box key={`${c.name}-d-${k}`} flexDirection="row" justifyContent="space-between" gap={2}>
              <Text dimColor>{k}</Text>
              <Text wrap="truncate-end">{val}</Text>
            </Box>
          ))}
        </Box>
      )

    const genericBody = (c: Card) =>
      genericRows(c) ?? (
        <Text key={`${c.name}-body`} dimColor>
          Nothing more to show yet.
        </Text>
      )

    const bodyOf = (c: Card, lk: Look) =>
      c.name === 'recipe-run'
        ? recipeBody()
        : c.name === 'burn-meter'
          ? burnBody(c, lk.section === 'look')
          : c.name === 'traffic-control'
            ? trafficBody()
            : c.name === 'dev-servers'
              ? devBody(c)
              : c.name === 'throttle'
                ? throttleBody(c)
                : genericBody(c)

    // ---- a card: coloured top bar, row 1 (pill, name, chevron | key number, On), row 2 status,
    // then the open detail under a hairline. Row 1 is always first.

    const card = (c: Card, width?: string) => {
      const lk = lookOf(c)
      const isOpen = open.has(c.name)
      const color = SECTION_COLOR[lk.section]
      const hasBars = c.name === 'burn-meter' && lk.section === 'look'
      return (
        <Box
          key={`card-${c.name}`}
          flexDirection="column"
          width={width}
          flexGrow={width ? 1 : undefined}
          flexShrink={width ? 1 : undefined}
          borderStyle="round"
          borderColor={isGui ? C.line : lk.section === 'idle' ? 'subtle' : color}
          backgroundColor={gui(C.card)}
          paddingX={1}
          gap={tight}
        >
          {Svg && (
            <Box position="absolute" top={0} left={1} right={1}>
              <Svg source={barSvg(color, 4, 2)} alt="" height={4} />
            </Box>
          )}
          <Box flexDirection="row" justifyContent="space-between" alignItems="center" gap={1}>
            <Box flexDirection="row" alignItems="center" gap={1} flexShrink={1}>
              {lk.pill && pill(lk.pill)}
              {nameButton(c, isOpen)}
            </Box>
            <Box flexDirection="row" alignItems="center" gap={1}>
              {lk.key && (
                <Text bold tabular color={lk.key.isHot ? C.amber : undefined}>
                  {lk.key.text}
                </Text>
              )}
              {onButton(c)}
            </Box>
          </Box>
          {hasBars ? (
            burnBars()
          ) : (
            <Text color={gui(C.dim)} dimColor={!isGui} wrap="truncate-end" tabular>
              {lk.status}
            </Text>
          )}
          {isOpen && (
            <Box key={`open-${c.name}`} flexDirection="column" gap={tight} marginTop={isGui ? 0 : 1}>
              {hairline(`hr-${c.name}`)}
              {bodyOf(c, lk)}
            </Box>
          )}
        </Box>
      )
    }

    // Off: a Turn on chip and one line. Not installed: the install command under the name.
    const offRow = (c: Card) =>
      c.power === 'missing' ? (
        <Box key={`off-${c.name}`} flexDirection="row" gap={1} alignItems="flex-start">
          <Box borderStyle="round" borderColor={C.missing} paddingX={1}>
            <Text dimColor size="caption">
              Not installed
            </Text>
          </Box>
          <Box flexDirection="column" flexShrink={1}>
            <Text weight="semibold" bold={!isGui}>
              {c.name}
            </Text>
            <Text mono size="footnote" backgroundColor={gui(C.inset)} wrap="truncate-end">
              {` /plugin install ${c.name} --marketplace travist6983/agent-kit `}
            </Text>
          </Box>
        </Box>
      ) : (
        <Box key={`off-${c.name}`} flexDirection="row" gap={1} alignItems="center">
          <Button key={`switch-${c.name}`} variant="secondary" label={c.pending === true ? 'Undo' : 'Turn on'} onPress={() => toggle($, c)} />
          <Text wrap="truncate-end">
            <Text weight="semibold" bold={!isGui}>
              {c.name}
            </Text>
            <Text dimColor> · {c.status}</Text>
          </Text>
        </Box>
      )

    // Idle cards go two up when there is room; an open one takes the full width above them.
    const idleGrid = (items: Card[]) => {
      const shut = items.filter(c => !open.has(c.name))
      const opened = items.filter(c => open.has(c.name))
      const isTwoUp = cols >= 64
      const pairs: Card[][] = []
      for (let i = 0; i < shut.length; i += isTwoUp ? 2 : 1) pairs.push(shut.slice(i, i + (isTwoUp ? 2 : 1)))
      return [
        ...opened.map(c => card(c)),
        ...pairs.map(pair => (
          <Box key={`idle-${pair[0]?.name}`} flexDirection="row" gap={1}>
            {pair.map(c => card(c, isTwoUp ? '50%' : undefined))}
            {isTwoUp && pair.length === 1 && <Box key="idle-pad" width="50%" flexGrow={1} />}
          </Box>
        )),
      ]
    }

    const sections = SECTIONS.map(sec => {
      const items = list.filter(c => lookOf(c).section === sec)
      if (items.length === 0) return null
      return (
        <Box key={`sec-${sec}`} flexDirection="column" gap={isGui ? 'sm' : 0}>
          <Text bold color={SECTION_COLOR[sec]}>
            {SECTION_TITLE[sec]} <Text dimColor>{items.length}</Text>
          </Text>
          {sec === 'off' ? items.map(offRow) : sec === 'idle' ? idleGrid(items) : items.map(c => card(c))}
        </Box>
      )
    })

    return (
      <Box flexDirection="column" gap={1} backgroundColor={gui(C.pane)} paddingX={isGui ? 2 : 0} paddingY={isGui ? 1 : 0}>
        {header}
        {strip}
        {sections}
      </Box>
    )
  })
}
