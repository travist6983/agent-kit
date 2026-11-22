// The styled pane's palette and the one drawing no element can make: the coloured bar on top of
// a card (also used as a hairline divider). It is an SVG with nothing to press in it; every
// control stays a real Button laid out beside it. The terminal draws no SVG.
// Copied from mods-panel/hooks/chrome.ts: each mod is self-contained.

export type Section = 'recalled' | 'review' | 'lessons' | 'offline'
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
  red: '#E58A8A',
  dots: '#6B5A44',
} as const

export const SECTION_COLOR: Record<Section, string> = { recalled: '#6FA8F0', review: '#F0C46C', lessons: '#6CC57C', offline: '#E58A8A' }
export const SECTION_TITLE: Record<Section, string> = { recalled: 'Recalled', review: 'To review', lessons: 'Lessons', offline: 'Offline' }
export const PILL: Record<Pill['tone'], { fg: string; bg: string }> = {
  warn: { fg: '#F0C46C', bg: '#3A2E14' },
  live: { fg: '#8DBBF5', bg: '#1D2C44' },
  attn: { fg: '#F29A55', bg: '#3A2A16' },
}

// Wide markup stretched to the slot: the Svg gets a height and no width, so it spans its box.
export function barSvg(color: string, height: number, radius = 0) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="2000" height="${height}" viewBox="0 0 2000 ${height}" preserveAspectRatio="none"><rect x="0" y="0" width="2000" height="${height}" rx="${radius}" fill="${color}"/></svg>`
}
