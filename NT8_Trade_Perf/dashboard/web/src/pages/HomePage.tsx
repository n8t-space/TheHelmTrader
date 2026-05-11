import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import {
  AUTO_ANALYSIS_MAX_SLOTS,
  AUTO_ANALYSIS_PERIODS,
  fetchJSON,
  putJSON,
  type AutoAnalysisConfigResp,
  type AutoAnalysisEntry,
  type AutoAnalysisStatusResp,
  type Signal,
} from '../api'

interface ActionItem {
  timestamp: string
  instrument: string
  result?: string
  source_signal_ts?: string
  confidence?: number
  floor?: number
}

interface HomeData {
  today: {
    date: string
    signal_count: number
    realized_pnl: number
    win_count: number
    loss_count: number
    instruments: string[]
    trade_count: number
    trade_pnl: number
  }
  action_queue: {
    pending_suggestions: ActionItem[]
    below_floor: ActionItem[]
    missing_journal: ActionItem[]
    total: number
  }
  cumulative_earnings: {
    live: number
    evals: number
    simulation: number
    signals: number
  }
  last_signal: Signal | null
  equity_curve: Array<{ date: string; daily_pnl: number; cumulative_pnl: number }>
}

const fmtMoney = (n: number) =>
  `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
const pnlClass = (n: number) => (n > 0 ? 'pnl-pos' : n < 0 ? 'pnl-neg' : '')

export function HomePage() {
  const q = useQuery<HomeData>({
    queryKey: ['home'],
    queryFn: () => fetchJSON<HomeData>('/api/home'),
  })

  if (q.isLoading) return <div className="card">Loading…</div>
  if (q.error) return <div className="card error">{String(q.error)}</div>
  if (!q.data) return <div className="card">No data.</div>
  const d = q.data

  return (
    <>
      <div className="grid">
        <TodayCard t={d.today} />
        <CumulativeEarningsCard e={d.cumulative_earnings} />
        <ActionQueueCard q={d.action_queue} />
        <AutoAnalysisCard />
      </div>
      <EquityCurveCard data={d.equity_curve} />
      <LastSignalCard signal={d.last_signal} />
    </>
  )
}

function TodayCard({ t }: { t: HomeData['today'] }) {
  return (
    <div className="card">
      <h2>Today · {t.date}</h2>
      <div className="big">
        <span className={pnlClass(t.realized_pnl)}>{fmtMoney(t.realized_pnl)}</span>
        <span className="big-sub"> realized (signals)</span>
      </div>
      <div className="kv"><span>Signals captured</span><span>{t.signal_count}</span></div>
      <div className="kv"><span>Resolved (W/L)</span><span>{t.win_count}W / {t.loss_count}L</span></div>
      <div className="kv"><span>Instruments</span><span>{t.instruments.join(', ') || '—'}</span></div>
      <div className="kv">
        <span>NT trades</span>
        <span>
          {t.trade_count} · <span className={pnlClass(t.trade_pnl)}>{fmtMoney(t.trade_pnl)}</span>
        </span>
      </div>
    </div>
  )
}

function ActionQueueCard({ q }: { q: HomeData['action_queue'] }) {
  if (q.total === 0) {
    return (
      <div className="card">
        <h2>Action Queue</h2>
        <p className="subtle">All clear — nothing waiting on you.</p>
      </div>
    )
  }
  return (
    <div className="card">
      <h2>Action Queue ({q.total})</h2>
      {q.pending_suggestions.length > 0 && (
        <ActionGroup
          title="Pending reconciliations"
          items={q.pending_suggestions}
          render={(s) => (
            <Link to={`/signals/${encodeURIComponent(s.source_signal_ts || s.timestamp)}`}>
              {s.timestamp.slice(0, 10)} {s.instrument} → {s.result}
            </Link>
          )}
        />
      )}
      {q.below_floor.length > 0 && (
        <ActionGroup
          title="Below confidence floor"
          items={q.below_floor}
          render={(s) => (
            <Link to={`/signals/${encodeURIComponent(s.timestamp)}`}>
              {s.timestamp.slice(0, 10)} {s.instrument} ·{' '}
              {s.confidence !== undefined ? `${(s.confidence * 100).toFixed(0)}%` : '—'}
              {s.floor !== undefined && (
                <span className="subtle"> (floor {(s.floor * 100).toFixed(0)}%)</span>
              )}
            </Link>
          )}
        />
      )}
      {q.missing_journal.length > 0 && (
        <ActionGroup
          title="Needs journal entry"
          items={q.missing_journal}
          render={(s) => (
            <Link to={`/signals/${encodeURIComponent(s.timestamp)}`}>
              {s.timestamp.slice(0, 10)} {s.instrument}
            </Link>
          )}
        />
      )}
    </div>
  )
}

function ActionGroup({
  title, items, render,
}: {
  title: string
  items: ActionItem[]
  render: (s: ActionItem) => React.ReactNode
}) {
  return (
    <div className="action-group">
      <h3>{title} ({items.length})</h3>
      <ul>
        {items.slice(0, 5).map((s) => <li key={s.timestamp}>{render(s)}</li>)}
        {items.length > 5 && <li className="subtle">+ {items.length - 5} more</li>}
      </ul>
    </div>
  )
}

function CumulativeEarningsCard({ e }: { e: HomeData['cumulative_earnings'] }) {
  const total = e.live + e.evals + e.simulation + e.signals
  return (
    <div className="card">
      <h2>Cumulative Earnings</h2>
      <div className="big">
        <span className={pnlClass(total)}>{fmtMoney(total)}</span>
        <span className="big-sub"> total</span>
      </div>
      <div className="kv">
        <span>Live</span>
        <span className={pnlClass(e.live)}>{fmtMoney(e.live)}</span>
      </div>
      <div className="kv">
        <span>Evals</span>
        <span className={pnlClass(e.evals)}>{fmtMoney(e.evals)}</span>
      </div>
      <div className="kv">
        <span>Simulation</span>
        <span className={pnlClass(e.simulation)}>{fmtMoney(e.simulation)}</span>
      </div>
      <div className="kv">
        <span>Signals</span>
        <span className={pnlClass(e.signals)}>{fmtMoney(e.signals)}</span>
      </div>
    </div>
  )
}

function emptyAutoAnalysisSlots(): AutoAnalysisEntry[] {
  return Array.from({ length: AUTO_ANALYSIS_MAX_SLOTS }, () => ({
    instrument: '', period: '5m', enabled: false,
  }))
}

function padAutoAnalysisToSlots(entries: AutoAnalysisEntry[]): AutoAnalysisEntry[] {
  const slots = emptyAutoAnalysisSlots()
  for (let i = 0; i < Math.min(entries.length, AUTO_ANALYSIS_MAX_SLOTS); i++) {
    slots[i] = { ...entries[i] }
  }
  return slots
}

function AutoAnalysisCard() {
  const qc = useQueryClient()
  const config = useQuery<AutoAnalysisConfigResp>({
    queryKey: ['auto-analysis-config'],
    queryFn: () => fetchJSON<AutoAnalysisConfigResp>('/api/auto-analysis/config'),
    refetchInterval: false,  // edited locally, not auto-refresh
  })
  const status = useQuery<AutoAnalysisStatusResp>({
    queryKey: ['auto-analysis-status'],
    queryFn: () => fetchJSON<AutoAnalysisStatusResp>('/api/auto-analysis/status'),
    refetchInterval: 5000,
  })

  const [draft, setDraft] = useState<AutoAnalysisEntry[]>(emptyAutoAnalysisSlots)
  const [error, setError] = useState<string | null>(null)
  const [savedFlash, setSavedFlash] = useState(false)

  useEffect(() => {
    if (config.data) setDraft(padAutoAnalysisToSlots(config.data.entries))
  }, [config.data])

  const armedCount = draft.filter(e => e.enabled && e.instrument.trim() !== '').length

  const save = useMutation({
    mutationFn: () => putJSON<AutoAnalysisConfigResp>('/api/auto-analysis/config', {
      entries: draft
        .map(e => ({ ...e, instrument: e.instrument.trim().toUpperCase() }))
        .filter(e => e.instrument !== ''),
    }),
    onSuccess: () => {
      setError(null)
      setSavedFlash(true)
      window.setTimeout(() => setSavedFlash(false), 1500)
      qc.invalidateQueries({ queryKey: ['auto-analysis-config'] })
    },
    onError: (e: Error) => setError(e.message),
  })

  const updateSlot = (i: number, patch: Partial<AutoAnalysisEntry>) => {
    setDraft(prev => prev.map((e, idx) => idx === i ? { ...e, ...patch } : e))
  }

  return (
    <div className="card">
      <h2>Auto Analysis</h2>
      <p className="subtle" style={{ marginTop: -4 }}>
        Up to {AUTO_ANALYSIS_MAX_SLOTS} (instrument, period) pairs analyzed automatically on each bar close.
      </p>
      <table className="auto-analysis-table">
        <tbody>
          {draft.map((e, i) => (
            <tr key={i}>
              <td>
                <input
                  type="text"
                  placeholder="instrument"
                  value={e.instrument}
                  onChange={ev => updateSlot(i, { instrument: ev.target.value.toUpperCase() })}
                  maxLength={16}
                />
              </td>
              <td>
                <select
                  value={e.period}
                  onChange={ev => updateSlot(i, { period: ev.target.value })}
                >
                  {AUTO_ANALYSIS_PERIODS.map(p => <option key={p} value={p}>{p}</option>)}
                </select>
              </td>
              <td className="auto-analysis-toggle">
                <label>
                  <input
                    type="checkbox"
                    checked={e.enabled}
                    onChange={ev => updateSlot(i, { enabled: ev.target.checked })}
                  />
                  on
                </label>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="auto-analysis-actions">
        <button
          onClick={() => save.mutate()}
          disabled={save.isPending || armedCount > AUTO_ANALYSIS_MAX_SLOTS}
        >
          {save.isPending ? 'Saving…' : 'Save'}
        </button>
        {savedFlash && <span className="subtle">Saved.</span>}
        {error && <span className="error">{error}</span>}
      </div>
      <p className="subtle">
        Worker {status.data?.worker_alive ? 'alive' : 'idle'}
        {' · '}Queue {status.data?.queue_size ?? 0}
        {' · '}Runs {status.data?.run_count ?? 0}
        {status.data?.last_run && (
          <>{' · last '}{status.data.last_run.instrument} {status.data.last_run.period}</>
        )}
      </p>
    </div>
  )
}

function EquityCurveCard({ data }: { data: HomeData['equity_curve'] }) {
  const fmtDate = (d: string) => d.slice(5)
  return (
    <div className="card">
      <h2>30-day Equity Curve</h2>
      <ResponsiveContainer width="100%" height={220}>
        <LineChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
          <XAxis dataKey="date" tickFormatter={fmtDate} stroke="#7d8590" fontSize={11} />
          <YAxis
            stroke="#7d8590"
            fontSize={11}
            tickFormatter={(v) => `$${v}`}
            width={60}
          />
          <Tooltip
            contentStyle={{
              background: '#161b22',
              border: '1px solid #30363d',
              borderRadius: 4,
              fontSize: 12,
            }}
            labelStyle={{ color: '#7d8590' }}
            formatter={(v) => (typeof v === 'number' ? fmtMoney(v) : String(v))}
          />
          <Line
            type="monotone"
            dataKey="cumulative_pnl"
            stroke="#58a6ff"
            strokeWidth={2}
            dot={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

function LastSignalCard({ signal }: { signal: Signal | null }) {
  if (!signal) {
    return (
      <div className="card">
        <h2>Last Signal</h2>
        <p className="subtle">
          No signals yet. Capture one with <Link to="/signals">Snip & Analyze</Link>.
        </p>
      </div>
    )
  }
  const p = signal.proposal
  const autoGen = signal.trigger === 'headless'
  const autoRes =
    signal.outcome?.result !== undefined &&
    signal.outcome?.result !== null &&
    signal.outcome_suggestion?.engine === 'resolver'
  return (
    <div className="card">
      <h2>
        Last Signal
        {autoGen && <span className="badge auto-gen" title="Auto-generated by headless analyzer">AUTO-GEN</span>}
        {autoRes && <span className="badge auto-res" title="Outcome auto-resolved by feed.db walker">AUTO-RES</span>}
      </h2>
      <p>
        <Link to={`/signals/${encodeURIComponent(signal.timestamp)}`}>{signal.timestamp}</Link>
        {' · '}
        <strong className={`dir-${p.direction}`}>{p.direction.toUpperCase()}</strong>
        {' '}{p.instrument}
      </p>
      <div className="kv">
        <span>Source</span>
        <span>{autoGen ? 'Auto (headless)' : 'Manual snip'}</span>
      </div>
      <div className="kv">
        <span>Entry → Stop → Target</span>
        <span>{p.entry} → {p.stop} → {p.target}</span>
      </div>
      <div className="kv">
        <span>R:R · Confidence</span>
        <span>{p.risk_reward.toFixed(2)} · {(p.confidence * 100).toFixed(0)}%</span>
      </div>
      <div className="kv">
        <span>Verdict · Outcome</span>
        <span>
          {signal.journal?.verdict ?? '—'} · {signal.outcome?.result ?? '—'}
          {autoRes && ' (auto)'}
        </span>
      </div>
    </div>
  )
}

