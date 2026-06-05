# The Helm - Feature Catalog

> Complete inventory of what the app does, grouped by subsystem.
> Last updated: 2026-06-04

## Purpose

The Helm is an AI-driven futures trading assistant. It captures NinjaTrader 8
chart screenshots, sends them to a vision LLM for analysis, turns the model's
read into a structured trade proposal, and can auto-arm and execute that
proposal on NT8 via an ATM strategy. A web dashboard manages the whole loop and
reports performance from real broker fills.

---

## 1. Analysis Pipeline (signal generation)

- **Chart screenshot capture** - pulls a live chart image from NT8 on demand or
  on a schedule (`screenshot_capturer.py`, `HelmAnalyzer.cs`).
- **Vision-LLM analysis** - sends the chart image to a vision model and parses a
  structured trade proposal: direction, entry, stop, target, reasoning
  (`local_llm_analyzer.py`, `pipeline.py`).
- **Multi-provider AI backend** - Ollama (local), Anthropic Claude, or OpenAI,
  selectable per provider with model + token + context-window controls.
- **Per-component provider routing** - signals and news can each use a different
  backend (e.g. signals on local Ollama, news on Claude).
- **Indicator-agnostic prompting** - the analyzer prompt adapts to whatever
  indicators are on the chart instead of assuming a fixed indicator set.
- **Symmetric direction gate** - prompt is bias-neutral (no long/short lean);
  flat is a valid call and produces no ATM.
- **ATM template selection** - the model picks a named ATM template sized to the
  setup; directional proposals must carry an ATM or they are rejected.
- **Proposal sanity checks** - validates the parsed proposal before it is stored
  (`proposal_sanity.py`); flat signals must have an empty ATM.
