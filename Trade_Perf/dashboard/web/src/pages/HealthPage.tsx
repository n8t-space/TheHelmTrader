import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { fetchJSON, postJSON } from '../api'

interface BotStats {
  provider: 'ollama' | 'claude' | 'openai'
  configured_model: string
  configured_fallback: string | null
  ollama_url: string | null
  api_key_configured: boolean
  request_timeout_s: number
  last_used_model: string | null
  // Back-compat alias for last_used_model -- still emitted by the API.
  model: string | null
  sample_size: number
  latency_p50_s: number | null
  latency_p95_s: number | null
  latency_min_s: number | null
  latency_max_s: number | null
}

const PROVIDER_LABEL: Record<BotStats['provider'], string> = {
  ollama: 'Ollama (local / LAN)',
  claude: 'Anthropic Claude (cloud)',
  openai: 'OpenAI / ChatGPT (cloud)',
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
        <KillSwitchCard />
      </div>
      <LogsViewer />
    </>
  )
}

function KillSwitchCard() {
  const [state, setState] = useState<'idle' | 'killing' | 'down'>('idle')
  const [error, setError] = useState<string | null>(null)

  const kill = async () => {
    if (!window.confirm(
      'Stop the Helm dashboard now?\n\n'
      + 'It will go offline within ~5 seconds and STAY down until you restart '
      + 'NinjaTrader (or restart the Helm service). This page will become '
      + 'unreachable.'
    )) return
    setState('killing')
    setError(null)
    try {
      await postJSON('/api/control/kill')
      setState('down')
    } catch (e) {
      setError(String(e))
      setState('idle')
    }
  }

  return (
    <div className="card">
      <h2>Service Control</h2>
      <p className="subtle" style={{ marginTop: -4 }}>
        Stop the Helm dashboard for the rest of this NinjaTrader session. The
        watchdog keeps it down until NinjaTrader restarts or the Helm service is
        restarted, then brings it back automatically.
      </p>
      {state === 'down' ? (
        <p className="pnl-neg">
          Kill switch armed — the dashboard is shutting down. This page will stop
          responding shortly. Restart NinjaTrader or the Helm service to bring it back.
        </p>
      ) : (
        <button type="button" className="btn-danger" onClick={kill} disabled={state === 'killing'}>
          {state === 'killing' ? 'Stopping…' : 'Kill Helm until NT restart'}
        </button>
      )}
      {error && <p className="error">{error}</p>}
    </div>
  )
}

function BotHealthCard({ h, loading }: { h: BotStats | null; loading: boolean }) {
  if (loading || !h) {
    return (
      <div className="card">
        <h2>Bot Health</h2>
        <div className="subtle">Loading…</div>
      </div>
    )
  }

  // Divergence hint: when the last actually-captured signal ran on a
  // different model than what Settings now points at, surface it so the
  // user knows the change hasn't taken effect on a real call yet.
  const diverged = h.last_used_model && h.configured_model
    && h.last_used_model !== h.configured_model

  const needsKey  = (h.provider === 'claude' || h.provider === 'openai') && !h.api_key_configured

  return (
    <div className="card">
      <h2>Bot Health</h2>
      <div className="kv">
        <span>Provider</span>
        <span>{PROVIDER_LABEL[h.provider]}</span>
      </div>
      <div className="kv">
        <span>Model (configured)</span>
        <span>
          {h.configured_model || <em className="subtle">none</em>}
          {' '}<Link to="/settings" className="subtle" title="Edit on the Settings &gt; AI Backend tab">(edit)</Link>
        </span>
      </div>
      {h.configured_fallback && (
        <div className="kv">
          <span>Fallback model</span>
          <span>{h.configured_fallback}</span>
        </div>
      )}
      {h.ollama_url && (
        <div className="kv">
          <span>Ollama URL</span>
          <span><code>{h.ollama_url}</code></span>
        </div>
      )}
      {(h.provider === 'claude' || h.provider === 'openai') && (
        <div className="kv">
          <span>API key</span>
          <span className={needsKey ? 'pnl-neg' : 'ok'}>
            {needsKey ? 'NOT SET' : 'configured'}
          </span>
        </div>
      )}
      <div className="kv">
        <span>Request timeout</span>
        <span>{h.request_timeout_s} s</span>
      </div>
      <div className="kv">
        <span>Last used model</span>
        <span>
          {h.last_used_model || <em className="subtle">no signals yet</em>}
          {diverged && (
            <>
              {' '}
              <span className="pnl-neg" title="Settings was changed -- next inference call will pick up the new model">
                (differs)
              </span>
            </>
          )}
        </span>
      </div>
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
