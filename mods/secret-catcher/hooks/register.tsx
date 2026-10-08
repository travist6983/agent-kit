import type { EngineInterface, Register } from 'claude-code'

import { redact } from './redact'

// The active Claude config folder, so each account (CLAUDE_CONFIG_DIR) keeps its own data.
async function configDir($: EngineInterface) {
  const home = (await $.env.get('HOME')) ?? '/tmp'
  const dir = (await $.env.get('CLAUDE_CONFIG_DIR')) ?? ''
  if (dir === '') return `${home}/.claude`
  return dir.replace(/^~(?=\/|$)/, home).replace(/\/+$/, '')
}

async function vaultPath($: EngineInterface) {
  const base = await configDir($)
  const cwd = await $.session.cwd()
  const top = await $.process.run(['git', 'rev-parse', '--show-toplevel'], { cwd })
  const project = (top.exitCode === 0 ? top.stdout.trim() : cwd).split('/').pop() || 'default'
  return { dir: `${base}/secrets`, file: `${base}/secrets/${project}.env` }
}

async function save($: EngineInterface, found: Map<string, string>) {
  const { dir, file } = await vaultPath($)
  await $.process.run(['mkdir', '-p', dir])
  await $.process.run(['chmod', '700', dir])
  let text = ''
  try {
    text = await $.fs.read(file)
  } catch {
    text = ''
  }
  const lines = text.split('\n').filter(l => l.trim() !== '')
  for (const [name, value] of found) {
    const at = lines.findIndex(l => l.startsWith(`${name}=`))
    const line = `${name}='${value.replace(/'/g, `'\\''`)}'`
    if (at >= 0) lines[at] = line
    else lines.push(line)
  }
  await $.fs.write(file, `${lines.join('\n')}\n`)
  await $.process.run(['chmod', '600', file])
  return file
}

export const register: Register = on => {
  on('prompt.submit', async ($, e, next) => {
    const { text, found } = redact(e.text)
    if (found.size === 0) return next(e)

    const file = await save($, found)
    const names = [...found.keys()]
    $.ui.toast(`Caught ${names.length} secret(s): ${names.join(', ')} → ${file}`, { timeoutMs: 10_000 })
    const note = [
      `The user pasted secrets; secret-catcher removed them from this prompt and saved them to ${file} (chmod 600) as: ${names.join(', ')}.`,
      `Where the prompt shows $NAME, that is the saved value. Use it with \`set -a; . "${file}"; set +a\` before a command, or copy it into the project's own env file or secret store (fly secrets set, wrangler secret put…) by reading the file in a shell pipeline.`,
      'Never print, echo, cat or log the values, and never write them into tracked files.',
    ].join(' ')
    return next({ ...e, text, context: [...(e.context ?? []), note] })
  }).catch(($, e, next) =>
    next.called ? next(e) : { drop: 'secret-catcher failed while checking this prompt for secrets; it was not sent. Try again or disable secret-catcher.' },
  )
}
