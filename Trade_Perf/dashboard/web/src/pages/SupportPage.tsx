import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { fetchJSON, postJSON } from '../api'

interface VersionResp {
  current_sha: string | null
  current_short: string | null
  latest_sha: string | null
  latest_short: string | null
  commits_behind: number
  update_available: boolean
  last_checked: number | null
  last_error: string | null
  is_git_checkout: boolean | null
  remote: string
  branch: string
}

type Tab = 'overview' | 'update' | 'troubleshooting' | 'configuration'

const TABS: Array<{ key: Tab; label: string }> = [
  { key: 'overview',        label: 'Overview' },
  { key: 'update',          label: 'Update' },
  { key: 'troubleshooting', label: 'Troubleshooting' },
  { key: 'configuration',   label: 'Configuration' },
]

function fmtChecked(ts: number | null): string {
  if (!ts) return 'never'
  return new Date(ts * 1000).toLocaleString()
}

function tabFromHash(hash: string): Tab {
  const t = hash.replace(/^#/, '') as Tab
  return TABS.find((x) => x.key === t)?.key ?? 'overview'
}

export function SupportPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const [tab, setTab] = useState<Tab>(() => tabFromHash(location.hash))

  // Deep-linkable via /support#update, /support#troubleshooting, etc.
  const switchTab = (t: Tab) => {
    setTab(t)
    navigate(`/support#${t}`, { replace: true })
  }

  return (
    <>
      <div className="card support-card">
        <h2 style={{ margin: 0 }}>Support</h2>
        <p className="support-prose subtle" style={{ margin: '4px 0 0' }}>
          Operational reference for The Helm — update procedure, troubleshooting, and
          recommended configuration.
        </p>
      </div>

      <div className="card settings-tabs">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            className={'tab' + (tab === t.key ? ' on' : '')}
            onClick={() => switchTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'overview'        && <OverviewTab onJump={switchTab} />}
      {tab === 'update'          && <UpdateTab />}
      {tab === 'troubleshooting' && <TroubleshootingTab />}
      {tab === 'configuration'   && <ConfigurationTab />}
    </>
  )
}

// ---------- Overview ----------

function OverviewTab({ onJump }: { onJump: (t: Tab) => void }) {
  return (
    <>
      <VersionCard />
      <div className="card support-card">
        <h2>Where to go next</h2>
        <ul className="support-list">
          <li>
            <button className="link-button" onClick={() => onJump('update')}>Update</button>
            {' — '}step-by-step install instructions for a new release.
          </li>
          <li>
            <button className="link-button" onClick={() => onJump('troubleshooting')}>Troubleshooting</button>
            {' — '}what to check when something stops working.
          </li>
          <li>
            <button className="link-button" onClick={() => onJump('configuration')}>Configuration</button>
            {' — '}recommended settings for AI backend, strategy thresholds, accounts, and indicators.
          </li>
          <li>
            <Link to="/settings">Settings</Link>{' — '}edit the live config.
          </li>
          <li>
            <Link to="/health">Health</Link>{' — '}log tail and bot latency stats.
          </li>
        </ul>
      </div>
      <UninstallCard />
      <HelpCard />
    </>
  )
}

// ---------- Update ----------

