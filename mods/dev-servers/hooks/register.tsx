import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { RecipeEntry, Server } from '../types'

const PANE = 'dev-servers'
const servers = atom({ plugin: 'dev-servers', key: 'servers' } as const, [])
const rootAtom = atom({ plugin: 'dev-servers', key: 'root' } as const, null)
const recipeAtom = atom({ plugin: 'dev-servers', key: 'recipe' } as const, [])

const DEV_PROCESS = /^(node|python[\d.]*|Python|uvicorn|gunicorn|hypercorn|deno|bun|ruby|rails|workerd|esbuild|php|java|go|vite|next-server|wrangler|flask|fastapi)$/i
const recipeKey = (root: string) => `recipe:${root}`

// The active Claude config folder, so each account (CLAUDE_CONFIG_DIR) keeps its own data.
async function configDir($: EngineInterface) {
  const home = (await $.env.get('HOME')) ?? '/tmp'
  const dir = (await $.env.get('CLAUDE_CONFIG_DIR')) ?? ''
  if (dir === '') return `${home}/.claude`
  return dir.replace(/^~(?=\/|$)/, home).replace(/\/+$/, '')
}

async function projectRoot($: EngineInterface) {
  const cwd = await $.session.cwd()
  const top = await $.process.run(['git', 'rev-parse', '--show-toplevel'], { cwd })
  return top.exitCode === 0 ? top.stdout.trim() : cwd
}

async function scan($: EngineInterface): Promise<Server[]> {
  const ls = await $.process.run(['lsof', '-nP', '-iTCP', '-sTCP:LISTEN', '-Fpcn'])
  const byPid = new Map<number, { process: string; ports: Set<number> }>()
  let pid = 0
  for (const line of ls.stdout.split('\n')) {
    const tag = line[0]
    const value = line.slice(1)
    if (tag === 'p') {
      pid = Number(value)
      if (!byPid.has(pid)) byPid.set(pid, { process: '', ports: new Set() })
    } else if (tag === 'c') {
      const row = byPid.get(pid)
      if (row) row.process = value
    } else if (tag === 'n') {
      const port = Number(value.split(':').pop())
      if (port) byPid.get(pid)?.ports.add(port)
    }
  }
  const pids = [...byPid].filter(([, row]) => DEV_PROCESS.test(row.process)).map(([p]) => p)
  if (pids.length === 0) return []

  const cwds = new Map<number, string>()
  const cw = await $.process.run(['lsof', '-a', '-p', pids.join(','), '-d', 'cwd', '-Fpn'])
  let cur = 0
  for (const line of cw.stdout.split('\n')) {
    if (line[0] === 'p') cur = Number(line.slice(1))
    else if (line[0] === 'n') cwds.set(cur, line.slice(1))
  }
  const info = new Map<number, { age: string; command: string }>()
  const ps = await $.process.run(['ps', '-o', 'pid=,etime=,command=', '-p', pids.join(',')])
  for (const line of ps.stdout.split('\n')) {
    const m = line.trim().match(/^(\d+)\s+(\S+)\s+(.*)$/)
    if (m) info.set(Number(m[1]), { age: m[2] ?? '', command: m[3] ?? '' })
  }

  const out: Server[] = []
  for (const p of pids) {
    const row = byPid.get(p)
    if (!row) continue
    for (const port of [...row.ports].sort((a, b) => a - b)) {
      out.push({
        pid: p,
        port,
        process: row.process,
        command: info.get(p)?.command ?? row.process,
        cwd: cwds.get(p) ?? '?',
        age: info.get(p)?.age ?? '',
      })
    }
  }
  return out.sort((a, b) => a.port - b.port)
}

async function refresh($: EngineInterface) {
  const root = await projectRoot($)
  const list = await scan($)
  const recipe = ((await $.store.get(recipeKey(root))) ?? []) as RecipeEntry[]
  await update($, servers, () => list)
  await update($, rootAtom, () => root)
  await update($, recipeAtom, () => recipe)
  const mine = list.filter(s => s.cwd.startsWith(root))
  $.ui.status(mine.length > 0 ? `dev: ${mine.map(s => `:${s.port}`).join(' ')}` : undefined)
  return { root, list, recipe }
}

function shortPath(path: string) {
  return path.replace(/^\/(Users|home)\/[^/]+/, '~')
}

async function up($: EngineInterface) {
  const { root, list, recipe } = await refresh($)
  if (recipe.length === 0) {
    await $.prompt.submit({
      text: [
        `Start every dev server this project (${root}) needs so I can use it locally (frontends, API, workers).`,
        'Run each in the background, wait until each answers on its port, and fix anything stale (kill old processes on the same port first).',
        'Then call the dev-servers tool `save_recipe` with each server: name, exact command, cwd and port, so /up can do this without you next time.',
        'Finish with only the list of local URLs and any login I need.',
      ].join('\n'),
    })
    return 'No recipe saved for this project yet: asked Claude to start the servers and save one.'
  }
  const listening = new Set(list.map(s => s.port))
  const logs = `${await configDir($)}/dev-servers/logs`
  await $.process.run(['mkdir', '-p', logs])
  const started: string[] = []
  for (const entry of recipe) {
    if (listening.has(entry.port)) continue
    const log = `${logs}/${entry.name.replace(/[^\w-]/g, '_')}.log`
    const cwd = entry.cwd.startsWith('/') ? entry.cwd : `${root}/${entry.cwd}`
    await $.process.run(['sh', '-c', `cd "$1" && nohup sh -c "$2" > "$3" 2>&1 &`, 'up', cwd, entry.command, log])
    started.push(entry.name)
  }
  await $.clock.sleep(started.length > 0 ? 6000 : 0)
  const after = await refresh($)
  const up = new Set(after.list.map(s => s.port))
  const lines = recipe.map(
    entry => `${up.has(entry.port) ? 'up  ' : 'DOWN'} ${entry.name}  http://localhost:${entry.port}`,
  )
  const down = recipe.filter(entry => !up.has(entry.port))
  if (down.length > 0) lines.push(`Logs: ${logs}/  (still starting? run /servers in a few seconds)`)
  return lines.join('\n')
}

