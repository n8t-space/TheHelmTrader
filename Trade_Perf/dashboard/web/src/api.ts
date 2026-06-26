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
  PA:         [],
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

export interface TaxAccount {
  account: string
  trades: number
  realized_pnl: number
  taxable_gain: number
  estimated_tax: number
}

export interface TaxEstimateResp {
  tax_year: number
  enabled: boolean
  rates: { lt_rate: number; st_rate: number; state_rate: number; blended_rate: number }
  accounts: TaxAccount[]
  total: { realized_pnl: number; taxable_gain: number; estimated_tax: number }
  note: string
}

export function fetchTaxEstimate(year?: number): Promise<TaxEstimateResp> {
  return fetchJSON<TaxEstimateResp>(`/api/tax-estimate${year ? `?year=${year}` : ''}`)
}

export interface MicroscalpAccount {
  account: string
  is_eval: boolean
  trades: number
  scalp_trades: number
  scalp_trade_pct: number
  gross_profit: number
  scalp_gross_profit: number
  scalp_pnl_pct: number
  compliant: boolean
}

export interface MicroscalpResp {
  scalp_seconds: number
  max_pct: number
  accounts: MicroscalpAccount[]
  note: string
}

export interface EvalProgressAccount {
  account: string
  profit_target: number
  net_pnl: number
  remaining: number | null
  passed: boolean
  has_target: boolean
  since_basis: boolean
}

export interface EvalProgressResp {
  accounts: EvalProgressAccount[]
}

// Business expense ledger. Categories mirror expenses.py CATEGORIES.
export const EXPENSE_CATEGORY_LABELS: Record<string, string> = {
  eval_fee:              'Eval fee',
  reset_fee:             'Reset fee',
  funded_activation:     'Funded activation',
  platform_data:         'Platform / data',
  software_subscription: 'Software / subscription',
  hardware:              'Hardware',
  education:             'Education',
  broker_fees:           'Broker fees',
  professional_services: 'Professional services',
  payout_fee:            'Payout fee',
  office:                'Office',
  other:                 'Other',
}

export interface Expense {
  id: number | null
  date: string
  category: string
  amount: number
  entity: string          // 'personal' | 'llc'
  vendor: string
  account: string
  recurring: boolean
  deductible: boolean
  note: string
  created_at: string
}

export interface ExpensesResp {
  expenses: Expense[]
  categories: string[]
  summary: {
    total: number
    count: number
    by_category: Record<string, number>
    by_entity: Record<string, number>
    by_account: Record<string, number>
    by_year: Record<string, number>
  }
}

// Per-trade journal. Closed set mirrors journal.py MOODS (minus the "" unset).
export const JOURNAL_MOODS = [
  'calm', 'focused', 'anxious', 'fomo', 'frustrated',
  'greedy', 'confident', 'bored', 'revenge',
] as const

export interface JournalSnapshot {
  symbol: string
  account: string
  direction: string
  net_pnl: number
  entry_time: string
  exit_time: string
  atm: string
  entry_price: number
  exit_price: number
}

export interface JournalEntry {
  trade_key: string
  notes: string
  discipline: number | null
  mood: string
  tags: string[]
  snapshot: JournalSnapshot
  updated_at: string
}

export interface JournalListResp {
  entries: JournalEntry[]
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
  reasoning: string
  tick_size_applied?: number
  tick_source?: string
  tick_adjustments?: Array<{ field: string; from: number; to: number }>
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
  provider?: string
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
  // Auto-Trader (Sim-only v1). Present once a signal is armed for execution.
  armed?: boolean
  arm_account?: string
  exec?: SignalExec
}

// Lifecycle of an armed signal, written by the dashboard (arm/disarm) and the
// NT8 HelmAutoTrader strategy (working/filled/cancelled/rejected).
export type ExecState =
  | 'armed' | 'working' | 'filled' | 'cancelled' | 'rejected' | 'disarmed'

export interface SignalExec {
  state: ExecState
  exec_tag?: string
  account?: string
  armed_at?: string
  working_at?: string
  filled_at?: string
  disarmed_at?: string
  updated_at?: string
  fill_price?: number | null
  fill_qty?: number | null
  note?: string | null
  dry_run?: boolean
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
  // Per-component provider overrides; '' = inherit `provider`.
  news_provider?: '' | 'ollama' | 'claude' | 'openai'
  signal_provider?: '' | 'ollama' | 'claude' | 'openai'
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
  reconciliation_cap: number
  retention_days: number
  stale_bar_seconds: number
}

export interface SettingsAccounts {
  live: string[]
  evals: string[]
  // PA = Paid Account (a passed eval, now funded). Real-money sibling to live.
  paid: string[]
  simulation: string[]
  // Friendly display names keyed by NT account ID (display-only).
  names: Record<string, string>
  // Entity ownership keyed by NT account ID: 'personal' | 'llc'. Unset -> personal.
  entities: Record<string, string>
}

