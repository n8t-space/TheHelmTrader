export interface Filters {
  account: string[]
  symbol: string
  strategy: string
  date_from: string
  date_to: string
}

export const EMPTY_FILTERS: Filters = {
  account: [], symbol: '', strategy: '', date_from: '', date_to: '',
}

// Quick-select presets for the Trade Performance filter bar. Empty by
// default -- the user populates the backend Settings page with their NT
// account IDs, and the SPA reads them via /api/settings. This constant
// is kept as a fallback for tests / standalone preview only.
export const ACCOUNT_GROUPS: Record<string, string[]> = {
  Live:       [],
  Eval:       [],
  'Sim-Demo': ['Sim101', 'Playback101', 'Backtest', 'SimBetaSIM'],
}

export function buildQuery(f: Filters, extras: Record<string, string | number> = {}): string {
  const params = new URLSearchParams()
  for (const a of f.account) params.append('account', a)
  if (f.symbol) params.set('symbol', f.symbol)
  if (f.strategy) params.set('strategy', f.strategy)
  if (f.date_from) params.set('date_from', f.date_from)
  if (f.date_to) params.set('date_to', f.date_to)
  for (const [k, v] of Object.entries(extras)) {
    if (v !== '' && v !== undefined && v !== null) params.set(k, String(v))
  }
  const s = params.toString()
  return s ? '?' + s : ''
}

export async function fetchJSON<T>(path: string): Promise<T> {
  const r = await fetch(path)
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
  return r.json()
}

export async function postJSON<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!r.ok) {
    const detail = await r.text().catch(() => '')
    throw new Error(`${r.status} ${r.statusText}${detail ? `: ${detail}` : ''}`)
  }
  if (r.status === 204) return undefined as T
  return r.json()
}

export async function putJSON<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!r.ok) {
    const detail = await r.text().catch(() => '')
    throw new Error(`${r.status} ${r.statusText}${detail ? `: ${detail}` : ''}`)
  }
  if (r.status === 204) return undefined as T
  return r.json()
}

export async function deleteJSON(path: string): Promise<void> {
  const r = await fetch(path, { method: 'DELETE' })
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
}

export interface HealthResp {
  status: string
  fills: number
  db_path: string
}

export interface DimensionsResp {
  accounts: string[]
  symbols: string[]
  strategies: string[]
  total_fills: number
  first_fill_time: string | null
  last_fill_time: string | null
}

export interface EquityPoint {
  exit_time: string
  cumulative_net_pnl: number
  drawdown: number
}

export interface DailyPnl {
  date: string
  net_pnl: number
}

export interface SymbolBreakdown {
  symbol: string
  trades: number
  net_pnl: number
  wins: number
  losses: number
}

export interface StrategyBreakdown {
  strategy: string
  trades: number
  net_pnl: number
  wins: number
  losses: number
}

export interface AccountBreakdown {
  account: string
  trades: number
  net_pnl: number
  wins: number
  losses: number
}

export interface StatsResp {
  trade_count: number
  win_count: number
  loss_count: number
  flat_count: number
  win_rate: number
  net_pnl: number
  gross_pnl: number
  commissions_and_fees: number
  avg_win: number
  avg_loss: number
  best_trade: number
  worst_trade: number
  profit_factor: number | null
  max_drawdown: number
  equity_curve: EquityPoint[]
  daily_pnl: DailyPnl[]
  by_symbol: SymbolBreakdown[]
  by_strategy: StrategyBreakdown[]
  by_account: AccountBreakdown[]
}

export interface TradeFill {
  time: string
  qty: number
  price: number
}

export interface TradeExitFill extends TradeFill {
  pnl: number       // dollar P&L of this leg vs avg entry
}

export interface Trade {
  account: string
  symbol: string
  contract: string
  direction: 'Long' | 'Short'
  qty: number
  entry_time: string
  exit_time: string
  entry_price: number      // qty-weighted average across all entry fills
  exit_price: number       // qty-weighted average across all exit fills
  point_value: number
  gross_pnl: number
  commission: number
  fee: number
  net_pnl: number
  duration_seconds: number
  strategies: string[]
  num_fills: number
  first_fill_id: number
  last_fill_id: number
  // Scale-out detail: present on every trade, but only meaningfully different
  // from the aggregate row when is_scale_out=true (i.e. NT8 closed the
  // position in multiple legs via TP1 + runner + trail).
  is_scale_out: boolean
  entry_fills: TradeFill[]
  exit_fills: TradeExitFill[]
}