async function stopPorts($: EngineInterface, ports: number[]) {
  const list = await scan($)
  const pids = [...new Set(list.filter(s => ports.includes(s.port)).map(s => s.pid))]
  if (pids.length === 0) return 0
  await $.process.run(['kill', ...pids.map(String)])
  await $.clock.sleep(1500)
  const still = (await scan($)).filter(s => pids.includes(s.pid)).map(s => String(s.pid))
  if (still.length > 0) await $.process.run(['kill', '-9', ...still])
  return pids.length
}

async function bounce($: EngineInterface) {
  const { root, list, recipe } = await refresh($)
  const ports = recipe.length > 0 ? recipe.map(r => r.port) : list.filter(s => s.cwd.startsWith(root)).map(s => s.port)
  const stopped = await stopPorts($, ports)
  return `Stopped ${stopped} process(es).\n${await up($)}`
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'servers', description: 'Show running dev servers and their URLs' })
    await $.command.register({ name: 'up', description: "Start this project's dev servers from its saved recipe" })
    await $.command.register({ name: 'bounce', description: "Restart this project's dev servers fresh" })
    await $.tool.register({
      name: 'save_recipe',
      description:
        "Save how to start this project's dev servers so the user's /up and /bounce commands can run them. Call after you have started them and confirmed each answers on its port.",
      inputSchema: {
        type: 'object',
        properties: {
          servers: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                name: { type: 'string' },
                command: { type: 'string', description: 'Foreground command, e.g. "pnpm dev --port 5173"' },
                cwd: { type: 'string', description: 'Relative to the repo root, or absolute' },
                port: { type: 'number' },
              },
              required: ['name', 'command', 'cwd', 'port'],
            },
          },
        },
        required: ['servers'],
      },
      isDeferred: false,
    })
    await refresh($)
    $.clock.every(15_000, () => void refresh($))
    return next(e)
  })

  on('command.run', { command: 'servers' }, async $ => {
    await refresh($)
    await $.ui.open({ id: PANE, title: 'Dev servers' })
    return { text: 'Dev servers pane opened.' }
  })
  on('command.run', { command: 'up' }, async $ => ({ text: await up($) }))
  on('command.run', { command: 'bounce' }, async $ => ({ text: await bounce($) }))

  on('tool.call', { tool: 'mcp__dev-servers__save_recipe' }, async ($, e) => {
    const root = await projectRoot($)
    const raw = Array.isArray(e.servers) ? (e.servers as RecipeEntry[]) : []
    const recipe = raw.map(r => ({
      name: String(r.name),
      command: String(r.command),
      cwd: String(r.cwd).startsWith(root) ? String(r.cwd).slice(root.length + 1) || '.' : String(r.cwd),
      port: Number(r.port),
    }))
    await $.store.set(recipeKey(root), recipe)
    await refresh($)
    return { result: `Saved ${recipe.length} server(s) for ${root}. The user can now run /up and /bounce.` }
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined && e.durationMs > 5 * 60_000) {
      const { root, list, recipe } = await refresh($)
      const running = new Set(list.map(s => s.port))
      if (recipe.length > 0 && !recipe.some(r => running.has(r.port))) {
        $.ui.toast(`Nothing is running for ${shortPath(root)}. /up starts it.`, { timeoutMs: 10_000 })
      }
    }
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e)
    const list = await read($, servers)
    const root = (await read($, rootAtom)) ?? ''
    const recipe = await read($, recipeAtom)
    const running = new Set(list.map(s => s.port))
    const mine = list.filter(s => s.cwd.startsWith(root))
    const others = list.filter(s => !s.cwd.startsWith(root))
    const missing = recipe.filter(r => !running.has(r.port))

    const row = (s: Server) => (
      <Box key={`s${s.pid}-${s.port}`}>
        <Text color="green">● </Text>
        <Text>http://localhost:{s.port} </Text>
        <Text dimColor>
          {s.process} · {shortPath(s.cwd)} · {s.age}{' '}
        </Text>
        <Button key={`open${s.port}`} label="Open" plain onPress={() => void $.process.run(['open', `http://localhost:${s.port}`])} />
        <Text> </Text>
        <Button key={`stop${s.port}`} label="Stop" plain onPress={() => void stopPorts($, [s.port]).then(() => refresh($))} />
      </Box>
    )

    return (
      <Box flexDirection="column">
        <Box>
          <Text bold>{shortPath(root) || 'this project'} </Text>
          <Button key="up" label="Up" variant="primary" onPress={() => void up($).then(text => $.ui.toast(text, { timeoutMs: 8000 }))} />
          <Button key="bounce" label="Bounce" onPress={() => void bounce($).then(text => $.ui.toast(text, { timeoutMs: 8000 }))} />
        </Box>
        {mine.length === 0 && <Text dimColor>Nothing running here.</Text>}
        {mine.map(row)}
        {missing.map(r => (
          <Text key={`miss${r.port}`} color="red">
            ○ {r.name} :{r.port} is down
          </Text>
        ))}
        {recipe.length === 0 && <Text dimColor>No recipe saved. /up asks Claude to start everything and save one.</Text>}
        {others.length > 0 && <Text bold>Other projects</Text>}
        {others.map(row)}
      </Box>
    )
  })
}
