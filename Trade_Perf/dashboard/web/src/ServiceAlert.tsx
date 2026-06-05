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
