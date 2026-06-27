import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { fetchJSON } from './api'

interface Service {
  name: string
  key: string
  reachable: boolean
  critical: boolean
  detail: string
}
interface ServicesResp {
  services: Service[]
  any_critical_down: boolean
}

interface Down { key: string; name: string; detail: string }

// --- Mute (client-side: the alert fires when the backend is unreachable, so
// the mute state must be readable WITHOUT the server -> localStorage). Value:
// epoch ms = muted until then; -1 = muted until manually turned back on;
// absent/0 = alerts on. ---
const MUTE_KEY = 'helm.serviceAlert.mutedUntil'
const MUTE_EVENT = 'helm-service-mute-changed'
export type MuteChoice = 'off' | 'indefinite' | number  // number = hours

export function getServiceMuteUntil(): number {
  try { return Number(localStorage.getItem(MUTE_KEY) || 0) } catch { return 0 }
}
export function isServiceAlertMuted(now = Date.now()): boolean {
  const u = getServiceMuteUntil()
  return u === -1 || (u > 0 && now < u)
}
export function setServiceAlertMute(choice: MuteChoice): void {
  let val = 0
  if (choice === 'indefinite') val = -1
  else if (choice === 'off') val = 0
  else val = Date.now() + choice * 3_600_000
  try {
    if (val === 0) localStorage.removeItem(MUTE_KEY)
    else localStorage.setItem(MUTE_KEY, String(val))
  } catch { /* ignore */ }
  window.dispatchEvent(new Event(MUTE_EVENT))
}
export function muteStatusLabel(): string {
  const u = getServiceMuteUntil()
  if (u === -1) return 'Muted until you turn it back on'
  if (u > Date.now()) return `Muted until ${new Date(u).toLocaleString()}`
  return 'Alerts on'
}

/**
 * Blocking modal when a critical backend service the bot depends on (the AI
 * inference backend) -- or the dashboard API itself -- is unreachable. Requires
 * the operator to acknowledge. Once dismissed it stays hidden until the service
 * recovers and then drops again, so it alerts on each NEW outage without nagging.
 */
export function ServiceAlert() {
  const q = useQuery<ServicesResp>({
    queryKey: ['services'],
    queryFn: () => fetchJSON<ServicesResp>('/api/health/services'),
    refetchInterval: 20_000,
    retry: false,
  })
  const [acked, setAcked] = useState<Set<string>>(new Set())
  // Re-render when the mute toggles (set from Settings) so the modal hides /
  // re-appears immediately, not just on the next 20s poll.
  const [, bumpMute] = useState(0)
  useEffect(() => {
    const h = () => bumpMute((x) => x + 1)
    window.addEventListener(MUTE_EVENT, h)
    return () => window.removeEventListener(MUTE_EVENT, h)
  }, [])

  // A thrown HTTP status ("404 Not Found", "500 ...") means the backend DID
  // respond -- e.g. an older build without this endpoint -- so it is NOT a
  // dashboard outage; only a network-level failure (no response) is.
  const errMsg = q.error instanceof Error ? q.error.message : ''
  const serverResponded = /^\d{3}\b/.test(errMsg)
  const down: Down[] = (q.isError && !serverResponded)
    ? [{ key: 'dashboard', name: 'Dashboard API', detail: 'the dashboard backend is not responding' }]
    : (q.data?.services ?? [])
        .filter((s) => s.critical && !s.reachable)
        .map((s) => ({ key: s.key, name: s.name, detail: s.detail }))

  const downKey = down.map((d) => d.key).sort().join(',')

  // A recovered service drops its ack, so a fresh outage re-alerts.
  useEffect(() => {
    setAcked((prev) => {
      const live = new Set(down.map((d) => d.key))
      const next = new Set([...prev].filter((k) => live.has(k)))
      return next.size === prev.size ? prev : next
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [downKey])

  const unacked = down.filter((d) => !acked.has(d.key))
  if (unacked.length === 0) return null
  // Suppressed by the operator's mute window (Settings -> Appearance).
  if (isServiceAlertMuted()) return null

  return (
    <div
      role="alertdialog"
      aria-modal="true"
      style={{
        position: 'fixed', inset: 0, zIndex: 9999,
        background: 'rgba(0,0,0,0.62)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 20,
      }}
    >
      <div
        style={{
          background: 'var(--panel, #161b22)',
          border: '1px solid var(--neg, #f85149)',
          borderRadius: 10,
          maxWidth: 460,
          width: '100%',
          padding: 24,
          boxShadow: '0 12px 40px rgba(0,0,0,0.5)',
          color: 'var(--text, #e6edf3)',
        }}
      >
        <h2 style={{ marginTop: 0, color: 'var(--neg, #f85149)' }}>
          &#9888; Service unreachable
        </h2>
        <p style={{ marginTop: 0 }}>
          The Helm can&rsquo;t reach a backend service it needs. <strong>Signal generation and
          auto-trading are affected</strong> until it recovers.
        </p>
        <ul style={{ margin: '12px 0', paddingLeft: 20 }}>
          {down.map((d) => (
            <li key={d.key} style={{ marginBottom: 4 }}>
              <strong>{d.name}</strong>
              <span className="subtle"> &mdash; {d.detail}</span>
            </li>
          ))}
        </ul>
        <p className="subtle" style={{ marginTop: 0 }}>
          Re-appears if a service drops again. The dashboard keeps checking every 20s.
        </p>
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 16 }}>
          <button className="primary" onClick={() => setAcked(new Set(down.map((d) => d.key)))}>
            I understand &mdash; dismiss
          </button>
        </div>
      </div>
    </div>
  )
}

/**
 * Settings control to mute the "Service unreachable" modal for a window (or
 * until manually re-enabled). Self-contained: reads/writes localStorage and
 * applies immediately (no Save needed) -- it must work even when the backend is
 * down, so it never touches the server settings doc.
 */
export function ServiceAlertMute() {
  const [, bump] = useState(0)
  const apply = (choice: MuteChoice) => { setServiceAlertMute(choice); bump((x) => x + 1) }
  const muted = isServiceAlertMuted()
  const options: { label: string; choice: MuteChoice }[] = [
    { label: '1 hour', choice: 1 },
    { label: '4 hours', choice: 4 },
    { label: '6 hours', choice: 6 },
    { label: 'Until I turn it back on', choice: 'indefinite' },
  ]
  return (
    <div className="settings-row" style={{ flexDirection: 'column', alignItems: 'flex-start', gap: 8 }}>
      <label style={{ display: 'block' }}>
        <span>Mute &ldquo;Service unreachable&rdquo; alert</span>
        <span className="subtle">
          Silences the blocking popup when a backend service (AI backend / dashboard API) drops.
          Applies instantly; stored in this browser so it works even while the service is down.
        </span>
      </label>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
        {options.map((o) => (
          <button key={String(o.choice)} type="button" className="quick-btn" onClick={() => apply(o.choice)}>
            Mute {o.label}
          </button>
        ))}
        <button type="button" className="quick-btn" onClick={() => apply('off')} disabled={!muted}>
          Unmute
        </button>
      </div>
      <span className={'subtle' + (muted ? ' pnl-neg' : '')}>{muteStatusLabel()}</span>
    </div>
  )
}
