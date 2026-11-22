// The redaction check every build-log draft passes before it is written.
// Pure functions only: register.tsx reads the config and the files, this decides.

export type DenyList = {
  names?: string[]
  products?: string[]
  domains?: string[]
  repos?: string[]
  paths?: string[]
  words?: string[]
}

export type BuildLogConfig = {
  deny?: DenyList
  // URLs and slugs a draft may contain: the owner's own site, the kit's repo.
  allow_urls?: string[]
  // Repos whose moments and screenshots are the owner's own work, not an employer's or a client's.
  safe_repos?: string[]
}

export type Hit = { part: string; line: number; rule: string; match: string; severity: 'block' | 'warn' }

// Always allowed: the kit's own repository, as a URL or as the slug an install command uses.
export const KIT_ALLOW = ['github.com/travist6983/agent-kit', 'travist6983/agent-kit']

// Top-level folders of the kit: a relative path under one of these names the kit, not a client.
const KIT_DIRS = ['mods', 'docs', 'scripts', 'contracts', 'lib', 'templates', 'examples', '.claude', '.claude-plugin']

const TLDS = 'com|dev|io|net|org|app|ai|co|us|biz|info|tech|xyz|me|so|sh|cloud|site|online|store|gg|tv|ly|to|page|ca|uk|de'

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

const DENY_KINDS: (keyof DenyList)[] = ['names', 'products', 'domains', 'repos', 'paths', 'words']

export function denyTerms(config: BuildLogConfig | null) {
  const deny = config?.deny ?? {}
  const out: { kind: keyof DenyList; term: string }[] = []
  for (const kind of DENY_KINDS) {
    for (const raw of deny[kind] ?? []) {
      const term = String(raw ?? '').trim()
      if (term) out.push({ kind, term })
    }
  }
  return out
}

