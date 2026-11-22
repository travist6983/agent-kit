import type { EngineInterface, Register } from 'claude-code'

import {
  KINDS,
  NOTABLE_ACTIONS,
  SHOT_EXT,
  latestShots,
  newId,
  originOf,
  parseArgs,
  parseMerge,
  parseMoments,
  parseRouter,
  parseShotArgs,
  routerAction,
  since,
  slugOf,
  slugify,
} from './capture'
import type { Moment, Shot, Where } from './capture'
import { checkFormat, checkText, configProblem, describeHits, figuresIn, shotRefs } from './check'
import type { BuildLogConfig, Hit } from './check'

// build-log collects post material while you work and drafts posts for you to publish yourself.
// It has no network code: it never posts, publishes, emails or calls a social API.
// Everything it keeps lives outside every repo, under the active Claude config folder.

const T = 'mcp__build-log__'

async function configDir($: EngineInterface) {
  const home = (await $.env.get('HOME')) ?? '/tmp'
  const dir = (await $.env.get('CLAUDE_CONFIG_DIR')) ?? ''
  if (dir === '') return `${home}/.claude`
  return dir.replace(/^~(?=\/|$)/, home).replace(/\/+$/, '')
}

// BUILD_LOG_HOME moves the folder; by default it is <config>/build-log, so each Claude account
// keeps its own notes, denylist and drafts, as every kit mod keeps its data.
async function dataDir($: EngineInterface) {
  const home = (await $.env.get('HOME')) ?? '/tmp'
  const override = (await $.env.get('BUILD_LOG_HOME')) ?? ''
  if (override) return override.replace(/^~(?=\/|$)/, home).replace(/\/+$/, '')
  return `${await configDir($)}/build-log`
}

const paths = async ($: EngineInterface) => {
  const d = await dataDir($)
  return {
    dir: d,
    config: `${d}/config.json`,
    moments: `${d}/moments.jsonl`,
    shots: `${d}/shots.jsonl`,
    shotDir: `${d}/shots`,
    drafts: `${d}/drafts`,
  }
}

async function sh($: EngineInterface, script: string, ...args: string[]) {
  return $.process.run(['sh', '-c', script, 'build-log', ...args], { timeoutMs: 20_000 })
}

async function appendLine($: EngineInterface, file: string, line: string) {
  const r = await sh($, 'umask 077; mkdir -p "$(dirname "$2")" && printf "%s\\n" "$1" >> "$2"', line, file)
  if (r.exitCode !== 0) throw new Error(r.stderr.trim() || `could not write ${file}`)
}

async function writeFile($: EngineInterface, file: string, text: string) {
  const r = await sh($, 'umask 077; mkdir -p "$(dirname "$2")" && printf "%s" "$1" > "$2"', text, file)
  if (r.exitCode !== 0) throw new Error(r.stderr.trim() || `could not write ${file}`)
}

async function readLines($: EngineInterface, file: string) {
  if (!(await $.fs.exists(file).catch(() => false))) return []
  const text = String(await $.fs.read(file).catch(() => ''))
  return text.split('\n').filter(l => l.trim() !== '')
}

async function loadConfig($: EngineInterface): Promise<BuildLogConfig | null> {
  const { config } = await paths($)
  if (!(await $.fs.exists(config).catch(() => false))) return null
  try {
    return JSON.parse(String(await $.fs.read(config))) as BuildLogConfig
  } catch {
    return null
  }
}

async function where($: EngineInterface): Promise<Where> {
  const cwd = await $.session.cwd()
  const top = await $.process.run(['git', 'rev-parse', '--show-toplevel'], { cwd, timeoutMs: 5000 }).catch(() => null)
  const root = top && top.exitCode === 0 ? top.stdout.trim() : ''
  const remote = root
    ? await $.process.run(['git', 'config', '--get', 'remote.origin.url'], { cwd: root, timeoutMs: 5000 }).catch(() => null)
    : null
  return {
    name: root ? (root.split('/').pop() ?? '') : '',
    slug: remote && remote.exitCode === 0 ? slugOf(remote.stdout) : '',
    loopProject: ((await $.env.get('AGENT_KIT_PROJECT')) ?? '').trim(),
  }
}

async function context($: EngineInterface) {
  const w = await where($)
  const config = await loadConfig($)
  return { repo: w.loopProject || w.name || 'unknown', origin: originOf(w, config), config }
}

