type Rule = { name: string; pattern: RegExp }

// Vendor-shaped keys. Each match is saved under `name` (suffixed _2, _3… when one prompt holds several).
export const RULES: Rule[] = [
  { name: 'ANTHROPIC_API_KEY', pattern: /\bsk-ant-[A-Za-z0-9_-]{20,}/g },
  { name: 'OPENAI_API_KEY', pattern: /\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}/g },
  { name: 'STRIPE_SECRET_KEY', pattern: /\b(?:sk|rk)_live_[A-Za-z0-9]{20,}/g },
  { name: 'RESEND_API_KEY', pattern: /\bre_[A-Za-z0-9]{6,}_[A-Za-z0-9]{12,}/g },
  { name: 'GITHUB_TOKEN', pattern: /\b(?:ghp|gho|ghu|ghs)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}/g },
  { name: 'AWS_ACCESS_KEY_ID', pattern: /\bAKIA[0-9A-Z]{16}\b/g },
  { name: 'SLACK_TOKEN', pattern: /\bxox[abprs]-[A-Za-z0-9-]{10,}/g },
  { name: 'GOOGLE_API_KEY', pattern: /\bAIza[0-9A-Za-z_-]{35}\b/g },
  { name: 'FLY_API_TOKEN', pattern: /\bFlyV1 fm2_[A-Za-z0-9_+/=-]{20,}/g },
  { name: 'NEON_API_KEY', pattern: /\bnapi_[a-z0-9]{40,}/g },
  { name: 'DATABASE_URL', pattern: /\b(?:postgres(?:ql)?|mysql|redis|rediss|mongodb(?:\+srv)?):\/\/[^\s:@/]+:[^\s@/]{6,}@[^\s'"]+/g },
]

// NAME=value / NAME: value where NAME looks secret. Catches keys no vendor rule knows (Upstash, Tigris…).
export const ASSIGNMENT = /\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|PWD|CREDENTIALS?))\s*[=:]\s*['"‘’“”]?([^\s'"‘’“”]{12,})['"‘’“”]?/g

const PLACEHOLDER = /^\$[A-Z_][A-Z0-9_]*$/

export function redact(input: string) {
  const found = new Map<string, string>()
  const nameFor = (base: string, value: string) => {
    for (const [n, v] of found) if (v === value) return n
    let name = base
    for (let i = 2; found.has(name); i += 1) name = `${base}_${i}`
    found.set(name, value)
    return name
  }
  let text = input.replace(ASSIGNMENT, (whole, key: string, value: string) =>
    PLACEHOLDER.test(value) || value.includes('$') ? whole : `${key}=$${nameFor(key, value)}`,
  )
  for (const rule of RULES) {
    text = text.replace(rule.pattern, value => `$${nameFor(rule.name, value)}`)
  }
  return { text, found }
}
