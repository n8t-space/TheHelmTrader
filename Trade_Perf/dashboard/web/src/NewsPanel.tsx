import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { fetchJSON, postJSON, type SettingsResp } from './api'

interface NewsEvent {
  time_utc: string
  currency: string
  impact: 'High' | 'Medium' | 'Low'
  title: string
  source: string
  sources?: string[]
  forecast?: string | null
  previous?: string | null
  actual?: string | null
}

interface SourceState {
  ok: boolean
  error: string | null
  last_refresh: string | null
  count: number
}

interface NewsResp {
  enabled: boolean
  trading_day: string
  events: NewsEvent[]
  total_cached: number
  filtered_count: number
  sources: Record<string, SourceState>
  fetched_at: string | null
  ai_required: boolean
  ai_ok: boolean
  ai_error: string | null
  filters: { impact: string[]; currency: string[] }
}

const fmtAge = (iso: string | null): string => {
  if (!iso) return 'never'
  const ageS = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000))
  if (ageS < 60)    return `${ageS}s ago`
  if (ageS < 3600)  return `${Math.floor(ageS / 60)}m ago`
  if (ageS < 86400) return `${Math.floor(ageS / 3600)}h ago`
  return `${Math.floor(ageS / 86400)}d ago`
}

const TIME_FMT = new Intl.DateTimeFormat('en-US', {
  hour: '2-digit', minute: '2-digit', hour12: false,
  timeZone: 'America/Chicago',
})
const fmtTime = (iso: string) => {
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  return TIME_FMT.format(d) + ' CT'
}

export function NewsCard() {
  const qc = useQueryClient()
  const settings = useQuery<SettingsResp>({
    queryKey: ['settings'],
    queryFn:  () => fetchJSON<SettingsResp>('/api/settings'),
    staleTime: 60_000,
  })

  const q = useQuery<NewsResp>({
    queryKey: ['news-today'],
    queryFn:  () => fetchJSON<NewsResp>('/api/news/today'),
    refetchInterval: 5 * 60 * 1000,
    staleTime: 60 * 1000,
  })

  const refresh = useMutation({
    mutationFn: () => postJSON<unknown>('/api/news/refresh'),
    onSuccess:  () => qc.invalidateQueries({ queryKey: ['news-today'] }),
  })

  const newsEnabled = settings.data?.settings.news?.enabled ?? true
  if (!newsEnabled) return null

  if (q.isLoading) {
    return <div className="card news-card"><h2>Economic Calendar</h2><div className="subtle">Loading...</div></div>
  }
  if (q.error || !q.data) {
    return (
      <div className="card news-card error">
        <h2>Economic Calendar</h2>
        <div>{String(q.error)}</div>
      </div>
    )
  }

  const d = q.data

  return (
    <div className="card news-card">
      <div className="news-head">
        <h2>Economic Calendar <span className="subtle news-day">{d.trading_day}</span></h2>
        <div className="news-actions">
          <span className="subtle">
            updated {fmtAge(d.fetched_at)} - {d.filtered_count} of {d.total_cached} shown
          </span>
          <button
            type="button"
            className="news-refresh-btn"
            onClick={() => refresh.mutate()}
            disabled={refresh.isPending}
          >
            {refresh.isPending ? 'Refreshing...' : 'Refresh'}
          </button>
        </div>
      </div>

      <NewsSourceBar sources={d.sources} aiRequired={d.ai_required} aiOk={d.ai_ok} aiError={d.ai_error} />

      {d.events.length === 0 ? (
        <div className="news-empty subtle">
          No events match the current filters for this CME session.{' '}
          <Link to="/settings">Adjust impact / currency filters</Link>
        </div>
      ) : (
        <table className="news-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Impact</th>
              <th>Ccy</th>
              <th>Event</th>
              <th>Forecast</th>
              <th>Previous</th>
              <th>Actual</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {d.events.map((e, i) => (
              <tr key={`${e.time_utc}-${i}`} className={`news-row impact-${e.impact.toLowerCase()}`}>
                <td className="news-time">{fmtTime(e.time_utc)}</td>
                <td><ImpactBadge impact={e.impact} /></td>
                <td className="news-ccy">{e.currency}</td>
                <td className="news-title">{e.title}</td>
                <td className="news-num">{e.forecast || '-'}</td>
                <td className="news-num">{e.previous || '-'}</td>
                <td className="news-num news-actual">{e.actual || '-'}</td>
                <td className="subtle news-source">{(e.sources ?? [e.source]).join(', ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function NewsSourceBar({ sources, aiRequired, aiOk, aiError }: {
  sources: Record<string, SourceState>
  aiRequired: boolean
  aiOk: boolean
  aiError: string | null
}) {
  // One chip per configured source (Item 7), keyed by the source name the
  // backend status map uses.
  const items: Array<{ label: string; ok: boolean; detail: string }> =
    Object.entries(sources).map(([name, st]) => ({
      label: name,
      ok: st.ok,
      detail: st.ok ? `${st.count} events` : (st.error || 'unavailable'),
    }))
  return (
    <div className="news-sources">
      {items.map((it) => (
        <span key={it.label} className={'news-source-chip ' + (it.ok ? 'ok' : 'off')}>
          <span className="news-source-name">{it.label}</span>
          <span className="news-source-detail">{it.detail}</span>
        </span>
      ))}
      {aiRequired && !aiOk && (
        <Link to="/settings" className="news-ai-cta">
          AI not reachable: {aiError || 'unknown'} - <strong>Configure -&gt;</strong>
        </Link>
      )}
    </div>
  )
}

function ImpactBadge({ impact }: { impact: NewsEvent['impact'] }) {
  return <span className={'impact-badge impact-' + impact.toLowerCase()}>{impact}</span>
}