const isPaused = async ($: EngineInterface) => (await $.store.get('paused')) === true

async function saveMoment($: EngineInterface, m: Omit<Moment, 'id' | 'at' | 'repo' | 'origin'>) {
  const now = await $.clock.now()
  const { repo, origin } = await context($)
  const moment: Moment = { id: newId('m', now, Math.floor(Math.random() * 1296)), at: new Date(now).toISOString(), repo, origin, ...m }
  await appendLine($, (await paths($)).moments, JSON.stringify(moment))
  return moment
}

async function loadShots($: EngineInterface) {
  return latestShots(await readLines($, (await paths($)).shots))
}

async function setShotStatus($: EngineInterface, id: string, status: Shot['status']) {
  const shots = await loadShots($)
  const shot = shots.find(s => s.id === id)
  if (!shot) return null
  await appendLine($, (await paths($)).shots, JSON.stringify({ id, status }))
  return { ...shot, status }
}

// Copy an image or clip into the build-log folder. From the kit or one of the owner's own repos
// it is ready to use; from any other repo it is kept only on the owner's yes and stays flagged
// until they have looked at it.
async function addShot($: EngineInterface, src: string, caption: string, approvedByTyping: boolean) {
  const cwd = await $.session.cwd()
  const file = src.startsWith('/') || src.startsWith('~') ? src : `${cwd}/${src}`
  if (!SHOT_EXT.test(file)) return { error: 'Only png, jpg, gif, webp, mp4 or mov files are kept.' }
  const home = (await $.env.get('HOME')) ?? ''
  const abs = file.replace(/^~(?=\/)/, home)
  if (!(await $.fs.exists(abs).catch(() => false))) return { error: `No file at ${abs}.` }
  const { repo, origin } = await context($)
  if (origin === 'work' && !approvedByTyping) {
    const keep = 'Keep it, flag for review'
    const answer = await $.ui
      .ask(`This shot is from ${repo}, not the kit or one of your own repos, so it may show names. Keep "${caption || 'no caption'}" and flag it for review?`, [keep, 'Do not keep it'])
      .catch(() => null)
    if (answer !== keep) {
      return { error: 'Not kept: a screenshot from this repo needs the user\'s own yes, and none was given. Shoot the kit or a demo repo instead, or have the user run /buildlog shot <path> themselves.' }
    }
  }
  const now = await $.clock.now()
  const id = newId('s', now, Math.floor(Math.random() * 1296))
  const ext = (abs.match(SHOT_EXT)?.[1] ?? 'png').toLowerCase()
  const p = await paths($)
  const dest = `${p.shotDir}/${id}.${ext}`
  const r = await sh($, 'umask 077; mkdir -p "$(dirname "$2")" && cp "$1" "$2"', abs, dest)
  if (r.exitCode !== 0) return { error: `Could not copy ${abs}: ${r.stderr.trim()}` }
  const status: Shot['status'] = origin === 'own' ? 'ok' : 'review'
  const shot: Shot = { id, at: new Date(now).toISOString(), file: dest, caption, repo, origin, status }
  await appendLine($, p.shots, JSON.stringify(shot))
  return { shot }
}

function shotLine(s: Shot) {
  return `${s.id} [${s.status === 'review' ? 'NEEDS REVIEW' : 'ok'}] ${s.caption || '(no caption)'} · ${s.origin === 'own' ? 'own repo' : 'work'} · ${s.file}`
}

async function lastDraftAt($: EngineInterface) {
  return ((await $.store.get('lastDraftAt')) as string | undefined) ?? ''
}

type DraftInput = { slug?: unknown; title?: unknown; site?: unknown; linkedin?: unknown; x_thread?: unknown; x_singles?: unknown }

const asText = (v: unknown) => (typeof v === 'string' ? v : '')
const asList = (v: unknown) => (Array.isArray(v) ? v.map(x => String(x ?? '')).filter(x => x.trim() !== '') : [])

