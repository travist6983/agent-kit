import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, Register, RenderChildren } from 'claude-code'

import type { Lock, Peer } from '../types'
import { C, PILL, SECTION_COLOR, SECTION_TITLE, barSvg } from './chrome'
import type { Pill, Section } from './chrome'

const PANE = 'traffic'
const meAtom = atom({ plugin: 'traffic-control', key: 'me' } as const, '')
const peersAtom = atom({ plugin: 'traffic-control', key: 'peers' } as const, [])
const locksAtom = atom({ plugin: 'traffic-control', key: 'locks' } as const, [])
// which cards the pane has open; null until the person toggles one (held locks start open)
const openAtom = atom({ plugin: 'traffic-control', key: 'open' } as const, null)

const BEAT_MS = 20_000
const LIVE_MS = 90_000
const LOCK_MS = 15 * 60_000
const PUSH = /\bgit\s+push\b/
const DEPLOY = /\b(wrangler (pages )?deploy|fly(ctl)? deploy|vercel( deploy)? --prod|netlify deploy --prod|(npm|pnpm|yarn) (run )?deploy|firebase deploy|gh pr merge)\b/

const self = { id: '', label: '', lastTool: '', warned: new Set<string>() }

// The active Claude config folder, so each account (CLAUDE_CONFIG_DIR) keeps its own data.
async function configDir($: EngineInterface) {
  const home = (await $.env.get('HOME')) ?? '/tmp'
  const dir = (await $.env.get('CLAUDE_CONFIG_DIR')) ?? ''
  if (dir === '') return `${home}/.claude`
  return dir.replace(/^~(?=\/|$)/, home).replace(/\/+$/, '')
}

async function dirs($: EngineInterface) {
  const base = `${await configDir($)}/traffic`
  return { sessions: `${base}/sessions`, locks: `${base}/locks` }
}

async function readJson<T>($: EngineInterface, path: string): Promise<T | null> {
  try {
    return JSON.parse(await $.fs.read(path)) as T
  } catch {
    return null
  }
}

async function where($: EngineInterface) {
  const cwd = await $.session.cwd()
  const repo = await $.session.repo()
  const top = await $.process.run(['git', 'rev-parse', '--show-toplevel'], { cwd })
  const br = await $.process.run(['git', 'branch', '--show-current'], { cwd })
  const worktree = top.exitCode === 0 ? top.stdout.trim() : cwd
  return { cwd, root: repo?.root ?? worktree, worktree, branch: br.stdout.trim() || '-' }
}

async function beat($: EngineInterface) {
  const d = await dirs($)
  const here = await where($)
  const now = await $.clock.now()
  const mine: Peer = { id: self.id, label: self.label, ...here, lastTool: self.lastTool, lastSeen: now }
  await $.fs.write(`${d.sessions}/${self.id}.json`, JSON.stringify(mine))

  const peers: Peer[] = []
  for (const entry of await $.fs.list(d.sessions)) {
    if (!entry.name.endsWith('.json')) continue
    const p = await readJson<Peer>($, `${d.sessions}/${entry.name}`)
    if (p === null) continue
    if (now - p.lastSeen > LIVE_MS) {
      if (now - p.lastSeen > 24 * 3600_000) await $.process.run(['rm', '-f', `${d.sessions}/${entry.name}`])
      continue
    }
    peers.push(p)
  }
  const locks: Lock[] = []
  for (const entry of await $.fs.list(d.locks)) {
    const l = await readJson<Lock>($, `${d.locks}/${entry.name}`)
    if (l !== null && now - l.at < LOCK_MS && peers.some(p => p.id === l.holder)) locks.push(l)
  }
  peers.sort((a, b) => a.root.localeCompare(b.root) || a.label.localeCompare(b.label))
  await update($, peersAtom, () => peers)
  await update($, locksAtom, () => locks)

  const sameRepo = peers.filter(p => p.id !== self.id && p.root === here.root)
  $.ui.status(sameRepo.length > 0 ? `traffic: ${sameRepo.map(p => p.label).join(', ')} also in ${base(here.root)}` : undefined)
  for (const p of sameRepo) {
    if (p.worktree === here.worktree && !self.warned.has(p.id)) {
      self.warned.add(p.id)
      $.ui.toast(`${p.label} is working in this same checkout (${p.branch}). Use a worktree to avoid stepping on each other.`, { timeoutMs: 12_000 })
    }
  }
}

function base(path: string) {
  return path.split('/').pop() ?? path
}

function lockKey(root: string, kind: string) {
  return `${root.replace(/[^\w.-]/g, '_')}__${kind}.json`
}

