import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

declare global {
  interface Window {
    __helmSettingsDirty?: boolean
  }
}
import {
  fetchJSON, postJSON, putJSON,
  type AtmStrategiesResp, type AtmStrategy,
  type DimensionsResp, type DrawdownConfig, type OllamaTestResp,
  type SettingsAccounts, type SettingsAiBackend,
  type SettingsAppearance, type SettingsDoc, type SettingsNews,
  type SettingsResp, type SettingsStrategy,
} from '../api'
import { applyAppearance, cacheAppearance } from '../lib/theme'

type Tab = 'appearance' | 'ai' | 'strategy' | 'accounts' | 'news'

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
        {(['appearance', 'ai', 'strategy', 'accounts', 'news'] as Tab[]).map((t) => (
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
          />
        )}
        {tab === 'accounts' && (
          <AccountsTab
            value={draft.accounts}
            onChange={(a) => setDraft({ ...draft, accounts: a })}
          />
        )}
        {tab === 'news' && (
          <NewsTab
            value={draft.news}
            onChange={(n) => setDraft({ ...draft, news: n })}
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
    : 'News'
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
  const [test, setTest] = useState<OllamaTestResp | null>(null)
  const [testing, setTesting] = useState(false)
  const runTest = async () => {
    setTesting(true)
    try {
      const r = await postJSON<OllamaTestResp>('/api/settings/test/ollama')
      setTest(r)
    } catch (e) {
      setTest({ ok: false, error: String(e) })
    } finally {
      setTesting(false)
    }
  }
  const provider = value.provider
  return (
    <>
      <h3 style={{ marginTop: 0 }}>AI Backend</h3>
      <p className="subtle">
        Vision LLM that turns chart screenshots into trade proposals. Three providers supported — pick by cost / latency / privacy tradeoff. See <Link to="/support#configuration">Support → Configuration</Link> for a full comparison.
      </p>
      <div className="settings-row">
        <label>
          <span>Provider</span>
          <select
            value={provider}
            onChange={(e) => onChange({ ...value, provider: e.target.value as SettingsAiBackend['provider'] })}
          >
            <option value="ollama">Ollama (local / LAN)</option>
            <option value="claude">Anthropic Claude (cloud)</option>
            <option value="openai">OpenAI ChatGPT (cloud)</option>
          </select>
          <span className="subtle">
            Ollama: free, on-network, ~5–15 s warm. Claude: best reasoning, ~2–4 s, ~$0.01–0.03/snip. OpenAI: balanced, ~2–5 s, ~$0.005–0.02/snip.
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
            <label>
              <span>Model</span>
              <input
                type="text"
                value={value.model}
                onChange={(e) => onChange({ ...value, model: e.target.value })}
              />
              <span className="subtle">Vision-capable model. Default: <code>qwen2.5vl:7b</code>.</span>
            </label>
            <label>
              <span>Fallback model</span>
              <input
                type="text"
                value={value.fallback_model}
                onChange={(e) => onChange({ ...value, fallback_model: e.target.value })}
              />
              <span className="subtle">Tried if primary times out. Use a smaller variant (e.g. <code>qwen2.5vl:3b</code>).</span>
            </label>
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
            <label>
              <span>Model</span>
              <input
                type="text"
                value={value.claude_model}
                onChange={(e) => onChange({ ...value, claude_model: e.target.value })}
                placeholder="claude-sonnet-4-6"
              />
              <span className="subtle">Default: <code>claude-sonnet-4-6</code>. Use <code>claude-opus-4-7</code> for max quality at higher cost.</span>
            </label>
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
            <label>
              <span>Model</span>
              <input
                type="text"
                value={value.openai_model}
                onChange={(e) => onChange({ ...value, openai_model: e.target.value })}
                placeholder="gpt-4o"
              />
              <span className="subtle">Default: <code>gpt-4o</code>. <code>gpt-4o-mini</code> is ~5x cheaper but noticeably weaker on charts.</span>
            </label>
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

// ---------- Strategy ----------

function StrategyTab({ value, onChange }: {
  value: SettingsStrategy
  onChange: (v: SettingsStrategy) => void
}) {
  return (
    <>
      <h3 style={{ marginTop: 0 }}>Strategy</h3>
      <p className="subtle">
        Tunable thresholds for signal generation and outcome resolution. Recommended baseline values are documented in <Link to="/support#configuration">Support → Configuration</Link>.
      </p>
      <div className="settings-row">
        <label>
          <span>Confidence floor</span>
          <input
            type="number"
            min={0} max={1} step={0.01}
            value={value.confidence_floor}
            onChange={(e) => onChange({ ...value, confidence_floor: Number(e.target.value) })}
          />
          <span className="subtle">Reject proposals below this (0–1). Default <code>0.65</code>. Higher = fewer signals; lower = more.</span>
        </label>
        <label>
          <span>Max attempts</span>
          <input
            type="number"
            min={1} max={5}
            value={value.max_attempts}
            onChange={(e) => onChange({ ...value, max_attempts: Number(e.target.value) })}
          />
          <span className="subtle">Retry budget when below floor. Each attempt costs latency + (for cloud providers) tokens.</span>
        </label>
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

      <ExistingStrategiesBlock />
    </>
  )
}

// ---------- Existing ATM strategies (read from NT8) ----------

interface ParsedStrategyName {
  instrument?: string
  style?:      string
  contracts?:  number
  stopTicks?:  number
  targetTicks?: number
  isStopOnly?: boolean
}

// Convention from the 2026-05-22 ATM family:
//   {INSTR}_{STYLE}_{N}c_{stop}-{target}      e.g. MES_SCALP_2c_6-15
//   {INSTR}_{STYLE}_1c_brk                    e.g. MCL_INTRA_1c_brk  (stop-only sibling)
// Legacy hand-named templates fall through the regex and only carry name+XML data.
function parseStrategyName(name: string): ParsedStrategyName {
  const m = name.match(/^([A-Z]+)_([A-Z]+)_(\d+)c(?:_(\d+)-(\d+)|_brk)$/i)
  if (!m) return {}
  const [, instrument, style, qty, stop, target] = m
  return {
    instrument,
    style:       style.toUpperCase(),
    contracts:   Number(qty),
    stopTicks:   stop  ? Number(stop)  : undefined,
    targetTicks: target ? Number(target) : undefined,
    isStopOnly:  !target,
  }
}

const STYLE_BLURBS: Record<string, string> = {
  SCALP: 'Fast in/out -- intraday move catcher, tight stop',
  INTRA: 'Intraday swing -- multi-hour hold inside the session',
  SWING: 'Multi-session hold -- wider stop, larger target',
  RUN:   'Runner -- minimal stop, ride the trend with trail',
}

function describeStrategy(s: AtmStrategy): string {
  const p = parseStrategyName(s.name)
  const parts: string[] = []

  if (p.instrument && p.style && p.contracts) {
    const scaleNote = p.contracts > 1 ? `${p.contracts}-contract scale-out` : '1-contract single bracket'
    const styleBlurb = STYLE_BLURBS[p.style] || p.style.toLowerCase()
    parts.push(`${p.instrument} ${scaleNote} ${styleBlurb.toLowerCase()}.`)
  } else if (s.total_qty && s.bracket_count) {
    parts.push(`${s.total_qty}-contract template, ${s.bracket_count} bracket${s.bracket_count > 1 ? 's' : ''}.`)
  }

  const stop   = p.stopTicks   ?? s.stop_ticks_min
  const target = p.targetTicks ?? s.target_ticks_max
  if (stop && target) {
    const rr = (target / stop).toFixed(target / stop >= 10 ? 0 : 1)
    parts.push(`${stop}t stop / ${target}t target (1:${rr}).`)
  } else if (stop) {
    parts.push(`${stop}t stop -- stop-only sibling, runner trails to a stop.`)
  }

  const flags: string[] = []
  if (s.AutoBreakEvenPlusProfit && s.AutoBreakEvenPlusProfit !== '0') flags.push(`BE+${s.AutoBreakEvenPlusProfit}`)
  if (s.AutoTrail  && s.AutoTrail  !== '0') flags.push(`trail ${s.AutoTrail}`)
  if (s.AutoChase  && s.AutoChase  !== '0') flags.push(`chase ${s.AutoChase}`)
  if (flags.length) parts.push(`Auto: ${flags.join(', ')}.`)

  return parts.join(' ') || 'Legacy template -- no derived description.'
}

function ExistingStrategiesBlock() {
  const q = useQuery<AtmStrategiesResp>({
    queryKey: ['atm-strategies'],
    queryFn:  () => fetchJSON<AtmStrategiesResp>('/api/atm-strategies'),
    staleTime: 30_000,
  })

  if (q.isLoading) return (
    <>
      <h4 style={{ marginTop: 24 }}>Existing ATM strategies (NT8)</h4>
      <p className="subtle">Loading templates from NT8...</p>
    </>
  )

  if (q.error || !q.data) return (
    <>
      <h4 style={{ marginTop: 24 }}>Existing ATM strategies (NT8)</h4>
      <p className="subtle">Could not read NT8 templates: {String(q.error)}</p>
    </>
  )

  const data = q.data
  if (!data.exists) return (
    <>
      <h4 style={{ marginTop: 24 }}>Existing ATM strategies (NT8)</h4>
      <p className="subtle">{data.warning || 'NT8 templates folder not found.'}</p>
    </>
  )

  // Group by instrument prefix when the convention applies; ungrouped fall in 'Other'.
  const groups: Record<string, AtmStrategy[]> = {}
  for (const s of data.strategies) {
    const p = parseStrategyName(s.name)
    const key = p.instrument ?? 'Other'
    groups[key] = groups[key] ?? []
    groups[key].push(s)
  }
  const groupKeys = Object.keys(groups).sort((a, b) => a === 'Other' ? 1 : b === 'Other' ? -1 : a.localeCompare(b))

  return (
    <>
      <h4 style={{ marginTop: 24 }}>Existing ATM strategies (NT8)</h4>
      <p className="subtle">
        Templates parsed from <code>{data.templates_dir}</code>. {data.count ?? data.strategies.length} found.
        These are what the bot's proposals are pinned to and what the Signal Analysis table's <code>ATM Strategy</code> column references. NT8 is the source of truth -- edit / add templates in the NT8 ATM editor and refresh this page.
      </p>

      {data.strategies.length === 0 ? (
        <p className="subtle"><em>No ATM templates found in that folder.</em></p>
      ) : (
        <div className="atm-strategy-groups">
          {groupKeys.map((g) => (
            <div key={g} className="atm-strategy-group">
              <h5 className="atm-strategy-group-head">{g}</h5>
              <div className="atm-strategy-list">
                {groups[g].map((s) => (
                  <div key={s.name} className="atm-strategy-card">
                    <div className="atm-strategy-name">{s.name}</div>
                    <div className="atm-strategy-desc">{describeStrategy(s)}</div>
                    <div className="atm-strategy-meta subtle">
                      {s.bracket_count !== undefined && <span>{s.bracket_count} bracket{s.bracket_count === 1 ? '' : 's'}</span>}
                      {s.total_qty !== undefined && <span>{s.total_qty}c qty</span>}
                      {s.stop_ticks_min !== undefined && <span>stop {s.stop_ticks_min}t</span>}
                      {s.target_ticks_max !== undefined && <span>target {s.target_ticks_max}t</span>}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  )
}

// ---------- Accounts ----------

type Visibility = 'hidden' | 'live' | 'evals' | 'simulation'

function AccountsTab({ value, onChange }: {
  value: SettingsAccounts
  onChange: (v: SettingsAccounts) => void
}) {
  // include_hidden=true so the candidate list includes accounts the user
  // hasn't opted into yet -- that's the whole point of this tab.
  const dims = useQuery({
    queryKey: ['dimensions', 'all'],
    queryFn:  () => fetchJSON<DimensionsResp>('/api/dimensions?include_hidden=true'),
    staleTime: 30_000,
  })
  const discovered: string[] = dims.data?.accounts ?? []

  const where = (acct: string): Visibility => {
    if (value.live.includes(acct))       return 'live'
    if (value.evals.includes(acct))      return 'evals'
    if (value.simulation.includes(acct)) return 'simulation'
    return 'hidden'
  }
  const setVisibility = (acct: string, v: Visibility) => {
    const stripped: SettingsAccounts = {
      ...value,
      live:       value.live.filter((a) => a !== acct),
      evals:      value.evals.filter((a) => a !== acct),
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
  const bucketed = [...value.live, ...value.evals, ...value.simulation]
  const allKnown = Array.from(new Set([...discovered, ...bucketed]))
    .filter((a) => a !== '')
    .sort()

  const visibleCount = allKnown.filter((a) => where(a) !== 'hidden').length
  const hiddenCount  = allKnown.length - visibleCount

  return (
    <>
      <h3 style={{ marginTop: 0 }}>Accounts</h3>
      <p className="subtle">
        Source of truth for which NT accounts are visible to the rest of the dashboard. The recorder keeps capturing fills for every account NT8 reports — these toggles only control what shows up in FilterBar, the Home cumulative-earnings card, and the Drawdown tracker. Account IDs match exactly what NT8 displays in Control Center → Accounts.
      </p>
      <p className="subtle">
        <strong>{visibleCount}</strong> visible · <strong>{hiddenCount}</strong> hidden · <strong>{allKnown.length}</strong> known
      </p>

      {allKnown.length === 0 ? (
        <p className="subtle"><em>No accounts known yet. The list populates once the recorder captures a fill, or once NT8 connects to an account it has previously traded on.</em></p>
      ) : (
        <table className="accounts-visibility-table">
          <thead>
            <tr>
              <th>Account ID</th>
              <th>Hidden</th>
              <th>Live</th>
              <th>Eval</th>
              <th>Sim</th>
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
                  {(['hidden', 'live', 'evals', 'simulation'] as Visibility[]).map((opt) => (
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
                </tr>
              )
            })}
          </tbody>
        </table>
      )}

      <DrawdownTrackingBlock
        accounts={value}
        onChange={(d) => onChange({ ...value, drawdowns: d })}
      />
    </>
  )
}

function DrawdownTrackingBlock({
  accounts, onChange,
}: {
  accounts: SettingsAccounts
  onChange: (d: Record<string, DrawdownConfig>) => void
}) {
  const drawdowns = accounts.drawdowns || {}
  const tracked = Object.keys(drawdowns).sort()
  // Eligible candidates = live + evals (sim accounts don't have DD limits).
  const candidates = [...accounts.live, ...accounts.evals]
    .map((a) => a.trim())
    .filter((a) => a !== '' && !(a in drawdowns))
    .sort()

  const update = (acct: string, patch: Partial<DrawdownConfig>) => {
    onChange({ ...drawdowns, [acct]: { ...drawdowns[acct], ...patch } })
  }
  const remove = (acct: string) => {
    const next = { ...drawdowns }
    delete next[acct]
    onChange(next)
  }
  const add = (acct: string) => {
    if (!acct || acct in drawdowns) return
    onChange({
      ...drawdowns,
      [acct]: {
        starting_balance: 50000,
        trailing_drawdown: 2500,
        daily_drawdown: 1500,
        profit_target: 3000,
      },
    })
  }

  return (
    <>
      <h4 style={{ marginTop: 24 }}>Drawdown tracking</h4>
      <p className="subtle">
        Track prop-firm drawdown limits per account. Only accounts listed here appear in the Home page Drawdown card.
        Defaults match a typical $50K Eval (trailing $2,500 / daily $1,500 / profit target $3,000) — edit as needed.
      </p>
      {tracked.length === 0 && (
        <p className="subtle"><em>No accounts tracked yet. Add one from the dropdown below.</em></p>
      )}
      {tracked.map((acct) => {
        const c = drawdowns[acct]
        return (
          <div key={acct} className="drawdown-row">
            <div className="drawdown-row-head">
              <strong>{acct}</strong>
              <button
                type="button"
                className="account-row-remove"
                onClick={() => remove(acct)}
                title={`Stop tracking ${acct}`}
                aria-label={`stop tracking ${acct}`}
              >
                {'×'}
              </button>
            </div>
            <div className="settings-row drawdown-row-fields">
              <label>
                <span>Starting balance ($)</span>
                <input
                  type="number" min={0} step="any"
                  value={c.starting_balance}
                  onChange={(e) => update(acct, { starting_balance: Number(e.target.value) })}
                />
              </label>
              <label>
                <span>Trailing DD ($)</span>
                <input
                  type="number" min={0} step="any"
                  value={c.trailing_drawdown}
                  onChange={(e) => update(acct, { trailing_drawdown: Number(e.target.value) })}
                />
                <span className="subtle">Distance from peak balance.</span>
              </label>
              <label>
                <span>Daily DD ($)</span>
                <input
                  type="number" min={0} step="any"
                  value={c.daily_drawdown}
                  onChange={(e) => update(acct, { daily_drawdown: Number(e.target.value) })}
                />
                <span className="subtle">Loss-per-day limit.</span>
              </label>
              <label>
                <span>Profit target ($)</span>
                <input
                  type="number" min={0} step="any"
                  value={c.profit_target}
                  onChange={(e) => update(acct, { profit_target: Number(e.target.value) })}
                />
                <span className="subtle">Eval pass threshold.</span>
              </label>
            </div>
          </div>
        )
      })}
      {candidates.length > 0 ? (
        <div className="drawdown-add-row">
          <span className="subtle">Track new account:</span>
          <select
            defaultValue=""
            onChange={(e) => { if (e.target.value) { add(e.target.value); e.currentTarget.value = '' } }}
          >
            <option value="">— pick an account —</option>
            {candidates.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        </div>
      ) : (
        <p className="subtle"><em>All Live + Eval accounts are already tracked. Add more under Live / Evals above to track them here.</em></p>
      )}
    </>
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

  return (
    <>
      <h3 style={{ marginTop: 0 }}>News</h3>
      <p className="subtle">
        Economic-calendar widget on the Home page. ForexFactory pulls from the public XML feed and works offline-ish (one HTTP call). Econoday scrapes the rendered page and requires the configured AI backend to be reachable -- the chart shows a precheck hint if it isn't.
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
      <div className="settings-row">
        <label className="settings-checkbox">
          <input
            type="checkbox"
            checked={value.forexfactory_enabled}
            onChange={(e) => onChange({ ...value, forexfactory_enabled: e.target.checked })}
          />
          <span>ForexFactory <span className="subtle">(XML feed, no AI required)</span></span>
        </label>
        <label className="settings-checkbox">
          <input
            type="checkbox"
            checked={value.econoday_enabled}
            onChange={(e) => onChange({ ...value, econoday_enabled: e.target.checked })}
          />
          <span>Econoday <span className="subtle">(scraped + AI-extracted; needs working AI backend)</span></span>
        </label>
      </div>

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