function UpdateTab() {
  return (
    <>
      <VersionCard />
      <div className="card support-card">
        <h2>How to update</h2>
        <p className="support-prose">
          The dashboard checks for updates every 6 hours and shows a banner at the top of every page when new commits are available on <code>main</code>. To install an update:
        </p>
        <ol className="support-steps">
          <li>Open <strong>PowerShell as Administrator</strong>.</li>
          <li>
            Pull the latest code and re-run the idempotent installer:
            <pre className="support-code">{`cd $HOME\\Documents\\Projects\\TheHelmTrader
git pull
.\\install.ps1`}</pre>
          </li>
          <li>
            Restart the dashboard service so it loads the new Python/API code:
            <pre className="support-code">{`Restart-Service HelmDashboardWatchdog`}</pre>
          </li>
          <li>
            <strong>Only if NinjaScript files changed</strong> (anything under <code>_Helm Locker\\*.cs</code>): open NinjaTrader, press <kbd>F11</kbd> to open the NinjaScript Editor, then <kbd>F5</kbd> to compile. Look for "Compile succeeded" in the bottom panel.
          </li>
          <li>
            Hard-refresh this dashboard tab with <kbd>Ctrl</kbd>+<kbd>F5</kbd> to bust the cached SPA bundle.
          </li>
        </ol>
        <p className="support-prose subtle">
          Received a release zip instead of using git? Unzip over the existing checkout, then run the same commands from step 2 onward. The update banner won't appear on zip installs (no <code>.git</code> directory to query) — check this tab manually after major changes.
        </p>
        <p className="support-prose subtle">
          Partial reruns: pass <code>-SkipPrereqs -SkipService</code> to <code>install.ps1</code> for a frontend-only refresh, or any combination of <code>-SkipPrereqs / -SkipNsIndicators / -SkipRecorder / -SkipService</code>.
        </p>
      </div>
      <div className="card support-card">
        <h2>What's preserved across updates</h2>
        <p className="support-prose">
          Neither <code>git pull</code> nor <code>install.ps1</code> touches your data:
        </p>
        <ul className="support-list">
          <li><code>%USERPROFILE%\.helm\settings.json</code> — settings + API keys (lives outside the repo)</li>
          <li><code>Trade_Perf\trades.db</code> — NT fill mirror (gitignored, recorder migrates schema in place)</li>
          <li><code>TradingBot\app\data\signals.jsonl</code> — LLM proposals + journal (gitignored, append-only)</li>
          <li><code>TradingBot\app\data\feed.db</code> — live bars + ticks (gitignored)</li>
          <li>NT8's own <code>NinjaTrader.sqlite</code> — never touched by Helm</li>
        </ul>
      </div>
    </>
  )
}

// ---------- Troubleshooting ----------

function TroubleshootingTab() {
  return (
    <>
      <div className="card support-card">
        <h2>Common issues</h2>
        <dl className="support-faq">
          <dt>Dashboard won't load (browser error)</dt>
          <dd>
            The dashboard only runs while NinjaTrader is open. Verify both are running:
            <pre className="support-code">{`Get-Service HelmDashboardWatchdog        # Status should be Running
Get-Process NinjaTrader -ErrorAction SilentlyContinue`}</pre>
            If the service is stopped, start it: <code>Start-Service HelmDashboardWatchdog</code>. If it's missing entirely, re-run <code>.\install.ps1</code>.
          </dd>

          <dt>Ctrl+Shift+F does nothing in NinjaTrader</dt>
          <dd>
            The HelmAnalyzer indicator must be added to the chart. Right-click chart {' → '} Indicators {' → '} HelmAnalyzer. If it's not in the list, NS hasn't compiled — open NinjaScript Editor (<kbd>F11</kbd>) and Compile (<kbd>F5</kbd>).
          </dd>

          <dt>Snip overlay doesn't open after a reboot</dt>
          <dd>
            The Windows Snipping URI handler needs to be warmed up in the user session. From a regular (non-elevated) PowerShell:
            <pre className="support-code">{`Start-Process "ms-screenclip:"
# Cancel the overlay that opens, then:
Restart-Service HelmDashboardWatchdog`}</pre>
            Newer NinjaScript builds (post-2026-05-12) embed the screenshot directly and bypass this — make sure the indicators in <code>_Helm Locker\\</code> are up to date.
          </dd>

          <dt>Update banner says "fetch failed"</dt>
          <dd>
            The dashboard runs <code>git fetch</code> over SSH to GitHub. Verify your SSH key is loaded and reachable for the user account running the service:
            <pre className="support-code">{`ssh -T git@github.com
cd $HOME\\Documents\\Projects\\TheHelmTrader
git fetch origin main`}</pre>
            If <code>ssh -T</code> fails, your SSH agent isn't running or the key isn't loaded. Check <code>%USERPROFILE%\\.ssh\\</code> and <code>%USERPROFILE%\\.ssh\\config</code>.
          </dd>

          <dt>"No fills" in Trade Performance</dt>
          <dd>
            The recorder mirrors NT8's SQLite every few seconds. If trades aren't appearing, confirm the recorder is running:
            <pre className="support-code">{`Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*recorder.py*' }`}</pre>
            If nothing returns, launch it via the Startup shortcut: <code>%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\NT8 Trade Recorder.lnk</code>.
          </dd>

          <dt>AI proposals aren't being generated</dt>
          <dd>
            Open <strong>Settings {' → '} AI Backend</strong> and click <strong>Test connection</strong>. A red badge means the configured provider (Ollama / Claude / OpenAI) isn't reachable. For Ollama, check the URL and that the model is pulled (<code>ollama list</code>). For Claude / OpenAI, verify the API key.
          </dd>
        </dl>
      </div>

      <div className="card support-card">
        <h2>Where to find logs</h2>
        <dl className="support-faq">
          <dt>Unified bot + dashboard log</dt>
          <dd>
            <code>TradingBot\app\data\tradebot.log</code> — also tailed live on the <Link to="/health">Health page</Link>.
          </dd>
          <dt>Watchdog log</dt>
          <dd><code>Trade_Perf\data\watchdog.log</code> — NSSM service start/stop events and uvicorn lifecycle.</dd>
          <dt>NinjaScript output</dt>
          <dd>NinjaTrader {' → '} Control Center {' → '} New {' → '} NinjaScript Output. <code>HelmAnalyzer</code> and <code>HelmFeed</code> log here when triggered.</dd>
          <dt>Windows Event Log</dt>
          <dd>Run <code>eventvwr.msc</code> and look under Windows Logs {' → '} System for <code>HelmDashboardWatchdog</code> service crashes.</dd>
        </dl>
      </div>
    </>
  )
}