// The example file ships placeholders that all start with "example" or "your-". A config holding
// nothing else has not been filled in, and a draft checked against it would pass anything.
export function isPlaceholder(term: string) {
  return /^(?:example|your[-_ ]|placeholder)/i.test(term.replace(/^[~/.\s]+/, '').replace(/^users\/|^home\//i, ''))
}

export function configProblem(config: BuildLogConfig | null) {
  if (config === null) return 'no config'
  const terms = denyTerms(config)
  if (terms.length === 0) return 'the denylist is empty'
  if (terms.every(t => isPlaceholder(t.term))) return 'the denylist still holds only the example placeholders'
  return null
}

// "Acme Corp" also catches "acme-corp", "AcmeCorp", "acme.corp" and "Acme Corp's".
export function termPattern(term: string, kind: keyof DenyList) {
  if (kind === 'paths') return new RegExp(escape(term), 'i')
  const alnum = term.toLowerCase().replace(/[^a-z0-9]/g, '')
  if (alnum.length === 0) return null
  if (alnum.length < 4) return new RegExp(`(?<![a-z0-9])${escape(term.toLowerCase())}(?![a-z0-9])`, 'i')
  const body = [...alnum].map(escape).join('[^a-z0-9\\n]{0,2}')
  return new RegExp(`(?<![a-z0-9])${body}s?(?![a-z0-9])`, 'i')
}

function allowPatterns(config: BuildLogConfig | null) {
  const entries = [...KIT_ALLOW, ...(config?.allow_urls ?? [])]
    .map(e => String(e).trim().replace(/^https?:\/\//i, '').replace(/^www\./i, '').replace(/\/+$/, ''))
    .filter(Boolean)
  return entries.map(e => new RegExp(`(?:https?:\\/\\/)?(?:www\\.)?${escape(e)}(?:[/?#][^\\s)\\]>"'\`]*)?(?![\\w.-]*\\w)`, 'gi'))
}

// Blank out allowed URLs and slugs so the URL and path rules don't see them. Same length, so
// columns stay put.
function maskAllowed(line: string, allow: RegExp[]) {
  let out = line
  for (const re of allow) out = out.replace(re, m => ' '.repeat(m.length))
  return out
}

const normFigure = (s: string) => s.toLowerCase().replace(/[\s,]/g, '')

const RULES: { rule: string; re: RegExp; severity: 'block' | 'warn'; figure?: boolean; keep?: (m: string) => boolean }[] = [
  { rule: 'dash', re: /[\u2013\u2014]/g, severity: 'block' },
  { rule: 'email', re: /[\w.+-]+@[\w-]+(?:\.[\w-]+)+/g, severity: 'block' },
  { rule: 'url', re: /\bhttps?:\/\/[^\s)\]>"'`]+/gi, severity: 'block' },
  { rule: 'domain', re: new RegExp(`(?<![\\w@/.-])(?:[a-z0-9-]+\\.)+(?:${TLDS})(?![\\w-])(?:/[^\\s)\\]>"'\`]*)?`, 'gi'), severity: 'block' },
  { rule: 'path', re: /(?<![\w.])(?:~\/|\/(?:Users|home|var|opt|srv|etc|mnt|tmp|private|Volumes|workspace|root)\/)[^\s)\]>"'`]*/g, severity: 'block' },
  { rule: 'path', re: /\b[A-Za-z]:\\[^\s)\]>"'`]+/g, severity: 'block' },
  {
    rule: 'path',
    re: /(?<![\w./:-])\.{0,2}\/?[\w.-]+(?:\/[\w.-]+)+\/?/g,
    severity: 'block',
    // and/or, on/off, 24/7 pass; a path needs two slashes or a file extension, and a kit path passes.
    keep: m => {
      const p = m.replace(/^\.{0,2}\//, '')
      const first = p.split('/')[0] ?? ''
      if (KIT_DIRS.includes(first)) return false
      const slashes = (p.match(/\//g) ?? []).length
      return slashes >= 2 || /\.[a-z][a-z0-9]{0,4}$/i.test(p)
    },
  },
  { rule: 'branch', re: /\b(?:feat|feature|fix|bugfix|hotfix|chore|release|refactor)\/[\w.-]+/gi, severity: 'block' },
  {
    rule: 'ticket-key',
    re: /\b[A-Z][A-Z0-9]{1,9}-\d+\b/g,
    severity: 'block',
    keep: m => !/^(?:PROJ|GPT|UTF|ISO|SHA|MD|CVE|ES)-/.test(m),
  },
  { rule: 'money', re: /\$\s?\d[\d,]*(?:\.\d+)?(?:\s?[kKmM]\b)?/g, severity: 'block', figure: true },
  { rule: 'money', re: /\b\d[\d,]*(?:\.\d+)?\s?(?:usd|dollars|bucks)\b/gi, severity: 'block', figure: true },
  { rule: 'hours', re: /\b\d+(?:\.\d+)?\s?(?:h|hrs?|hours?)\b(?!\s*(?:limit|window|rate|cap))/gi, severity: 'block', figure: true },
  { rule: 'hours', re: /\b(?:per hour|hourly|an hour of billing)\b|\/\s?hr\b/gi, severity: 'block' },
  { rule: 'phone', re: /(?<!\d)\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)/g, severity: 'block' },
  { rule: 'secret', re: /\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})/g, severity: 'block' },
  {
    rule: 'job-hint',
    re: /\b(?:at work|my (?:new )?(?:job|employer|boss|company|client)|our client|the client's|new role|day job|at the office|this week at)\b/gi,
    severity: 'warn',
  },
]

export type CheckOptions = {
  part?: string
  // Dollar and hour figures that came from the owner's own repos (safe-origin moments).
  allowedFigures?: Set<string>
}

export function checkText(text: string, config: BuildLogConfig | null, opts: CheckOptions = {}): Hit[] {
  const part = opts.part ?? 'text'
  const figures = opts.allowedFigures ?? new Set<string>()
  const allow = allowPatterns(config)
  const terms = denyTerms(config)
    .filter(t => !isPlaceholder(t.term))
    .map(t => ({ ...t, re: termPattern(t.term, t.kind) }))
  const hits: Hit[] = []
  const lines = text.split('\n')
  lines.forEach((raw, i) => {
    const line = i + 1
    for (const t of terms) {
      const m = t.re?.exec(raw)
      if (m) hits.push({ part, line, rule: `denylist:${t.kind}`, match: m[0], severity: 'block' })
    }
    const masked = maskAllowed(raw, allow)
    const taken: [number, number][] = []
    for (const r of RULES) {
      r.re.lastIndex = 0
      for (const m of masked.matchAll(r.re)) {
        const at = m.index ?? 0
        const end = at + m[0].length
        if (r.keep && !r.keep(m[0])) continue
        if (r.figure && figures.has(normFigure(m[0]))) continue
        // One finding per stretch of text: a URL is not also reported as a domain and a path.
        if (r.severity === 'block' && taken.some(([a, b]) => at < b && end > a)) continue
        if (r.severity === 'block') taken.push([at, end])
        hits.push({ part, line, rule: r.rule, match: m[0].trim(), severity: r.severity })
      }
    }
  })
  return hits
}

// Every dollar and hour figure in a piece of text, normalized, for the allowed set.
export function figuresIn(text: string) {
  const out = new Set<string>()
  for (const r of RULES) {
    if (!r.figure) continue
    r.re.lastIndex = 0
    for (const m of text.matchAll(r.re)) out.add(normFigure(m[0]))
  }
  return out
}

export const words = (s: string) => s.split(/\s+/).filter(w => /[A-Za-z0-9]/.test(w)).length

export type DraftParts = { site: string; linkedin: string; x_thread: string[]; x_singles?: string[] }

// Shape rules from the posting plan. Only an X post over the limit blocks; lengths warn.
export function checkFormat(d: DraftParts): Hit[] {
  const hits: Hit[] = []
  const warn = (part: string, rule: string, match: string) => hits.push({ part, line: 0, rule, match, severity: 'warn' })
  const site = words(d.site)
  if (site < 600 || site > 1200) warn('site', 'length', `${site} words; the plan aims for 600 to 1,200`)
  for (const h of ['Problem', 'What the kit did', 'What broke', 'What I changed', 'Try it']) {
    if (!new RegExp(`^#{1,4}\\s*${h}\\b`, 'im').test(d.site)) warn('site', 'template', `no "${h}" section`)
  }
  const li = words(d.linkedin)
  if (li < 150 || li > 250) warn('linkedin', 'length', `${li} words; the plan aims for 150 to 250`)
  if (d.x_thread.length > 0 && (d.x_thread.length < 4 || d.x_thread.length > 6)) {
    warn('x', 'length', `${d.x_thread.length} posts; the plan aims for 4 to 6`)
  }
  const posts = [...d.x_thread.map((p, i) => [`x ${i + 1}`, p] as const), ...(d.x_singles ?? []).map((p, i) => [`x single ${i + 1}`, p] as const)]
  for (const [part, post] of posts) {
    const n = [...post.replace(/!\[[^\]]*\]\(shot:[\w-]+\)/g, '')].length
    if (n > 280) hits.push({ part, line: 0, rule: 'x-limit', match: `${n} characters, over 280`, severity: 'block' })
  }
  return hits
}

// shot:<id> references, as markdown images `![caption](shot:s-abc)` or bare.
export function shotRefs(text: string) {
  return [...new Set([...text.matchAll(/shot:([\w-]+)/g)].map(m => m[1] ?? ''))].filter(Boolean)
}

export function describeHits(hits: Hit[]) {
  return hits.map(h => `${h.severity === 'block' ? 'BLOCK' : 'warn'} ${h.part}${h.line ? `:${h.line}` : ''} ${h.rule}: ${h.match}`).join('\n')
}
