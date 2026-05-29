# Trade_Perf — The Helm dashboard

> Unified local-only NT8 dashboard by **Lodestone & Purser**. Brand = "The Helm" (same brand as the sibling [TradingBot](../TradingBot/) bot/pipeline; this is the dashboard surface).

## Read first

- [`PROJECT.md`](PROJECT.md) — current architecture, where things live, runtime modes, conventions.
- [`../TradingBot/MIGRATION.md`](../TradingBot/MIGRATION.md) — canonical "Outstanding (next session pickup)" list across both projects.

## Architecture in one breath

FastAPI (`dashboard/api/main.py`) on `127.0.0.1:8000` serves both `/api/*` routes AND the built React SPA from `dashboard/web/dist/`. Vite dev server (`run_dev.ps1`) only used for active frontend development. Three pages: Home / Trade Performance / Signal Analysis (+ Health).

Two data sources:

- `trades.db` (SQLite, this project) — fed by `recorder.py` polling NT8's own SQLite at `~/Documents/NinjaTrader 8/db/NinjaTrader.sqlite`.
- `../TradingBot/app/data/signals.jsonl` — append-only LLM proposals + journal updates. Imported via the `_tradebot_bridge.py` sys.path shim.

A third data source (`../TradingBot/app/data/feed.db`) holds NS-published live bars + ticks for the in-flight Independent Confirmation / Auto Analysis project — see TradingBot MIGRATION.md.

## Runtime modes

- **Production:** `runtime/watchdog.ps1` runs as a Windows Service (`HelmDashboardWatchdog`) wrapped by NSSM. Auto-starts at boot, polls every 5s, brings uvicorn up while NT is running, stops it when NT exits. Install with `pwsh -ExecutionPolicy Bypass -File runtime/install_service.ps1` from an elevated shell. Browse to `http://localhost:8000/`. The legacy Task Scheduler installer at `runtime/install_watchdog.ps1` is deprecated and kept only as a fallback.
- **Frontend dev:** `dashboard/run_dev.ps1` starts Vite at `:5173` with HMR + `/api` proxy. Browse to `http://localhost:5173/`.
- **Manual one-shot:** `python -m uvicorn dashboard.api.main:app --port 8000 --reload` from project root.

## Working agreement (overrides TradingBot's)

For this project specifically, the user's preferences differ from TradingBot's working agreement:

- **Write + run the code directly.** Don't switch to architect-and-teach mode here. The user wants a working tool, not a learning exercise.
- **Cloud services are allowed.** External APIs, hosted dashboards (Vercel/Render/Supabase/etc), SaaS auth — all fine. Not constrained to local-first / offline-only.

(TradingBot is the opposite on both counts: I guide / user types, and local-only.)

## NT8 SQLite gotchas (load-bearing)

- **NT8 8.1+ uses SQLite, not SQL CE.** Live db at `~/Documents/NinjaTrader 8/db/NinjaTrader.sqlite`. WAL mode — safe to open **read-only** while NT8 writes.
- **Round-trip classification is already done by NT8.** `Executions.IsEntry` / `IsExit` are populated. Don't reconstruct from a stateful pass.
- **ATM strategy quirk:** the exit side of a long trade emits action code `2` (`SellShort`), not `Sell`. Derive trade direction from `is_entry` / `is_exit` + `market_position`, not from the raw `order_action` string.
- **`Time` column is .NET DateTime.Ticks** (100-ns units since 0001-01-01 UTC), not Unix epoch. Translate at the boundary.

## User accounts

Settings is the source of truth for which NT accounts are visible site-wide. The Settings Accounts tab renders one row per account known to trades.db (plus any pre-seeded sims), with radios: `Hidden | Live | Eval | Sim`. The union of the three buckets = the visible set. NT-default sims (`Sim101`, `Playback101`, `Backtest`, `SimBetaSIM`) ship pre-listed under Simulation.

- Helper: `settings.visible_accounts() -> set[str]`. Consumed by `db._apply_visibility`.
- `db.fetch_fills` / `fetch_fills_for_derivation` self-filter to the visible set when called with no `account=`, and intersect when an explicit account list is passed (defense against URL tampering hitting hidden accounts).
- `db.list_dimensions(include_hidden=False)` filters its `accounts` field to visible. The Settings tab passes `?include_hidden=true` on `/api/dimensions` so it can offer hidden accounts as toggle candidates.
- The recorder keeps writing fills for hidden accounts -- visibility is render-only. Re-selecting an account restores its history immediately.