// Per-account trading config (Item 3), keyed by NT account id in
// SettingsDoc.account_configs. Rendered as a card for LIVE + EVAL accounts only;
// Sim accounts have no card and fall back to the global auto_trader defaults.
export interface AccountConfig {
  name: string
  // User-entered base cash ("cash now" as of cash_basis_ts, stamped server-side).
  // Current cash = base_cash + realized P&L of trades closed after the basis.
  base_cash: number
  // UTC ISO stamp of when base_cash was saved (server-managed; read-only here).
  cash_basis_ts: string
  risk_per_trade_value: number
  risk_per_trade_mode: 'percent' | 'price'
  max_daily_loss: number
  max_concurrent_per_instrument: number
  max_contracts_per_instrument: number
  stop_if_balance_below: number
  // User-entered trailing max-drawdown limit ($); tracked vs a server-computed HWM.
  trailing_dd_limit: number
  // User-entered profit target to pass an eval ($). Eval-only concept; 0 = unset.
  profit_target: number
}

// Live readout for an account-config card (GET /api/account-configs/live).
export interface AccountConfigLive {
  account: string
  // Computed current cash = base_cash + realized_since (null when no basis set).
  cash: number | null
  base_cash: number
  cash_basis_ts: string
  realized_since: number
  high_water_mark: number | null
  trailing_dd_used: number | null
  trailing_dd_limit: number
  dd_breached: boolean
}

/** Friendly display name for an account ID, or the raw ID if none is set. */
export function accountLabel(id: string, names?: Record<string, string>): string {
  const n = names?.[id]?.trim()
  return n ? n : id
}

export interface ModelsResp {
  ok:       boolean
  provider: string
  models:   string[]
  error?:   string
}

// A user-configurable economic-calendar source (Item 7).
export interface NewsSource {
  name: string
  url: string
  type: 'xml' | 'scrape' | 'ai-extract' | 'factbase'
  enabled: boolean
}

export interface SettingsNews {
  enabled: boolean
  // DEPRECATED (2.0.0): superseded by `sources`; kept readable for the 2.0.x
  // rollback window. The UI writes only `sources` going forward.
  forexfactory_enabled: boolean
  econoday_enabled: boolean
  sources: NewsSource[]
  impact_filter: string[]
  currency_filter: string[]
  refresh_interval_minutes: number
}

export interface SettingsAutoTrader {
  enabled: boolean
  account: string
  max_contracts_per_order: number
  max_concurrent: number
  daily_loss_cutoff: number
  min_account_balance: number
  poll_seconds: number
  entry_window_minutes: number
  capture_entry_screenshot: boolean
}

export interface SettingsAuditor {
  enabled: boolean
  interval_minutes: number
}

export interface BlackoutWindow {
  start: string   // "HH:MM"
  end: string     // "HH:MM"
  label: string
}
export interface SettingsAutomation {
  blackout_windows: BlackoutWindow[]
}

export interface SettingsTax {
  enabled: boolean
  lt_rate: number       // long-term cap-gains fraction (0.20 = 20%)
  st_rate: number       // short-term / ordinary fraction
  state_rate: number    // flat state fraction on all gains
}

export interface HungTrade {
  ts: string
  instrument?: string
  direction?: string
  state: string
  age_minutes: number
  outcome?: string | null
  account?: string
  fill_price?: number | null
}

export interface SettingsDoc {
  schema_version: number
  appearance: SettingsAppearance
  ai_backend: SettingsAiBackend
  strategy: SettingsStrategy
  accounts: SettingsAccounts
  account_configs: Record<string, AccountConfig>
  // User-entered commission ($/contract per side, NT8 commission-template
  // style) keyed by master instrument symbol (e.g. "MES"). When > 0 it
  // overrides the NT8-reported commission in P&L; 0/absent keeps the fills'.
  commissions: Record<string, number>
  news: SettingsNews
  auto_trader: SettingsAutoTrader
  auditor: SettingsAuditor
  automation: SettingsAutomation
  tax: SettingsTax
  // Display name for the business entity (used wherever "llc" is shown). Blank -> "LLC".
  llc_name: string
}

export interface AuditorLogEntry {
  checked_at: string
  signal_ts: string
  instrument?: string
  account?: string
  action: string
  prev_realized?: number | null
  prev_source?: string | null
  new_realized?: number | null
  confidence?: number | null
}

export interface AuditorStatus {
  enabled: boolean
  interval_minutes: number
  auto_window_minutes?: number
  running: boolean
  last_run: string | null
  last_scope?: string | null
  last_summary: { checked: number; corrected: number; in_sync: number; unverified: number } | null
  recent: AuditorLogEntry[]
}

export interface AuditorRunResp {
  checked: number
  corrected: number
  in_sync: number
  unverified: number
  checked_at: string
  details: AuditorLogEntry[]
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