function clockTime(at: number) {
  try {
    return new Date(at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
  } catch {
    return ''
  }
}

function ago(ms: number) {
  const s = Math.round(ms / 1000)
  return s < 90 ? `${s}s` : `${Math.round(s / 60)}m`
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    self.id = await $.session.id()
    const d = await dirs($)
    await $.process.run(['mkdir', '-p', d.sessions, d.locks])
    const here = await where($)
    self.label = `${base(here.worktree)}-${self.id.slice(0, 4)}`
    await update($, meAtom, () => self.id)
    await $.command.register({ name: 'traffic', description: 'Show live Claude sessions per repo and who holds push/deploy' })
    await beat($)
    $.clock.every(BEAT_MS, () => void beat($))
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    const d = await dirs($)
    await $.process.run(['rm', '-f', `${d.sessions}/${self.id}.json`])
    return next(e)
  })

  on('command.run', { command: 'traffic' }, async $ => {
    await beat($)
    await $.ui.open({ id: PANE, title: 'Sessions' })
    return { text: 'Sessions pane opened.' }
  })

  on('tool.call', async ($, e, next) => {
    if (e.agentId === undefined) self.lastTool = String(e.tool)
    if (e.tool !== 'Bash') return next(e)
    const kind = DEPLOY.test(e.command) ? 'deploy' : PUSH.test(e.command) ? 'push' : null
    if (kind === null) return next(e)

    const d = await dirs($)
    const here = await where($)
    const path = `${d.locks}/${lockKey(here.root, kind)}`
    const now = await $.clock.now()
    const held = await readJson<Lock>($, path)
    if (held !== null && held.holder !== self.id && now - held.at < LOCK_MS) {
      const holder = await readJson<Peer>($, `${d.sessions}/${held.holder}.json`)
      if (holder !== null && now - holder.lastSeen < LIVE_MS) {
        $.ui.toast(`Blocked ${kind}: ${held.label} holds it for ${base(here.root)}`, { timeoutMs: 10_000 })
        return {
          deny: `traffic-control: another Claude session (${held.label}, cwd ${holder.cwd}, branch ${holder.branch}) started a ${kind} of ${base(here.root)} ${ago(now - held.at)} ago: \`${held.command.slice(0, 120)}\`. Do not ${kind} now. Wait for it, or coordinate with that session (ListAgents / SendMessage), then retry. Tell the user if they need to decide.`,
        }
      }
    }
    const lock: Lock = { key: path, kind, root: here.root, holder: self.id, label: self.label, command: e.command, at: now }
    await $.fs.write(path, JSON.stringify(lock))
    void beat($)
    try {
      return await next(e)
    } finally {
      const still = await readJson<Lock>($, path)
      if (still?.holder === self.id) await $.process.run(['rm', '-f', path])
      void beat($)
    }
  }).catch(($, e, next) => next(e))

  // ---- the pane: header, stat tiles, then held locks (Needs a look), busy sessions (Live) and
  // idle sessions (Idle) as cards. Desktop, VS Code and mobile get the palette and SVG bars; the
  // terminal keeps the same layout in plain text.

  type Card = {
    id: string
    section: Section
    pill: Pill | null
    name: string
    key: { text: string; isHot: boolean } | null
    status: string
    details: [string, string][]
    command?: string
  }

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e)
    const { Box, Button, Text } = els
    const isGui = e.surface !== 'terminal'
    const Svg = isGui ? ((els as Partial<Elements['desktop']>).Svg ?? undefined) : undefined
    const gui = <T,>(v: T) => (isGui ? v : undefined)
    const tight = isGui ? ('xs' as const) : 0
    const me = await read($, meAtom)
    const peers = await read($, peersAtom)
    const locks = await read($, locksAtom)
    const now = await $.clock.now()
    const mine = peers.find(p => p.id === me)
    const here = mine?.root ?? ''

    const lockCards: Card[] = locks.map(l => ({
      id: l.key,
      section: 'look',
      pill: { text: l.kind, tone: 'warn' },
      name: base(l.root),
      key: { text: ago(now - l.at), isHot: true },
      status: `🔒 ${l.kind} held by ${l.label}${l.holder === me ? ' (you)' : ''}`,
      details: [
        ['Repo', l.root],
        ['Holder', l.label],
        ['Started', `${ago(now - l.at)} ago`],
        ['Expires', `in ${ago(Math.max(0, LOCK_MS - (now - l.at)))} unless it finishes first`],
      ],
      command: l.command,
    }))
    const peerCard = (p: Peer): Card => ({
      id: p.id,
      section: p.lastTool !== '' ? 'live' : 'idle',
      pill:
        p.id === me
          ? { text: 'you', tone: 'live' }
          : here !== '' && p.worktree === mine?.worktree
            ? { text: 'same checkout', tone: 'attn' }
            : null,
      name: p.label,
      key: { text: ago(now - p.lastSeen), isHot: false },
      status: `${base(p.root)} · ${p.branch}${p.worktree !== p.root ? ` · worktree ${base(p.worktree)}` : ''} · ${p.lastTool || 'idle'}`,
      details: [
        ['Repo', p.root],
        ['Worktree', p.worktree],
        ['Branch', p.branch],
        ['Folder', p.cwd],
        ['Last tool', p.lastTool || 'none yet'],
        ['Seen', `${ago(now - p.lastSeen)} ago`],
      ],
    })
    const all = [...lockCards, ...peers.map(peerCard)]
    const open = new Set((await read($, openAtom)) ?? lockCards.map(c => c.id))
    const setOpen = (id: string) =>
      update($, openAtom, cur => {
        const set = new Set(cur ?? lockCards.map(c => c.id))
        if (set.has(id)) set.delete(id)
        else set.add(id)
        return [...set]
      })

    const others = peers.filter(p => p.id !== me)
    const busy = others.filter(p => p.lastTool !== '').length
    const sameRepo = others.filter(p => p.root === here).length

    const header = (
      <Box key="head" flexDirection="row" justifyContent="space-between" alignItems="center">
        <Text>
          <Text bold size="heading">
            Sessions
          </Text>
          <Text dimColor> traffic-control</Text>
        </Text>
        <Box flexDirection="row" gap={1} alignItems="center">
          <Text dimColor size="footnote">
            ↻ {clockTime(mine?.lastSeen ?? now)}
          </Text>
          <Button key="refresh" variant="secondary" hotkey="r" label="Refresh" onPress={() => beat($)} />
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
          'tile-others',
          'Other sessions',
          <Text bold size="title" tabular>
            <Text color={others.length > 0 ? C.green : undefined} dimColor={others.length === 0}>
              {String(others.length)}
            </Text>
            {sameRepo > 0 && (
              <Text dimColor size="footnote">
                {' '}
                {sameRepo} here
              </Text>
            )}
          </Text>,
        )}
        {tile(
          'tile-busy',
          'Busy',
          <Text bold size="title" tabular color={busy > 0 ? C.blue : undefined} dimColor={busy === 0}>
            {String(busy)}
          </Text>,
        )}
        {tile(
          'tile-lock',
          'Push lock',
          <Text bold size="title" color={locks.length > 0 ? C.amber : C.green}>
            {locks.length > 0 ? `Held${locks.length > 1 ? ` ·${locks.length}` : ''}` : 'Free'}
          </Text>,
        )}
      </Box>
    )

    const pill = (p: Pill) => (
      <Text bold size="caption" color={PILL[p.tone].fg} backgroundColor={gui(PILL[p.tone].bg)}>
        {` ${p.text.toUpperCase()} `}
      </Text>
    )

    const hairline = (k: string) => (Svg ? <Svg key={k} source={barSvg(C.divider, 1)} alt="" height={1} /> : null)

    const body = (c: Card) => (
      <Box key={`body-${c.id}`} flexDirection="column" gap={tight}>
        {c.command !== undefined && (
          <Box key={`cmd-${c.id}`} backgroundColor={gui(C.inset)} paddingX={1}>
            <Text mono size="footnote" wrap="truncate-end">
              $ {c.command}
            </Text>
          </Box>
        )}
        {c.details.map(([k, val]) => (
          <Box key={`d-${c.id}-${k}`} flexDirection="row" justifyContent="space-between" gap={2}>
            <Text dimColor>{k}</Text>
            <Text wrap="truncate-middle">{val}</Text>
          </Box>
        ))}
      </Box>
    )

    const card = (c: Card) => {
      const isOpen = open.has(c.id)
      const color = SECTION_COLOR[c.section]
      return (
        <Box
          key={`card-${c.id}`}
          flexDirection="column"
          borderStyle="round"
          borderColor={isGui ? C.line : c.section === 'idle' ? 'subtle' : color}
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
              {c.pill && pill(c.pill)}
              <Button key={`expand-${c.id}`} plain onPress={() => setOpen(c.id)}>
                <Text bold color={gui(C.name)}>
                  {c.name}
                </Text>
                <Text dimColor>{isOpen ? ' ⌄' : ' ›'}</Text>
              </Button>
            </Box>
            {c.key && (
              <Text bold tabular color={c.key.isHot ? C.amber : undefined} dimColor={!c.key.isHot}>
                {c.key.text}
              </Text>
            )}
          </Box>
          <Text color={gui(C.dim)} dimColor={!isGui} wrap="truncate-end" tabular>
            {c.status}
          </Text>
          {isOpen && (
            <Box key={`open-${c.id}`} flexDirection="column" gap={tight} marginTop={isGui ? 0 : 1}>
              {hairline(`hr-${c.id}`)}
              {body(c)}
            </Box>
          )}
        </Box>
      )
    }

    const sections = (['look', 'live', 'idle'] as const).map(sec => {
      const items = all.filter(c => c.section === sec)
      if (items.length === 0) return null
      return (
        <Box key={`sec-${sec}`} flexDirection="column" gap={isGui ? 'sm' : 0}>
          <Text bold color={SECTION_COLOR[sec]}>
            {SECTION_TITLE[sec]} <Text dimColor>{items.length}</Text>
          </Text>
          {items.map(card)}
        </Box>
      )
    })

    return (
      <Box flexDirection="column" gap={1} backgroundColor={gui(C.pane)} paddingX={isGui ? 2 : 0} paddingY={isGui ? 1 : 0}>
        {header}
        {strip}
        {peers.length === 0 && <Text dimColor>No live sessions.</Text>}
        {sections}
      </Box>
    )
  })
}