## Conventions

- **Loopback only.** FastAPI binds `127.0.0.1`. Ollama (configured via the Settings page) is the one external dep — default localhost, may point at a LAN workstation if inference is offloaded.
- **System Python.** Uvicorn runs from `%LOCALAPPDATA%\Programs\Python\Python312\python.exe` (real winget install — NOT the `WindowsApps` alias, which is service-incompatible). The watchdog's `Resolve-PythonExe` enforces this. Bot pipeline deps (`fastapi`, `uvicorn[standard]`, `pydantic`, `requests`, `Pillow`, `httpx`, `tzdata`) installed system-wide. **Not** the TradingBot venv. `tzdata` is mandatory on Windows — `zoneinfo` ships without IANA data, so without it the trading-day helper + drawdown card throw `ZoneInfoNotFoundError`.
- **No auto-execution.** The bot proposes; the user decides. Forever.
- **`signals.jsonl` is append-only.** Updates are new lines with the same timestamp; readers merge latest-wins. Soft-delete = a line with `deleted: true`.
- **Entry/outcome invariant (enforced 2026-05-19).** `outcome=no_fill` ⇔ `entry_triggered=False`; any other outcome implies `entry_triggered=True`. The `POST /api/signals/{ts}/outcome` and `/entry-triggered` routes coerce the pair on every write. Outcomes default to `position_size=1` so realized P&L populates without manual sizing. The bar walker (`outcome_watcher`) auto-stamps both fields for manual and headless signals; the only blocker is having feed.db data covering the signal's time window.
- **Scale-out legs are TOP-LEVEL (added 2026-05-20).** For multi-bracket ATMs (the new `MES_*` / `MCL_*` 2c templates), per-leg fills live at `signal.legs`, **not** inside `signal.outcome`. Done deliberately so the auto-resolver can publish progressive per-leg fills without clobbering a user-edited aggregate outcome, and the user can edit the aggregate `outcome.result` without touching auto-resolved legs. `legs` is in `signal_storage.MERGEABLE_FIELDS`. Future readers: `rec["legs"]`, NOT `rec["outcome"]["legs"]`. When `legs` is present, `compute_trade_metrics` sums realized P&L across them and falls back to `closing_price` then single-outcome math otherwise.
- **`is_scale_out` on trades (added 2026-05-20).** `derive_trades` flags any round-trip with >1 exit fill. The aggregate `exit_price` is still the qty-weighted average (volume-weighted math gives correct total P&L) — the per-leg `entry_fills[]` + `exit_fills[]` arrays expose what NT8 actually filled so the table can show TP1 + Runner detail under an expandable row.
- **Trading day = 6 PM CT roll, NOT midnight (added 2026-05-22).** Every "today" aggregation across the dashboard uses the futures-correct trading-day boundary: a trade closed at 5 PM CDT is today's session; closed at 7 PM CDT it's tomorrow's. Helpers live in `dashboard/api/trading_day.py` (`current_trading_day`, `trading_day_for_ts`, `trading_day_bounds_utc`) + the JS mirror at `web/src/lib/trading_day.ts`. UI labels: `Current CME Session` (the trading-day view) vs `Calendar Day / Range` (the raw `date_from/date_to` view). Routes `/api/trades` and `/api/stats` accept `trading_day=`, `trading_day_from=`, `trading_day_to=` params (legacy `date_from/date_to` still work).
- **`tzdata` is a hard dependency on Windows (added 2026-05-22).** Python 3.12's `zoneinfo` ships without IANA data on Windows; the first call to any `ZoneInfo(...)` throws `ZoneInfoNotFoundError` if `tzdata` isn't `pip install`-ed. The trading-day helpers + drawdown card both rely on `zoneinfo` so this is load-bearing. Already in `install.ps1` + README; flag if anyone copies the manual pip-install snippet from an older source.
- **CORS dev mode:** `allow_methods=["GET","POST","PUT","DELETE"]` for `:5173`. PUT was added when the Auto Analysis config endpoint landed.

