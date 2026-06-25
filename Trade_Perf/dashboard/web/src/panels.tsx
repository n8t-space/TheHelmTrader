import React, { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  ACCOUNT_GROUPS, accountLabel, buildQuery, fetchJSON, EMPTY_FILTERS,
  type DimensionsResp,
  type Filters, type HealthResp, type SettingsResp, type StatsResp,
  type Trade, type TradesResp, type Fill, type FillsResp,
  type TaxEstimateResp, type MicroscalpResp,
} from './api'
import { arrow, flip, sortBy, type Sort } from './lib/sorting'
import { JournalEditor, useJournalKeys } from './Journal'

// ---------- Formatting helpers ----------

const fmtMoney = (n: number) => `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

// Render a UTC ISO timestamp in America/Chicago time (CST/CDT, DST-aware).
// Output: 'YYYY-MM-DD HH:mm:ss CST' (or CDT). Returns '—' for falsy input.
const CT_FMT = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/Chicago',
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
  hour12: false, timeZoneName: 'short',
})
const fmtTime = (iso: string | null | undefined): string => {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  const parts = CT_FMT.formatToParts(d)
  const get = (t: string) => parts.find(p => p.type === t)?.value ?? ''
  return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')}:${get('second')} ${get('timeZoneName')}`
}
const fmtDuration = (sec: number) => {
  if (sec < 60) return `${Math.round(sec)}s`
  if (sec < 3600) return `${(sec / 60).toFixed(1)}m`
  return `${(sec / 3600).toFixed(2)}h`
}

// ---------- StatusPanel ----------

export function StatusPanel() {
  const health = useQuery({ queryKey: ['health'], queryFn: () => fetchJSON<HealthResp>('/api/health') })
  const dims = useQuery({ queryKey: ['dimensions'], queryFn: () => fetchJSON<DimensionsResp>('/api/dimensions') })
  if (health.isLoading) return <div className="card">Connecting to API…</div>
  if (health.error) return <div className="card error">API unreachable: {String(health.error)}</div>
  return (
    <div className="card">
      <h2>Recorder Status</h2>
      <div className="kv"><span>Status</span><span className="ok">{health.data?.status}</span></div>
      <div className="kv"><span>Fills in trades.db</span><span>{health.data?.fills.toLocaleString()}</span></div>
      <div className="kv"><span>Symbols</span><span>{dims.data?.symbols.join(', ') || '—'}</span></div>
      <div className="kv"><span>First fill</span><span>{fmtTime(dims.data?.first_fill_time)}</span></div>
      <div className="kv"><span>Last fill</span><span>{fmtTime(dims.data?.last_fill_time)}</span></div>
    </div>
  )
}

// ---------- StatsPanel ----------

