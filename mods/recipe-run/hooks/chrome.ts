// The styled pane's palette and the few drawings no element can make: the coloured bar on top of
// a card, a hairline divider and the burn meter's segmented bars. Each is an SVG with nothing to
// press in it; every control stays a real Button laid out beside it. The terminal draws no SVG.

// Copied from mods-panel's chrome.ts (each mod is self-contained), with its two types defined here.
export type Section = 'look' | 'live' | 'idle' | 'off'
export type Pill = { text: string; tone: 'warn' | 'live' | 'attn' }

export const C = {
  pane: '#17181C',
  card: '#1C1F26',
  line: '#343944',
  divider: '#2C3039',
  inset: '#22262F',
  name: '#F4F4F5',
  dim: '#9C9CA6',
  missing: '#4A4F5B',
  green: '#8FD39A',
  amber: '#F0C46C',
  orange: '#E8792F',
  tan: '#C9A882',
  blue: '#8DBBF5',
  dots: '#6B5A44',
} as const

export const SECTION_COLOR: Record<Section, string> = { look: '#F0C46C', live: '#6FA8F0', idle: '#6CC57C', off: '#E58A8A' }
export const SECTION_TITLE: Record<Section, string> = { look: 'Needs a look', live: 'Live', idle: 'Idle', off: 'Off or not installed' }
export const PILL: Record<Pill['tone'], { fg: string; bg: string }> = {
  warn: { fg: '#F0C46C', bg: '#3A2E14' },
  live: { fg: '#8DBBF5', bg: '#1D2C44' },
  attn: { fg: '#F29A55', bg: '#3A2A16' },
}

// Wide markup stretched to the slot: the Svg gets a height and no width, so it spans its box.
export function barSvg(color: string, height: number, radius = 0) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="2000" height="${height}" viewBox="0 0 2000 ${height}" preserveAspectRatio="none"><rect x="0" y="0" width="2000" height="${height}" rx="${radius}" fill="${color}"/></svg>`
}

export const BURN_BLOCKS = 32

// burn-meter's segmented bar: 14px blocks with 1px gaps, tan (orange past 80%), and a dot
// texture where it is empty. Natural size 479x16; it scales down to a narrower slot.
export function burnSvg(pct: number, isHot: boolean, n = BURN_BLOCKS) {
  const step = 15
  const w = n * step - 1
  const on = Math.max(0, Math.min(n, Math.round((pct / 100) * n)))
  const fill = isHot ? C.orange : C.tan
  const blocks = Array.from({ length: on }, (_, i) => `<rect x="${i * step}" y="0" width="14" height="16" fill="${fill}"/>`).join('')
  const from = on * step
  const empty = from < w ? `<rect x="${from}" y="0" width="${w - from}" height="16" fill="url(#dots)"/>` : ''
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="16" viewBox="0 0 ${w} 16"><defs><pattern id="dots" width="4" height="4" patternUnits="userSpaceOnUse"><rect x="1" y="1" width="1" height="1" fill="${C.dots}"/></pattern></defs>${empty}${blocks}</svg>`
}