// ---------- Configuration ----------

function ConfigurationTab() {
  return (
    <>
      <div className="card support-card">
        <h2>Ideal configuration</h2>
        <p className="support-prose">
          Recommended baseline for a fresh install. Everything here is editable on the <Link to="/settings">Settings</Link> page; this tab explains <em>why</em> each value matters.
        </p>
        <p className="support-prose subtle">
          The same content lives at <code>CONFIGURATION.md</code> in the repo root, so you can read it in an editor without opening the dashboard.
        </p>
      </div>

      <div className="card support-card">
        <h2>1. AI Backend</h2>
        <p className="support-prose">
          The vision LLM that turns chart screenshots into trade proposals. Picked on <strong>Settings {' → '} AI Backend</strong>.
        </p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Provider</th><th>Cost</th><th>Warm latency</th><th>Privacy</th><th>Best for</th></tr>
            </thead>
            <tbody>
              <tr>
                <td><strong>Ollama</strong> (local/LAN)</td>
                <td>$0</td>
                <td>5–15 s</td>
                <td>On-network</td>
                <td>Volume / sensitive charts</td>
              </tr>
              <tr>
                <td><strong>Claude</strong> sonnet-4-6</td>
                <td>~$0.01–0.03/snip</td>
                <td>2–4 s</td>
                <td>Anthropic cloud</td>
                <td>Important / conviction trades</td>
              </tr>
              <tr>
                <td><strong>OpenAI</strong> gpt-4o</td>
                <td>~$0.005–0.02/snip</td>
                <td>2–5 s</td>
                <td>OpenAI cloud</td>
                <td>Balanced cost/quality</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p className="support-prose"><strong>Recommended:</strong> Ollama for volume, Claude for conviction. Switch per session via Settings.</p>

        <h3 className="support-h3">Ollama (recommended default)</h3>
        <ul className="support-list">
          <li><code>Provider</code>: <code>ollama</code></li>
          <li><code>Ollama URL</code>: <code>http://127.0.0.1:11434/api/generate</code> (local) or <code>http://&lt;host&gt;:11434/api/generate</code> (LAN GPU)</li>
          <li><code>Model</code>: <code>qwen2.5vl:7b</code> — run <code>ollama pull qwen2.5vl:7b</code> first</li>
          <li><code>Fallback model</code>: <code>qwen2.5vl:3b</code></li>
          <li><code>num_ctx</code>: <code>8192</code></li>
          <li><code>Request timeout</code>: <code>180</code> s (iGPU cold start can take 30–60s)</li>
        </ul>

        <h3 className="support-h3">Claude</h3>
        <ul className="support-list">
          <li><code>API key</code>: from <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer">console.anthropic.com</a></li>
          <li><code>Model</code>: <code>claude-sonnet-4-6</code> (or <code>claude-opus-4-7</code> for max quality / higher cost)</li>
          <li><code>Max tokens</code>: <code>2048</code></li>
          <li><code>Request timeout</code>: <code>60</code> s</li>
        </ul>

        <h3 className="support-h3">OpenAI</h3>
        <ul className="support-list">
          <li><code>API key</code>: from <a href="https://platform.openai.com/api-keys" target="_blank" rel="noreferrer">platform.openai.com</a></li>
          <li><code>Model</code>: <code>gpt-4o</code> (<code>gpt-4o-mini</code> is cheaper but weaker on chart reading)</li>
          <li><code>Max tokens</code>: <code>2048</code></li>
          <li><code>Request timeout</code>: <code>60</code> s</li>
        </ul>
      </div>

      <div className="card support-card">
        <h2>2. Strategy thresholds</h2>
        <p className="support-prose">
          <strong>Settings {' → '} Strategy</strong>. Controls signal acceptance and data cleanup.
        </p>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Field</th><th>Recommended</th><th>What it does</th></tr></thead>
            <tbody>
              <tr><td>Confidence floor</td><td><code>0.65</code></td><td>Rejects sub-0.65 proposals + triggers a retry. Up for less noise, down for more setups.</td></tr>
              <tr><td>Max attempts</td><td><code>3</code></td><td>Retry budget when below floor. More attempts = more cost/latency per snip.</td></tr>
              <tr><td>Reconciliation cap</td><td><code>5</code></td><td>Max in-flight trades reconciliation pass touches per manual snip.</td></tr>
              <tr><td>Retention (days)</td><td><code>14</code></td><td>Feed.db auto-prune window. Covers the outcome resolver with margin.</td></tr>
              <tr><td>Stale bar (s)</td><td><code>300</code></td><td>Skip auto-analysis if latest bar is older than this. Prevents weekend backfill triggers.</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div className="card support-card">
        <h2>3. Accounts</h2>
        <p className="support-prose">
          <strong>Settings {' → '} Accounts</strong>. Categorizes your NT account IDs into Live / Evals / Simulation buckets. Drives the Home page Cumulative Earnings card and the Trade Performance quick-filter buttons.
        </p>
        <p className="support-prose">
          Pre-listed sims: <code>Sim101</code>, <code>Playback101</code>, <code>Backtest</code>, <code>SimBetaSIM</code>. Copy live broker account IDs exactly as NT8 reports them in the Control Center → Accounts tab.
        </p>
      </div>

      <div className="card support-card">
        <h2>4. Auto Analysis (Home page)</h2>
        <p className="support-prose">
          Headless analysis runs without you pressing Ctrl+Shift+F. Configured on the <Link to="/">Home page</Link> → Auto Analysis card. Up to 4 instrument/period slots.
        </p>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Slot</th><th>Instrument</th><th>Period</th><th>Purpose</th></tr></thead>
            <tbody>
              <tr><td>1</td><td><code>MES 03-26</code></td><td><code>5m</code></td><td>Primary scalping timeframe</td></tr>
              <tr><td>2</td><td><code>MES 03-26</code></td><td><code>15m</code></td><td>Intraday context</td></tr>
              <tr><td>3</td><td><code>MCL 04-26</code></td><td><code>5m</code></td><td>Crude scalping</td></tr>
              <tr><td>4</td><td><code>MCL 04-26</code></td><td><code>15m</code></td><td>Crude intraday</td></tr>
            </tbody>
          </table>
        </div>
        <p className="support-prose subtle">
          Use the active front-month contract for each instrument. Slots are checked against the HelmFeed data store, so the corresponding chart must have <code>HelmFeed</code> running.
        </p>
      </div>

      <div className="card support-card">
        <h2>5. NinjaScript indicators</h2>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Indicator</th><th>Add to</th><th>Purpose</th></tr></thead>
            <tbody>
              <tr><td><code>HelmAnalyzer</code></td><td>Every chart you might Ctrl+Shift+F</td><td>Captures bitmap + market context, POSTs to dashboard</td></tr>
              <tr><td><code>HelmFeed</code></td><td>Every chart in Auto Analysis</td><td>Streams live bars + ticks into <code>feed.db</code></td></tr>
            </tbody>
          </table>
        </div>
        <p className="support-prose">After adding, compile via NinjaScript Editor (<kbd>F11</kbd> → <kbd>F5</kbd>). "Compile succeeded" = ready.</p>
      </div>

      <div className="card support-card">
        <h2>6. Appearance (optional)</h2>
        <p className="support-prose">
          <strong>Settings {' → '} Appearance</strong>. Cosmetic — defaults are fine. Worth setting:
        </p>
        <ul className="support-list">
          <li><code>Theme</code>: <code>Dark</code> — easier on the eyes during long sessions</li>
          <li><code>Timezone</code>: <code>America/Chicago</code> — CME session timing</li>
          <li><code>Table page size</code>: <code>100</code></li>
        </ul>
      </div>
    </>
  )
}

