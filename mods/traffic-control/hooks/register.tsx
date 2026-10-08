import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Lock, Peer } from '../types'

const PANE = 'traffic'
const meAtom = atom({ plugin: 'traffic-control', key: 'me' } as const, '')
const peersAtom = atom({ plugin: 'traffic-control', key: 'peers' } as const, [])
const locksAtom = atom({ plugin: 'traffic-control', key: 'locks' } as const, [])

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

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const me = await read($, meAtom)
    const peers = await read($, peersAtom)
    const locks = await read($, locksAtom)
    const now = await $.clock.now()
    const roots = [...new Set(peers.map(p => p.root))]

    return (
      <Box flexDirection="column">
        {peers.length === 0 && <Text dimColor>No live sessions.</Text>}
        {roots.map(root => (
          <Box key={root} flexDirection="column" marginBottom={1}>
            <Text bold>{base(root)}</Text>
            {locks
              .filter(l => l.root === root)
              .map(l => (
                <Text key={l.key} color="yellow">
                  🔒 {l.kind} held by {l.label} · {ago(now - l.at)}
                </Text>
              ))}
            {peers
              .filter(p => p.root === root)
              .map(p => (
                <Text key={p.id} dimColor={p.id !== me}>
                  {p.id === me ? '▸ ' : '  '}
                  {p.label} · {p.branch}
                  {p.worktree !== root ? ` · worktree ${base(p.worktree)}` : ''} · {p.lastTool || 'idle'} · {ago(now - p.lastSeen)}
                </Text>
              ))}
          </Box>
        ))}
      </Box>
    )
  })
}
