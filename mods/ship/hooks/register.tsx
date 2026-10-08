import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { RepoStatus } from '../types'

const status = atom({ plugin: 'ship', key: 'status' } as const, null)
const isHidden = atom({ plugin: 'ship', key: 'isHidden' } as const, false)

const DEPLOY = /\b(wrangler (pages )?deploy|fly deploy|flyctl deploy|vercel( deploy)? --prod|netlify deploy --prod|npm run deploy|pnpm (run )?deploy|firebase deploy|gh pr merge)\b/

const recipeKey = (root: string) => `recipe:${root}`
const deployKey = (root: string) => `deploy:${root}`

async function readStatus($: EngineInterface): Promise<RepoStatus | null> {
  const cwd = await $.session.cwd()
  const top = await $.process.run(['git', 'rev-parse', '--show-toplevel'], { cwd })
  if (top.exitCode !== 0) return null
  const root = top.stdout.trim()
  const st = await $.process.run(['git', 'status', '--porcelain=v2', '--branch'], { cwd: root })
  let branch = '?'
  let ahead = 0
  let behind = 0
  let hasUpstream = false
  let changed = 0
  for (const line of st.stdout.split('\n')) {
    if (line.startsWith('# branch.head ')) branch = line.slice(14)
    else if (line.startsWith('# branch.upstream ')) hasUpstream = true
    else if (line.startsWith('# branch.ab ')) {
      const [a = '+0', b = '-0'] = line.slice(12).split(' ')
      ahead = Number(a.slice(1))
      behind = Number(b.slice(1))
    } else if (line && !line.startsWith('#')) changed += 1
  }
  const lastDeploy = ((await $.store.get(deployKey(root))) ?? null) as RepoStatus['lastDeploy']
  const recipe = ((await $.store.get(recipeKey(root))) ?? null) as string | null
  const name = root.split('/').pop() ?? root

  return { root, name, branch, changed, ahead, behind, hasUpstream, lastDeploy, recipe }
}

async function refresh($: EngineInterface) {
  const next = await readStatus($)
  await update($, status, () => next)
}

function ago(ms: number) {
  const m = Math.round(ms / 60000)
  if (m < 60) return `${m}m ago`
  const h = Math.round(m / 60)
  return h < 48 ? `${h}h ago` : `${Math.round(h / 24)}d ago`
}

function shipPrompt(s: RepoStatus, mode: 'deploy' | 'pr', note: string) {
  const steps = [
    `Ship ${s.name} (branch ${s.branch}).`,
    s.changed > 0
      ? `1. Review the ${s.changed} uncommitted change(s), leave out anything that looks like a secret, scratch file or unrelated work in progress, and commit with a clear message.`
      : '1. Nothing is uncommitted; skip committing.',
    mode === 'pr'
      ? `2. Push the branch and open a pull request against the default branch. If you are on the default branch, create a branch first.`
      : `2. Push to origin${s.hasUpstream ? '' : ' and set the upstream'}.`,
  ]
  if (mode === 'deploy') {
    steps.push(
      s.recipe
        ? `3. Deploy with this saved recipe, from the repo root: \`${s.recipe}\`. If it fails, stop and show me the error.`
        : '3. No deploy recipe is saved for this repo. Work out how it deploys (wrangler.toml, fly.toml, package.json scripts, GitHub Pages, CI on push). Deploy it, then call the ship tool `save_recipe` with the exact command(s) so next time is one step. If deploying is just "push to main" (Pages/CI), save "push-only".',
    )
  }
  steps.push('Finish with one short block: commit sha, what was deployed and the live URL. No narration.')
  if (note) steps.push(`Note from me: ${note}`)
  return steps.join('\n')
}

async function ship($: EngineInterface, mode: 'deploy' | 'pr', note: string) {
  const s = await readStatus($)
  if (s === null) return 'Not in a git repository.'
  await $.prompt.submit({ text: shipPrompt(s, mode, note) })
  return mode === 'pr' ? `Opening a PR for ${s.name}…` : `Shipping ${s.name}…`
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'ship',
      description: 'Commit, push and deploy this repo with its saved recipe',
      argumentHint: '[pr | set <deploy cmd> | show | note…]',
    })
    await $.tool.register({
      name: 'save_recipe',
      description:
        "Save this repo's deploy command(s) for /ship. Call after you have deployed successfully and the user's /ship prompt asked you to. Use 'push-only' when a push deploys.",
      inputSchema: {
        type: 'object',
        properties: { command: { type: 'string', description: 'Shell command(s) run from the repo root' } },
        required: ['command'],
      },
      isDeferred: false,
    })
    await refresh($)
    $.clock.every(60_000, () => void refresh($))

    return next(e)
  })

  on('command.run', { command: 'ship' }, async ($, e) => {
    const args = e.args.trim()
    const s = await readStatus($)
    if (s === null) return { text: 'Not in a git repository.' }
    if (args.startsWith('set ')) {
      await $.store.set(recipeKey(s.root), args.slice(4).trim())
      await refresh($)
      return { text: `Saved deploy recipe for ${s.name}: ${args.slice(4).trim()}` }
    }
    if (args === 'show') {
      return { text: `${s.name}: ${s.recipe ?? 'no recipe saved yet (the first /ship will work one out)'}` }
    }
    if (args === 'pr' || args.startsWith('pr ')) return { text: await ship($, 'pr', args.slice(2).trim()) }
    return { text: await ship($, 'deploy', args) }
  })

  on('tool.call', { tool: 'mcp__ship__save_recipe' }, async ($, e) => {
    const s = await readStatus($)
    if (s === null) return { result: 'Not in a git repository; nothing saved.' }
    const command = String(e.command ?? '').trim()
    await $.store.set(recipeKey(s.root), command)
    await refresh($)
    return { result: `Saved deploy recipe for ${s.name}: ${command}` }
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const ran = await next(e)
    if (/\bgit (commit|push|checkout|switch|merge|pull|reset)\b/.test(e.command) || DEPLOY.test(e.command)) {
      if (DEPLOY.test(e.command) && ran.deny === undefined && ran.isError !== true) {
        const s = await readStatus($)
        if (s !== null) {
          const what = (e.command.match(DEPLOY)?.[0] ?? 'deploy').trim()
          await $.store.set(deployKey(s.root), { at: await $.clock.now(), what })
        }
      }
      await refresh($)
    }
    return ran
  }).catch(($, e, next) => next(e))

  on('turn.complete', async ($, e, next) => {
    await refresh($)
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const s = await read($, status)
    if (e.props.hasSurvey || s === null || (await read($, isHidden))) return next(e)
    if (s.changed === 0 && s.ahead === 0) return next(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    const now = await $.clock.now()
    const parts = [
      s.branch,
      s.changed > 0 ? `${s.changed} changed` : null,
      s.ahead > 0 ? `${s.ahead} to push` : null,
      s.behind > 0 ? `${s.behind} behind` : null,
      s.lastDeploy ? `deployed ${ago(now - s.lastDeploy.at)}` : 'no deploy seen',
    ].filter(Boolean)

    return (
      <Box>
        <Text dimColor>ship · {s.name} · {parts.join(' · ')} </Text>
        <Button key="ship" label="Ship" variant="primary" onPress={() => void ship($, 'deploy', '')} />
        <Button key="pr" label="PR" onPress={() => void ship($, 'pr', '')} />
        <Button key="hide" label="Hide" onPress={() => update($, isHidden, () => true)} />
      </Box>
    )
  })
}