export interface TradesResp {
  count: number
  trades: Trade[]
}

export interface Fill {
  id: number
  time_utc: string
  account_name: string
  symbol: string
  master_symbol: string
  order_name: string
  order_action: string
  order_type: string
  qty: number
  price: number
  commission: number
  fee: number
  is_entry: number
  is_exit: number
  market_position: string
  strategy_name: string | null
  strategy_template: string | null
  position: number
}

export interface FillsResp {
  count: number
  fills: Fill[]
}

// ---------- Signal Analysis ----------

// Per-bracket plan extracted from the NT8 ATM XML. Single-bracket ATMs
// degrade trivially to a 1-element array; scale-out ATMs (e.g. TP1 + runner)
// carry one entry per bracket with the per-bracket SL/TP/BE/trail config.
export interface AtmBracket {
  qty: number
  stop_ticks: number
  target_ticks: number
  auto_be_plus: number
  auto_be_trigger: number
  trail_steps: Array<{
    frequency: number
    profit_trigger: number
    stop_loss: number
  }>
}

export interface Proposal {
  instrument: string
  direction: 'long' | 'short' | 'flat'
  entry: number
  stop: number
  target: number
  risk_reward: number
  confidence: number
  reasoning: string
  tick_size_applied?: number
  tick_source?: string
  tick_adjustments?: Array<{ field: string; from: number; to: number }>
  attempts?: number
  reassessed?: boolean
  attempt_confidences?: number[]
  confidence_floor?: number
  // ATM strategy the LLM picked. Either an exact name from the user's NT
  // templates, or the literal "custom" (in which case custom_stop_ticks /
  // custom_target_ticks carry the LLM's suggested bracket; user would need
  // to create that strategy in NT to take the trade as proposed).
  atm_strategy?: string
  atm_strategy_resolved?: boolean
  atm_stop_ticks?: number
  atm_target_ticks?: number
  // Full per-bracket plan for scale-out templates (TP1 + runner, etc.).
  // Drives the per-leg display + the resolver state machine. Empty for
  // custom / unknown ATM picks.
  atm_brackets?: AtmBracket[]
  atm_total_qty?: number
  custom_stop_ticks?: number
  custom_target_ticks?: number
}

export type LegResult = 'target' | 'stop' | 'trail' | 'be' | 'manual' | 'neither'

export interface Leg {
  bracket_idx: number
  qty: number
  result: LegResult
  exit_price: number | null
  exit_ts: number | null         // unix milliseconds
  method?: 'tick' | 'bar' | 'manual' | null
  engine?: 'resolver' | 'manual' | null
}

export interface LegBreakdownItem {
  bracket_idx: number | null
  qty: number
  result: LegResult | null
  exit_price: number
  exit_ts: number | null
  method: string | null
  pnl: number
}

export interface Journal {
  note: string | null
}

// Display helper. Default 2 decimals; forex non-JPY pairs use 4 to preserve
// pip precision (EURUSD 1.0851). Forex JPY pairs (USDJPY 154.32) stay at 2.
// Anything not matching the 6-letter forex shape (futures, stocks) falls
// through to 2 decimals -- the LLM already tick-rounds, so 2 is enough.
export function fmtPrice(n: number | undefined | null, instrument?: string): string {
  if (n === undefined || n === null || (typeof n === 'number' && isNaN(n))) return ''
  const digits = _isForexNonJpy(instrument) ? 4 : 2
  return n.toFixed(digits)
}

function _isForexNonJpy(instr?: string): boolean {
  if (!instr) return false
  const sym = instr.trim().toUpperCase()
  if (!/^[A-Z]{6}$/.test(sym)) return false
  return !sym.endsWith('JPY')
}

export interface Outcome {
  result: 'pending' | 'target' | 'stop' | 'breakeven' | 'partial' | 'no_fill' | 'not_watched' | 'other'
  note: string | null
  closing_price: number | null
  // Set to true when outcome_watcher auto-applied the resolved outcome for
  // a headless signal (no user-in-the-loop on creation, so no Confirm step).
  // Manual signals never get this; they require the dashboard Confirm click.
  auto_confirmed?: boolean
}

export interface OutcomeSuggestion {
  result: Outcome['result']
  source_signal_ts: string
  reasoning?: string
  confidence?: number
  // Set by outcome_watcher when the resolver produced the suggestion.
  // 'resolver' = walked bars/ticks in feed.db, no LLM. Other values
  // (or absent) typically indicate the LLM-reconciliation pipeline.
  engine?: string
  hit_ts?: number
  hit_price?: number
  method?: 'tick' | 'bar'
}

