import { useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchJSON } from '../api'

interface BotStats {
  model: string | null
  sample_size: number
  latency_p50_s: number | null
  latency_p95_s: number | null
  latency_min_s: number | null
  latency_max_s: number | null
}

interface LogsResp {
  path: string
  total_lines: number
  lines: string[]
}

const fmtSec = (n: number | null) => (n === null ? '—' : `${n.toFixed(2)} s`)

export function HealthPage() {
  const stats = useQuery<BotStats>({
    queryKey: ['health-bot-stats'],
    queryFn: () => fetchJSON<BotStats>('/api/health/bot-stats'),
    refetchInterval: 10000,
  })

  return (
    <>
      <div className="grid">
        <BotHealthCard h={stats.data ?? null} loading={stats.isLoading} />
      </div>
      <LogsViewer />
    </>
  )
}

function BotHealthCard({ h, loading }: { h: BotStats | null; loading: boolean }) {
  return (
    <div className="card">
      <h2>Bot Health</h2>
      {loading || !h ? (
        <div className="subtle">Loading…</div>
      ) : (
        <>
          <div className="kv"><span>Model</span><span>{h.model || '—'}</span></div>
          <div className="kv"><span>Sample size</span><span>{h.sample_size} calls</span></div>
          <div className="kv"><span>Latency p50</span><span>{fmtSec(h.latency_p50_s)}</span></div>
          <div className="kv"><span>Latency p95</span><span>{fmtSec(h.latency_p95_s)}</span></div>
          <div className="kv">
            <span>Min / Max</span>
            <span>
              {h.latency_min_s !== null
                ? `${h.latency_min_s.toFixed(2)} / ${(h.latency_max_s ?? 0).toFixed(2)} s`
                : '—'}
            </span>
          </div>
        </>
      )}
    </div>
  )
}

function LogsViewer() {
  const q = useQuery<LogsResp>({
    queryKey: ['health-logs'],
    queryFn: () => fetchJSON<LogsResp>('/api/health/logs?lines=400'),
    refetchInterval: 3000,
  })
  const preRef = useRef<HTMLPreElement>(null)
  // Auto-scroll to bottom on new content if user is already near the bottom.
  useEffect(() => {
    const el = preRef.current
    if (!el) return
    const nearBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 80
    if (nearBottom) el.scrollTop = el.scrollHeight
  }, [q.data?.lines])

  return (
    <div className="card">
      <h2>Logs</h2>
      <p className="subtle">
        {q.data ? (
          <>
            Tailing <code>{q.data.path}</code> · last {q.data.lines.length} of {q.data.total_lines.toLocaleString()} lines · refreshing every 3 s
          </>
        ) : 'Loading log file…'}
        {q.error && <span className="error"> {String(q.error)}</span>}
      </p>
      <pre ref={preRef} className="log-viewer">
        {q.data?.lines.join('\n') ?? ''}
      </pre>
    </div>
  )
}