// ---------- Shared cards ----------

function VersionCard() {
  const qc = useQueryClient()
  const v = useQuery<VersionResp>({
    queryKey: ['version'],
    queryFn:  () => fetchJSON<VersionResp>('/api/version'),
    refetchInterval: 10 * 60 * 1000,
    staleTime: 60 * 1000,
    retry: 0,
  })
  const checkNow = useMutation({
    mutationFn: () => postJSON<VersionResp>('/api/version/check'),
    onSuccess: (data) => qc.setQueryData(['version'], data),
  })

  const d = v.data
  return (
    <div className="card support-card">
      <h2>Installed version</h2>
      {!d ? (
        <div className="subtle">Loading...</div>
      ) : (
        <>
          <div className="kv"><span>Installed commit</span><span><code>{d.current_short ?? '—'}</code></span></div>
          <div className="kv"><span>Latest on {d.remote}/{d.branch}</span><span><code>{d.latest_short ?? '—'}</code></span></div>
          <div className="kv">
            <span>Status</span>
            <span>
              {!d.is_git_checkout
                ? <span className="subtle">release-zip install (no update check)</span>
                : d.update_available
                  ? <span style={{ color: 'var(--accent)' }}>
                      {d.commits_behind === 1 ? '1 commit behind' : `${d.commits_behind} commits behind`}
                    </span>
                  : <span className="ok">up to date</span>}
            </span>
          </div>
          <div className="kv"><span>Last checked</span><span>{fmtChecked(d.last_checked)}</span></div>
          {d.last_error && (
            <div className="kv"><span>Last error</span><span style={{ color: 'var(--neg)' }}>{d.last_error}</span></div>
          )}
          <div className="support-actions">
            <button
              type="button"
              onClick={() => checkNow.mutate()}
              disabled={checkNow.isPending}
            >
              {checkNow.isPending ? 'Checking...' : 'Check for updates now'}
            </button>
          </div>
        </>
      )}
    </div>
  )
}