export interface TradeMetrics {
  point_value: number | null
  tick_size: number | null
  tick_value: number | null
  display_mode: 'ticks' | 'points'
  position_size: number
  risk_points: number
  reward_points: number
  risk_ticks: number
  reward_ticks: number
  risk_per_contract: number
  reward_per_contract: number
  total_risk: number
  total_reward: number
  realized_pnl: number | null
  realized_pnl_source: string | null
  // Per-leg P&L attribution when the signal carries `legs`. null when no
  // per-leg fills exist (single-bracket trade, or scale-out still pending).
  leg_breakdown: LegBreakdownItem[] | null
}

export interface Signal {
  timestamp: string
  screenshot_path: string | null
  screenshot_filename?: string | null
  proposal: Proposal
  raw_response?: string
  duration_s?: number
  model?: string
  market_context?: Record<string, unknown>
  journal?: Journal
  outcome?: Outcome
  position_size?: number
  // Top-level array of per-leg fills for scale-out trades. May be
  // partial (some legs resolved while runner is still open) or complete.
  // Auto-resolved entries carry engine='resolver'; user edits carry
  // engine='manual'.
  legs?: Leg[]
  outcome_suggestion?: OutcomeSuggestion
  outcome_suggestion_dismissed?: boolean
  deleted?: boolean
  metrics?: TradeMetrics
  // 'headless' = auto-generated by headless_analyzer; absent or other
  // value = manual capture from the snip pipeline.
  trigger?: string
  // Filled by the bot's entry_resolver (or, legacy, by manual user toggle):
  //   true       -> entry price was touched after signal timestamp
  //   false      -> 4h watch window elapsed without a touch ("no entry")
  //   undefined  -> still pending (within window, no touch yet)
  entry_triggered?: boolean
  // Unix milliseconds when the entry was hit; tooltip data for the cell.
  entry_hit_ts?: number
}

export interface SignalListResp {
  count: number
  signals: Signal[]
}

export interface SignalDetailResp {
  signal: Signal
}

// ---------- Auto Analysis ----------

export interface AutoAnalysisEntry {
  instrument: string
  period: string
  enabled: boolean
}

export interface AutoAnalysisConfigResp {
  entries: AutoAnalysisEntry[]
}

export interface AutoAnalysisLastRun {
  instrument: string
  period: string
  bar_ts: number
  ran_at_run_count: number
}

export interface AutoAnalysisStatusResp {
  queue_size: number
  run_count: number
  last_run: AutoAnalysisLastRun | null
  worker_alive: boolean
}

export const AUTO_ANALYSIS_PERIODS = ['1m', '5m', '15m', '1h', '4h'] as const
export const AUTO_ANALYSIS_MAX_SLOTS = 4

// ---------- Settings ----------

export interface SettingsAppearance {
  theme: 'dark' | 'light' | 'system'
  accent: string
  bg: string
  panel: string
  border: string
  text: string
  muted: string
  pos: string
  neg: string
  timezone: string
  table_page_size: number
}

export interface SettingsAiBackend {
  provider: 'ollama' | 'claude' | 'openai'
  request_timeout_s: number
  // Ollama
  ollama_url: string
  model: string
  fallback_model: string
  num_ctx: number
  // Claude
  claude_api_key: string
  claude_model: string
  claude_max_tokens: number
  // OpenAI
  openai_api_key: string
  openai_model: string
  openai_max_tokens: number
}

export interface SettingsStrategy {
  confidence_floor: number
  reconciliation_cap: number
  max_attempts: number
  retention_days: number
  stale_bar_seconds: number
}

export interface SettingsAccounts {
  live: string[]
  evals: string[]
  simulation: string[]
}

export interface SettingsDoc {
  schema_version: number
  appearance: SettingsAppearance
  ai_backend: SettingsAiBackend
  strategy: SettingsStrategy
  accounts: SettingsAccounts
}

export interface SettingsResp {
  schema_version: number
  path: string
  exists_on_disk: boolean
  settings: SettingsDoc
}

export interface OllamaTestResp {
  ok: boolean
  error?: string
  probed?: string
  latency_s?: number
  models?: string[]
  configured_model_present?: boolean
  configured_model?: string
}

export const SETTINGS_THEME_KEY = 'helm.theme.cache'  // localStorage; pre-paint hint
