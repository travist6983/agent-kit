import type { ThemeSetArgs } from 'claude-code'

// The /mods glass look: a dark backdrop of slow colored light that the desktop app frosts its
// panels over (the Mods pane among them). The mobile app lays one tinted veil over it. The
// terminal draws no look. Orbs sit near the corners so the middle, where the words are, stays calm.
export const GLASS_SCENE_ID = 'mods-glass'

export const SCENE = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 1000" preserveAspectRatio="xMidYMid slice">
<style>
.a { animation: driftA 60s ease-in-out infinite alternate; }
.b { animation: driftB 75s ease-in-out infinite alternate; }
.c { animation: driftC 90s ease-in-out infinite alternate; }
@keyframes driftA { from { transform: translate(0px, 0px); } to { transform: translate(160px, 90px); } }
@keyframes driftB { from { transform: translate(0px, 0px); } to { transform: translate(-180px, -70px); } }
@keyframes driftC { from { transform: translate(0px, 0px); } to { transform: translate(120px, -110px); } }
</style>
<defs>
<radialGradient id="orange" cx="50%" cy="50%" r="50%"><stop offset="0%" stop-color="#ff9f0a" stop-opacity="0.85"/><stop offset="100%" stop-color="#ff9f0a" stop-opacity="0"/></radialGradient>
<radialGradient id="blue" cx="50%" cy="50%" r="50%"><stop offset="0%" stop-color="#0a84ff" stop-opacity="0.7"/><stop offset="100%" stop-color="#0a84ff" stop-opacity="0"/></radialGradient>
<radialGradient id="violet" cx="50%" cy="50%" r="50%"><stop offset="0%" stop-color="#bf5af2" stop-opacity="0.6"/><stop offset="100%" stop-color="#bf5af2" stop-opacity="0"/></radialGradient>
</defs>
<rect width="1600" height="1000" fill="#0b0b10"/>
<circle class="a" cx="240" cy="180" r="520" fill="url(#orange)"/>
<circle class="b" cx="1400" cy="860" r="560" fill="url(#blue)"/>
<circle class="c" cx="1360" cy="120" r="420" fill="url(#violet)"/>
</svg>`

export const GLASS_LOOK: ThemeSetArgs = {
  backdrop: { scene: GLASS_SCENE_ID, glass: { blur: 28, tint: '#16161c', opacity: 0.55 } },
  overrideColorScheme: 'dark',
}
