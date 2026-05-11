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

- `<live-account-id>` — live brokerage
- `<demo-account-id>` — sim
- `<eval-account-id>` — Tradify prop firm eval
- `Sim101`, `Playback101`, `Backtest`, `SimBetaSIM` — NT-default sims

Trades CL / MCL / MES / ES futures through Tradovate.

## Conventions

- **Loopback only.** FastAPI binds `127.0.0.1`. Workstation Ollama at `<workstation-LAN-IP>:11434` is the one external dep, firewalled to GEEKOM via UFW.
- **System Python.** Uvicorn runs from `%USERPROFILE%\AppData\Local\Microsoft\WindowsApps\python.exe`. Bot pipeline deps (`requests`, `Pillow`, `httpx`, FastAPI/Pydantic) installed there. **Not** the TradingBot venv.
- **No auto-execution.** The bot proposes; the user decides. Forever.
- **`signals.jsonl` is append-only.** Updates are new lines with the same timestamp; readers merge latest-wins. Soft-delete = a line with `deleted: true`.
- **CORS dev mode:** `allow_methods=["GET","POST","PUT","DELETE"]` for `:5173`. PUT was added when the Auto Analysis config endpoint landed.

## Routers

- `signals.py` — Signal Analysis page (LLM proposals, journal, outcome).
- `trades.py` (helper module — actual route in `main.py`) — derives round-trip P&L from fills.
- `home.py` — today's session card, action queue, equity curve.
- `health.py` — log tail + latency stats.
- `feed.py` — `/api/feed/{bar,ticks,prune}` ingest from `HelmFeed.cs`. Includes session-gap warmup gate.
- `auto_analysis.py` — `/api/auto-analysis/{config,status}` for the Auto Analysis dashboard panel.
- `db.py` — `trades.db` connection + queries.
- `_tradebot_bridge.py` — sys.path shim importing `TradingBot/app/src/` modules.

## NS bridge

`HelmAnalyzer.cs` (hotkey-driven, Ctrl+Shift+F → `/api/capture-from-nt`) and `HelmFeed.cs` (auto-publisher → `/api/feed/{bar,ticks}`) live in `../TradingBot/ninjascript/_Helm Locker/`. Two-copy gotcha applies: project canonical there, NT compiles from `~/Documents/NinjaTrader 8/bin/Custom/Indicators/_Helm Locker/`.

## Logging

All FastAPI-side logs append to `../TradingBot/app/data/tradebot.log`, tagged `[api]`. The Health page tails it. Watchdog has its own log at `data/watchdog.log`.

## Don't propose

- Moving FastAPI off `127.0.0.1`. Loopback-only is policy.
- Re-introducing the retired Flask dashboard from TradingBot.
- Touching `trades.db` schema without checking `recorder.py`'s migration helpers — schema changes require a migration step.