function UninstallCard() {
  return (
    <div className="card support-card">
      <h2>Uninstall</h2>
      <p className="support-prose">
        The repo ships an idempotent uninstaller. From <strong>elevated PowerShell</strong>:
      </p>
      <pre className="support-code">{`cd $HOME\\Documents\\Projects\\TheHelmTrader
.\\uninstall.ps1`}</pre>
      <p className="support-prose">
        By default it stops + removes the service, kills the recorder process, and removes the NinjaScript indicators. Settings (<code>%USERPROFILE%\.helm</code>) and trade data (<code>trades.db</code>, <code>signals.jsonl</code>, <code>feed.db</code>) are <strong>preserved</strong> unless you pass <code>-PurgeSettings</code>, <code>-PurgeData</code>, or <code>-All</code>.
      </p>
    </div>
  )
}

function HelpCard() {
  return (
    <div className="card support-card">
      <h2>Still stuck?</h2>
      <p className="support-prose">
        The Helm is a private tool by Lodestone &amp; Purser. For bug reports or feature requests, contact the operator who provisioned this install. Include:
      </p>
      <ul className="support-list">
        <li>The installed commit short SHA (Overview tab)</li>
        <li>A copy of <code>tradebot.log</code> for the last hour (Health page lets you copy the tail)</li>
        <li>The exact reproduction steps and any error message from the browser console (<kbd>F12</kbd>)</li>
      </ul>
    </div>
  )
}