- **Tick snapping** - entry/stop/target are snapped to the instrument's valid
  tick increment (banker's rounding) and any adjustment is annotated
  (`instruments.py`).
- **Instrument normalization** - strips contract-month suffixes (MES JUN26 ->
  MES) and resolves tick size + point value from `instruments.json`, with
  fallback rules for forex and stocks.

## 2. Auto-Analysis (hands-off signal loop)

- **Scheduled auto-analysis** - runs the capture -> analyze -> store loop on an
  interval without manual triggering (`auto_analyzer.py`, `auto_analysis.py`).
- **Headless analyzer** - background worker that processes captures off the
  request path (`headless_analyzer.py`).
- **Active-trade skip** - skips analysis while a trade is already live so it does
  not stack signals on an open position.
- **CME session / maintenance awareness** - respects the futures session roll and
  maintenance halt windows.
- **Stale-bar guard** - detects a frozen feed (bars not advancing) and skips
  rather than analyzing dead data (`stale_bar_seconds`).
- **Auto-prune / retention** - old signals are aged out per `retention_days`.

## 3. Auto-Trader (execution)

- **Per-signal manual arm** - each signal can be individually armed for execution.
- **Master switch** - global enable/disable; defaults OFF and must be turned on
  deliberately (`auto_trader.py`, `HelmAutoTrader.cs`).
- **Single-account scoping** - execution is hard-scoped to one user-selected
  account; the dashboard account selection drives the strategy.
- **NT8 account dropdown** - the strategy's Allowed Account is a dropdown of
  connected accounts, kept in sync with the dashboard selection.
- **ATM-based order placement** - places the proposal's named ATM template
  (entry + brackets) on NT8.
- **Per-instrument concurrency** - one open trade per instrument (e.g. one MES
  and one MCL simultaneously), with an overall `max_concurrent` ceiling.
- **One-queued-per-instrument** - will not stack a queue; supersede-expiry clears
  stale queued signals.
- **Entry window** - armed signals expire after `entry_window_minutes` if unfilled.
- **Chart-cancel detection** - if you cancel the order from the NT chart, the
  strategy detects it and the signal is marked cancelled / no-fill.
- **Balance fail-safe** - the strategy reports live account equity
  (NetLiquidation); if it drops to or below the configured floor, the master
  switch is forced OFF (stops new trades, does not flatten) and requires manual
  re-enable.
- **Daily loss cutoff** - optional daily realized-loss threshold that halts new
  entries.
- **Contract cap** - `max_contracts_per_order` guards order size.

## 4. Signal Lifecycle & Outcomes

- **Signal storage** - every proposal is persisted with its image, reasoning, and
  metadata (`signal_storage.py`).
- **Model attribution** - records which AI model generated each signal.
- **Real-trade-time attribution** - signals that actually filled are displayed
  and sorted by their real entry-fill time, not generation time.
- **Execution state tracking** - armed -> working -> filled -> cancelled
  (`update_exec`).
- **Entry resolver** - confirms whether and when the proposed entry was reached
  (`entry_resolver.py`).
- **Independent outcome resolution** - walks live ticks to decide the result
  independently of the sim, sets per-leg outcomes (`outcome_resolver.py`,
  `outcome_watcher.py`).
- **Multi-leg / scale-out support** - per-bracket legs with individual exit
  price, result, and P&L; sizing and risk/reward derived from actual legs.
- **Fill linker** - heuristically links a signal to its real NT8 round-trip so
  P&L can use actual trailed-stop fills instead of the paper sim
  (`fill_linker.py`).
- **Manual outcome override** - outcome can be set from a dropdown on the signal
  detail page (target / stop / breakeven / closing price).

## 5. Trade Performance & Analytics

- **Real round-trip derivation** - reconstructs trades from NT8 fills using the
  signed position column as the boundary (`trades.py`).
- **Correct direction from position** - derives Long/Short from signed position,
  fixing the NT8 quirk where an ATM short's entry is labeled BuyToCover.
- **Gross / commission / fee / net P&L** - per trade and aggregated.
- **Scale-out fill detail** - shows TP1 vs Runner fills with per-leg P&L instead
  of a misleading volume-weighted average.
- **Aggregate statistics** - win rate, profit factor, avg win/loss, best/worst
  trade, max drawdown.
- **Equity curve** - cumulative net P&L with running drawdown.
- **Trading-day attribution** - P&L is booked by the CME 5 PM CT session roll,
  not calendar midnight (`trading_day.py`).
- **Breakdowns** - by symbol, by strategy/ATM template, by account.
- **Drawdown / prop-account tracking** - per-account starting balance, trailing
  drawdown, daily drawdown, and profit target (`drawdown.py`).

## 5a. Data Integrity Auditor

- **Signal-vs-fill reconciliation** - links each executed signal to its real NT8
  round-trip and compares the paper P&L to the broker net (`auditor.py`).
- **NT database as ground truth** - confidently linked mismatches are
  auto-corrected to the real fill P&L; the signal's displayed win/loss flips to
  match the account. No guessing or adjusting.
- **Honest unlinked handling** - a filled signal with no confident fill match is
  flagged `unverified` and left as paper, never assigned a made-up number.
- **Immutable audit log** - every correction (paper -> fills, confidence) is
  appended to `audit_log.jsonl` and surfaced in the UI.
- **Hourly sweep** - runs automatically on a configurable interval (default 60
  min) plus an on-demand "Run audit now"; status, counts, and recent corrections
  show on the Settings > Data Integrity page.

## 6. Economic News

- **ForexFactory calendar** - high-impact event ingestion (`news.py`).
- **Econoday calendar** - second source, AI-summarized.
- **Impact + currency filters** - filter to e.g. High impact / USD only.
- **Auto-refresh** - configurable refresh interval.
- **AI summarization** - routes to the configured news provider (Claude by
  default to handle large HTML payloads local models choke on).

## 7. Dashboard (web UI)

- **Home** - status overview and quick actions.
- **Signal Analysis** - sortable/filterable signal table: model, qty,
  time-in-trade, outcome, with date+time and trading-day filters.
- **Signal Detail** - full proposal, chart image, reasoning, leg breakdown, and
  manual outcome control.
- **Trade Performance** - stats, equity curve, and breakdowns from real fills.
- **Health** - service/feed/pipeline status.
- **Settings** - all configuration (see section 8), including the Data Integrity
  auditor page.
- **Support** - help / diagnostics surface.
- **Capture-from-NT** - trigger a chart capture + analysis directly from the UI.
- **Theming** - light/dark/system, custom accent and panel colors, timezone, and
  table page size.

## 8. Configuration (Settings)

- **Appearance** - theme, color palette, timezone, table page size.
- **AI backend** - provider per component (signal/news), Ollama URL + model +
  fallback + context window, Claude/OpenAI keys + models + max tokens, request
  timeout.
- **Strategy** - reconciliation cap, signal retention days, stale-bar threshold.
- **Accounts** - live / eval / simulation account lists with per-account drawdown
  configs.
- **Auto-trader** - account, max contracts per order, max concurrent, daily loss
  cutoff, minimum account balance floor, poll interval, entry window.
- **News** - enable, ForexFactory/Econoday toggles, impact + currency filters,
  refresh interval.
- **Data Integrity** - auditor enable + sweep interval; run-now button; last-run
  status and recent-corrections log.

## 9. NinjaScript Components (NT8 side)

- **HelmFeed.cs** - streams live bars/ticks from NT8 into the feed store so the
  pipeline and resolvers have market data.
- **HelmAnalyzer.cs** - captures chart screenshots for the analysis pipeline.
- **HelmAutoTrader.cs** - polls the dashboard for armed signals, places ATM
  trades on the scoped account, reports live equity for the balance fail-safe,
  keeps its account in sync with the dashboard, and detects chart-side cancels.

## 10. Platform / Operations

- **FastAPI + uvicorn backend** - serves the API and the built React frontend.
- **NSSM service + watchdog** - runs the dashboard as a Windows service with a
  watchdog that restarts on failure (`watchdog.ps1`, `install_service.ps1`).
- **In-app updater** - update the deployment from the dashboard (`version.py`,
  `update.ps1`); the page action is "Update", not just "Check".
- **Credentials separation** - account names and API keys are split into a local
  `credentials.json` (HELM_HOME) and never pushed to git.
- **Live-on-refresh frontend** - frontend changes go live on browser refresh;
  prompts are read per-call (no restart); Python backend changes need a uvicorn
  restart; NinjaScript needs an F5 compile.
- **Dev environment** - `run-dev.ps1` / `setup-dev-env.ps1` for local development
  outside the service.

---

## References

- Backend API: `Trade_Perf/dashboard/api/`
- Pipeline: `TradingBot/app/src/`
- Frontend: `Trade_Perf/dashboard/web/src/pages/`
- NinjaScript: `TradingBot/ninjascript/_Helm Locker/`
- Config schema: `Trade_Perf/dashboard/api/settings.py`