export function StatsPanel({ label, filters, extra = {} }: { label: string; filters: Filters; extra?: Record<string, string> }) {
  const qstr = buildQuery(filters, extra)
  const q = useQuery<StatsResp>({
    queryKey: ['stats', filters, extra],
    queryFn: () => fetchJSON<StatsResp>('/api/stats' + qstr),
  })
  if (q.isLoading) return <div className="card"><h2>{label}</h2><div>Loading…</div></div>
  if (q.error || !q.data) return <div className="card error"><h2>{label}</h2><div>{String(q.error)}</div></div>
  const s = q.data
  const pnlClass = s.net_pnl > 0 ? 'pnl-pos' : s.net_pnl < 0 ? 'pnl-neg' : ''
  return (
    <div className="card">
      <h2>{label}</h2>
      <div className="big">
        <span className={pnlClass}>${s.net_pnl.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
        <span className="big-sub"> net</span>
      </div>
      <div className="kv"><span>Trades</span><span>{s.trade_count}</span></div>
      <div className="kv"><span>Win rate</span><span>{(s.win_rate * 100).toFixed(1)}% ({s.win_count}W / {s.loss_count}L)</span></div>
      <div className="kv"><span>Gross P&amp;L</span><span>${s.gross_pnl.toFixed(2)}</span></div>
      <div className="kv"><span>Fees</span><span>${s.commissions_and_fees.toFixed(2)}</span></div>
      <div className="kv"><span>Best / Worst</span><span>${s.best_trade.toFixed(2)} / ${s.worst_trade.toFixed(2)}</span></div>
      <div className="kv"><span>Profit factor</span><span>{s.profit_factor?.toFixed(2) ?? '∞'}</span></div>
      <div className="kv"><span>Max drawdown</span><span>${s.max_drawdown.toFixed(2)}</span></div>
    </div>
  )
}

// ---------- FilterBar ----------

export function FilterBar({ filters, setFilters }: { filters: Filters; setFilters: (f: Filters) => void }) {
  const dims = useQuery({ queryKey: ['dimensions'], queryFn: () => fetchJSON<DimensionsResp>('/api/dimensions') })
  // Account groups come from live Settings (accounts.live / evals / simulation).
  // Falls back to the static ACCOUNT_GROUPS preset only when /api/settings is
  // unreachable — without this, the Live + Eval buttons are bound to the empty
  // arrays in the const and clicking them does nothing.
  const settings = useQuery({
    queryKey: ['settings'],
    queryFn: () => fetchJSON<SettingsResp>('/api/settings'),
    staleTime: 60_000,
  })
  const groups: Record<string, string[]> = settings.data
    ? {
        Live:       settings.data.settings.accounts.live,
        Eval:       settings.data.settings.accounts.evals,
        Simulation: settings.data.settings.accounts.simulation,
      }
    : ACCOUNT_GROUPS
  const names = settings.data?.settings.accounts.names
  const update = (patch: Partial<Filters>) => setFilters({ ...filters, ...patch })
  const cleared = JSON.stringify(filters) === JSON.stringify(EMPTY_FILTERS)
  const accounts = dims.data?.accounts ?? []
  const accountSet = new Set(filters.account)
  const toggleAccount = (a: string) => {
    const next = new Set(accountSet)
    if (next.has(a)) next.delete(a); else next.add(a)
    update({ account: Array.from(next) })
  }
  const setGroup = (members: string[]) => {
    // Empty group (no accounts configured for this bucket) -> no-op; clearing
    // the filter would mimic "All" and silently swallow the click. Honest
    // signal: button stays inactive + we don't change state.
    if (members.length === 0) return
    // Keep only members that actually exist in the recorder so the query
    // never carries dead IDs. If none exist, fall back to the raw group --
    // an empty result set is the honest answer.
    const present = members.filter((m) => accounts.includes(m))
    update({ account: present.length ? present : members })
  }
  const groupActive = (members: string[]) => {
    if (filters.account.length === 0 || members.length === 0) return false
    const set = new Set(filters.account)
    const present = members.filter((m) => accounts.includes(m))
    const ref = present.length ? present : members
    return ref.length === filters.account.length && ref.every((m) => set.has(m))
  }
  return (
    <div className="filter-bar card">
      <div className="filter-accounts">
        <div className="filter-account-quick">
          <span className="subtle">Accounts:</span>
          {Object.entries(groups).map(([label, members]) => (
            <button
              key={label}
              type="button"
              className={'quick-btn' + (groupActive(members) ? ' on' : '')}
              onClick={() => setGroup(members)}
              disabled={members.length === 0}
              title={members.length === 0
                ? `No accounts assigned to ${label} — configure on the Settings page.`
                : `Filter to ${label}: ${members.join(', ')}`}
            >
              {label}{members.length === 0 ? ' (0)' : ''}
            </button>
          ))}
          <button
            type="button"
            className={'quick-btn' + (filters.account.length === 0 ? ' on' : '')}
            onClick={() => update({ account: [] })}
          >
            All
          </button>
          <span className="subtle">
            {filters.account.length === 0
              ? `(all ${accounts.length})`
              : `(${filters.account.length} selected)`}
          </span>
        </div>
        <div className="filter-account-list">
          {accounts.map((a) => (
            <label key={a} className="account-chip">
              <input
                type="checkbox"
                checked={accountSet.has(a)}
                onChange={() => toggleAccount(a)}
              />
              {accountLabel(a, names)}
            </label>
          ))}
        </div>
      </div>
      <label>
        <span>Symbol</span>
        <select value={filters.symbol} onChange={(e) => update({ symbol: e.target.value })}>
          <option value="">All</option>
          {dims.data?.symbols.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </label>
      <label>
        <span>Strategy</span>
        <select value={filters.strategy} onChange={(e) => update({ strategy: e.target.value })}>
          <option value="">All</option>
          {dims.data?.strategies.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </label>
      <label>
        <span>From</span>
        <input type="date" value={filters.date_from.slice(0, 10)} onChange={(e) => update({ date_from: e.target.value })} />
      </label>
      <label>
        <span>To</span>
        <input type="date" value={filters.date_to.slice(0, 10)} onChange={(e) => update({ date_to: e.target.value })} />
      </label>
      <button onClick={() => setFilters(EMPTY_FILTERS)} disabled={cleared}>Clear</button>
    </div>
  )
}

// ---------- TradesTable ----------

type TradeKey = keyof Pick<Trade, 'entry_time' | 'account' | 'symbol' | 'direction' | 'qty' | 'entry_price' | 'exit_price' | 'net_pnl' | 'commission' | 'duration_seconds'>

export function TradesTable({ filters }: { filters: Filters }) {
  const [sort, setSort] = useState<Sort<TradeKey>>({ key: 'entry_time', dir: 'desc' })
  // Set of trade keys (first_fill_id-last_fill_id) whose scale-out detail
  // row is currently expanded. Scale-out trades default collapsed; click to
  // expand and see the per-leg fill breakdown.
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const toggleExpanded = (k: string) =>
    setExpanded((s) => { const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n })
  // Trade keys whose journal editor row is open. Independent of the scale-out
  // expand state so a trade can show legs and its journal at the same time.
  const [journalOpen, setJournalOpen] = useState<Set<string>>(new Set())
  const toggleJournal = (k: string) =>
    setJournalOpen((s) => { const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n })
  const journalKeys = useJournalKeys()
  const q = useQuery<TradesResp>({
    queryKey: ['trades', filters],
    queryFn: () => fetchJSON<TradesResp>('/api/trades' + buildQuery(filters)),
  })
  const settings = useQuery({
    queryKey: ['settings'],
    queryFn: () => fetchJSON<SettingsResp>('/api/settings'),
    staleTime: 60_000,
  })
  const names = settings.data?.settings.accounts.names
  const trades = useMemo(() => {
    if (!q.data) return []
    return sortBy(q.data.trades, sort, (t, k) => t[k])
  }, [q.data, sort])
  const totals = useMemo(() => {
    return trades.reduce(
      (acc, t) => ({
        qty:      acc.qty      + t.qty,
        net_pnl:  acc.net_pnl  + t.net_pnl,
        fees:     acc.fees     + t.commission + t.fee,
        duration: acc.duration + t.duration_seconds,
      }),
      { qty: 0, net_pnl: 0, fees: 0, duration: 0 },
    )
  }, [trades])
  const Th = ({ k, children, num }: { k: TradeKey; children: React.ReactNode; num?: boolean }) => (
    <th className={num ? 'num' : ''} onClick={() => setSort(flip(sort, k))}>{children}{arrow(sort, k)}</th>
  )
  return (
    <div className="card">
      <h2>Round-trip Trades {q.data && `(${q.data.count})`}</h2>
      {q.isLoading ? <div>Loading…</div> : q.error ? <div className="error">{String(q.error)}</div> : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 24 }}></th>
                <Th k="entry_time">Entry time (CT)</Th>
                <Th k="account">Account</Th>
                <Th k="symbol">Symbol</Th>
                <Th k="direction">Dir</Th>
                <Th k="qty" num>Qty</Th>
                <Th k="entry_price" num>Entry</Th>
                <Th k="exit_price" num>Exit</Th>
                <Th k="net_pnl" num>Net P&amp;L</Th>
                <Th k="commission" num>Fees</Th>
                <Th k="duration_seconds" num>Duration</Th>
                <th>Strategies</th>
                <th>Journal</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t) => {
                const cls = t.net_pnl > 0 ? 'pnl-pos' : t.net_pnl < 0 ? 'pnl-neg' : ''
                const key = `${t.first_fill_id}-${t.last_fill_id}`
                const isExpanded = expanded.has(key)
                return (
                  <React.Fragment key={key}>
                    <tr>
                      <td style={{ textAlign: 'center', cursor: t.is_scale_out ? 'pointer' : 'default' }}
                          onClick={() => t.is_scale_out && toggleExpanded(key)}
                          title={t.is_scale_out ? 'Click to show per-leg fills' : ''}>
                        {t.is_scale_out
                          ? <span className="scale-out-arrow">{isExpanded ? '▼' : '▶'}</span>
                          : ''}
                      </td>
                      <td>{fmtTime(t.entry_time)}</td>
                      <td>{accountLabel(t.account, names)}</td>
                      <td>{t.contract || t.symbol}</td>
                      <td>{t.direction}</td>
                      <td className="num">{t.qty}</td>
                      <td className="num">{t.entry_price}</td>
                      <td className="num">
                        {t.exit_price}
                        {t.is_scale_out && (
                          <div className="subtle" style={{ fontSize: 11 }}>
                            avg of {t.exit_fills.length} legs
                          </div>
                        )}
                      </td>
                      <td className={'num ' + cls}>{fmtMoney(t.net_pnl)}</td>
                      <td className="num">{fmtMoney(t.commission + t.fee)}</td>
                      <td className="num">{fmtDuration(t.duration_seconds)}</td>
                      <td>{t.strategies.join(', ') || <span className="subtle">—</span>}</td>
                      <td style={{ textAlign: 'center' }}>
                        <button
                          type="button"
                          className={'journal-btn ' + (journalKeys.has(key) ? 'has-entry' : '')}
                          onClick={() => toggleJournal(key)}
                          title={journalKeys.has(key) ? 'Edit journal entry' : 'Add journal entry'}
                        >
                          {journalKeys.has(key) ? '📝' : '＋'}
                        </button>
                      </td>
                    </tr>
                    {isExpanded && t.is_scale_out && (
                      <tr className="scale-out-detail">
                        <td></td>
                        <td colSpan={12}>
                          <ScaleOutDetail trade={t} />
                        </td>
                      </tr>
                    )}
                    {journalOpen.has(key) && (
                      <tr className="journal-detail">
                        <td></td>
                        <td colSpan={12}>
                          <JournalEditor
                            tradeKey={key}
                            snapshot={{
                              symbol: t.contract || t.symbol,
                              account: t.account,
                              direction: t.direction,
                              net_pnl: t.net_pnl,
                              entry_time: t.entry_time,
                              exit_time: t.exit_time,
                              atm: t.strategies.join(', '),
                              entry_price: t.entry_price,
                              exit_price: t.exit_price,
                            }}
                            onClose={() => toggleJournal(key)}
                          />
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                )
              })}
              {trades.length === 0 && (
                <tr><td colSpan={13} className="subtle" style={{ textAlign: 'center', padding: '20px' }}>No trades match these filters.</td></tr>
              )}
            </tbody>
            {trades.length > 0 && (
              <tfoot>
                <tr className="totals-row">
                  <td></td>
                  <td colSpan={4}>Totals ({trades.length} trades)</td>
                  <td className="num">{totals.qty}</td>
                  <td className="num"></td>
                  <td className="num"></td>
                  <td className={'num ' + (totals.net_pnl > 0 ? 'pnl-pos' : totals.net_pnl < 0 ? 'pnl-neg' : '')}>{fmtMoney(totals.net_pnl)}</td>
                  <td className="num">{fmtMoney(totals.fees)}</td>
                  <td className="num">{fmtDuration(totals.duration)}</td>
                  <td></td>
                  <td></td>
                </tr>
              </tfoot>
            )}
          </table>
        </div>
      )}
    </div>
  )
}

