import type { SettingsAppearance } from '../api'
import { SETTINGS_THEME_KEY } from '../api'

// Maps Pydantic field -> CSS custom property. Centralized so Settings page +
// boot-time application can't drift.
const VAR_MAP: Record<string, string> = {
  accent: '--accent',
  bg:     '--bg',
  panel:  '--panel',
  border: '--border',
  text:   '--text',
  muted:  '--muted',
  pos:    '--pos',
  neg:    '--neg',
}

export function applyAppearance(a: SettingsAppearance): void {
  const root = document.documentElement
  for (const [key, cssVar] of Object.entries(VAR_MAP)) {
    const v = a[key as keyof SettingsAppearance]
    if (typeof v === 'string') root.style.setProperty(cssVar, v)
  }
  // 'pnl-pos' and 'pnl-neg' utility colors derive from pos/neg.
  root.style.setProperty('--ok', a.pos)
  // Theme attribute lets future light/dark-specific rules hook in.
  root.setAttribute('data-theme', a.theme)
}

// Cache the full appearance object in localStorage so the SPA can paint the
// correct colors immediately on next load, before the GET /api/settings round
// trip completes. Without this you'd see a brief flash of defaults.
export function cacheAppearance(a: SettingsAppearance): void {
  try {
    localStorage.setItem(SETTINGS_THEME_KEY, JSON.stringify(a))
  } catch {
    // Quota or privacy mode -- non-fatal.
  }
}

export function loadCachedAppearance(): SettingsAppearance | null {
  try {
    const raw = localStorage.getItem(SETTINGS_THEME_KEY)
    return raw ? (JSON.parse(raw) as SettingsAppearance) : null
  } catch {
    return null
  }
}