async function saveDraft($: EngineInterface, input: DraftInput) {
  const config = await loadConfig($)
  const p = await paths($)
  const problem = configProblem(config)
  if (problem) {
    return `Not saved: ${problem} (${p.config}). build-log fails closed: no draft is written until the user has filled in their denylist. Ask them to run /buildlog init and add the names, products, domains and repos to keep out of posts.`
  }
  const title = asText(input.title).trim()
  const parts = { site: asText(input.site), linkedin: asText(input.linkedin), x_thread: asList(input.x_thread), x_singles: asList(input.x_singles) }
  if (!title || !parts.site.trim()) return 'Not saved: a draft needs a title and the site version at least.'

  const moments = parseMoments(await readLines($, p.moments))
  const figures = new Set<string>()
  for (const m of moments.filter(x => x.origin === 'own')) {
    for (const f of figuresIn([m.title, m.problem, m.kit_did, m.broke, m.changed].filter(Boolean).join('\n'))) figures.add(f)
  }
  const opts = (part: string) => ({ part, allowedFigures: figures })
  const hits: Hit[] = [
    ...checkText(title, config, opts('title')),
    ...checkText(parts.site, config, opts('site')),
    ...checkText(parts.linkedin, config, opts('linkedin')),
    ...parts.x_thread.flatMap((x, i) => checkText(x, config, opts(`x ${i + 1}`))),
    ...parts.x_singles.flatMap((x, i) => checkText(x, config, opts(`x single ${i + 1}`))),
    ...checkFormat(parts),
  ]

  const all = [title, parts.site, parts.linkedin, ...parts.x_thread, ...parts.x_singles].join('\n')
  const shots = await loadShots($)
  const used = shotRefs(all)
  for (const id of used) {
    const s = shots.find(x => x.id === id)
    if (!s) hits.push({ part: 'shots', line: 0, rule: 'unknown-shot', match: id, severity: 'block' })
    else if (s.status === 'review') hits.push({ part: 'shots', line: 0, rule: 'shot-needs-review', match: `${id}: the user looks at it, then /buildlog ok ${id}`, severity: 'block' })
  }

  const blocks = hits.filter(h => h.severity === 'block')
  if (blocks.length > 0) {
    return `Not saved: the redaction check blocked ${blocks.length} finding(s). Rewrite those lines (generic words such as "a production codebase" or "a client project", no names, paths, links, costs or hours from work) and call save_draft again.\n${describeHits(hits)}`
  }

  const now = await $.clock.now()
  const day = new Date(now).toISOString().slice(0, 10)
  const folder = `${p.drafts}/${day}-${slugify(asText(input.slug) || title)}`
  const x = [
    ...parts.x_thread.map((t, i) => `## ${i + 1}/${parts.x_thread.length}\n\n${t}`),
    ...parts.x_singles.map((t, i) => `## single ${i + 1}\n\n${t}`),
  ].join('\n\n')
  await writeFile($, `${folder}/site.md`, `# ${title}\n\n${parts.site.trim()}\n`)
  if (parts.linkedin.trim()) await writeFile($, `${folder}/linkedin.md`, `${parts.linkedin.trim()}\n`)
  if (x) await writeFile($, `${folder}/x.md`, `${x}\n`)
  const usedShots = shots.filter(s => used.includes(s.id))
  await writeFile(
    $,
    `${folder}/check.json`,
    JSON.stringify({ checked_at: new Date(now).toISOString(), passed: true, warnings: hits, shots: usedShots.map(s => ({ id: s.id, file: s.file, caption: s.caption, origin: s.origin })) }, null, 2),
  )
  await $.store.set('lastDraftAt', new Date(now).toISOString())
  $.ui.toast(`build-log: draft saved to ${folder}`, { timeoutMs: 8000 })
  const warn = hits.length > 0 ? `\nWarnings (not blocking):\n${describeHits(hits)}` : ''
  const look = usedShots.length > 0
    ? `\nImages used: ${usedShots.map(s => `${s.id} (${s.file})`).join(', ')}. The text check cannot see inside images: tell the user to look at each one for names, paths, tabs and URLs before posting.`
    : ''
  return `Saved to ${folder} (site.md${parts.linkedin.trim() ? ', linkedin.md' : ''}${x ? ', x.md' : ''}, check.json). Nothing was posted: the user reads it and publishes it themselves.${warn}${look}`
}

async function status($: EngineInterface) {
  const p = await paths($)
  const config = await loadConfig($)
  const problem = configProblem(config)
  const moments = parseMoments(await readLines($, p.moments))
  const fresh = since(moments, await lastDraftAt($))
  const shots = await loadShots($)
  const review = shots.filter(s => s.status === 'review').length
  const { repo, origin } = await context($)
  return [
    `build-log${(await isPaused($)) ? ' (paused)' : ''}: ${moments.length} moments (${fresh.length} since the last draft), ${shots.length} shots${review ? ` (${review} need review)` : ''}.`,
    `This session: ${repo}, ${origin === 'own' ? 'your own repo' : 'work (strict: shots need your yes)'}.`,
    problem ? `Denylist: ${problem}. Run /buildlog init and fill in ${p.config}; drafts are refused until then.` : `Denylist: ${p.config}`,
    `Notes, shots and drafts: ${p.dir}`,
  ].join('\n')
}

