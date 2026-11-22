import { atom, read, update } from 'claude-code'
import type { Elements, EngineInterface, Register, RenderChildren } from 'claude-code'

import type { RecipeEntry, Server } from '../types'
import { C, PILL, SECTION_COLOR, barSvg } from './chrome'
import type { Pill, Section } from './chrome'

const PANE = 'dev-servers'
const servers = atom({ plugin: 'dev-servers', key: 'servers' } as const, [])
const rootAtom = atom({ plugin: 'dev-servers', key: 'root' } as const, null)
const recipeAtom = atom({ plugin: 'dev-servers', key: 'recipe' } as const, [])
const scannedAtom = atom({ plugin: 'dev-servers', key: 'scannedAt' } as const, null)
const stopArmedAtom = atom({ plugin: 'dev-servers', key: 'stopArmed' } as const, null)

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

// The tool a command runs, from its path: .../node_modules/.bin/vite -> vite,
// .../node_modules/@scope/pkg/... -> @scope/pkg. Null when the path says nothing.
function toolOf(command: string) {
  const bin = command.match(/node_modules\/\.bin\/([\w.@-]+)/)
  if (bin) return bin[1] ?? null
  const pkg = command.match(/node_modules\/((?:@[\w.-]+\/)?[\w.-]+)\//)
  return pkg ? (pkg[1] ?? null) : null
}

// The project a folder belongs to: the nearest package.json name (under its git repo's name when
// it lacks it), else the git repo's folder name.
// Cached per folder for the session, since a scan runs every 15 seconds.
const projects = new Map<string, string | null>()
async function projectOf($: EngineInterface, cwd: string) {
  if (cwd === '?' || cwd === '/') return null
  if (projects.has(cwd)) return projects.get(cwd) ?? null
  let pkg: string | null = null
  const near = await $.process.run([
    'sh',
    '-c',
    'd="$1"; while [ "$d" != / ]; do [ -f "$d/package.json" ] && { cat "$d/package.json"; exit 0; }; d=$(dirname "$d"); done; exit 1',
    'project',
    cwd,
  ])
  if (near.exitCode === 0) {
    try {
      const n = (JSON.parse(near.stdout) as { name?: unknown }).name
      if (typeof n === 'string' && n !== '') pkg = n
    } catch {}
  }
  const top = await $.process.run(['git', '-C', cwd, 'rev-parse', '--show-toplevel'])
  const repo = top.exitCode === 0 ? top.stdout.trim().split('/').pop() || null : null
  // A package named "web" or "api" says little alone: show it under its repo, as pm-tool/web.
  const name = pkg && repo && !pkg.includes(repo) ? `${repo}/${pkg}` : (pkg ?? repo)
  projects.set(cwd, name)
  return name
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

  const named = new Map<string, string | null>()
  for (const dir of new Set(cwds.values())) named.set(dir, await projectOf($, dir))

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
        tool: toolOf(info.get(p)?.command ?? ''),
        project: named.get(cwds.get(p) ?? '?') ?? null,
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
  const at = await $.clock.now()
  await update($, scannedAtom, () => at)
  const mine = list.filter(s => s.cwd.startsWith(root))
  $.ui.status(mine.length > 0 ? `dev: ${mine.map(s => `:${s.port}`).join(' ')}` : undefined)
  return { root, list, recipe }
}

function clockTime(at: number) {
  try {
    return new Date(at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
  } catch {
    return ''
  }
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

  // ---- the pane

  // Header, three stat tiles, then sections: this project (live blue while anything answers,
  // the off colour when its recipe is down), other projects (live blue, a card each) and the
  // recipe entries that are not running (off). The terminal keeps the layout in plain text:
  // no SVG and no background colours.
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const els = $.ui.resolve(e)
    const { Box, Button, Text } = els
    const isGui = e.surface !== 'terminal'
    const Svg = isGui ? ((els as Partial<Elements['desktop']>).Svg ?? undefined) : undefined
    const gui = <T,>(v: T) => (isGui ? v : undefined)
    const tight = isGui ? ('xs' as const) : 0

    const list = await read($, servers)
    const root = (await read($, rootAtom)) ?? ''
    const recipe = await read($, recipeAtom)
    const scannedAt = await read($, scannedAtom)
    const armed = await read($, stopArmedAtom)
    const running = new Set(list.map(s => s.port))
    const mine = list.filter(s => s.cwd.startsWith(root))
    const others = list.filter(s => !s.cwd.startsWith(root))
    const missing = recipe.filter(r => !running.has(r.port))
    const recipeUp = recipe.length - missing.length

    // other projects, a card each, in port order of their first server
    const groups = new Map<string, Server[]>()
    for (const s of others) {
      const name = s.project ?? shortPath(s.cwd)
      groups.set(name, [...(groups.get(name) ?? []), s])
    }

    const header = (
      <Box key="head" flexDirection="row" justifyContent="space-between" alignItems="center">
        <Text>
          <Text bold size="heading">
            Dev servers
          </Text>
          <Text dimColor> {shortPath(root) || 'this project'}</Text>
        </Text>
        <Box flexDirection="row" gap={1} alignItems="center">
          {scannedAt !== null && (
            <Text dimColor size="footnote">
              ↻ {clockTime(scannedAt)}
            </Text>
          )}
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
          'tile-here',
          'Running here',
          <Text bold size="title" tabular>
            <Text color={mine.length > 0 ? C.blue : undefined} dimColor={mine.length === 0}>
              {String(mine.length)}
            </Text>
            {recipe.length > 0 && <Text dimColor>/{recipe.length}</Text>}
          </Text>,
        )}
        {tile(
          'tile-stopped',
          'Stopped',
          <Text bold size="title" tabular color={missing.length > 0 ? SECTION_COLOR.off : undefined} dimColor={missing.length === 0}>
            {String(missing.length)}
          </Text>,
        )}
        {tile(
          'tile-others',
          'Other projects',
          <Text bold size="title" tabular dimColor={others.length === 0}>
            {String(others.length)}
          </Text>,
        )}
      </Box>
    )

    // ---- small parts

    const pill = (p: Pill) => (
      <Text bold size="caption" color={PILL[p.tone].fg} backgroundColor={gui(PILL[p.tone].bg)}>
        {` ${p.text.toUpperCase()} `}
      </Text>
    )

    const hairline = (k: string) => (Svg ? <Svg key={k} source={barSvg(C.divider, 1)} alt="" height={1} /> : null)

    const sectionHead = (sec: Section, title: string, count: number) => (
      <Text bold color={SECTION_COLOR[sec]}>
        {title} <Text dimColor>{count}</Text>
      </Text>
    )

    // Stop asks once: the first press arms it ("Confirm stop" in amber), the second stops.
    const stop = async (port: number) => {
      if (armed !== port) return update($, stopArmedAtom, () => port)
      await update($, stopArmedAtom, () => null)
      await stopPorts($, [port])
      await refresh($)
    }

    const row = (s: Server) => (
      <Box key={`s${s.pid}-${s.port}`} flexDirection="column" backgroundColor={gui(C.inset)} paddingX={1}>
        <Box flexDirection="row" justifyContent="space-between" alignItems="center" gap={1}>
          <Text wrap="truncate-end" tabular>
            <Text weight="semibold" bold={!isGui}>
              {s.tool ?? s.process}
            </Text>
            <Text dimColor>{s.project ? ` in ${s.project}` : ''}</Text>
            <Text color={C.blue}> :{s.port}</Text>
            {s.age !== '' && <Text dimColor> {s.age}</Text>}
          </Text>
          <Box flexDirection="row" gap={1}>
            <Button key={`open${s.port}`} variant="secondary" label="Open" onPress={() => void $.process.run(['open', `http://localhost:${s.port}`])} />
            <Button key={`stop${s.port}`} variant="secondary" onPress={() => void stop(s.port)}>
              <Text color={armed === s.port ? C.amber : undefined}>{armed === s.port ? 'Confirm stop' : 'Stop'}</Text>
            </Button>
          </Box>
        </Box>
        <Text dimColor size="footnote" wrap="truncate-middle">
          http://localhost:{s.port} · {shortPath(s.cwd)}
        </Text>
      </Box>
    )

    // A card: coloured top bar, row 1 (pill, name | key number, actions), row 2 dim status,
    // then the rows under a hairline.
    const card = (k: string, sec: Section, p: Pill | null, name: string, keyText: string | null, actions: RenderChildren, status: string, body: RenderChildren) => {
      const color = SECTION_COLOR[sec]
      return (
        <Box
          key={`card-${k}`}
          flexDirection="column"
          borderStyle="round"
          borderColor={isGui ? C.line : sec === 'idle' ? 'subtle' : color}
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
              {p && pill(p)}
              <Text bold color={gui(C.name)} wrap="truncate-end">
                {name}
              </Text>
            </Box>
            <Box flexDirection="row" alignItems="center" gap={1}>
              {keyText !== null && (
                <Text bold tabular>
                  {keyText}
                </Text>
              )}
              {actions}
            </Box>
          </Box>
          <Text color={gui(C.dim)} dimColor={!isGui} wrap="truncate-end" tabular>
            {status}
          </Text>
          {body !== null && (
            <Box key={`open-${k}`} flexDirection="column" gap={tight} marginTop={isGui ? 0 : 1}>
              {hairline(`hr-${k}`)}
              {body}
            </Box>
          )}
        </Box>
      )
    }

    // ---- this project

    const hereSection: Section = mine.length > 0 ? 'live' : missing.length > 0 ? 'off' : 'idle'
    const herePill: Pill | null =
      missing.length > 0 && mine.length === 0 ? { text: 'down', tone: 'warn' } : missing.length > 0 ? { text: `${missing.length} down`, tone: 'attn' } : mine.length > 0 ? { text: 'live', tone: 'live' } : null
    const hereStatus =
      mine.length === 0
        ? 'Nothing running here.'
        : `${mine.length} listening${missing.length > 0 ? ` · ${missing.length} stopped` : ''}`
    const here = card(
      'here',
      hereSection,
      herePill,
      shortPath(root) || 'this project',
      recipe.length > 0 ? `${recipeUp}/${recipe.length}` : null,
      <Box flexDirection="row" gap={1}>
        <Button key="up" label="Up" variant="primary" onPress={() => void up($).then(text => $.ui.toast(text, { timeoutMs: 8000 }))} />
        <Button key="bounce" label="Bounce" variant="secondary" onPress={() => void bounce($).then(text => $.ui.toast(text, { timeoutMs: 8000 }))} />
      </Box>,
      hereStatus,
      mine.length > 0 || recipe.length === 0 ? (
        <Box key="here-rows" flexDirection="column" gap={tight}>
          {mine.map(row)}
          {recipe.length === 0 && <Text dimColor>No recipe saved. /up asks Claude to start everything and save one.</Text>}
        </Box>
      ) : null,
    )

    const sections = [
      <Box key="sec-here" flexDirection="column" gap={isGui ? 'sm' : 0}>
        {sectionHead(hereSection, 'This project', mine.length)}
        {here}
      </Box>,
      groups.size > 0 && (
        <Box key="sec-others" flexDirection="column" gap={isGui ? 'sm' : 0}>
          {sectionHead('live', 'Other projects', others.length)}
          {[...groups].map(([name, rows]) =>
            card(
              `p-${name}`,
              'live',
              { text: 'live', tone: 'live' },
              name,
              String(rows.length),
              null,
              rows.map(s => `:${s.port}`).join(' '),
              <Box key={`rows-${name}`} flexDirection="column" gap={tight}>
                {rows.map(row)}
              </Box>,
            ),
          )}
        </Box>
      ),
      missing.length > 0 && (
        <Box key="sec-stopped" flexDirection="column" gap={isGui ? 'sm' : 0}>
          {sectionHead('off', 'Stopped', missing.length)}
          {missing.map(r => (
            <Box key={`miss${r.port}`} flexDirection="row" justifyContent="space-between" backgroundColor={gui(C.inset)} paddingX={1}>
              <Text wrap="truncate-end" tabular>
                <Text color={SECTION_COLOR.off}>○ </Text>
                <Text weight="semibold" bold={!isGui}>
                  {r.name}
                </Text>
                <Text color={C.blue}> :{r.port}</Text>
                <Text dimColor> is down</Text>
              </Text>
            </Box>
          ))}
        </Box>
      ),
    ]

    return (
      <Box flexDirection="column" gap={1} backgroundColor={gui(C.pane)} paddingX={isGui ? 2 : 0} paddingY={isGui ? 1 : 0}>
        {header}
        {strip}
        {sections}
      </Box>
    )
  })
}
