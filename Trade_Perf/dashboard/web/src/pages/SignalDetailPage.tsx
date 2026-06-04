import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  deleteJSON,
  fetchJSON,
  fmtPrice,
  postJSON,
  type AtmBracket,
  type Leg,
  type LegResult,
  type Outcome,
  type Signal,
  type SignalDetailResp,
  type SignalExec,
  type SettingsResp,
  type TradeMetrics,
} from '../api'

const OUTCOME_RESULTS: Outcome['result'][] = [
  'pending', 'target', 'stop', 'breakeven', 'partial', 'no_fill', 'not_watched', 'other',
]

// ---------- Formatting helpers ----------
const fmtMoney = (n: number) =>
  `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
const fmtNum = (n: number | null | undefined, digits = 2) =>
  n === undefined || n === null ? '—' : n.toFixed(digits)

// ---------- Page ----------
export function SignalDetailPage() {
  const { timestamp } = useParams<{ timestamp: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const ts = timestamp || ''
  const tsEnc = encodeURIComponent(ts)

  const q = useQuery<SignalDetailResp>({
    queryKey: ['signal', ts],
    queryFn: () => fetchJSON<SignalDetailResp>(`/api/signals/${tsEnc}`),
    enabled: !!ts,
  })

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['signal', ts] })
    qc.invalidateQueries({ queryKey: ['signals'] })
  }
  const onDeleted = () => {
    qc.invalidateQueries({ queryKey: ['signals'] })
    navigate('/signals')
  }

  // ---- Editable state lifted to page ----
  const [journalNote, setJournalNote] = useState('')
  const [positionSize, setPositionSize] = useState('')
  const [outcomeResult, setOutcomeResult] = useState<Outcome['result']>('pending')
  const [outcomeNote, setOutcomeNote] = useState('')
  const [outcomeClose, setOutcomeClose] = useState('')
  // Per-leg draft for scale-out ATMs. One row per ATM bracket. Pre-filled
  // from existing signal.legs (auto-resolved or user-saved); falls back to
  // blank rows matching the bracket plan if no legs exist yet.
  const [legsDraft, setLegsDraft] = useState<LegDraft[]>([])

  const signal = q.data?.signal
  useEffect(() => {
    if (!signal) return
    setJournalNote(signal.journal?.note ?? '')
    setPositionSize(
      signal.metrics?.position_size && signal.metrics.position_size > 0
        ? String(signal.metrics.position_size)
        : '',
    )
    setOutcomeResult(signal.outcome?.result ?? 'pending')
    setOutcomeNote(signal.outcome?.note ?? '')
    setOutcomeClose(
      signal.outcome?.closing_price != null ? String(signal.outcome.closing_price) : '',
    )
    setLegsDraft(buildLegsDraft(signal))
  }, [signal])

  // ---- Dirty tracking ----
  const journalDirty =
    !!signal &&
    ((journalNote.trim() || null) !== (signal.journal?.note ?? null))

  const positionDirty = (() => {
    if (!signal) return false
    const current = signal.metrics?.position_size ?? 0
    const next = parseFloat(positionSize || '0')
    if (!Number.isFinite(next) || next < 0) return false
    return next !== current
  })()

  const outcomeDirty = (() => {
    if (!signal) return false
    if (outcomeResult !== (signal.outcome?.result ?? 'pending')) return true
    if ((outcomeNote.trim() || null) !== (signal.outcome?.note ?? null)) return true
    const cur = signal.outcome?.closing_price ?? null
    const nxt = outcomeClose.trim() === '' ? null : parseFloat(outcomeClose)
    if (nxt !== null && !Number.isFinite(nxt)) return false
    return nxt !== cur
  })()

  const legsDirty = (() => {
    if (!signal) return false
    const baseline = JSON.stringify(buildLegsDraft(signal))
    return JSON.stringify(legsDraft) !== baseline
  })()

  const anyDirty = journalDirty || positionDirty || outcomeDirty || legsDirty

  // ---- Single save mutation ----
  const [saveError, setSaveError] = useState<string | null>(null)
  const saveAll = useMutation({
    mutationFn: async () => {
      const ops: Promise<unknown>[] = []
      if (journalDirty) {
        ops.push(postJSON(`/api/signals/${tsEnc}/journal`, {
          note: journalNote.trim() || null,
        }))
      }
      if (positionDirty) {
        ops.push(postJSON(`/api/signals/${tsEnc}/position`, {
          position_size: parseFloat(positionSize || '0'),
        }))
      }
      if (outcomeDirty) {
        const cp = outcomeClose.trim() === '' ? null : parseFloat(outcomeClose)
        ops.push(postJSON(`/api/signals/${tsEnc}/outcome`, {
          result: outcomeResult,
          note: outcomeNote.trim() || null,
          closing_price: cp,
        }))
      }
      if (legsDirty) {
        const payload = legsDraft
          .filter((l) => l.exit_price.trim() !== '' && Number.isFinite(parseFloat(l.exit_price)))
          .map((l) => ({
            bracket_idx: l.bracket_idx,
            qty: l.qty,
            result: l.result,
            exit_price: parseFloat(l.exit_price),
            exit_ts: l.exit_ts,
            method: l.method ?? 'manual',
            engine: l.engine === 'resolver' ? 'resolver' : 'manual',
          }))
        ops.push(postJSON(`/api/signals/${tsEnc}/legs`, { legs: payload }))
      }
      await Promise.all(ops)
    },
    onSuccess: () => {
      setSaveError(null)
      refresh()
    },
    onError: (e: Error) => setSaveError(e.message),
  })

  if (!ts) return <div className="card">No timestamp.</div>
  if (q.isLoading) return <div className="card">Loading…</div>
  if (q.error || !q.data) return <div className="card error">{String(q.error ?? 'no data')}</div>

  const sig = q.data.signal
  const m = sig.metrics

  return (
    <>
      <div className="card detail-header">
        <div>
          <h2>{sig.timestamp}</h2>
          <Link to="/signals">← All signals</Link>
        </div>
        <DeleteButton ts={ts} onDeleted={onDeleted} />
      </div>

      <AutoTraderSection signal={sig} refresh={refresh} />

      <div className="card">
        <h2>Proposal</h2>
        <ProposalDetails signal={sig} />
        <h3>Reasoning</h3>
        <p>{sig.proposal.reasoning}</p>
        {sig.market_context && <MarketContextSection ctx={sig.market_context} />}
        <h3>Trade Recap</h3>
        <TradeRecap signal={sig} metrics={m} />
        <div className="position-form">
          <label htmlFor="position_size">Contracts / Shares:</label>
          <input
            id="position_size"
            type="number"
            min={0}
            step="any"
            placeholder="e.g. 1"
            value={positionSize}
            onChange={(e) => setPositionSize(e.target.value)}
          />
          {positionDirty && <span className="dirty-flag">unsaved</span>}
        </div>
        {m && m.position_size > 0 && m.point_value && <Totals metrics={m} />}
        <TickAdjustmentsBlock signal={sig} />
      </div>

      <BracketsSection
        signal={sig}
        legsDraft={legsDraft}
        setLegsDraft={setLegsDraft}
        dirty={legsDirty}
        metrics={m}
      />

      <div className="forms-grid">
        <JournalSection
          note={journalNote} setNote={setJournalNote}
          saved={sig.journal} dirty={journalDirty}
        />
        <OutcomeSection
          result={outcomeResult} setResult={setOutcomeResult}
          note={outcomeNote} setNote={setOutcomeNote}
          closingPrice={outcomeClose} setClosingPrice={setOutcomeClose}
          suggestedClose={sig.proposal.target}
          saved={sig.outcome} dirty={outcomeDirty}
          locked={sig.entry_triggered === false}
        />
      </div>

      <div className="save-bar">
        <button
          type="button"
          className="primary"
          onClick={() => saveAll.mutate()}
          disabled={!anyDirty || saveAll.isPending}
        >
          {saveAll.isPending
            ? 'Saving…'
            : anyDirty
              ? `Save changes (${[journalDirty, positionDirty, outcomeDirty, legsDirty].filter(Boolean).length} section${[journalDirty, positionDirty, outcomeDirty, legsDirty].filter(Boolean).length === 1 ? '' : 's'})`
              : 'No changes'}
        </button>
        {saveError && <div className="error">{saveError}</div>}
      </div>

      <ScreenshotSection filename={sig.screenshot_filename} />

      <JsonSnippetSection signal={sig} />
    </>
  )
}

// ---------- Auto-Trader (arm / disarm) ----------
function ExecBadge({ exec }: { exec?: SignalExec }) {
  if (!exec?.state) return null
  const cls =
    exec.state === 'filled' ? 'badge auto-res'
      : exec.state === 'armed' || exec.state === 'working' ? 'badge auto-gen'
        : 'badge'
  const label = exec.state.toUpperCase() + (exec.dry_run ? ' (DRY)' : '')
  return <span className={cls} title={exec.exec_tag ?? ''}>{label}</span>
}

function AutoTraderSection({ signal, refresh }: { signal: Signal; refresh: () => void }) {
  const qc = useQueryClient()
  const tsEnc = encodeURIComponent(signal.timestamp)
  const sq = useQuery<SettingsResp>({
    queryKey: ['settings'],
    queryFn: () => fetchJSON<SettingsResp>('/api/settings'),
  })
  const cfg = sq.data?.settings.auto_trader
  const exec = signal.exec
  const state = exec?.state
  const p = signal.proposal
  const [err, setErr] = useState<string | null>(null)

  const arm = useMutation({
    mutationFn: () => postJSON(`/api/signals/${tsEnc}/arm`, {}),
    onSuccess: () => { setErr(null); refresh() },
    onError: (e: Error) => setErr(e.message),
  })
  const disarm = useMutation({
    mutationFn: () => postJSON(`/api/signals/${tsEnc}/disarm`, {}),
    onSuccess: () => { setErr(null); refresh() },
    onError: (e: Error) => setErr(e.message),
  })
  const toggleEnable = useMutation({
    mutationFn: (next: boolean) => postJSON('/api/auto-trader/enable', { enabled: next }),
    onSuccess: () => { setErr(null); qc.invalidateQueries({ queryKey: ['settings'] }) },
    onError: (e: Error) => setErr(e.message),
  })

  const accountSet = !!cfg?.account
  const execEnabled = !!cfg?.enabled
  const isFlat = p.direction === 'flat'
  const live = state === 'working' || state === 'filled'
  // Arming is staging intent -- allowed once an account is set, regardless of
  // whether auto trading is enabled. Execution is gated separately below.
  const canArm = accountSet && !isFlat && !live && state !== 'armed'
  const canDisarm = state === 'armed'

  return (
    <div className="card">
      <h2>Auto-Trader {' '}<ExecBadge exec={exec} /></h2>

      {!accountSet ? (
        <p className="subtle">
          No account set. Pick a Sim account in{' '}
          <Link to="/settings">Settings → Auto-Trader</Link> before arming.
        </p>
      ) : (
        <>
          <label className="settings-checkbox" style={{ marginBottom: 8 }}>
            <input
              type="checkbox"
              checked={execEnabled}
              disabled={toggleEnable.isPending}
              onChange={(e) => toggleEnable.mutate(e.target.checked)}
            />
            <span>
              <strong>Enable auto trading</strong> — auto-execute qualifying signals on{' '}
              <strong>{cfg!.account}</strong>.{' '}
              {execEnabled
                ? <span className="pnl-pos">ON</span>
                : <span className="subtle">OFF</span>}
            </span>
          </label>
          {execEnabled ? (
            <p className="subtle">
              Auto trading is <strong className="pnl-pos">ON</strong> — new non-flat signals
              created after you enabled execute automatically on{' '}
              <code>{p.atm_strategy ?? 'custom'}</code> at {fmtPrice(p.entry, p.instrument)}.
              <strong> No need to arm.</strong> Arming below forces this specific signal even if
              it is older.
            </p>
          ) : (
            <p className="subtle">
              Auto trading is OFF — arming stages a LIMIT entry at {fmtPrice(p.entry, p.instrument)}{' '}
              via the <code>{p.atm_strategy ?? 'custom'}</code> template; it executes once you enable
              auto trading, and cancels if unfilled after the entry window.
            </p>
          )}
        </>
      )}

      {state === 'filled' && exec?.fill_price != null && (
        <p className="subtle">
          Filled @ {fmtPrice(exec.fill_price, p.instrument)}
          {exec.fill_qty ? ` × ${exec.fill_qty}` : ''}
          {exec.filled_at ? ` (${exec.filled_at})` : ''}
        </p>
      )}
      {(state === 'cancelled' || state === 'rejected' || state === 'disarmed') && exec?.note && (
        <p className="subtle">{state}: {exec.note}</p>
      )}

      <div className="save-bar">
        <button
          type="button"
          className="primary"
          disabled={!canArm || arm.isPending}
          onClick={() => arm.mutate()}
        >
          {arm.isPending ? 'Arming…' : state === 'armed' ? 'Armed' : execEnabled ? 'Arm now (force)' : 'Arm for execution'}
        </button>
        {canDisarm && (
          <button
            type="button"
            className="danger"
            disabled={disarm.isPending}
            onClick={() => disarm.mutate()}
          >
            {disarm.isPending ? 'Disarming…' : 'Disarm'}
          </button>
        )}
      </div>
      {isFlat && accountSet && <p className="subtle">Flat signal — nothing to execute.</p>}
      {err && <div className="error">{err}</div>}
    </div>
  )
}

// ---------- JSON snippet ----------
function JsonSnippetSection({ signal }: { signal: Signal }) {
  // market_context is already shown in its own collapsed section in the
  // proposal card; strip it here so the snippet stays focused on the record.
  const { market_context: _mc, raw_response: _rr, ...rest } = signal
  void _mc; void _rr
  return (
    <div className="card">
      <h2>Raw signal record</h2>
      <details open>
        <summary className="subtle">JSON (signals.jsonl entry)</summary>
        <pre style={{ overflowX: 'auto', fontSize: 12, maxHeight: 480, marginTop: 8 }}>
          {JSON.stringify(rest, null, 2)}
        </pre>
      </details>
    </div>
  )
}

// ---------- Screenshot ----------
function ScreenshotSection({ filename }: { filename: string | null | undefined }) {
  if (!filename) {
    return <div className="card subtle">No screenshot on record.</div>
  }
  const url = `/api/screenshots/${encodeURIComponent(filename)}`
  return (
    <div className="card screenshot">
      <h2>Chart screenshot</h2>
      <a href={url} target="_blank" rel="noreferrer">
        <img src={url} alt="chart" />
      </a>
    </div>
  )
}

// ---------- Proposal details ----------
function ProposalDetails({ signal }: { signal: Signal }) {
  const p = signal.proposal
  const enteredAt = signal.entry_hit_ts
    ? new Date(signal.entry_hit_ts).toLocaleString()
    : null
  return (
    <dl className="kv-grid">
      <dt>Instrument</dt><dd>{p.instrument || '—'}</dd>
      <dt>Direction</dt><dd className={`dir-${p.direction}`}>{p.direction || '—'}</dd>
      <dt>Entry</dt><dd>{fmtPrice(p.entry, p.instrument)}</dd>
      <dt>Entered at</dt><dd>{
        p.direction === 'flat'
          ? <span className="subtle">no entry (flat — no trade)</span>
          : signal.entry_triggered === true && enteredAt
            ? enteredAt
            : signal.entry_triggered === false
              ? <span className="subtle">no entry (4 h window expired)</span>
              : <span className="subtle">pending</span>
      }</dd>
      <dt>Stop</dt><dd>{fmtPrice(p.stop, p.instrument)}</dd>
      <dt>Target</dt><dd>{fmtPrice(p.target, p.instrument)}</dd>
      <dt>R:R</dt><dd>{fmtNum(p.risk_reward, 2)}</dd>
    </dl>
  )
}

// ---------- Market context ----------
type AnyDict = Record<string, unknown>

function MarketContextSection({ ctx }: { ctx: AnyDict }) {
  const current = (ctx.current ?? null) as { bid?: number; ask?: number; last?: number } | null
  const daily = (ctx.daily_levels ?? null) as AnyDict | null
  return (
    <>
      <h3>Market Context (NinjaTrader)</h3>
      <div className="market-context">
        <p className="subtle">
          Instrument: <strong>{String(ctx.instrument ?? '?')}</strong>
          {current && (
            <>
              {' · '}bid <strong>{current.bid}</strong>
              {' / ask '}<strong>{current.ask}</strong>
              {' / last '}<strong>{current.last}</strong>
            </>
          )}
        </p>
        {daily && (
          <p className="subtle">
            {daily.pivot_p !== undefined && (
              <>Pivot {String(daily.pivot_p)} (R1 {String(daily.pivot_r1)} / R2 {String(daily.pivot_r2)} / R3 {String(daily.pivot_r3)} · S1 {String(daily.pivot_s1)} / S2 {String(daily.pivot_s2)} / S3 {String(daily.pivot_s3)})</>
            )}
            {daily.today_high !== undefined && (
              <>{' · Today H/L: '}{String(daily.today_high)} / {String(daily.today_low)}</>
            )}
            {daily.yesterday_high !== undefined && (
              <>{' · Yesterday H/L/C: '}{String(daily.yesterday_high)} / {String(daily.yesterday_low)} / {String(daily.yesterday_close)}</>
            )}
          </p>
        )}
        <details>
          <summary>Full snapshot (JSON)</summary>
          <pre style={{ overflowX: 'auto', fontSize: 12 }}>{JSON.stringify(ctx, null, 2)}</pre>
        </details>
      </div>
    </>
  )
}

// ---------- Trade recap ----------
function TradeRecap({ signal, metrics }: { signal: Signal; metrics: TradeMetrics | undefined }) {
  const p = signal.proposal
  const usingTicks = metrics?.display_mode === 'ticks'
  return (
    <dl className="kv-grid">
      <dt>Instrument</dt>
      <dd>
        {p.instrument || '—'}{' '}
        {metrics?.point_value ? (
          <span className="subtle">($ / point: ${metrics.point_value})</span>
        ) : (
          <span className="subtle">(point value unknown)</span>
        )}
      </dd>
      <dt>Levels</dt>
      <dd>Entry {p.entry} → Stop {p.stop} → Target {p.target}</dd>
      {metrics && metrics.point_value && (
        <>
          <dt>Risk / contract</dt>
          <dd>
            {usingTicks
              ? <>{metrics.risk_ticks.toFixed(0)} ticks × ${metrics.tick_value} = <strong>{fmtMoney(metrics.risk_per_contract)}</strong></>
              : <>{metrics.risk_points.toFixed(2)} pt × ${metrics.point_value} = <strong>{fmtMoney(metrics.risk_per_contract)}</strong></>}
          </dd>
          <dt>Reward / contract</dt>
          <dd>
            {usingTicks
              ? <>{metrics.reward_ticks.toFixed(0)} ticks × ${metrics.tick_value} = <strong>{fmtMoney(metrics.reward_per_contract)}</strong></>
              : <>{metrics.reward_points.toFixed(2)} pt × ${metrics.point_value} = <strong>{fmtMoney(metrics.reward_per_contract)}</strong></>}
          </dd>
        </>
      )}
    </dl>
  )
}

// ---------- Totals (totals + realized P/L) ----------
function Totals({ metrics: m }: { metrics: TradeMetrics }) {
  const pnlClass = m.realized_pnl == null
    ? '' : m.realized_pnl > 0 ? 'pnl-pos' : m.realized_pnl < 0 ? 'pnl-neg' : ''
  return (
    <dl className="kv-grid totals">
      <dt>Total risk</dt><dd>{fmtMoney(m.total_risk)}</dd>
      <dt>Total reward</dt><dd>{fmtMoney(m.total_reward)}</dd>
      {m.realized_pnl !== null && (
        <>
          <dt>Realized P/L</dt>
          <dd className={pnlClass}>
            {fmtMoney(m.realized_pnl)}
            {m.realized_pnl_source && (
              <span className="subtle"> (from {m.realized_pnl_source})</span>
            )}
          </dd>
        </>
      )}
    </dl>
  )
}

// ---------- Tick adjustments ----------
function TickAdjustmentsBlock({ signal }: { signal: Signal }) {
  const p = signal.proposal
  if (p.tick_adjustments && p.tick_adjustments.length > 0) {
    return (
      <div className="tick-adjustments">
        <strong>Tick adjustments applied</strong>{' '}
        (tick={p.tick_size_applied}, source={p.tick_source}):
        <ul>
          {p.tick_adjustments.map((a, i) => (
            <li key={i}>{a.field}: {a.from} → {a.to}</li>
          ))}
        </ul>
      </div>
    )
  }
  if (p.tick_source === 'unknown') {
    return (
      <div className="tick-warning">
        Unknown instrument <code>{p.instrument}</code> — no tick rounding applied.
        Add it to <code>instruments.json</code> if you trade this regularly.
      </div>
    )
  }
  return null
}

// ---------- Notes section (controlled) ----------
function JournalSection({
  note, setNote, saved, dirty,
}: {
  note: string
  setNote: (n: string) => void
  saved: { note: string | null } | undefined
  dirty: boolean
}) {
  return (
    <div className="card form-block">
      <h2>Notes {dirty && <span className="dirty-flag">unsaved</span>}</h2>
      {saved?.note && (
        <p className="subtle">Saved: {saved.note}</p>
      )}
      <textarea
        placeholder="Notes on this signal -- what you saw, what you'd change, etc."
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
    </div>
  )
}

// ---------- Outcome section (controlled) ----------
function OutcomeSection({
  result, setResult, note, setNote, closingPrice, setClosingPrice,
  suggestedClose, saved, dirty, locked,
}: {
  result: Outcome['result']
  setResult: (r: Outcome['result']) => void
  note: string
  setNote: (n: string) => void
  closingPrice: string
  setClosingPrice: (s: string) => void
  suggestedClose: number
  saved: Outcome | undefined
  dirty: boolean
  locked: boolean
}) {
  return (
    <div className="card form-block">
      <h2>Outcome {dirty && <span className="dirty-flag">unsaved</span>}</h2>
      {locked && (
        <p className="subtle">
          <strong>Locked:</strong> signal was not entered (4 h window expired or marked
          no-entry). Outcome stays <code>no_fill</code>.
        </p>
      )}
      <p className="subtle">
        Saved: <strong>{saved?.result ?? 'none'}</strong>
        {saved?.note && ` — ${saved.note}`}
        {saved?.closing_price != null && ` • close @ ${saved.closing_price}`}
      </p>
      <fieldset disabled={locked}>
        {OUTCOME_RESULTS.map((r) => (
          <label key={r}>
            <input
              type="radio"
              name="result"
              value={r}
              checked={result === r}
              onChange={() => setResult(r)}
            />
            {' '}{r}
          </label>
        ))}
      </fieldset>
      <label className="inline-label">
        Closing price (optional, overrides default P/L):{' '}
        <input
          type="number"
          step="any"
          value={closingPrice}
          onChange={(e) => setClosingPrice(e.target.value)}
          placeholder={`e.g. ${suggestedClose}`}
          disabled={locked}
        />
      </label>
      <textarea
        placeholder="What happened?"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        disabled={locked}
      />
    </div>
  )
}

// ---------- Per-leg fills (scale-out ATMs) ----------

const LEG_RESULTS: LegResult[] = ['target', 'stop', 'trail', 'be', 'manual', 'neither']

interface LegDraft {
  bracket_idx: number
  qty: number
  result: LegResult
  exit_price: string          // string for the input control; parse on save
  exit_ts: number | null      // unix ms; preserved as-is
  method: 'tick' | 'bar' | 'manual' | null
  engine: 'resolver' | 'manual' | null
}

function buildLegsDraft(signal: Signal): LegDraft[] {
  const brackets = signal.proposal.atm_brackets ?? []
  const legs = signal.legs ?? []
  if (brackets.length === 0) return []
  return brackets.map((b, i) => {
    const existing: Leg | undefined =
      legs.find((l) => l.bracket_idx === i) ?? legs[i]
    return {
      bracket_idx: i,
      qty: existing?.qty ?? b.qty,
      result: (existing?.result as LegResult | undefined) ?? 'neither',
      exit_price:
        existing?.exit_price != null ? String(existing.exit_price) : '',
      exit_ts: existing?.exit_ts ?? null,
      method: (existing?.method as LegDraft['method'] | undefined) ?? null,
      engine: (existing?.engine as LegDraft['engine'] | undefined) ?? null,
    }
  })
}

function bracketPrices(
  bracket: AtmBracket,
  entry: number,
  direction: 'long' | 'short' | 'flat',
  tickSize: number | null,
): { stop: number | null; target: number | null; be: number | null } {
  if (!tickSize || direction === 'flat') {
    return { stop: null, target: null, be: null }
  }
  const sign = direction === 'long' ? 1 : -1
  return {
    stop:   entry - sign * bracket.stop_ticks   * tickSize,
    target: entry + sign * bracket.target_ticks * tickSize,
    be:     bracket.auto_be_trigger > 0
              ? entry + sign * bracket.auto_be_plus * tickSize
              : null,
  }
}

function BracketsSection({
  signal, legsDraft, setLegsDraft, dirty, metrics,
}: {
  signal: Signal
  legsDraft: LegDraft[]
  setLegsDraft: (next: LegDraft[]) => void
  dirty: boolean
  metrics: TradeMetrics | undefined
}) {
  const brackets = signal.proposal.atm_brackets ?? []
  if (brackets.length === 0) return null

  const p = signal.proposal
  const tickSize = metrics?.tick_size ?? p.tick_size_applied ?? null
  const allAutoResolver =
    legsDraft.length > 0 && legsDraft.every((l) => l.engine === 'resolver')
  const anyResolved = legsDraft.some((l) => l.result !== 'neither')
  const breakdown = metrics?.leg_breakdown ?? null

  const update = (i: number, patch: Partial<LegDraft>) => {
    setLegsDraft(legsDraft.map((l, idx) => (idx === i ? { ...l, ...patch } : l)))
  }

  return (
    <div className="card">
      <h2>
        Scale-out brackets {dirty && <span className="dirty-flag">unsaved</span>}
        {' '}
        <span className="subtle">
          {brackets.length} bracket{brackets.length === 1 ? '' : 's'} · {signal.proposal.atm_strategy ?? 'custom'}
        </span>
      </h2>
      {!anyResolved && (
        <p className="subtle">
          No legs resolved yet. The outcome watcher will fill these in automatically as price walks through each
          bracket's stop / target / trail — or you can enter the actual fills below to record what really happened.
        </p>
      )}
      {anyResolved && allAutoResolver && (
        <p className="subtle">
          Legs filled in automatically by the outcome resolver. Edit any row to override with the actual fill from NinjaTrader.
        </p>
      )}

      <div className="table-wrap">
        <table className="brackets-table">
          <thead>
            <tr>
              <th>#</th>
              <th className="num">Qty</th>
              <th>Plan</th>
              <th>Result</th>
              <th>Exit price</th>
              <th className="num">P&amp;L</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {brackets.map((b, i) => {
              const leg = legsDraft[i]
              if (!leg) return null
              const prices = bracketPrices(b, p.entry, p.direction, tickSize)
              const pnl = breakdown?.[i]?.pnl
              return (
                <tr key={i}>
                  <td>{i + 1}</td>
                  <td className="num">{leg.qty}</td>
                  <td>
                    <BracketPlanCell bracket={b} prices={prices} instrument={p.instrument} />
                  </td>
                  <td>
                    <select
                      value={leg.result}
                      onChange={(e) => update(i, { result: e.target.value as LegResult, engine: 'manual' })}
                    >
                      {LEG_RESULTS.map((r) => (
                        <option key={r} value={r}>{r}</option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <input
                      type="number"
                      step="any"
                      value={leg.exit_price}
                      onChange={(e) => update(i, { exit_price: e.target.value, engine: 'manual' })}
                      placeholder={prices.target != null ? fmtPrice(prices.target, p.instrument) : ''}
                      style={{ width: 120 }}
                    />
                  </td>
                  <td className={`num ${pnl == null ? '' : pnl > 0 ? 'pnl-pos' : pnl < 0 ? 'pnl-neg' : ''}`}>
                    {pnl == null ? '—' : fmtMoney(pnl)}
                  </td>
                  <td>
                    {leg.engine === 'resolver' ? (
                      <span className="badge auto-res">auto</span>
                    ) : leg.engine === 'manual' ? (
                      <span className="badge auto-gen">manual</span>
                    ) : (
                      <span className="subtle">—</span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
          {breakdown && breakdown.some((b) => b != null) && (
            <tfoot>
              <tr className="totals-row">
                <td colSpan={5}><strong>Total (sum of legs)</strong></td>
                <td className={`num ${(metrics?.realized_pnl ?? 0) > 0 ? 'pnl-pos' : (metrics?.realized_pnl ?? 0) < 0 ? 'pnl-neg' : ''}`}>
                  <strong>{metrics?.realized_pnl != null ? fmtMoney(metrics.realized_pnl) : '—'}</strong>
                </td>
                <td></td>
              </tr>
            </tfoot>
          )}
        </table>
      </div>
    </div>
  )
}

function BracketPlanCell({
  bracket, prices, instrument,
}: {
  bracket: AtmBracket
  prices: { stop: number | null; target: number | null; be: number | null }
  instrument: string
}) {
  const parts: string[] = []
  parts.push(`SL ${bracket.stop_ticks}t${prices.stop != null ? ` @ ${fmtPrice(prices.stop, instrument)}` : ''}`)
  parts.push(`TP ${bracket.target_ticks}t${prices.target != null ? ` @ ${fmtPrice(prices.target, instrument)}` : ''}`)
  if (bracket.auto_be_trigger > 0) {
    parts.push(`BE+${bracket.auto_be_plus}@+${bracket.auto_be_trigger}t`)
  }
  if (bracket.trail_steps && bracket.trail_steps.length > 0) {
    const t = bracket.trail_steps[0]
    parts.push(`Trail ${t.stop_loss}t every ${t.frequency}t from +${t.profit_trigger}t`)
  }
  return <span className="subtle" style={{ fontSize: 12 }}>{parts.join(' · ')}</span>
}

// ---------- Delete button ----------
function DeleteButton({ ts, onDeleted }: { ts: string; onDeleted: () => void }) {
  const m = useMutation({
    mutationFn: () => deleteJSON(`/api/signals/${encodeURIComponent(ts)}`),
    onSuccess: onDeleted,
  })
  return (
    <button
      type="button"
      className="danger"
      onClick={() => {
        if (confirm('Delete this signal? It will be hidden from the dashboard. (Recoverable by editing signals.jsonl.)'))
          m.mutate()
      }}
      disabled={m.isPending}
    >
      {m.isPending ? 'Deleting…' : 'Delete'}
    </button>
  )
}
