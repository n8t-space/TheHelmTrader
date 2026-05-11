# The Helm — Project Context

> **Brand:** "The Helm", offered by **Lodestone & Purser**.
>
> **What it is:** A unified local-only web dashboard for NinjaTrader 8 analysis and automation. Three pages: **Home** (today's session at a glance + action queue), **Trade Performance** (real fills from NT8 with derived round-trip P/L + stats), **Signal Analysis** (LLM-driven chart proposals with journal, outcome, soft-delete, and snip-and-analyze).

This file orients a fresh conversation in *current state*. For history, see [`../TradingBot/MIGRATION.md`](../TradingBot/MIGRATION.md).

---

## 1. Architecture

```
┌─ NinjaTrader 8 (chart, ATM, fills) ─────────────────────┐
│   ↓                                                     │
│   NT SQLite (Documents\NinjaTrader 8\db\NinjaTrader.sqlite)
│      ↓ recorder.py polls every 1s
│   trades.db  ──────────────────┐                        │
│                                │                        │
│   HelmAnalyzer NS indicator    │                        │
│   (Ctrl+Shift+F) ──────────POST→ FastAPI :8000 ◄───────► Vite-built React SPA
│                                │   ├── /api/* (signals, trades, home, health)
│                                │   └── / (SPA — index.html + bundled assets)
│                                │
│   data/signals.jsonl ←─────────┘
│   (TradingBot/app/data/signals.jsonl, via sys.path bridge)
└─────────────────────────────────────────────────────────┘

           Workstation (<workstation-LAN-IP>)
           ↑ HTTP / Ollama qwen2.5vl:7b
           │
       FastAPI background pipeline
       calls when a snip lands
```

- **One process at runtime.** Uvicorn serves both the API and the built React bundle. Vite dev server (`run_dev.ps1`) only used for active frontend development.
- **Two data sources, both file-local.** `trades.db` (SQLite, fed by `recorder.py`) for actual NT fills. `TradingBot/app/data/signals.jsonl` (append-only) for LLM proposals + journal/outcome updates. The dashboard joins them at read time. *(2026-05-08: a third source, `TradingBot/app/data/feed.db`, holds NS-published live bars + trade ticks for the in-flight Independent Confirmation / Auto Analysis project — see MIGRATION.md.)*
- **AI inference on the workstation.** Bot calls Ollama at `<workstation-LAN-IP>:11434` via the URL hardcoded in [`TradingBot/app/src/local_llm_analyzer.py`](../TradingBot/app/src/local_llm_analyzer.py).

## 2. Where things live

| | Path |
|---|---|
| Backend | [`dashboard/api/`](dashboard/api/) — FastAPI app + routers (`signals.py`, `trades.py`, `home.py`, `health.py`, `feed.py`, `auto_analysis.py`) |
| Frontend | [`dashboard/web/`](dashboard/web/) — Vite + React + TypeScript; pages under `src/pages/` |
| Recorder | [`recorder.py`](recorder.py) — long-running poll loop, NT SQLite → `trades.db` |
| Watchdog | [`runtime/watchdog.ps1`](runtime/watchdog.ps1) + `install_watchdog.ps1` — autostart while NT is running |
| Bridge | [`dashboard/api/_tradebot_bridge.py`](dashboard/api/_tradebot_bridge.py) — sys.path shim to import TradingBot's `app/src/` modules |
| Dev launcher | [`dashboard/run_dev.ps1`](dashboard/run_dev.ps1) — backend + Vite dev server in two windows |
| Storage on disk | `trades.db` (this project); `../TradingBot/app/data/signals.jsonl` + `screenshots/` (other project) |

## 3. Runtime modes

**Production (autostart):** the watchdog brings uvicorn up when `NinjaTrader.exe` is running and stops it when NT exits. Browse to `http://localhost:8000/`. Install once with `pwsh runtime/install_watchdog.ps1`.

**Frontend dev:** run `dashboard/run_dev.ps1` to start Vite at `:5173` with HMR + a `/api` proxy to FastAPI on `:8000`. Browse to `http://localhost:5173/` for hot-reload.

**Manual one-shot:** `python -m uvicorn dashboard.api.main:app --port 8000 --reload` from project root.

## 4. NS indicators

Two indicators ship in [`TradingBot/ninjascript/_Helm Locker/`](../TradingBot/ninjascript/_Helm%20Locker/):

- **`HelmAnalyzer.cs`** — hotkey-driven (Ctrl+Shift+F). POSTs the chart's higher-timeframe context + indicators + market-structure (3-lens BOS/CHoCH) to `:8000/api/capture-from-nt`. Bot opens the Snipping overlay, snips → analyze → store, with the NS payload prepended to the prompt as authoritative price context.
- **`HelmFeed.cs`** *(added 2026-05-08, Phase 1 of the live-feed pipeline)* — auto-publisher. On every closed bar (chart's native period) POSTs to `:8000/api/feed/bar`; via `OnMarketData` filtered to `MarketDataType.Last`, batches trade ticks every ~250 ms to `:8000/api/feed/ticks`. Skips historical replay (only `State.Realtime`). Multi-chart safe — the bot dedupes on `(instrument, period, ts)` for bars and `(instrument, ts_ms, price)` for ticks, so two charts on the same MES 5m don't double-store. Apply to any chart you want piped into the bot.

The **two-copy gotcha** applies to both: project canonicals live in `TradingBot/ninjascript/_Helm Locker/`, but NT compiles from `~/Documents/NinjaTrader 8/bin/Custom/Indicators/_Helm Locker/`. Edits must be synced; NT must be **fully restarted** (F5 alone has been unreliable when class names change or files move).

## 5. Logging & health

- All FastAPI-side logs append to TradingBot's `data/tradebot.log` (configured at app startup in [`main.py`](dashboard/api/main.py) `_configure_unified_logging()`), tagged `[api]` to distinguish from bot-pipeline logs.
- The **Health page** (`/health`) tails that file every 3 s and shows latency stats (p50/p95) computed from the last 50 signals' `duration_s`.
- Watchdog has its own log: [`data/watchdog.log`](data/) — start/stop transitions only.

## 6. Conventions worth knowing

- **Loopback only.** FastAPI binds to `127.0.0.1`. Workstation Ollama at `<workstation-LAN-IP>:11434` is the one external dependency, firewalled to the GEEKOM via UFW.
- **No auto-execution.** The bot proposes; the user decides. Forever.
- **System Python.** Uvicorn runs from system Python at `%USERPROFILE%\AppData\Local\Microsoft\WindowsApps\python.exe`. Bot pipeline deps (`requests`, `Pillow`, `httpx`, plus all the FastAPI/Pydantic stack) are installed there. Single-venv consolidation is on the deferred list — see MIGRATION.md.
- **`signals.jsonl` is append-only.** Updates are new lines with the same timestamp; readers merge latest-wins. Soft-delete = a line with `deleted: true`. Recoverable by hand-editing the JSONL.

## 7. Status & open work

See [`../TradingBot/MIGRATION.md`](../TradingBot/MIGRATION.md) "Outstanding (next session pickup)" for the canonical task list. Highlights as of 2026-05-09:

- **Live Feed Pipeline** — Phases 1–4 all shipped (NS publisher + transport + storage; outcome resolver; auto-analysis hook-up with `auto_analysis_config` table, queue/coalescing worker, dashboard "Auto Analysis" card on Home; retention prune + session-gap warmup gate). Three tails remain: live verification at next market open, swapping the stub auto-analyzer for a real headless LLM call, auto-trigger of `/api/feed/prune`.
- **SHARING** (next initiative) — configuration page exposing every key a deploying user would need to override, and an installer package that bundles backend + frontend + watchdog + NS indicators.
- NS account-state indicator → Open Positions card on Home (still a placeholder).
- Single-venv consolidation (eliminates the sys.path bridge; gets more important once the installer lands).
- Bigger / Q8 vision model A/B.
