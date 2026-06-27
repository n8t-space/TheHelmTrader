import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import {
  AUTO_ANALYSIS_MAX_SLOTS,
  AUTO_ANALYSIS_PERIODS,
  fetchJSON,
  putJSON,
  type AutoAnalysisConfigResp,
  type AutoAnalysisEntry,
  type AutoAnalysisStatusResp,
} from '../api'
import { NewsCard } from '../NewsPanel'

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
  cumulative_earnings: {
    live: number
    evals: number
    paid: number
    simulation: number
    signals: number
  }
  session_calendar: SessionDay[]
}

type CalCat = 'live' | 'evals' | 'paid' | 'simulation'

interface CalCell { net_pnl: number; trade_count: number }

interface SessionDay {
  date: string  // YYYY-MM-DD (trading day, CME 5 PM CT roll)
  by_category: Partial<Record<CalCat, CalCell>>
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
      <NewsCard />
      <div className="grid">
        <TodayCard t={d.today} />
        <CumulativeEarningsCard e={d.cumulative_earnings} />
        <AutoAnalysisCard />
      </div>
      <SessionCalendarCard days={d.session_calendar} />
    </>
  )
}

const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]
const WEEKDAY_LABELS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

const fmtCompactMoney = (n: number) => {
  const abs = Math.abs(n)
  const sign = n < 0 ? '-' : ''
  if (abs >= 1000) return `${sign}$${(abs / 1000).toFixed(abs >= 10000 ? 0 : 1)}k`
  return `${sign}$${abs.toFixed(0)}`
}

const CAL_FILTERS: { k: CalCat; label: string }[] = [
  { k: 'live', label: 'Live' },
  { k: 'evals', label: 'Eval' },
  { k: 'paid', label: 'PA' },
  { k: 'simulation', label: 'Sim' },
]

// Sum a day's P&L + trade count across the selected account-type buckets.
function sumDay(sd: SessionDay | undefined, sel: Set<CalCat>): { net: number; count: number; has: boolean } {
  let net = 0, count = 0, has = false
  if (sd) {
    for (const k of sel) {
      const cell = sd.by_category[k]
      if (cell) { net += cell.net_pnl; count += cell.trade_count; has = true }
    }
  }
  return { net, count, has }
}

function SessionCalendarCard({ days }: { days: SessionDay[] }) {
  const byDate = new Map(days.map(d => [d.date, d]))

  // Account-type filter. Default = real money: Live + PA.
  const [selected, setSelected] = useState<Set<CalCat>>(() => new Set<CalCat>(['live', 'paid']))
  const toggleCat = (k: CalCat) =>
    setSelected(s => { const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n })

  // Default the view to the most recent month that has a session; fall back
  // to the current calendar month when there's no history yet.
  const latest = days.length ? days[days.length - 1].date : null
  const initial = latest ? latest.slice(0, 7) : null
  const fallback = (() => {
    const now = new Date()
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
  })()
  const [ym, setYm] = useState<string>(initial ?? fallback)

  const [year, month] = ym.split('-').map(Number)  // month is 1-based
  const firstDow = new Date(year, month - 1, 1).getDay()
  const daysInMonth = new Date(year, month, 0).getDate()

  const isoOf = (day: number) =>
    `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`

  const shiftMonth = (delta: number) => {
    const dt = new Date(year, month - 1 + delta, 1)
    setYm(`${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}`)
  }

  const monthTotal = days
    .filter(d => d.date.startsWith(ym))
    .reduce((acc, d) => acc + sumDay(d, selected).net, 0)

  return (
    <div className="card calendar-card">
      <div className="calendar-head">
        <h2>Session Results</h2>
        <div className="calendar-filter">
          {CAL_FILTERS.map(f => (
            <button
              key={f.k}
              type="button"
              className={'calendar-filter-btn' + (selected.has(f.k) ? ' on' : '')}
              onClick={() => toggleCat(f.k)}
              aria-pressed={selected.has(f.k)}
            >{f.label}</button>
          ))}
        </div>
        <div className="calendar-nav">
          <button type="button" onClick={() => shiftMonth(-1)} aria-label="Previous month">&lsaquo;</button>
          <span className="calendar-title">{MONTH_NAMES[month - 1]} {year}</span>
          <button type="button" onClick={() => shiftMonth(1)} aria-label="Next month">&rsaquo;</button>
        </div>
        <span className={`calendar-total ${pnlClass(monthTotal)}`}>{fmtMoney(monthTotal)}</span>
      </div>
      <div className="calendar-grid">
        {WEEKDAY_LABELS.map(w => (
          <div key={w} className="calendar-weekday">{w}</div>
        ))}
        {Array.from({ length: firstDow }).map((_, i) => (
          <div key={`pad-${i}`} className="calendar-cell calendar-empty" />
        ))}
        {Array.from({ length: daysInMonth }).map((_, idx) => {
          const day = idx + 1
          const iso = isoOf(day)
          const { net, count, has } = sumDay(byDate.get(iso), selected)
          const tone = !has ? '' : net > 0 ? 'win' : net < 0 ? 'loss' : 'flat'
          return (
            <div
              key={iso}
              className={`calendar-cell ${tone}`}
              title={has ? `${iso} · ${count} trade${count === 1 ? '' : 's'} · ${fmtMoney(net)}` : iso}
            >
              <span className="calendar-daynum">{day}</span>
              {has && <span className="calendar-amount">{fmtCompactMoney(net)}</span>}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function TodayCard({ t }: { t: HomeData['today'] }) {
  // Trades-only on Home. Signal KPI lives on the Signal Analysis page.
  // Label reflects CME session attribution (5 PM CT roll) -- t.date is the
  // current trading day, NOT the wall-clock calendar date.
  return (
    <div className="card">
      <h2>Current CME Session · {t.date}</h2>
      <div className="big">
        <span className={pnlClass(t.trade_pnl)}>{fmtMoney(t.trade_pnl)}</span>
        <span className="big-sub"> net (NT fills)</span>
      </div>
      <div className="kv"><span>Trade count</span><span>{t.trade_count}</span></div>
    </div>
  )
}

function CumulativeEarningsCard({ e }: { e: HomeData['cumulative_earnings'] }) {
  const total = e.live + e.evals + e.paid + e.simulation + e.signals
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
        <span>Paid (PA)</span>
        <span className={pnlClass(e.paid)}>{fmtMoney(e.paid)}</span>
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


