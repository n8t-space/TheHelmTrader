import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

declare global {
  interface Window {
    __helmSettingsDirty?: boolean
  }
}
import {
  fetchJSON, postJSON, putJSON, accountLabel,
  type AccountConfig, type AccountConfigLive,
  type DimensionsResp, type ModelsResp, type OllamaTestResp,
  type NewsSource, type SettingsAccounts, type SettingsAiBackend,
  type BlackoutWindow, type SettingsAutomation,
  type SettingsAppearance, type SettingsAutoTrader, type SettingsDoc, type SettingsNews,
  type SettingsResp, type SettingsStrategy,
} from '../api'
import { applyAppearance, cacheAppearance } from '../lib/theme'

type Tab = 'appearance' | 'ai' | 'strategy' | 'accounts' | 'execution' | 'news' | 'integrity' | 'tax'

export function SettingsPage() {
  const qc = useQueryClient()
  const q = useQuery<SettingsResp>({
    queryKey: ['settings'],
    queryFn: () => fetchJSON<SettingsResp>('/api/settings'),
    refetchInterval: false,
    staleTime: Infinity,
  })

  const [draft, setDraft] = useState<SettingsDoc | null>(null)
  const [tab, setTab] = useState<Tab>('appearance')
  const baselineRef = useRef<string>('')

  useEffect(() => {
    if (q.data) {
      setDraft(q.data.settings)
      baselineRef.current = JSON.stringify(q.data.settings)
    }
  }, [q.data])

  const dirty = useMemo(() => {
    if (!draft) return false
    return JSON.stringify(draft) !== baselineRef.current
  }, [draft])

  // Browser-close / refresh guard. (In-app NavLink guard is handled by a
  // global click-capture in App.tsx, since useBlocker requires a data router.)
  useEffect(() => {
    if (!dirty) return
    const h = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', h)
    // Expose a dirty flag the global guard can read.
    window.__helmSettingsDirty = true
    return () => {
      window.removeEventListener('beforeunload', h)
      window.__helmSettingsDirty = false
    }
  }, [dirty])

  // Preview: apply appearance live so the user sees changes before Save.
  useEffect(() => {
    if (draft) applyAppearance(draft.appearance)
  }, [draft])

  const save = useMutation({
    mutationFn: (doc: SettingsDoc) =>
      putJSON<{ settings: SettingsDoc; path: string }>('/api/settings', doc),
    onSuccess: (resp) => {
      // Refresh draft from server's canonical (cleaned) response so the
      // dirty-state comparison stays accurate.
      setDraft(resp.settings)
      baselineRef.current = JSON.stringify(resp.settings)
      cacheAppearance(resp.settings.appearance)
      qc.invalidateQueries({ queryKey: ['settings'] })
    },
  })

  const reset = useMutation({
    mutationFn: () => postJSON<{ settings: SettingsDoc; path: string }>('/api/settings/reset'),
    onSuccess: (resp) => {
      setDraft(resp.settings)
      baselineRef.current = JSON.stringify(resp.settings)
      cacheAppearance(resp.settings.appearance)
      applyAppearance(resp.settings.appearance)
      qc.invalidateQueries({ queryKey: ['settings'] })
    },
  })

  if (q.isLoading) return <div className="card">Loading settings…</div>
  if (q.error || !q.data || !draft) return <div className="card error">{String(q.error || 'failed to load settings')}</div>

  const discard = () => {
    const baseline = JSON.parse(baselineRef.current) as SettingsDoc
    setDraft(baseline)
    applyAppearance(baseline.appearance)
  }

  return (
    <>
      <div className="card settings-header">
        <div>
          <h2 style={{ margin: 0 }}>Settings</h2>
          <p className="subtle" style={{ margin: '4px 0 0' }}>
            Stored at <code>{q.data.path}</code>
            {!q.data.exists_on_disk && <> · <span>(file not yet created — defaults shown)</span></>}
            {' · '}
            <Link to="/support#configuration">See recommended configuration</Link>
          </p>
        </div>
        <div className="settings-actions">
          {dirty && <span className="subtle">Unsaved changes</span>}
          <button onClick={discard} disabled={!dirty || save.isPending}>Discard</button>
          <button
            className="primary"
            onClick={() => save.mutate(draft)}
            disabled={!dirty || save.isPending}
          >
            {save.isPending ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>

      {save.error && <div className="card error">Save failed: {String(save.error)}</div>}

      <div className="card settings-tabs">
        {(['appearance', 'ai', 'strategy', 'accounts', 'execution', 'news', 'integrity', 'tax'] as Tab[]).map((t) => (
          <button
            key={t}
            type="button"
            className={'tab' + (tab === t ? ' on' : '')}
            onClick={() => setTab(t)}
          >
            {tabLabel(t)}
          </button>
        ))}
      </div>

      <div className="card">
        {tab === 'appearance' && (
          <AppearanceTab
            value={draft.appearance}
            onChange={(a) => setDraft({ ...draft, appearance: a })}
          />
        )}
        {tab === 'ai' && (
          <AiTab
            value={draft.ai_backend}
            onChange={(a) => setDraft({ ...draft, ai_backend: a })}
          />
        )}
        {tab === 'strategy' && (
          <StrategyTab
            value={draft.strategy}
            onChange={(s) => setDraft({ ...draft, strategy: s })}
            accounts={draft.accounts}
            accountConfigs={draft.account_configs}
            onConfigsChange={(c) => setDraft({ ...draft, account_configs: c })}
          />
        )}
        {tab === 'accounts' && (
          <AccountsTab
            value={draft.accounts}
            onChange={(a) => setDraft({ ...draft, accounts: a })}
            commissions={draft.commissions}
            onCommissionsChange={(c) => setDraft({ ...draft, commissions: c })}
            accountConfigs={draft.account_configs}
            onConfigsChange={(c) => setDraft({ ...draft, account_configs: c })}
            llcName={draft.llc_name}
            onLlcNameChange={(n) => setDraft({ ...draft, llc_name: n })}
          />
        )}
        {tab === 'execution' && (
          <>
            <AutoTraderTab
              value={draft.auto_trader}
              simAccounts={draft.accounts.simulation}
              onChange={(a) => setDraft({ ...draft, auto_trader: a })}
            />
            <hr style={{ margin: '28px 0', borderColor: 'var(--border)' }} />
            <AutomationTab
              value={draft.automation}
              tz={draft.appearance.timezone}
              onChange={(a) => setDraft({ ...draft, automation: a })}
            />
          </>
        )}
        {tab === 'news' && (
          <NewsTab
            value={draft.news}
            onChange={(n) => setDraft({ ...draft, news: n })}
          />
        )}
        {tab === 'integrity' && (
          <IntegrityTab
            value={draft.auditor}
            onChange={(a) => setDraft({ ...draft, auditor: a })}
          />
        )}
        {tab === 'tax' && (
          <TaxTab
            value={draft.tax}
            onChange={(t) => setDraft({ ...draft, tax: t })}
          />
        )}
      </div>

      <div className="card">
        <h3 style={{ marginTop: 0 }}>Reset</h3>
        <p className="subtle">Restore every section to factory defaults. Persists immediately.</p>
        <button
          className="danger"
          onClick={() => {
            if (window.confirm('Reset ALL settings to defaults?')) reset.mutate()
          }}
          disabled={reset.isPending}
        >
          {reset.isPending ? 'Resetting…' : 'Reset to defaults'}
        </button>
      </div>
    </>
  )
}

function tabLabel(t: Tab): string {
  return t === 'appearance' ? 'Appearance'
    : t === 'ai' ? 'AI Backend'
    : t === 'strategy' ? 'Strategy'
    : t === 'accounts' ? 'Accounts'
    : t === 'execution' ? 'Auto-Trader & Automation'
    : t === 'news' ? 'News'
    : t === 'integrity' ? 'Data Integrity'
    : 'Tax'
}

function TaxTab({ value, onChange }: {
  value: import('../api').SettingsTax
  onChange: (v: import('../api').SettingsTax) => void
}) {
  const pct = (f: number) => (f * 100).toFixed(1)
  const setPct = (key: 'lt_rate' | 'st_rate' | 'state_rate') => (e: React.ChangeEvent<HTMLInputElement>) => {
    const n = parseFloat(e.target.value)
    onChange({ ...value, [key]: isNaN(n) ? 0 : Math.min(100, Math.max(0, n)) / 100 })
  }
  const blended = 0.6 * value.lt_rate + 0.4 * value.st_rate + value.state_rate
  return (
    <div>
      <h3 style={{ marginTop: 0 }}>Estimated Tax</h3>
      <p className="subtle">
        Futures (MES/MCL incl. micros) are IRC <strong>Section 1256</strong> contracts: net gains are
        taxed <strong>60% long-term / 40% short-term</strong> regardless of holding period. The blended
        effective rate below is applied to realized gains per account on the Trade Performance page.
        Estimate only -- not tax advice.
      </p>
      <label className="kv">
        <span>Show tax estimate</span>
        <input type="checkbox" checked={value.enabled}
          onChange={(e) => onChange({ ...value, enabled: e.target.checked })} />
      </label>
      <label className="kv">
        <span>Long-term rate (60%)</span>
        <span><input type="number" step="0.5" min="0" max="100" value={pct(value.lt_rate)} onChange={setPct('lt_rate')} /> %</span>
      </label>
      <label className="kv">
        <span>Short-term / ordinary rate (40%)</span>
        <span><input type="number" step="0.5" min="0" max="100" value={pct(value.st_rate)} onChange={setPct('st_rate')} /> %</span>
      </label>
      <label className="kv">
        <span>State rate (on all gains, optional)</span>
        <span><input type="number" step="0.5" min="0" max="100" value={pct(value.state_rate)} onChange={setPct('state_rate')} /> %</span>
      </label>
      <p className="subtle" style={{ marginTop: 10 }}>
        Blended effective rate: <strong>{(blended * 100).toFixed(2)}%</strong>
        {' '}= 0.60 x {pct(value.lt_rate)}% + 0.40 x {pct(value.st_rate)}% + {pct(value.state_rate)}%
      </p>
    </div>
  )
}

// ---------- Automation (blackout windows) ----------

function AutomationTab({ value, tz, onChange }: {
  value: SettingsAutomation
  tz: string
  onChange: (v: SettingsAutomation) => void
}) {
  const windows = value.blackout_windows ?? []
  const update = (i: number, patch: Partial<BlackoutWindow>) =>
    onChange({ ...value, blackout_windows: windows.map((w, j) => (j === i ? { ...w, ...patch } : w)) })
  const remove = (i: number) =>
    onChange({ ...value, blackout_windows: windows.filter((_, j) => j !== i) })
  const add = () =>
    onChange({ ...value, blackout_windows: [...windows, { start: '12:00', end: '13:00', label: '' }] })

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>Automation</h3>
      <p className="subtle">
        Time windows when automation <strong>pauses</strong> — no signal generation and no
        auto-execution. Open positions keep their own ATM stop/target. Times are in your configured
        timezone (<code>{tz}</code>) and repeat daily; a window whose end is before its start spans
        midnight (e.g. 16:00&ndash;08:30 = overnight).
      </p>

      {windows.length === 0 ? (
        <p className="subtle">No blackout windows — automation runs whenever it&rsquo;s enabled.</p>
      ) : (
        <table className="data-table" style={{ maxWidth: 560 }}>
          <thead>
            <tr><th>Start</th><th>End</th><th>Label (optional)</th><th></th></tr>
          </thead>
          <tbody>
            {windows.map((w, i) => (
              <tr key={i}>
                <td><input type="time" value={w.start} onChange={(e) => update(i, { start: e.target.value })} /></td>
                <td><input type="time" value={w.end} onChange={(e) => update(i, { end: e.target.value })} /></td>
                <td>
                  <input type="text" placeholder="e.g. lunch, news" value={w.label}
                         onChange={(e) => update(i, { label: e.target.value })} style={{ width: '100%' }} />
                </td>
                <td><button type="button" className="danger" onClick={() => remove(i)}>Remove</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <button type="button" style={{ marginTop: 10 }} onClick={add}>+ Add window</button>
      <p className="subtle" style={{ marginTop: 10 }}>Remember to Save.</p>
    </div>
  )
}

// ---------- Data Integrity (signal <-> NT fill auditor) ----------

function IntegrityTab({ value, onChange }: {
  value: import('../api').SettingsAuditor
  onChange: (v: import('../api').SettingsAuditor) => void
}) {
  const qc = useQueryClient()
  const status = useQuery<import('../api').AuditorStatus>({
    queryKey: ['auditor', 'status'],
    queryFn: () => fetchJSON<import('../api').AuditorStatus>('/api/auditor/status'),
    refetchInterval: 30_000,
  })

  const runNow = useMutation({
    mutationFn: () => postJSON<import('../api').AuditorRunResp>('/api/auditor/run'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['auditor'] }),
  })

  const s = status.data
  const sum = s?.last_summary
  const fmt = (n?: number | null) =>
    n === null || n === undefined ? '--' : `$${n.toFixed(2)}`
  const fmtTs = (t?: string | null) => {
    if (!t) return 'never'
    const d = new Date(t)
    return isNaN(d.getTime()) ? t : d.toLocaleString()
  }

  return (
    <div>
      <h3 style={{ marginTop: 0 }}>Data Integrity</h3>
      <p className="subtle">
        Reconciles each executed signal's P&amp;L against the real NinjaTrader fills.
        The NT database is ground truth -- confidently linked trades whose paper
        number disagrees are corrected to the broker net; filled signals that can't
        be linked are flagged for review, never guessed.
      </p>

      <label className="settings-row">
        <span>Enabled</span>
        <input
          type="checkbox"
          checked={value.enabled}
          onChange={(e) => onChange({ ...value, enabled: e.target.checked })}
        />
        <span className="subtle">Run the integrity sweep automatically.</span>
      </label>

      <label className="settings-row">
        <span>Interval (minutes)</span>
        <input
          type="number"
          min={5}
          max={1440}
          value={value.interval_minutes}
          onChange={(e) => onChange({ ...value, interval_minutes: Number(e.target.value) })}
        />
        <span className="subtle">
          How often the automatic sweep runs (default 60 = hourly). Each automatic pass reviews only
          trades from the last <strong>{s?.auto_window_minutes ?? Math.round(value.interval_minutes * 1.5)} min</strong> (1.5&times; the interval). Save to apply.
        </span>
      </label>

      <div style={{ display: 'flex', alignItems: 'center', gap: 12, margin: '16px 0' }}>
        <button
          type="button"
          className="primary"
          onClick={() => runNow.mutate()}
          disabled={runNow.isPending || s?.running}
        >
          {runNow.isPending || s?.running ? 'Auditing...' : 'Full Audit (review everything)'}
        </button>
        <span className="subtle">
          Last run: {fmtTs(s?.last_run)}{s?.last_scope ? ` (${s.last_scope})` : ''}
        </span>
      </div>

      {runNow.error && <div className="card error">Audit failed: {String(runNow.error)}</div>}

      {sum && (
        <div className="settings-row" style={{ gap: 18 }}>
          <span><strong>{sum.checked}</strong> checked</span>
          <span className="pnl-neg"><strong>{sum.corrected}</strong> corrected</span>
          <span className="pnl-pos"><strong>{sum.in_sync}</strong> in sync</span>
          <span className="subtle"><strong>{sum.unverified}</strong> unverified</span>
        </div>
      )}

      <h4 style={{ marginBottom: 6 }}>Recent corrections</h4>
      {!s?.recent?.length ? (
        <p className="subtle">No corrections logged yet.</p>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table className="data-table">
            <thead>
              <tr>
                <th>Audited</th>
                <th>Signal</th>
                <th>Instrument</th>
                <th>Action</th>
                <th style={{ textAlign: 'right' }}>Paper</th>
                <th style={{ textAlign: 'right' }}>Real (fills)</th>
                <th style={{ textAlign: 'right' }}>Conf</th>
              </tr>
            </thead>
            <tbody>
              {s.recent.map((e, i) => (
                <tr key={i}>
                  <td>{fmtTs(e.checked_at)}</td>
                  <td><Link to={`/signals/${encodeURIComponent(e.signal_ts)}`}>{e.signal_ts}</Link></td>
                  <td>{e.instrument || '--'}</td>
                  <td>{e.action}</td>
                  <td style={{ textAlign: 'right' }}>{fmt(e.prev_realized)}</td>
                  <td style={{ textAlign: 'right' }}
                      className={(e.new_realized ?? 0) < 0 ? 'pnl-neg' : 'pnl-pos'}>
                    {fmt(e.new_realized)}
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    {e.confidence == null ? '--' : e.confidence.toFixed(2)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ---------- Auto-Trader ----------

function AutoTraderTab({ value, simAccounts, onChange }: {
  value: SettingsAutoTrader
  simAccounts: string[]
  onChange: (v: SettingsAutoTrader) => void
}) {
  const accountOptions = Array.from(new Set(simAccounts.filter(Boolean))).sort()
  return (
    <>
      <h3 style={{ marginTop: 0 }}>Auto-Trader <span className="subtle">(Sim-only)</span></h3>
      <p className="subtle">
        When <strong>auto trading is enabled</strong>, the NinjaTrader <code>HelmAutoTrader</code>{' '}
        strategy auto-executes qualifying signals (non-flat, created after you enabled) on the
        locked account — no manual arm needed. With it OFF you can still arm individual signals to
        stage them. An account must be set either way.
      </p>
      <p className="subtle">
        The contract / concurrency / loss / balance limits below are the <strong>global
        defaults</strong>. A LIVE or EVAL account with a per-account config on the{' '}
        <Link to="#" onClick={(e) => e.preventDefault()}>Strategy</Link> tab overrides these;
        Sim accounts (the only ones tradable in v1) always use these defaults.
      </p>

      <div className="settings-row">
        <label className="settings-checkbox">
          <input
            type="checkbox"
            checked={value.enabled}
            onChange={(e) => onChange({ ...value, enabled: e.target.checked })}
          />
          <span>
            Enable auto trading (live execution){' '}
            <span className="subtle">
              — the NT strategy auto-executes qualifying signals (no manual arm needed). With it
              off you can still arm individual signals to stage them; they execute when you turn
              this on. Flip it here or from a signal's Auto-Trader card.
            </span>
          </span>
        </label>
      </div>

      <div className="settings-row">
        <label>
          <span>Locked account (Sim only)</span>
          <select
            value={value.account}
            onChange={(e) => onChange({ ...value, account: e.target.value })}
          >
            <option value="">— none (disabled) —</option>
            {accountOptions.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
          <span className="subtle">
            The single account the strategy may act on. Only Simulation-bucket accounts are
            offered in v1. The NT strategy also refuses to run if its own account differs.
          </span>
        </label>
        <label>
          <span>Max contracts / order</span>
          <input
            type="number" min={1} max={50}
            value={value.max_contracts_per_order}
            onChange={(e) => onChange({ ...value, max_contracts_per_order: Number(e.target.value) })}
          />
          <span className="subtle">Caps the ATM total qty placed per signal.</span>
        </label>
        <label>
          <span>Max concurrent positions</span>
          <input
            type="number" min={1} max={20}
            value={value.max_concurrent}
            onChange={(e) => onChange({ ...value, max_concurrent: Number(e.target.value) })}
          />
          <span className="subtle">Max open ATM strategies at once; extra armed signals are skipped.</span>
        </label>
        <label>
          <span>Daily loss cutoff ($)</span>
          <input
            type="number" min={0} step="any"
            value={value.daily_loss_cutoff}
            onChange={(e) => onChange({ ...value, daily_loss_cutoff: Number(e.target.value) })}
          />
          <span className="subtle">Auto-disarm once session realized loss reaches this. 0 = off.</span>
        </label>
        <label>
          <span>Stop if balance &le; ($)</span>
          <input
            type="number" min={0} step="any"
            value={value.min_account_balance}
            onChange={(e) => onChange({ ...value, min_account_balance: Number(e.target.value) })}
          />
          <span className="subtle">
            Fail-safe: when the account's live equity reaches this or lower, auto-trading is forced OFF (master switch). Open trades keep their own ATM stop. 0 = off. Requires manual re-enable.
          </span>
        </label>
        <label>
          <span>Poll interval (s)</span>
          <input
            type="number" min={1} max={60}
            value={value.poll_seconds}
            onChange={(e) => onChange({ ...value, poll_seconds: Number(e.target.value) })}
          />
          <span className="subtle">How often the strategy checks the arm queue.</span>
        </label>
        <label>
          <span>Entry window (min)</span>
          <input
            type="number" min={1} max={1440}
            value={value.entry_window_minutes}
            onChange={(e) => onChange({ ...value, entry_window_minutes: Number(e.target.value) })}
          />
          <span className="subtle">Unfilled LIMIT entries cancel after this. Default 240 (4 h) matches the entry resolver.</span>
        </label>
      </div>

      <div className="settings-row">
        <label className="settings-checkbox">
          <input
            type="checkbox"
            checked={value.capture_entry_screenshot}
            onChange={(e) => onChange({ ...value, capture_entry_screenshot: e.target.checked })}
          />
          <span>
            Capture entry screenshot on auto-trades{' '}
            <span className="subtle">
              — when an auto-entered trade fills, save HelmFeed's latest chart for that instrument
              and show it on the trade's Journal entry. Needs HelmFeed running on the chart. Off by default.
            </span>
          </span>
        </label>
      </div>

      {value.enabled && !value.account && (
        <p className="subtle" style={{ color: 'var(--neg)' }}>
          Master switch is on but no account is selected — arming stays blocked until you pick one.
        </p>
      )}
    </>
  )
}

// ---------- Appearance ----------

function AppearanceTab({ value, onChange }: {
  value: SettingsAppearance
  onChange: (v: SettingsAppearance) => void
}) {
  const colorFields: Array<{ key: keyof SettingsAppearance; label: string; hint: string }> = [
    { key: 'accent', label: 'Accent',           hint: 'Links, buttons, focus rings' },
    { key: 'bg',     label: 'Background',       hint: 'Page background' },
    { key: 'panel',  label: 'Panel',            hint: 'Card / table background' },
    { key: 'border', label: 'Border',           hint: 'Lines + dividers' },
    { key: 'text',   label: 'Text',             hint: 'Primary text' },
    { key: 'muted',  label: 'Muted',            hint: 'Subtle text' },
    { key: 'pos',    label: 'Positive P&L',     hint: 'Win color' },
    { key: 'neg',    label: 'Negative P&L',     hint: 'Loss color' },
  ]
  return (
    <>
      <h3 style={{ marginTop: 0 }}>Appearance</h3>
      <p className="subtle">Visual + locale settings. Defaults are tuned for long sessions on a dark editor.</p>
      <div className="settings-row">
        <label>
          <span>Theme</span>
          <select value={value.theme} onChange={(e) => onChange({ ...value, theme: e.target.value as SettingsAppearance['theme'] })}>
            <option value="dark">Dark</option>
            <option value="light">Light</option>
            <option value="system">System</option>
          </select>
          <span className="subtle">System follows your OS preference.</span>
        </label>
        <label>
          <span>Timezone (IANA)</span>
          <input
            type="text"
            value={value.timezone}
            onChange={(e) => onChange({ ...value, timezone: e.target.value })}
            placeholder="America/Chicago"
          />
          <span className="subtle">Used for chart timestamps + session boundaries. CME futures: <code>America/Chicago</code>.</span>
        </label>
        <label>
          <span>Table page size</span>
          <input
            type="number"
            min={10}
            max={2000}
            value={value.table_page_size}
            onChange={(e) => onChange({ ...value, table_page_size: Number(e.target.value) })}
          />
          <span className="subtle">Rows per page on Trade Performance / Signal Analysis tables.</span>
        </label>
      </div>

      <h4>Colors</h4>
      <p className="subtle">Live-previewed as you change them. Save to persist.</p>
      <div className="color-grid">
        {colorFields.map(({ key, label, hint }) => (
          <label key={key} className="color-field">
            <span className="color-label">{label}</span>
            <span className="color-controls">
              <input
                type="color"
                value={value[key] as string}
                onChange={(e) => onChange({ ...value, [key]: e.target.value })}
              />
              <input
                type="text"
                value={value[key] as string}
                onChange={(e) => onChange({ ...value, [key]: e.target.value })}
                maxLength={9}
                className="hex-input"
              />
            </span>
            <span className="subtle color-hint">{hint}</span>
          </label>
        ))}
      </div>
    </>
  )
}

// ---------- AI Backend ----------

function AiTab({ value, onChange }: {
  value: SettingsAiBackend
  onChange: (v: SettingsAiBackend) => void
}) {
  const qc = useQueryClient()
  const [test, setTest] = useState<OllamaTestResp | null>(null)
  const [testing, setTesting] = useState(false)
  const runTest = async () => {
    setTesting(true)
    try {
      const r = await postJSON<OllamaTestResp>('/api/settings/test/ollama')
      setTest(r)
      // Test endpoint also refreshes the catalog -- bust the models cache so
      // dropdowns reflect any new model that just showed up.
      qc.invalidateQueries({ queryKey: ['models'] })
    } catch (e) {
      setTest({ ok: false, error: String(e) })
    } finally {
      setTesting(false)
    }
  }
  const provider = value.provider

  // Fetch the live model catalog from whichever provider is currently
  // configured on the SAVED settings doc -- the route reads
  // get_settings(), not the draft. So the dropdown reflects what's
  // actually installed on disk; tweaking the URL/key in the draft and
  // saving will refresh the list on the next render.
  const modelsQ = useQuery<ModelsResp>({
    queryKey: ['models', provider],
    queryFn:  () => fetchJSON<ModelsResp>(`/api/settings/models?provider=${provider}`),
    staleTime: 60_000,
    retry: 0,
  })
  return (
    <>
      <h3 style={{ marginTop: 0 }}>AI Backend</h3>
      <p className="subtle">
        Vision LLM that turns chart screenshots into trade proposals. Three providers supported — pick by cost / latency / privacy tradeoff. See <Link to="/support#configuration">Support → Configuration</Link> for a full comparison.
      </p>
      <div className="settings-row">
        <label>
          <span>Default provider</span>
          <select
            value={provider}
            onChange={(e) => onChange({ ...value, provider: e.target.value as SettingsAiBackend['provider'] })}
          >
            <option value="ollama">Ollama (local / LAN)</option>
            <option value="claude">Anthropic Claude (cloud)</option>
            <option value="openai">OpenAI ChatGPT (cloud)</option>
          </select>
          <span className="subtle">
            Used by any component set to "default" below. Ollama: free, on-network, ~5–15 s warm. Claude: best reasoning, ~2–4 s. OpenAI: balanced.
          </span>
        </label>
        <label>
          <span>Request timeout (s)</span>
          <input
            type="number" min={10} max={1800}
            value={value.request_timeout_s}
            onChange={(e) => onChange({ ...value, request_timeout_s: Number(e.target.value) })}
          />
          <span className="subtle">
            How long to wait before aborting an inference call. Local Ollama on iGPU: 180+ for cold starts. Cloud: 60 is plenty.
          </span>
        </label>
      </div>

      <div className="settings-row">
        <label>
          <span>News provider</span>
          <select
            value={value.news_provider ?? ''}
            onChange={(e) => onChange({ ...value, news_provider: e.target.value as SettingsAiBackend['news_provider'] })}
          >
            <option value="">Use default ({provider})</option>
            <option value="ollama">Ollama (local / LAN)</option>
            <option value="claude">Anthropic Claude (cloud)</option>
            <option value="openai">OpenAI ChatGPT (cloud)</option>
          </select>
          <span className="subtle">
            Economic-calendar (Econoday) extraction. The HTML is large — a cloud model (Claude/OpenAI) is far more reliable here than local Ollama.
          </span>
        </label>
        <label>
          <span>Signal provider</span>
          <select
            value={value.signal_provider ?? ''}
            onChange={(e) => onChange({ ...value, signal_provider: e.target.value as SettingsAiBackend['signal_provider'] })}
          >
            <option value="">Use default ({provider})</option>
            <option value="ollama">Ollama (local / LAN)</option>
            <option value="claude">Anthropic Claude (cloud)</option>
            <option value="openai">OpenAI ChatGPT (cloud)</option>
          </select>
          <span className="subtle">
            Chart/signal analysis (manual snip + auto-analysis). Leave on default unless you want a different model for trade reads.
          </span>
        </label>
      </div>

      {provider === 'ollama' && (
        <>
          <h4>Ollama config</h4>
          <p className="subtle">Run <code>ollama pull qwen2.5vl:7b</code> on the inference host before testing.</p>
          <div className="settings-row">
            <label className="span-2">
              <span>Ollama URL</span>
              <input
                type="text"
                value={value.ollama_url}
                onChange={(e) => onChange({ ...value, ollama_url: e.target.value })}
                placeholder="http://127.0.0.1:11434/api/generate"
              />
              <span className="subtle">
                Local: <code>http://127.0.0.1:11434/api/generate</code>. LAN GPU: <code>http://&lt;host&gt;:11434/api/generate</code>.
              </span>
            </label>
            <ModelPicker
              label="Model"
              hint={<>Vision-capable model. Default: <code>qwen2.5vl:7b</code>.</>}
              value={value.model}
              onChange={(m) => onChange({ ...value, model: m })}
              models={modelsQ}
            />
            <ModelPicker
              label="Fallback model"
              hint={<>Tried if primary times out. Use a smaller variant (e.g. <code>qwen2.5vl:3b</code>).</>}
              value={value.fallback_model}
              onChange={(m) => onChange({ ...value, fallback_model: m })}
              models={modelsQ}
            />
            <label>
              <span>num_ctx (tokens)</span>
              <input
                type="number" min={2048} max={131072} step={1024}
                value={value.num_ctx}
                onChange={(e) => onChange({ ...value, num_ctx: Number(e.target.value) })}
              />
              <span className="subtle">Context window. 8192 is the sweet spot; raise only if your prompt is dense.</span>
            </label>
          </div>
        </>
      )}

      {provider === 'claude' && (
        <>
          <h4>Anthropic Claude config</h4>
          <p className="subtle">
            API key from <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer">console.anthropic.com</a>.
            Cloud call — chart screenshots leave this machine.
          </p>
          <div className="settings-row">
            <label className="span-2">
              <span>API key</span>
              <input
                type="password"
                value={value.claude_api_key}
                onChange={(e) => onChange({ ...value, claude_api_key: e.target.value })}
                placeholder="sk-ant-..."
              />
              <span className="subtle">Stored at <code>~/.helm/settings.json</code>. Never committed to git.</span>
            </label>
            <ModelPicker
              label="Model"
              hint={<>Default: <code>claude-sonnet-4-6</code>. Use <code>claude-opus-4-7</code> for max quality at higher cost.</>}
              value={value.claude_model}
              onChange={(m) => onChange({ ...value, claude_model: m })}
              models={modelsQ}
              placeholder="claude-sonnet-4-6"
            />
            <label>
              <span>Max tokens</span>
              <input
                type="number" min={256} max={16384} step={128}
                value={value.claude_max_tokens}
                onChange={(e) => onChange({ ...value, claude_max_tokens: Number(e.target.value) })}
              />
              <span className="subtle">Upper bound on response size. 2048 covers a structured proposal + reasoning.</span>
            </label>
          </div>
        </>
      )}

      {provider === 'openai' && (
        <>
          <h4>OpenAI / ChatGPT config</h4>
          <p className="subtle">
            API key from <a href="https://platform.openai.com/api-keys" target="_blank" rel="noreferrer">platform.openai.com</a>.
            Cloud call — chart screenshots leave this machine.
          </p>
          <div className="settings-row">
            <label className="span-2">
              <span>API key</span>
              <input
                type="password"
                value={value.openai_api_key}
                onChange={(e) => onChange({ ...value, openai_api_key: e.target.value })}
                placeholder="sk-proj-..."
              />
              <span className="subtle">Stored at <code>~/.helm/settings.json</code>. Never committed to git.</span>
            </label>
            <ModelPicker
              label="Model"
              hint={<>Default: <code>gpt-4o</code>. <code>gpt-4o-mini</code> is ~5x cheaper but noticeably weaker on charts.</>}
              value={value.openai_model}
              onChange={(m) => onChange({ ...value, openai_model: m })}
              models={modelsQ}
              placeholder="gpt-4o"
            />
            <label>
              <span>Max tokens</span>
              <input
                type="number" min={256} max={16384} step={128}
                value={value.openai_max_tokens}
                onChange={(e) => onChange({ ...value, openai_max_tokens: Number(e.target.value) })}
              />
              <span className="subtle">Upper bound on response size. 2048 is enough for the structured proposal.</span>
            </label>
          </div>
        </>
      )}

      <div className="settings-test">
        <button onClick={runTest} disabled={testing}>
          {testing ? 'Testing…' : `Test ${provider} connection`}
        </button>
        {test && (
          <span className={test.ok ? 'pnl-pos' : 'pnl-neg'}>
            {test.ok
              ? `OK · ${test.latency_s}s · ${test.models?.length ?? 0} models · ${test.configured_model_present ? `${test.configured_model} present` : `${test.configured_model} NOT FOUND`}`
              : `FAIL · ${test.error}`}
          </span>
        )}
      </div>
    </>
  )
}

// ---------- Model picker (shared by AI tab) ----------

function ModelPicker({
  label, hint, value, onChange, models, placeholder,
}: {
  label:       string
  hint?:       React.ReactNode
  value:       string
  onChange:    (m: string) => void
  models:      UseQueryResult<ModelsResp>
  placeholder?: string
}) {
  const qc = useQueryClient()
  const r  = models.data
  const ok = !!r && r.ok && r.models.length > 0
  // Make sure the currently-saved value shows up in the dropdown even if it
  // isn't in the fetched catalog (custom local Ollama model, model deprecated
  // upstream but still working, etc.). Prepending vs appending shouldn't
  // matter since we de-dupe -- list goes: current first, then the catalog.
  const options = ok
    ? Array.from(new Set([value, ...r!.models].filter(Boolean)))
    : []

  return (
    <label>
      <span>{label}</span>
      {ok ? (
        <div className="model-picker-row">
          <select value={value} onChange={(e) => onChange(e.target.value)}>
            {options.map((m) => (
              <option key={m} value={m}>{m}{m === value && !r!.models.includes(m) ? ' (not in catalog)' : ''}</option>
            ))}
          </select>
          <button
            type="button"
            className="model-picker-refresh"
            title="Refetch the model catalog from the provider"
            onClick={() => qc.invalidateQueries({ queryKey: ['models'] })}
            disabled={models.isFetching}
          >
            {models.isFetching ? '...' : '↻'}
          </button>
        </div>
      ) : (
        <div className="model-picker-row">
          <input
            type="text"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            placeholder={placeholder}
          />
          <button
            type="button"
            className="model-picker-refresh"
            title={models.isFetching ? 'Loading...' : r?.error || 'Click to fetch the model catalog'}
            onClick={() => qc.invalidateQueries({ queryKey: ['models'] })}
            disabled={models.isFetching}
          >
            {models.isFetching ? '...' : '↻'}
          </button>
        </div>
      )}
      {hint && <span className="subtle">{hint}</span>}
      {!ok && r?.error && (
        <span className="subtle" style={{ color: 'var(--neg)' }}>
          Catalog fetch failed: {r.error}. Save the API key/URL above and click ↻ to retry.
        </span>
      )}
    </label>
  )
}

// ---------- Strategy ----------

function StrategyTab({ value, onChange, accounts, accountConfigs, onConfigsChange }: {
  value: SettingsStrategy
  onChange: (v: SettingsStrategy) => void
  accounts: SettingsAccounts
  accountConfigs: Record<string, AccountConfig>
  onConfigsChange: (c: Record<string, AccountConfig>) => void
}) {
  return (
    <>
      <h3 style={{ marginTop: 0 }}>Strategy</h3>
      <p className="subtle">
        Tunable thresholds for signal generation and outcome resolution. Recommended baseline values are documented in <Link to="/support#configuration">Support → Configuration</Link>.
      </p>
      <div className="settings-row">
        <label>
          <span>Reconciliation cap</span>
          <input
            type="number"
            min={0} max={20}
            value={value.reconciliation_cap}
            onChange={(e) => onChange({ ...value, reconciliation_cap: Number(e.target.value) })}
          />
          <span className="subtle">Open trades reconciled per manual snip.</span>
        </label>
        <label>
          <span>Retention (days)</span>
          <input
            type="number"
            min={1} max={90}
            value={value.retention_days}
            onChange={(e) => onChange({ ...value, retention_days: Number(e.target.value) })}
          />
          <span className="subtle">Feed bars + ticks older than this are pruned nightly.</span>
        </label>
        <label>
          <span>Stale bar (s)</span>
          <input
            type="number"
            min={10} max={3600}
            value={value.stale_bar_seconds}
            onChange={(e) => onChange({ ...value, stale_bar_seconds: Number(e.target.value) })}
          />
          <span className="subtle">Bars older than this skip auto-analysis (backfill safety).</span>
        </label>
      </div>

      <PerAccountConfigBlock
        accounts={accounts}
        configs={accountConfigs}
        onChange={onConfigsChange}
      />
    </>
  )
}

// ---------- Per-account trading configs (Item 3) ----------

// One card per LIVE + EVAL account (D6); Sim accounts have no card and fall back
// to the global Auto-Trader defaults at runtime. Each card holds the friendly
// name, the user-entered limits incl. the trailing-DD limit + risk-per-trade,
// and a live cash + trailing-DD readout from /api/account-configs/live.
const EMPTY_CFG: AccountConfig = {
  name: '',
  base_cash: 0,
  cash_basis_ts: '',
  risk_per_trade_value: 0,
  risk_per_trade_mode: 'percent',
  max_daily_loss: 0,
  max_concurrent_per_instrument: 1,
  max_contracts_per_instrument: 1,
  stop_if_balance_below: 0,
  trailing_dd_limit: 0,
  profit_target: 0,
}

const fmtMoneyOrDash = (n: number | null | undefined) =>
  n === null || n === undefined ? '--' : `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

const fmtSignedMoney = (n: number | null | undefined) => {
  if (n === null || n === undefined) return '--'
  const sign = n < 0 ? '-' : '+'
  return `${sign}$${Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

const fmtBasisDate = (iso: string | null | undefined) => {
  if (!iso) return '--'
  const d = new Date(iso)
  return isNaN(d.getTime()) ? iso : d.toLocaleString()
}

function PerAccountConfigBlock({ accounts, configs, onChange }: {
  accounts: SettingsAccounts
  configs: Record<string, AccountConfig>
  onChange: (c: Record<string, AccountConfig>) => void
}) {
  // LIVE + EVAL + PA. De-dupe + drop blanks; Sim is intentionally excluded.
  const ids = Array.from(new Set([...accounts.live, ...accounts.evals, ...accounts.paid]))
    .map((a) => a.trim())
    .filter(Boolean)
    .sort()

  const update = (id: string, patch: Partial<AccountConfig>) => {
    const cur = configs[id] ?? EMPTY_CFG
    onChange({ ...configs, [id]: { ...EMPTY_CFG, ...cur, ...patch } })
  }

  return (
    <>
      <h4 style={{ marginTop: 24 }}>Per-account trading config <span className="subtle">(Live + Eval + PA)</span></h4>
      <p className="subtle">
        One config per Live / Eval account. These override the global Auto-Trader defaults
        for that account (risk sizing, contract / concurrency caps, daily-loss + balance-floor
        kill-switches). Simulation accounts have no card and use the global defaults. The
        trailing max-drawdown limit is user-entered and enforced server-side against an equity
        high-water mark computed from each account's current cash (base cash + realized P&L
        from Trade Performance), so it works even when no strategy is running on the account.
      </p>
      {ids.length === 0 ? (
        <p className="subtle"><em>No Live or Eval accounts yet. Add accounts under the Accounts tab (Live / Eval) to configure them here.</em></p>
      ) : (
        ids.map((id) => (
          <AccountConfigCard
            key={id}
            id={id}
            label={accountLabel(id, accounts.names)}
            cfg={configs[id] ?? EMPTY_CFG}
            onChange={(patch) => update(id, patch)}
          />
        ))
      )}
    </>
  )
}

function AccountConfigCard({ id, label, cfg, onChange }: {
  id: string
  label: string
  cfg: AccountConfig
  onChange: (patch: Partial<AccountConfig>) => void
}) {
  const live = useQuery<AccountConfigLive>({
    queryKey: ['account-config-live', id],
    queryFn: () => fetchJSON<AccountConfigLive>(`/api/account-configs/live?account=${encodeURIComponent(id)}`),
    refetchInterval: 5_000,
  })
  const d = live.data
  return (
    <div className="account-config-card" style={{ border: '1px solid var(--border)', borderRadius: 6, padding: 14, marginBottom: 14 }}>
      <div className="settings-row">
        <label>
          <span>Config name</span>
          <input
            type="text"
            placeholder={label}
            value={cfg.name}
            onChange={(e) => onChange({ name: e.target.value })}
          />
          <span className="subtle">Account <code>{id}</code> (defaults to <strong>{label}</strong>).</span>
        </label>
        <label>
          <span>Base cash ($)</span>
          <input
            type="number" min={0} step="any"
            value={cfg.base_cash}
            onChange={(e) => onChange({ base_cash: Number(e.target.value) })}
          />
          <span className="subtle">Account cash "as of now". On save, the moment is stamped as the basis; trades that close after it adjust the current cash below.</span>
        </label>
        <label>
          <span>Current cash (computed)</span>
          <input type="text" disabled value={d ? fmtMoneyOrDash(d.cash) : '...'} />
          <span className="subtle">
            {d && d.cash === null
              ? 'Enter a base cash above and save to set the basis.'
              : `= base ${fmtMoneyOrDash(d?.base_cash)} ${fmtSignedMoney(d?.realized_since)} realized since ${fmtBasisDate(d?.cash_basis_ts)}`}
          </span>
        </label>
      </div>

      <div className="settings-row">
        <label>
          <span>Risk per trade</span>
          <input
            type="number" min={0} step="any"
            value={cfg.risk_per_trade_value}
            onChange={(e) => onChange({ risk_per_trade_value: Number(e.target.value) })}
          />
        </label>
        <label>
          <span>Risk mode</span>
          <select
            value={cfg.risk_per_trade_mode}
            onChange={(e) => onChange({ risk_per_trade_mode: e.target.value as AccountConfig['risk_per_trade_mode'] })}
          >
            <option value="percent">% of account</option>
            <option value="price">price ($)</option>
          </select>
          <span className="subtle">% of current cash (base + realized), or a fixed $ risk per trade. Sizes the ATM-less order from the stop distance.</span>
        </label>
        <label>
          <span>Max daily loss ($)</span>
          <input
            type="number" min={0} step="any"
            value={cfg.max_daily_loss}
            onChange={(e) => onChange({ max_daily_loss: Number(e.target.value) })}
          />
          <span className="subtle">0 = use global default.</span>
        </label>
        <label>
          <span>Max concurrent / instrument</span>
          <input
            type="number" min={1} max={20}
            value={cfg.max_concurrent_per_instrument}
            onChange={(e) => onChange({ max_concurrent_per_instrument: Number(e.target.value) })}
          />
          <span className="subtle">1 = one open trade per instrument (today's lock).</span>
        </label>
        <label>
          <span>Max contracts / instrument</span>
          <input
            type="number" min={1} max={50}
            value={cfg.max_contracts_per_instrument}
            onChange={(e) => onChange({ max_contracts_per_instrument: Number(e.target.value) })}
          />
          <span className="subtle">Hard ceiling on risk-sized qty.</span>
        </label>
        <label>
          <span>Stop if balance &le; ($)</span>
          <input
            type="number" min={0} step="any"
            value={cfg.stop_if_balance_below}
            onChange={(e) => onChange({ stop_if_balance_below: Number(e.target.value) })}
          />
          <span className="subtle">0 = use global default.</span>
        </label>
        <label>
          <span>Trailing max-DD limit ($)</span>
          <input
            type="number" min={0} step="any"
            value={cfg.trailing_dd_limit}
            onChange={(e) => onChange({ trailing_dd_limit: Number(e.target.value) })}
          />
          <span className="subtle">0 = off. Enforced vs the server-computed high-water mark.</span>
        </label>
      </div>

      <div className="settings-row" style={{ alignItems: 'center', gap: 16 }}>
        <span className="subtle">
          High-water mark: <strong>{fmtMoneyOrDash(d?.high_water_mark)}</strong>
          {' · '}Trailing DD used: <strong>{fmtMoneyOrDash(d?.trailing_dd_used)}</strong>
          {' / '}limit <strong>{fmtMoneyOrDash(cfg.trailing_dd_limit || null)}</strong>
        </span>
        {d?.dd_breached && (
          <span className="badge status-breach" style={{ color: 'var(--neg)' }}>TRAILING DD BREACHED — auto-trading forced OFF</span>
        )}
      </div>
    </div>
  )
}

// ---------- Accounts ----------

type Visibility = 'hidden' | 'live' | 'evals' | 'paid' | 'simulation'

function AccountsTab({ value, onChange, commissions, onCommissionsChange, accountConfigs, onConfigsChange, llcName, onLlcNameChange }: {
  value: SettingsAccounts
  onChange: (v: SettingsAccounts) => void
  commissions: Record<string, number>
  onCommissionsChange: (v: Record<string, number>) => void
  accountConfigs: Record<string, AccountConfig>
  onConfigsChange: (v: Record<string, AccountConfig>) => void
  llcName: string
  onLlcNameChange: (v: string) => void
}) {
  // include_hidden=true so the candidate list includes accounts the user
  // hasn't opted into yet -- that's the whole point of this tab.
  const dims = useQuery({
    queryKey: ['dimensions', 'all'],
    queryFn:  () => fetchJSON<DimensionsResp>('/api/dimensions?include_hidden=true'),
    staleTime: 30_000,
  })
  const discovered: string[] = dims.data?.accounts ?? []
  const discoveredSymbols: string[] = dims.data?.symbols ?? []

  const where = (acct: string): Visibility => {
    if (value.live.includes(acct))       return 'live'
    if (value.evals.includes(acct))      return 'evals'
    if (value.paid.includes(acct))       return 'paid'
    if (value.simulation.includes(acct)) return 'simulation'
    return 'hidden'
  }
  const setVisibility = (acct: string, v: Visibility) => {
    const stripped: SettingsAccounts = {
      ...value,
      live:       value.live.filter((a) => a !== acct),
      evals:      value.evals.filter((a) => a !== acct),
      paid:       value.paid.filter((a) => a !== acct),
      simulation: value.simulation.filter((a) => a !== acct),
    }
    if (v === 'hidden') {
      onChange(stripped)
      return
    }
    onChange({ ...stripped, [v]: [...stripped[v], acct] })
  }

  // Union: every discovered account + every account already in a bucket
  // (even if it has no fills yet, e.g. the NT-default Sim101/Playback101/
  // Backtest/SimBetaSIM that ship pre-listed under Simulation on first run).
  const bucketed = [...value.live, ...value.evals, ...value.paid, ...value.simulation]
  const allKnown = Array.from(new Set([...discovered, ...bucketed]))
    .filter((a) => a !== '')
    .sort()

  const names = value.names || {}
  const setName = (acct: string, name: string) => {
    const next = { ...names }
    if (name.trim() === '') delete next[acct]
    else next[acct] = name
    onChange({ ...value, names: next })
  }

  // Entity ownership (personal | llc) keyed by account id; unset -> personal.
  const entities = value.entities || {}
  const entityOf = (acct: string) => entities[acct] || 'personal'
  const setEntity = (acct: string, ent: string) => {
    onChange({ ...value, entities: { ...entities, [acct]: ent } })
  }
  const llcLabel = llcName.trim() || 'LLC'

  // Profit Target + Trailing DD live in account_configs[id]; edit them inline.
  // A blank/0 entry persists 0 (= unset/off). Profit Target is Eval-only;
  // Trailing DD applies to the funded/risk buckets (Live, Eval, PA).
  const cfgNum = (acct: string, field: 'profit_target' | 'trailing_dd_limit'): number =>
    accountConfigs[acct]?.[field] ?? 0
  const setCfgNum = (acct: string, field: 'profit_target' | 'trailing_dd_limit', raw: string) => {
    const n = parseFloat(raw)
    const cur = accountConfigs[acct] ?? EMPTY_CFG
    onConfigsChange({ ...accountConfigs, [acct]: { ...EMPTY_CFG, ...cur, [field]: isFinite(n) && n > 0 ? n : 0 } })
  }

  const visibleCount = allKnown.filter((a) => where(a) !== 'hidden').length
  const hiddenCount  = allKnown.length - visibleCount

  return (
    <>
      <h3 style={{ marginTop: 0 }}>Accounts</h3>
      <p className="subtle">
        Source of truth for which NT accounts are visible to the rest of the dashboard. The recorder keeps capturing fills for every account NT8 reports — these toggles only control what shows up in FilterBar and the Home cumulative-earnings card. <strong>PA</strong> = Paid Account (a passed eval, now funded); move an eval here once it passes. <strong>Profit target</strong> (Eval only) is the amount needed to pass; <strong>Trailing DD</strong> (Live / Eval / PA) is the trailing max-drawdown limit — both also editable on the Strategy tab. Account IDs match exactly what NT8 displays in Control Center → Accounts.
      </p>
      <p className="subtle">
        <strong>{visibleCount}</strong> visible · <strong>{hiddenCount}</strong> hidden · <strong>{allKnown.length}</strong> known
      </p>

      <div className="settings-row">
        <label>
          <span>Business entity name</span>
          <input
            type="text"
            placeholder="e.g. VQR Ventures LLC"
            value={llcName}
            onChange={(e) => onLlcNameChange(e.target.value)}
          />
          <span className="subtle">Shown wherever an account or expense is tagged "LLC" (Entity column below, Expenses page).</span>
        </label>
      </div>

      {allKnown.length === 0 ? (
        <p className="subtle"><em>No accounts known yet. The list populates once the recorder captures a fill, or once NT8 connects to an account it has previously traded on.</em></p>
      ) : (
        <table className="accounts-visibility-table">
          <thead>
            <tr>
              <th>Account ID</th>
              <th>Friendly name</th>
              <th>Entity</th>
              <th>Hidden</th>
              <th>Live</th>
              <th>Eval</th>
              <th>PA</th>
              <th>Sim</th>
              <th className="num">Profit target ($)</th>
              <th className="num">Trailing DD ($)</th>
            </tr>
          </thead>
          <tbody>
            {allKnown.map((acct) => {
              const v = where(acct)
              const inDb = discovered.includes(acct)
              return (
                <tr key={acct}>
                  <td>
                    <span className="account-id-mono">{acct}</span>
                    {!inDb && (
                      <span className="subtle" style={{ marginLeft: 8 }}>
                        (no fills yet)
                      </span>
                    )}
                  </td>
                  <td>
                    <input
                      type="text"
                      placeholder="e.g. Main Sim"
                      value={names[acct] ?? ''}
                      onChange={(e) => setName(acct, e.target.value)}
                      aria-label={`Friendly name for ${acct}`}
                      style={{ width: '100%' }}
                    />
                  </td>
                  <td>
                    <select
                      value={entityOf(acct)}
                      onChange={(e) => setEntity(acct, e.target.value)}
                      aria-label={`Entity for ${acct}`}
                    >
                      <option value="personal">Personal</option>
                      <option value="llc">{llcLabel}</option>
                    </select>
                  </td>
                  {(['hidden', 'live', 'evals', 'paid', 'simulation'] as Visibility[]).map((opt) => (
                    <td key={opt} style={{ textAlign: 'center' }}>
                      <input
                        type="radio"
                        name={`acct-${acct}`}
                        checked={v === opt}
                        onChange={() => setVisibility(acct, opt)}
                        aria-label={`${acct} -> ${opt}`}
                      />
                    </td>
                  ))}
                  <td className="num">
                    {v === 'evals' ? (
                      <input
                        type="number" min={0} step="any"
                        value={cfgNum(acct, 'profit_target') || ''}
                        onChange={(e) => setCfgNum(acct, 'profit_target', e.target.value)}
                        placeholder="0"
                        aria-label={`Profit target for ${acct}`}
                        style={{ width: 100, textAlign: 'right' }}
                      />
                    ) : <span className="subtle">--</span>}
                  </td>
                  <td className="num">
                    {(v === 'live' || v === 'evals' || v === 'paid') ? (
                      <input
                        type="number" min={0} step="any"
                        value={cfgNum(acct, 'trailing_dd_limit') || ''}
                        onChange={(e) => setCfgNum(acct, 'trailing_dd_limit', e.target.value)}
                        placeholder="0"
                        aria-label={`Trailing drawdown for ${acct}`}
                        style={{ width: 100, textAlign: 'right' }}
                      />
                    ) : <span className="subtle">--</span>}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}

      <CommissionsSection
        symbols={discoveredSymbols}
        commissions={commissions}
        onChange={onCommissionsChange}
      />
    </>
  )
}

function CommissionsSection({ symbols, commissions, onChange }: {
  symbols: string[]
  commissions: Record<string, number>
  onChange: (v: Record<string, number>) => void
}) {
  // Union of instruments NT8 has reported fills for + any already configured
  // (so a rate stays editable even after its instrument rolls to a new
  // contract month and stops appearing in recent fills).
  const known = Array.from(new Set([...symbols, ...Object.keys(commissions)]))
    .filter((s) => s !== '')
    .sort()

  const setRate = (sym: string, raw: string) => {
    const next = { ...commissions }
    const n = parseFloat(raw)
    // Blank/zero/invalid clears the override -> P&L falls back to NT8's fills.
    if (raw.trim() === '' || !isFinite(n) || n <= 0) delete next[sym]
    else next[sym] = n
    onChange(next)
  }

  const [newSym, setNewSym] = useState('')
  const addSym = () => {
    const s = newSym.trim().toUpperCase()
    if (s && !known.includes(s)) setRate(s, '0.01')
    setNewSym('')
  }

  return (
    <div style={{ marginTop: 32 }}>
      <h3>Commissions</h3>
      <p className="subtle">
        Per-instrument commission, mirroring NT8's commission templates: a rate in dollars <strong>per contract, per side</strong> (charged on each execution, so a one-contract round trip pays it twice). When set above 0 it overrides whatever commission NT8 booked on that instrument's fills in net-P&amp;L and stats. Leave blank to use NT8's reported commission — useful for Sim/Eval fills, where NT8 records $0. Keyed by master symbol (<code>MES</code>, <code>MCL</code>, …), so every contract month shares one rate.
      </p>

      {known.length === 0 ? (
        <p className="subtle"><em>No instruments known yet. They populate once the recorder captures a fill, or add one manually below.</em></p>
      ) : (
        <table className="accounts-visibility-table">
          <thead>
            <tr>
              <th>Instrument</th>
              <th>Commission ($/contract, per side)</th>
            </tr>
          </thead>
          <tbody>
            {known.map((sym) => (
              <tr key={sym}>
                <td><span className="account-id-mono">{sym}</span></td>
                <td>
                  <input
                    type="number"
                    min="0"
                    step="0.01"
                    inputMode="decimal"
                    placeholder="e.g. 0.62"
                    value={commissions[sym] ?? ''}
                    onChange={(e) => setRate(sym, e.target.value)}
                    aria-label={`Commission per contract per side for ${sym}`}
                    style={{ width: 160 }}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div style={{ marginTop: 12, display: 'flex', gap: 8, alignItems: 'center' }}>
        <input
          type="text"
          placeholder="Add instrument (e.g. NQ)"
          value={newSym}
          onChange={(e) => setNewSym(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addSym() } }}
          aria-label="Add instrument symbol"
          style={{ width: 200 }}
        />
        <button type="button" onClick={addSym} disabled={newSym.trim() === ''}>Add</button>
      </div>
    </div>
  )
}

// ---------- News ----------

const IMPACT_LEVELS = ['High', 'Medium', 'Low'] as const
const CURRENCY_OPTS = ['USD', 'EUR', 'GBP', 'JPY', 'CAD', 'AUD', 'CHF', 'NZD', 'CNY'] as const

function NewsTab({ value, onChange }: {
  value: SettingsNews
  onChange: (v: SettingsNews) => void
}) {
  const toggleListMember = (list: string[], item: string): string[] =>
    list.includes(item) ? list.filter((x) => x !== item) : [...list, item]

  const sources = value.sources ?? []
  const updateSource = (i: number, patch: Partial<NewsSource>) =>
    onChange({ ...value, sources: sources.map((s, j) => (j === i ? { ...s, ...patch } : s)) })
  const removeSource = (i: number) =>
    onChange({ ...value, sources: sources.filter((_, j) => j !== i) })
  const addSource = () =>
    onChange({ ...value, sources: [...sources, { name: '', url: '', type: 'xml', enabled: true }] })

  return (
    <>
      <h3 style={{ marginTop: 0 }}>News</h3>
      <p className="subtle">
        Economic-calendar widget on the Home page. Each source has a parsing adapter:{' '}
        <strong>xml</strong> fetches + XML-parses a ForexFactory-schema feed (no AI);{' '}
        <strong>scrape</strong> / <strong>ai-extract</strong> fetch HTML and extract events with
        the configured AI backend (needs AI reachable). A non-ForexFactory XML feed needs its own
        adapter to parse (v1 supports the FF schema only). Each scrape/ai-extract source is one AI
        call per refresh -- adding many raises cost.
      </p>

      <div className="settings-row">
        <label className="settings-checkbox">
          <input
            type="checkbox"
            checked={value.enabled}
            onChange={(e) => onChange({ ...value, enabled: e.target.checked })}
          />
          <span>Show News card on Home page</span>
        </label>
      </div>

      <h4>Sources</h4>
      {sources.length === 0 ? (
        <p className="subtle"><em>No sources configured. Add one below.</em></p>
      ) : (
        <table className="data-table" style={{ maxWidth: 760 }}>
          <thead>
            <tr><th>Name</th><th>URL</th><th>Type</th><th>On</th><th></th></tr>
          </thead>
          <tbody>
            {sources.map((s, i) => (
              <tr key={i}>
                <td><input type="text" placeholder="ForexFactory" value={s.name}
                           onChange={(e) => updateSource(i, { name: e.target.value })} /></td>
                <td><input type="text" placeholder="https://..." value={s.url}
                           onChange={(e) => updateSource(i, { url: e.target.value })} style={{ width: '100%' }} /></td>
                <td>
                  <select value={s.type} onChange={(e) => updateSource(i, { type: e.target.value as NewsSource['type'] })}>
                    <option value="xml">xml</option>
                    <option value="scrape">scrape</option>
                    <option value="ai-extract">ai-extract</option>
                  </select>
                </td>
                <td style={{ textAlign: 'center' }}>
                  <input type="checkbox" checked={s.enabled}
                         onChange={(e) => updateSource(i, { enabled: e.target.checked })} />
                </td>
                <td><button type="button" className="danger" onClick={() => removeSource(i)}>Remove</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <button type="button" style={{ marginTop: 10 }} onClick={addSource}>+ Add source</button>

      <h4>Impact filter</h4>
      <p className="subtle">Events at or above the selected levels appear on the Home card. Unchecking all hides every event.</p>
      <div className="settings-row chip-row">
        {IMPACT_LEVELS.map((lvl) => {
          const on = value.impact_filter.includes(lvl)
          return (
            <button
              key={lvl}
              type="button"
              className={'chip impact-' + lvl.toLowerCase() + (on ? ' on' : '')}
              onClick={() => onChange({ ...value, impact_filter: toggleListMember(value.impact_filter, lvl) })}
            >
              {lvl}
            </button>
          )
        })}
      </div>

      <h4>Currency filter</h4>
      <p className="subtle">Only events tagged with these currencies appear. Default USD for US futures; widen if you trade Globex products on other regions.</p>
      <div className="settings-row chip-row">
        {CURRENCY_OPTS.map((c) => {
          const on = value.currency_filter.includes(c)
          return (
            <button
              key={c}
              type="button"
              className={'chip' + (on ? ' on' : '')}
              onClick={() => onChange({ ...value, currency_filter: toggleListMember(value.currency_filter, c) })}
            >
              {c}
            </button>
          )
        })}
      </div>

      <h4>Refresh cadence</h4>
      <div className="settings-row">
        <label>
          <span>Background refresh (minutes)</span>
          <input
            type="number"
            min={5}
            max={180}
            value={value.refresh_interval_minutes}
            onChange={(e) => onChange({ ...value, refresh_interval_minutes: Number(e.target.value) })}
          />
          <span className="subtle">How often the backend pulls fresh data. The Home card also auto-refreshes its view every 5 min and on manual click.</span>
        </label>
      </div>
    </>
  )
}

