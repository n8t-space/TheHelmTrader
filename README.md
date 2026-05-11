# The Helm

> Local-only NinjaTrader 8 chart-analysis dashboard and LLM signal pipeline. Built by Lodestone & Purser.

This is a monorepo containing two coupled projects.

## Layout

```
TheHelmTrader/
+- TradingBot/        # LLM chart-analysis bot + NinjaScript bridges
|   +- app/           # Python: pipeline, analyzer, outcome resolver, feed store
|   +- ninjascript/   # HelmAnalyzer.cs (hotkey) + HelmFeed.cs (live bars/ticks)
|   +- PROJECT.md     # design + history
|   +- MIGRATION.md   # what's changed since v1, what's outstanding
+- NT8_Trade_Perf/    # FastAPI + React dashboard, NT fill recorder, watchdog
    +- dashboard/     # FastAPI on :8000 serving /api/* and the React SPA
    +- recorder.py    # mirrors NT8's SQLite -> trades.db
    +- runtime/       # NSSM Windows Service installer + watchdog.ps1
    +- PROJECT.md     # architecture + runtime modes
```

## Architecture in one breath

NinjaScript indicator on hotkey -> FastAPI on `:8000` -> bot opens snipping overlay -> snip + market context -> workstation Ollama (`<workstation-LAN-IP>:11434`) -> proposal in `signals.jsonl` -> React dashboard renders.

NT fills are mirrored independently into `NT8_Trade_Perf/trades.db` via `recorder.py`. The dashboard joins both data sources at request time.

## Quick start (existing operator)

- Open one of the two subprojects in your editor. Each has its own `CLAUDE.md` capturing the working agreement and conventions.
- Production runtime is the `HelmDashboardWatchdog` NSSM service (installed via `NT8_Trade_Perf/runtime/install_service.ps1`).
- Frontend dev: `NT8_Trade_Perf/dashboard/run_dev.ps1` for Vite HMR at `:5173`.

## Conventions

- Loopback only. FastAPI binds `127.0.0.1`. The one external dependency is the workstation Ollama, firewalled to the GEEKOM IP via UFW.
- No auto-execution. The bot proposes; the user decides. Forever.
- Trade data lives in three files that are git-ignored and never push:
  - `TradingBot/app/data/signals.jsonl` (LLM proposals + journal updates)
  - `TradingBot/app/data/feed.db` (NS-published bars + ticks)
  - `NT8_Trade_Perf/trades.db` (mirror of NT8 fills)

## Status

Private. Pre-distribution. Not licensed for redistribution.