## Routers

- `signals.py` — Signal Analysis page (LLM proposals, journal, outcome). Coerces the entry/outcome invariants: `outcome=no_fill` ⇔ `entry_triggered=False`; any other outcome implies `entry_triggered=True`. Also hosts `POST /api/signals/{ts}/legs` for manual per-leg fill editing on scale-out ATMs.
- `trades.py` (helper module — actual route in `main.py`) — derives round-trip P&L from fills. Outputs `is_scale_out`, `entry_fills[]`, `exit_fills[]` (with pre-computed per-leg pnl) so the dashboard can show per-leg detail without recomputing client-side.
- `home.py` — today's session card, action queue (below_floor + missing_journal), equity curve, cumulative-earnings by Live / Evals / Simulation / Signals bucket. Buckets are read from the live Settings doc.
- `health.py` — log tail + latency stats.
- `feed.py` — `/api/feed/{bar,ticks,prune}` ingest from `HelmFeed.cs`. Includes session-gap warmup gate.
- `auto_analysis.py` — `/api/auto-analysis/{config,status}` for the Auto Analysis dashboard panel.
- `settings.py` — `/api/settings` GET/PUT/reset + `/test/ollama`. Pydantic schema at `~/.helm/settings.json`. Consumed by `runtime_config.py` on the bot side.
- `atm_strategies.py` — `/api/atm-strategies` enumerates NT8 ATM XMLs on every call (no caching — user creates new strategies mid-session).
- `version.py` — `/api/version` returns the cached git HEAD-vs-origin/main comparison; `/api/version/check` forces a refresh. Background loop in `main.py` lifespan refreshes every 6 h. Safe-fails on release-zip (no `.git`) installs. Also hosts the one-click updater: `POST /api/version/update` spawns a detached `runtime/update.ps1` helper (copied to `%TEMP%` so a `git reset` on it mid-run can't break the running script). Helper does `git fetch && git reset --hard origin/main`, conditionally re-pips `requirements.txt`, conditionally re-`npm install`s, always re-`npm run build`s, then `Stop-Process` on the uvicorn PID. The watchdog notices and respawns uvicorn (~5 s gap) with the new code. Progress JSON at `~/.helm/update-status.json` survives the restart so the frontend's poll resumes against the new API instance. `/api/version/update/status` is the poll endpoint. **PS 5.1 BOM gotcha:** read the status file with `encoding="utf-8-sig"`, not `"utf-8"` -- 5.1's `Set-Content -Encoding utf8` writes a BOM.
- `drawdown.py` — `/api/drawdown/accounts` per-account prop-firm drawdown tracker (current balance, peak, today P&L, trailing/daily DD remaining, profit-target progress, ok/warn/breach status). Opt-in per account via `Accounts.drawdowns` in Settings. Daily window uses trading-day bounds.
- `trading_day.py` — pure helpers (no router). Future-trading-correct date attribution at 6 PM CT roll. Consumed by `trades.compute_stats`, `home.py`, `drawdown.py`, and the `_resolve_date_window` helper for the trading_day query params on `/api/trades` + `/api/stats`.
- `db.py` — `trades.db` connection + queries. Multi-account filter supports `?account=A&account=B`.
- `_tradebot_bridge.py` — sys.path shim importing `TradingBot/app/src/` modules.

## NS bridge

`HelmAnalyzer.cs` (hotkey-driven, Ctrl+Shift+F → `/api/capture-from-nt`) and `HelmFeed.cs` (auto-publisher → `/api/feed/{bar,ticks}`) live in `../TradingBot/ninjascript/_Helm Locker/`. Two-copy gotcha applies: project canonical there, NT compiles from `~/Documents/NinjaTrader 8/bin/Custom/Indicators/_Helm Locker/`.

## Logging

All FastAPI-side logs append to `../TradingBot/app/data/tradebot.log`, tagged `[api]`. The Health page tails it. Watchdog has its own log at `data/watchdog.log`.

## Don't propose

- Moving FastAPI off `127.0.0.1`. Loopback-only is policy.
- Re-introducing the retired Flask dashboard from TradingBot.
- Touching `trades.db` schema without checking `recorder.py`'s migration helpers — schema changes require a migration step.