const USAGE = [
  '/buildlog                    status',
  '/buildlog init               create your private denylist file',
  '/buildlog note <text>        save a moment in your own words',
  '/buildlog shot <path> [cap]  keep a screenshot or clip',
  '/buildlog grab [caption]     macOS: pick a screen region to capture',
  '/buildlog shots              list kept shots',
  '/buildlog ok <id>            mark a flagged shot reviewed',
  '/buildlog drop <id>          forget a shot',
  '/buildlog draft [YYYY-MM-DD] draft posts from moments since a day (default: since the last draft)',
  '/buildlog check <file>       run the redaction check on any file',
  '/buildlog off | on           pause or resume collecting',
].join('\n')

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'buildlog', description: 'Collect post material as you work and draft posts for you to publish yourself', argumentHint: '[init | note | shot | grab | shots | ok | drop | draft | check | off | on]' }).catch(() => undefined)
    await $.tool.register({
      name: 'log_moment',
      description:
        "Save a moment worth a build-log post to the user's private build-log notes (never committed, never posted): a recipe run that retried or escalated, a PR merged, a bug a mod caught, a spend surprise, a before and after, a lesson. Write it in plain words using the post template's parts. Leave out client names; the notes are private but the habit keeps drafts clean.",
      inputSchema: {
        type: 'object',
        properties: {
          kind: { type: 'string', enum: [...KINDS] },
          title: { type: 'string', description: 'The lesson or result in one line, not the tool name' },
          problem: { type: 'string', description: 'What went wrong or slowed things down' },
          kit_did: { type: 'string', description: 'The skill, recipe or mod involved and what it did' },
          broke: { type: 'string', description: 'The honest failure' },
          changed: { type: 'string', description: 'The fix or change that followed' },
        },
        required: ['kind', 'title'],
      },
      isDeferred: true,
    })
    await $.tool.register({
      name: 'list_moments',
      description: "List the user's saved build-log moments and kept screenshots, to draft posts from. Each moment says whether it came from the user's own repo or from work.",
      inputSchema: { type: 'object', properties: { since: { type: 'string', description: 'ISO date; default is since the last saved draft' } } },
      isDeferred: true,
    })
    await $.tool.register({
      name: 'add_screenshot',
      description:
        "Keep a screenshot or short clip (png, jpg, gif, webp, mp4, mov) for build-log posts. Shots from the kit or the user's own repos are kept; from any other repo the user is asked, and a kept one stays flagged until they review it. Prefer shots of the kit's own panes or a demo repo.",
      inputSchema: {
        type: 'object',
        properties: { path: { type: 'string' }, caption: { type: 'string' } },
        required: ['path'],
      },
      isDeferred: true,
    })
    await $.tool.register({
      name: 'save_draft',
      description:
        'Save a build-log draft: the long site version, the short LinkedIn version and an X thread. It runs the redaction check first and writes nothing if the check blocks. Reference shots as ![caption](shot:<id>). It never posts anything.',
      inputSchema: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          slug: { type: 'string' },
          site: { type: 'string', description: 'Markdown with ## Problem, ## What the kit did, ## What broke, ## What I changed, ## Try it' },
          linkedin: { type: 'string', description: '150 to 250 words, links to the site post' },
          x_thread: { type: 'array', items: { type: 'string' }, description: '4 to 6 posts, each 280 characters or fewer' },
          x_singles: { type: 'array', items: { type: 'string' }, description: 'Optional one-screenshot posts for quiet weeks' },
        },
        required: ['title', 'site'],
      },
      isDeferred: true,
    })
    return next(e)
  })

  on('tool.call', { tool: `${T}log_moment` }, async ($, e) => {
    if (await isPaused($)) return { result: 'build-log is paused (/buildlog on resumes it). Nothing saved.' }
    const input = e as unknown as Record<string, unknown>
    const title = asText(input.title).trim()
    if (title.length < 5) return { result: 'Not saved: a moment needs a one-line title.' }
    const kind = KINDS.includes(asText(input.kind) as (typeof KINDS)[number]) ? asText(input.kind) : 'note'
    const m = await saveMoment($, {
      kind,
      title,
      problem: asText(input.problem) || undefined,
      kit_did: asText(input.kit_did) || undefined,
      broke: asText(input.broke) || undefined,
      changed: asText(input.changed) || undefined,
      source: 'claude',
    })
    return { result: `Saved moment ${m.id} (${m.origin === 'own' ? 'own repo' : 'work: drafts will keep it generic'}).` }
  }).catch(() => ({ result: 'build-log hit an error and saved nothing. Try again, or /buildlog for its status.' }))

  on('tool.call', { tool: `${T}list_moments` }, async ($, e) => {
    const p = await paths($)
    const from = asText((e as unknown as Record<string, unknown>).since) || (await lastDraftAt($))
    const moments = since(parseMoments(await readLines($, p.moments)), from)
    const shots = await loadShots($)
    const problem = configProblem(await loadConfig($))
    return {
      result: JSON.stringify({
        since: from || 'the beginning',
        denylist: problem ? `NOT READY: ${problem}; save_draft will refuse until the user fills it in` : 'ready',
        moments,
        shots: shots.map(s => ({ id: s.id, caption: s.caption, status: s.status, origin: s.origin, file: s.file })),
      }),
    }
  }).catch(() => ({ result: 'build-log hit an error and saved nothing. Try again, or /buildlog for its status.' }))

  on('tool.call', { tool: `${T}add_screenshot` }, async ($, e) => {
    if (await isPaused($)) return { result: 'build-log is paused. Nothing kept.' }
    const input = e as unknown as Record<string, unknown>
    const r = await addShot($, asText(input.path), asText(input.caption), false)
    if ('error' in r) return { result: r.error ?? 'Not kept.' }
    const s = r.shot
    return { result: `Kept ${s.id}${s.status === 'review' ? ': flagged for review. It cannot go in a draft until the user looks at it and runs /buildlog ok ' + s.id : ''}.` }
  }).catch(() => ({ result: 'build-log hit an error and saved nothing. Try again, or /buildlog for its status.' }))

  on('tool.call', { tool: `${T}save_draft` }, async ($, e) => ({ result: await saveDraft($, e as unknown as DraftInput) })).catch(() => ({ result: 'build-log hit an error and wrote no draft. Try again, or /buildlog for its status.' }))

  // Seeds for posts, noticed without being asked: a merged PR, a router verdict worth telling.
  // Only the fact is kept, never command output. Never blocks or changes the call.
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const res = await next(e)
    try {
      if (res.deny !== undefined || res.isError === true || (await isPaused($))) return res
      const command = String((e as unknown as { command?: unknown }).command ?? '')
      const merge = parseMerge(command)
      if (merge) {
        await saveMoment($, { kind: 'pr-merged', title: merge.pr ? `Merged PR ${merge.pr}` : 'Merged a PR', source: 'auto' })
        $.ui.toast('build-log: saved the merge as a moment')
      }
      const router = parseRouter(command)
      if (router) {
        const r = res.result as { stdout?: unknown } | undefined
        const action = routerAction(typeof r?.stdout === 'string' ? r.stdout : (res.text ?? ''))
        if (NOTABLE_ACTIONS.has(action)) {
          await saveMoment($, { kind: 'recipe-run', title: `Recipe router: ${router.stage} ${router.result || ''} -> ${action}`.replace(/\s+/g, ' '), source: 'auto' })
        }
      }
    } catch {
      // collecting is best effort; the tool's own result always goes back untouched
    }
    return res
  }).catch(($, e, next) => next(e))

  on('command.run', { command: 'buildlog' }, async ($, e) => {
    const { verb, rest } = parseArgs(e.args)
    const p = await paths($)

    if (verb === '' || verb === 'status') return { text: await status($) }
    if (verb === 'help') return { text: USAGE }

    if (verb === 'init') {
      if (await $.fs.exists(p.config).catch(() => false)) return { text: `Already set up: ${p.config}. Edit it to change the denylist.` }
      const example = String(await $.fs.read(`${$.plugin.root}/config.example.json`).catch(() => ''))
      if (!example) return { text: `The example config is missing from the mod (${$.plugin.root}/config.example.json).` }
      await writeFile($, p.config, example)
      return {
        text: `Created ${p.config} (private to you, outside every repo). Replace the example entries with the names, products, domains, repos and paths to keep out of posts, your site under allow_urls, and your own repos under safe_repos. Drafts are refused until the examples are gone.`,
      }
    }

    if (verb === 'off' || verb === 'on') {
      await $.store.set('paused', verb === 'off')
      return { text: verb === 'off' ? 'build-log paused: nothing is collected until /buildlog on.' : 'build-log collecting again.' }
    }

    if (verb === 'note') {
      if (rest.length < 5) return { text: 'Usage: /buildlog note <what happened, in a sentence>' }
      const m = await saveMoment($, { kind: 'note', title: rest, source: 'user' })
      return { text: `Saved ${m.id}.` }
    }

    if (verb === 'shot') {
      const { path, caption } = parseShotArgs(rest)
      if (!path) return { text: 'Usage: /buildlog shot <path> [caption]' }
      // Typing the command is the owner's yes for this one shot; from work it is still flagged.
      const r = await addShot($, path, caption, true)
      if ('error' in r) return { text: r.error ?? 'Not kept.' }
      return { text: r.shot.status === 'review' ? `Kept ${r.shot.id}, flagged: look at it, then /buildlog ok ${r.shot.id}.` : `Kept ${r.shot.id}.` }
    }

    if (verb === 'grab') {
      const os = await $.process.run(['uname'], { timeoutMs: 3000 }).catch(() => null)
      if (os?.stdout.trim() !== 'Darwin') return { text: '/buildlog grab uses macOS screencapture. Elsewhere, save a screenshot and use /buildlog shot <path>.' }
      const now = await $.clock.now()
      const tmp = `${p.dir}/tmp-${now}.png`
      await sh($, 'umask 077; mkdir -p "$1"', p.dir)
      const r = await $.process.run(['screencapture', '-i', '-x', tmp], { timeoutMs: 120_000 }).catch(() => null)
      if (!r || !(await $.fs.exists(tmp).catch(() => false))) return { text: 'Nothing captured.' }
      const added = await addShot($, tmp, rest, true)
      await sh($, 'rm -f "$1"', tmp)
      if ('error' in added) return { text: added.error ?? 'Not kept.' }
      return { text: added.shot.status === 'review' ? `Kept ${added.shot.id}, flagged: look at it, then /buildlog ok ${added.shot.id}.` : `Kept ${added.shot.id}.` }
    }

    if (verb === 'shots') {
      const shots = await loadShots($)
      return { text: shots.length ? shots.map(shotLine).join('\n') : 'No shots kept yet.' }
    }

    if (verb === 'ok' || verb === 'drop') {
      const s = await setShotStatus($, rest, verb === 'ok' ? 'ok' : 'dropped')
      if (!s) return { text: `No shot ${rest}. /buildlog shots lists them.` }
      if (verb === 'drop') await sh($, 'rm -f "$1"', s.file)
      return { text: verb === 'ok' ? `${rest} marked reviewed.` : `${rest} forgotten and its file deleted.` }
    }

    if (verb === 'check') {
      if (!rest) return { text: 'Usage: /buildlog check <file>' }
      const config = await loadConfig($)
      const problem = configProblem(config)
      if (problem) return { text: `Can't check: ${problem} (${p.config}). /buildlog init sets it up.` }
      const file = rest.startsWith('/') ? rest : `${await $.session.cwd()}/${rest}`
      if (!(await $.fs.exists(file).catch(() => false))) return { text: `No file at ${file}.` }
      const hits = checkText(String(await $.fs.read(file)), config, { part: file.split('/').pop() ?? 'file' })
      const blocks = hits.filter(h => h.severity === 'block').length
      return { text: hits.length === 0 ? 'Clean: no findings.' : `${blocks} blocking, ${hits.length - blocks} warnings:\n${describeHits(hits)}` }
    }

    if (verb === 'draft') {
      const from = /^\d{4}-\d{2}-\d{2}/.test(rest) ? rest : await lastDraftAt($)
      const problem = configProblem(await loadConfig($))
      if (problem) return { text: `Drafts are refused until the denylist is ready: ${problem}. /buildlog init, then fill in ${p.config}.` }
      return {
        text: `Drafting from moments since ${from || 'the beginning'}.`,
        context: [
          `The user ran /buildlog draft. Use the build-log skill: call ${T}list_moments with since "${from}", pick the one story worth a post, draft the site, LinkedIn and X versions with the template, and save them only through ${T}save_draft. Never post, publish or send anything.`,
        ],
      }
    }

    return { text: USAGE }
  })
}

