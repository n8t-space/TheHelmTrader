import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

declare global {
  interface Window {
    __helmSettingsDirty?: boolean
  }
}
import {
  fetchJSON, postJSON, putJSON,
  type OllamaTestResp, type SettingsAccounts, type SettingsAiBackend,
  type SettingsAppearance, type SettingsDoc, type SettingsResp,
  type SettingsStrategy,
} from '../api'
import { applyAppearance, cacheAppearance } from '../lib/theme'

type Tab = 'appearance' | 'ai' | 'strategy' | 'accounts'

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
    mutationFn: (doc: SettingsDoc) => {
      // Strip empty / whitespace-only account entries before PUT.
      const clean = (xs: string[]) => xs.map((s) => s.trim()).filter((s) => s !== '')
      const cleaned: SettingsDoc = {
        ...doc,
        accounts: {
          live:       clean(doc.accounts.live),
          evals:      clean(doc.accounts.evals),
          simulation: clean(doc.accounts.simulation),
        },
      }
      return putJSON<{ settings: SettingsDoc; path: string }>('/api/settings', cleaned)
    },
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
        {(['appearance', 'ai', 'strategy', 'accounts'] as Tab[]).map((t) => (
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
    : 'Accounts'
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
      <div className="settings-row">
        <label>
          <span>Theme</span>
          <select value={value.theme} onChange={(e) => onChange({ ...value, theme: e.target.value as SettingsAppearance['theme'] })}>
            <option value="dark">Dark</option>
            <option value="light">Light</option>
            <option value="system">System</option>
          </select>
        </label>
        <label>
          <span>Timezone (IANA)</span>
          <input
            type="text"
            value={value.timezone}
            onChange={(e) => onChange({ ...value, timezone: e.target.value })}
            placeholder="America/Chicago"
          />
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
      <p className="subtle">The vision LLM endpoint that turns chart screenshots into proposals.</p>
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
        </label>
        <label>
          <span>Request timeout (s)</span>
          <input
            type="number" min={10} max={1800}
            value={value.request_timeout_s}
            onChange={(e) => onChange({ ...value, request_timeout_s: Number(e.target.value) })}
          />
        </label>
      </div>

      {provider === 'ollama' && (
        <>
          <h4>Ollama config</h4>
          <div className="settings-row">
            <label className="span-2">
              <span>Ollama URL</span>
              <input
                type="text"
                value={value.ollama_url}
                onChange={(e) => onChange({ ...value, ollama_url: e.target.value })}
                placeholder="http://<workstation-LAN-IP>:11434/api/generate"
              />
            </label>
            <label>
              <span>Model</span>
              <input
                type="text"
                value={value.model}
                onChange={(e) => onChange({ ...value, model: e.target.value })}
              />
            </label>
            <label>
              <span>Fallback model</span>
              <input
                type="text"
                value={value.fallback_model}
                onChange={(e) => onChange({ ...value, fallback_model: e.target.value })}
              />
            </label>
            <label>
              <span>num_ctx (tokens)</span>
              <input
                type="number" min={2048} max={131072} step={1024}
                value={value.num_ctx}
                onChange={(e) => onChange({ ...value, num_ctx: Number(e.target.value) })}
              />
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
            </label>
            <label>
              <span>Model</span>
              <input
                type="text"
                value={value.claude_model}
                onChange={(e) => onChange({ ...value, claude_model: e.target.value })}
                placeholder="claude-sonnet-4-6"
              />
            </label>
            <label>
              <span>Max tokens</span>
              <input
                type="number" min={256} max={16384} step={128}
                value={value.claude_max_tokens}
                onChange={(e) => onChange({ ...value, claude_max_tokens: Number(e.target.value) })}
              />
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
            </label>
            <label>
              <span>Model</span>
              <input
                type="text"
                value={value.openai_model}
                onChange={(e) => onChange({ ...value, openai_model: e.target.value })}
                placeholder="gpt-4o"
              />
            </label>
            <label>
              <span>Max tokens</span>
              <input
                type="number" min={256} max={16384} step={128}
                value={value.openai_max_tokens}
                onChange={(e) => onChange({ ...value, openai_max_tokens: Number(e.target.value) })}
              />
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
      <p className="subtle">Tunable thresholds for signal generation and outcome resolution.</p>
      <div className="settings-row">
        <label>
          <span>Confidence floor</span>
          <input
            type="number"
            min={0} max={1} step={0.01}
            value={value.confidence_floor}
            onChange={(e) => onChange({ ...value, confidence_floor: Number(e.target.value) })}
          />
          <span className="subtle">Below this, the model retries up to max_attempts.</span>
        </label>
        <label>
          <span>Max attempts</span>
          <input
            type="number"
            min={1} max={5}
            value={value.max_attempts}
            onChange={(e) => onChange({ ...value, max_attempts: Number(e.target.value) })}
          />
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
    </>
  )
}

// ---------- Accounts ----------

function AccountsTab({ value, onChange }: {
  value: SettingsAccounts
  onChange: (v: SettingsAccounts) => void
}) {
  const editList = (key: keyof SettingsAccounts, list: string[]) =>
    onChange({ ...value, [key]: list })

  return (
    <>
      <h3 style={{ marginTop: 0 }}>Accounts</h3>
      <p className="subtle">
        Categorizes recorder fills for the Home page Cumulative Earnings card and the
        Trade Performance quick-filter buttons. One account ID per line.
      </p>
      <div className="settings-row">
        <AccountList
          label="Live"
          value={value.live}
          onChange={(l) => editList('live', l)}
        />
        <AccountList
          label="Evals"
          value={value.evals}
          onChange={(l) => editList('evals', l)}
        />
        <AccountList
          label="Simulation"
          value={value.simulation}
          onChange={(l) => editList('simulation', l)}
        />
      </div>
    </>
  )
}

function AccountList({ label, value, onChange }: {
  label: string
  value: string[]
  onChange: (l: string[]) => void
}) {
  // Always render at least one row so the user can type into an empty bucket.
  // The save handler upstream strips blank entries before PUT.
  const rows = value.length === 0 ? [''] : value
  const inputRefs = useRef<Array<HTMLInputElement | null>>([])
  const focusIndex = useRef<number | null>(null)

  useEffect(() => {
    if (focusIndex.current !== null) {
      inputRefs.current[focusIndex.current]?.focus()
      focusIndex.current = null
    }
  })

  const updateAt = (i: number, v: string) => {
    const next = [...rows]
    next[i] = v
    onChange(next)
  }
  const removeAt = (i: number) => {
    const next = rows.filter((_, idx) => idx !== i)
    onChange(next)
  }
  const appendBlank = () => {
    focusIndex.current = rows.length  // focus the new row once it mounts
    onChange([...rows, ''])
  }

  return (
    <div className="account-bucket">
      <span className="account-bucket-label">{label}</span>
      <div className="account-bucket-rows">
        {rows.map((acc, i) => (
          <div key={i} className="account-row">
            <input
              type="text"
              ref={(el) => { inputRefs.current[i] = el }}
              value={acc}
              placeholder="account ID"
              onChange={(e) => updateAt(i, e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') { e.preventDefault(); appendBlank() }
              }}
            />
            <button
              type="button"
              className="account-row-remove"
              onClick={() => removeAt(i)}
              title="Remove this account"
              aria-label={`remove ${acc || 'empty'}`}
              disabled={rows.length === 1 && rows[0] === ''}
            >
              {'×'}
            </button>
          </div>
        ))}
      </div>
      <button
        type="button"
        className="account-bucket-add"
        onClick={appendBlank}
      >
        + Add account
      </button>
    </div>
  )
}