// ---------- Scale-out detail row ----------

const pnlClass = (n: number) => (n > 0 ? 'pnl-pos' : n < 0 ? 'pnl-neg' : '')

export function TaxEstimateCard() {
  const q = useQuery<TaxEstimateResp>({
    queryKey: ['tax-estimate'],
    queryFn: () => fetchJSON<TaxEstimateResp>('/api/tax-estimate'),
    refetchInterval: 30_000,
  })
  const settings = useQuery({
    queryKey: ['settings'],
    queryFn: () => fetchJSON<SettingsResp>('/api/settings'),
    staleTime: 60_000,
  })
  const names = settings.data?.settings.accounts.names
  if (q.isLoading || !q.data || !q.data.enabled) return null
  const d = q.data
  const ratePct = (d.rates.blended_rate * 100).toFixed(1)
  return (
    <div className="card">
      <h2>
        Estimated tax {d.tax_year}{' '}
        <span className="subtle">(Section 1256 60/40, {ratePct}% blended)</span>
      </h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Account</th>
              <th className="num">Trades</th>
              <th className="num">Realized P&amp;L</th>
              <th className="num">Taxable gain</th>
              <th className="num">Est. tax</th>
            </tr>
          </thead>
          <tbody>
            {d.accounts.map((a) => (
              <tr key={a.account}>
                <td><strong>{accountLabel(a.account, names)}</strong></td>
                <td className="num">{a.trades}</td>
                <td className={'num ' + pnlClass(a.realized_pnl)}>{fmtMoney(a.realized_pnl)}</td>
                <td className="num">{fmtMoney(a.taxable_gain)}</td>
                <td className="num">{fmtMoney(a.estimated_tax)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td><strong>Total (netted)</strong></td>
              <td className="num"></td>
              <td className={'num ' + pnlClass(d.total.realized_pnl)}>{fmtMoney(d.total.realized_pnl)}</td>
              <td className="num">{fmtMoney(d.total.taxable_gain)}</td>
              <td className="num"><strong>{fmtMoney(d.total.estimated_tax)}</strong></td>
            </tr>
          </tfoot>
        </table>
      </div>
      <p className="subtle" style={{ marginTop: 8, marginBottom: 0 }}>
        Total nets all accounts (a losing account offsets a winning one on one Form 6781),
        so it can be less than the per-account taxes summed. {d.note}
      </p>
    </div>
  )
}

export function MicroscalpComplianceCard() {
  const q = useQuery<MicroscalpResp>({
    queryKey: ['microscalp-compliance'],
    queryFn: () => fetchJSON<MicroscalpResp>('/api/microscalp-compliance'),
    refetchInterval: 30_000,
  })
  const settings = useQuery({
    queryKey: ['settings'],
    queryFn: () => fetchJSON<SettingsResp>('/api/settings'),
    staleTime: 60_000,
  })
  const names = settings.data?.settings.accounts.names
  if (q.isLoading || !q.data) return null
  const d = q.data
  if (d.accounts.length === 0) return null

  return (
    <div className="card">
      <h2>Microscalping Compliance</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Account</th>
              <th className="num">Trades</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {d.accounts.map((a) => (
              <tr key={a.account}>
                <td>
                  <strong>{accountLabel(a.account, names)}</strong>
                  {a.is_eval && <span className="subtle"> (eval)</span>}
                </td>
                <td className="num">{a.trades}</td>
                <td>
                  <span className={'compliance-badge ' + (a.compliant ? 'ok' : 'bad')}>
                    {a.compliant ? 'PASS' : 'BREACH'}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="subtle" style={{ marginTop: 8, marginBottom: 0 }}>{d.note}</p>
    </div>
  )
}

function ScaleOutDetail({ trade }: { trade: Trade }) {
  // Entry usually a single fill (1 row), exits split across N legs. Render
  // both as a compact two-column block so the reader sees Entry → Legs at a
  // glance. Per-leg P&L is pre-computed server-side.
  return (
    <div className="scale-out-detail-content">
      <div className="scale-out-block">
        <div className="scale-out-label">Entry</div>
        <table className="scale-out-mini">
          <tbody>
            {trade.entry_fills.map((f, i) => (
              <tr key={i}>
                <td className="num">{f.qty}</td>
                <td>@</td>
                <td className="num">{f.price}</td>
                <td className="subtle">{fmtTime(f.time)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="scale-out-block">
        <div className="scale-out-label">
          Exit fills ({trade.exit_fills.length})
        </div>
        <table className="scale-out-mini">
          <tbody>
            {trade.exit_fills.map((f, i) => {
              const cls = f.pnl > 0 ? 'pnl-pos' : f.pnl < 0 ? 'pnl-neg' : ''
              return (
                <tr key={i}>
                  <td className="num">{f.qty}</td>
                  <td>@</td>
                  <td className="num">{f.price}</td>
                  <td className="subtle">{fmtTime(f.time)}</td>
                  <td className={'num ' + cls}>{fmtMoney(f.pnl)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}


// ---------- FillsTable ----------

type FillKey = keyof Pick<Fill, 'id' | 'time_utc' | 'account_name' | 'symbol' | 'order_name' | 'order_action' | 'order_type' | 'qty' | 'price' | 'commission' | 'position'>

export function FillsTable({ filters }: { filters: Filters }) {
  const [sort, setSort] = useState<Sort<FillKey>>({ key: 'id', dir: 'desc' })
  const [limit, setLimit] = useState<number>(100)
  const q = useQuery<FillsResp>({
    queryKey: ['fills', filters, limit],
    queryFn: () => fetchJSON<FillsResp>('/api/fills' + buildQuery(filters, { limit })),
  })
  const settings = useQuery({
    queryKey: ['settings'],
    queryFn: () => fetchJSON<SettingsResp>('/api/settings'),
    staleTime: 60_000,
  })
  const names = settings.data?.settings.accounts.names
  const fills = useMemo(() => {
    if (!q.data) return []
    return sortBy(q.data.fills, sort, (f, k) => f[k])
  }, [q.data, sort])
  const fillTotals = useMemo(() => {
    return fills.reduce(
      (acc, f) => ({
        qty:        acc.qty        + (f.qty || 0),
        commission: acc.commission + (f.commission || 0) + (f.fee || 0),
      }),
      { qty: 0, commission: 0 },
    )
  }, [fills])
  const Th = ({ k, children, num }: { k: FillKey; children: React.ReactNode; num?: boolean }) => (
    <th className={num ? 'num' : ''} onClick={() => setSort(flip(sort, k))}>{children}{arrow(sort, k)}</th>
  )
  return (
    <div className="card">
      <h2>
        Fills {q.data && `(showing ${q.data.count})`}
        <span className="subtle" style={{ marginLeft: 12 }}>
          limit:{' '}
          <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
            <option value={50}>50</option>
            <option value={100}>100</option>
            <option value={500}>500</option>
            <option value={2000}>2000</option>
          </select>
        </span>
      </h2>
      {q.isLoading ? <div>Loading…</div> : q.error ? <div className="error">{String(q.error)}</div> : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <Th k="id" num>Id</Th>
                <Th k="time_utc">Time (CT)</Th>
                <Th k="account_name">Account</Th>
                <Th k="symbol">Symbol</Th>
                <Th k="order_name">Role</Th>
                <Th k="order_action">Action</Th>
                <Th k="order_type">Type</Th>
                <Th k="qty" num>Qty</Th>
                <Th k="price" num>Price</Th>
                <Th k="commission" num>Comm</Th>
                <Th k="position" num>Pos</Th>
                <th>Strategy</th>
              </tr>
            </thead>
            <tbody>
              {fills.map((f) => (
                <tr key={f.id}>
                  <td className="num">{f.id}</td>
                  <td>{fmtTime(f.time_utc)}</td>
                  <td>{accountLabel(f.account_name, names)}</td>
                  <td>{f.symbol}</td>
                  <td>{f.order_name}</td>
                  <td>{f.order_action}</td>
                  <td>{f.order_type}</td>
                  <td className="num">{f.qty}</td>
                  <td className="num">{f.price}</td>
                  <td className="num">{fmtMoney(f.commission)}</td>
                  <td className="num">{f.position}</td>
                  <td>{f.strategy_template || f.strategy_name || <span className="subtle">—</span>}</td>
                </tr>
              ))}
              {fills.length === 0 && (
                <tr><td colSpan={12} className="subtle" style={{ textAlign: 'center', padding: '20px' }}>No fills match these filters.</td></tr>
              )}
            </tbody>
            {fills.length > 0 && (
              <tfoot>
                <tr className="totals-row">
                  <td colSpan={7}>Totals ({fills.length} fills)</td>
                  <td className="num">{fillTotals.qty}</td>
                  <td className="num"></td>
                  <td className="num">{fmtMoney(fillTotals.commission)}</td>
                  <td className="num"></td>
                  <td></td>
                </tr>
              </tfoot>
            )}
          </table>
        </div>
      )}
    </div>
  )
}
