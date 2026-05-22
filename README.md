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
+- Trade_Perf/    # FastAPI + React dashboard, NT fill recorder, watchdog
    +- dashboard/     # FastAPI on :8000 serving /api/* and the React SPA
    +- recorder.py    # mirrors NT8's SQLite -> trades.db
    +- runtime/       # NSSM Windows Service installer + watchdog.ps1
    +- PROJECT.md     # architecture + runtime modes
```

## Documentation

- [`CONFIGURATION.md`](CONFIGURATION.md) — recommended baseline settings for AI backend, strategy thresholds, accounts, Auto Analysis, and NinjaScript indicators. Read this before tweaking anything on the Settings page.
- The dashboard's **Support** page mirrors the same content under the **Configuration** tab, alongside the update guide and a troubleshooting FAQ.

## Architecture in one breath

NinjaScript indicator on hotkey -> FastAPI on `:8000` -> bot opens snipping overlay -> snip + market context -> vision LLM backend (Ollama local/LAN, Anthropic Claude, or OpenAI — selected on the Settings page) -> proposal in `signals.jsonl` -> React dashboard renders.

NT fills are mirrored independently into `Trade_Perf/trades.db` via `recorder.py`. The dashboard joins both data sources at request time.

## Installation

Tested on Windows 11. The bot pipeline assumes NinjaTrader 8 on the same machine. The vision LLM backend is configurable on the Settings page — pick one:

- **Ollama** (local or LAN — free, runs your own model). https://ollama.com/ — `ollama pull qwen2.5vl:7b` after install.
- **Anthropic Claude** (cloud, paid — vision via the Messages API). Needs an API key.
- **OpenAI** (cloud, paid — vision via Chat Completions). Needs an API key.

### Quick install (recommended)

One non-Helm prerequisite the installer can't grab for you:

- **NinjaTrader 8** (8.1.6.3+) — your brokerage connection lives here. https://ninjatrader.com/

Then, from an **elevated** PowerShell. Either clone the repo (requires SSH access):

```powershell
cd $HOME\Documents\Projects
git clone git@github.com:n8t-space/TheHelmTrader.git
cd TheHelmTrader
.\install.ps1
```

Or unzip the bundle you were given:

```powershell
cd $HOME\Documents\Projects
Expand-Archive TheHelmTrader.zip -DestinationPath .
cd TheHelmTrader
.\install.ps1
```

`install.ps1` will:

1. Verify (or `winget install`) Python 3.12+, Node LTS, Git, NSSM
2. `pip install` the dashboard's Python deps
3. `npm install` + `npm run build` the React frontend
4. Copy NinjaScript indicators into `Documents\NinjaTrader 8\bin\Custom\Indicators\_Helm Locker\`
5. Register `recorder.py` as a Startup-folder shortcut + launch it now
6. Register `HelmDashboardWatchdog` as an NSSM service (prompts for your Windows password once — NSSM stores it encrypted)

Re-runnable. Each step skips work already done. Skip individual steps with `-SkipPrereqs`, `-SkipNsIndicators`, `-SkipRecorder`, `-SkipService`.

When the script finishes:

1. Start NinjaTrader 8 (the watchdog will spawn uvicorn within 5s)
2. In NT: NinjaScript Editor (F11) -> Compile (F5)
3. Add `HelmAnalyzer` and `HelmFeed` to each chart you want to use them on
4. Open http://127.0.0.1:8000/ -> Settings -> AI Backend (pick a provider and configure) and account categorization
5. Click **Test connection** on the AI Backend tab

---

### Manual install (if `install.ps1` fails or you want fine control)

#### Prerequisites

Install once. Skip any line you already have.

```powershell
# From an elevated PowerShell
winget install Python.Python.3.12          # python 3.12+ -- NOT the Microsoft Store alias
winget install OpenJS.NodeJS.LTS           # node for the React build (>=18)
winget install Git.Git
winget install NSSM.NSSM                   # service wrapper (install_service.ps1 will also auto-install if missing)
```

#### Clone

```powershell
cd $HOME\Documents\Projects
git clone git@github.com:n8t-space/TheHelmTrader.git
cd TheHelmTrader
```

#### Python dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install fastapi "uvicorn[standard]" pydantic requests Pillow httpx tzdata
```

#### Build the frontend

```powershell
cd Trade_Perf\dashboard\web
npm install
npm run build
cd ..\..\..
```

#### Install NinjaScript indicators

The bot's NS bridge lives in `TradingBot\ninjascript\_Helm Locker\`. NT compiles from its own user folder, so the files must be copied:

```powershell
$src = ".\TradingBot\ninjascript\_Helm Locker"
$dst = "$HOME\Documents\NinjaTrader 8\bin\Custom\Indicators\_Helm Locker"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item -Recurse -Force "$src\*" $dst
```

Then in NinjaTrader: **NinjaScript Editor (F11) -> Compile (F5)**. Look for "Compile succeeded" in the bottom panel.

Add `HelmAnalyzer` and `HelmFeed` to each chart you want to use them on (right-click chart -> Indicators).

### Install the Windows service

Brings the dashboard up automatically while NinjaTrader is running and tears it down on NT exit.

```powershell
# Elevated PowerShell
cd Trade_Perf
.\runtime\install_service.ps1
```

The script will:
- Auto-install NSSM if missing
- Prompt for your user credentials (the service runs as you, not LocalSystem, so the snipping overlay can reach your desktop)
- Register the service as `HelmDashboardWatchdog`
- Start it

Verify:

```powershell
Get-Service HelmDashboardWatchdog                     # Status should be Running
Invoke-WebRequest http://127.0.0.1:8000/api/health    # Status 200 once NT8 is up
```

### First-run configuration

1. Start NinjaTrader 8 — the watchdog will spawn uvicorn within 5 seconds.
2. Open the dashboard: http://127.0.0.1:8000/
3. Navigate to **Settings**.
4. Set:
   - **AI Backend -> Provider** — `ollama`, `claude`, or `openai`.
     - **Ollama** — set the URL (`http://127.0.0.1:11434/api/generate` if local, or `http://<host>:11434/api/generate` if LAN) and the model name (default `qwen2.5vl:7b`).
     - **Claude** — paste your API key; default model is `claude-sonnet-4-6`.
     - **OpenAI** — paste your API key; default model is `gpt-4o`.
   - **Accounts** — map your NT account IDs into Live / Evals / Simulation buckets so the cumulative-earnings card on Home aggregates correctly.
5. Click **Test connection** on the AI Backend tab — green badge with latency + model present means you're good.
6. Settings persist to `%USERPROFILE%\.helm\settings.json`.

### Daily use

Two ways analyses get into `signals.jsonl`:

- **Manual snip (hotkey-driven).** Focus any NT chart and press **Ctrl+Shift+F**. The snipping overlay dims the screen — drag a rectangle around the chart area you want analyzed. The selection plus current market context posts to `/api/capture-from-nt`, the configured AI backend produces a proposal, and a new card appears on the Signal Analysis page. Cold: ~30s; warm: ~1s.
- **Auto Analysis (headless).** Configure cadence and instrument scope on the Settings page (Auto Analysis tab). `HelmFeed` publishes live bars + ticks, the bot polls them, and produces proposals on its own schedule. No hotkey needed.

Trade fills are mirrored independently — `recorder.py` polls NT8's SQLite every few seconds, so executions show up on the Trade Performance page without any operator action.

### Verify end-to-end

- **Recorder:** make a paper trade in NT (or wait for one in Sim). Within ~5 seconds the dashboard's Trade Performance page should show the new fill.
- **Manual snip pipeline:** focus an NT chart and press **Ctrl+Shift+F**. The snipping overlay should dim the screen. Drag a rectangle around the chart. Within 30 seconds (cold) or 1 second (warm), a new entry appears on the Signal Analysis page.
- **Live feed:** if both `HelmAnalyzer` and `HelmFeed` are on charts, the Health page's log tail should show `[feed.bar]` POSTs every minute (or per your bar period).

### Frontend dev mode (optional)

When iterating on the dashboard UI without rebuilding:

```powershell
cd Trade_Perf\dashboard
.\run_dev.ps1                  # Vite with HMR at http://localhost:5173/
```

### Update

The dashboard runs a background check every 6 hours that compares the installed checkout against `origin/main`. When new commits are available, a banner appears at the top of every page showing the current and latest short SHAs, a **View update guide** link to the Support page, and a **Check now** button. Dismissing the banner remembers the latest SHA in `localStorage` — it reappears automatically when a newer commit lands. The endpoint is `GET /api/version`; force a re-check via `POST /api/version/check`.

The **Support** page (in the top nav, or `http://127.0.0.1:8000/support`) breaks the same surface into four tabs — **Overview** (version + uninstall + help), **Update** (full procedure), **Troubleshooting** (FAQ + log locations), and **Configuration** (recommended settings, mirrored from [`CONFIGURATION.md`](CONFIGURATION.md)). Deep-linkable via `/support#update`, `/support#troubleshooting`, `/support#configuration`.

`install.ps1` is idempotent — re-running it after a fresh checkout picks up changes safely. From an **elevated** PowerShell:

```powershell
cd $HOME\Documents\Projects\TheHelmTrader
git pull
.\install.ps1                                  # rebuilds the frontend, refreshes NS indicators, redeploys deps
Restart-Service HelmDashboardWatchdog          # picks up Python/API changes
```

Then in NinjaTrader: **NinjaScript Editor (F11) -> Compile (F5)** — only required if anything under `_Helm Locker\*.cs` changed.

Hard-refresh the dashboard tab (`Ctrl+F5`) to bust the cached SPA bundle.

If you received a release zip instead of using git, unzip over the existing checkout and run the same two commands.

Use the same `-SkipPrereqs / -SkipNsIndicators / -SkipRecorder / -SkipService` switches as the initial install if you want to limit what the rerun touches (e.g. `.\install.ps1 -SkipPrereqs -SkipService` for a frontend-only refresh).

### Uninstall

```powershell
# Elevated PowerShell
cd $HOME\Documents\Projects\TheHelmTrader
.\uninstall.ps1
```

`uninstall.ps1` is the mirror of `install.ps1`. By default it:

1. Stops + removes the `HelmDashboardWatchdog` NSSM service
2. Stops the `recorder.py` process and removes its Startup shortcut
3. Removes the NinjaScript indicators from `Documents\NinjaTrader 8\bin\Custom\Indicators\_Helm Locker\`

It **preserves** `%USERPROFILE%\.helm\settings.json`, `trades.db`, `signals.jsonl`, and `feed.db` so an accidental run can't destroy trade history or API keys.

Switches:

| Flag | Effect |
|---|---|
| `-PurgeSettings` | Also delete `%USERPROFILE%\.helm` |
| `-PurgeData` | Also delete `trades.db`, `signals.jsonl`, `feed.db` |
| `-All` | `-PurgeSettings` + `-PurgeData` |
| `-SkipService` / `-SkipRecorder` / `-SkipNsIndicators` | Leave that piece alone |

After removing the NS indicators, open NinjaTrader and run **NinjaScript Editor (F11) -> Compile (F5)** to clear the compiled assembly. The checkout directory itself is never touched — delete it by hand for a fully clean wipe.

## Quick start (existing operator)

- Open one of the two subprojects in your editor. Each has its own `CLAUDE.md` capturing the working agreement and conventions.
- Production runtime is the `HelmDashboardWatchdog` NSSM service (installed via `Trade_Perf/runtime/install_service.ps1`).
- Frontend dev: `Trade_Perf/dashboard/run_dev.ps1` for Vite HMR at `:5173`.

## Conventions

- Loopback only. FastAPI binds `127.0.0.1`. The only external dependency is whichever AI backend you select on the Settings page — Ollama (local or LAN; firewall the inference port if LAN), Anthropic Claude (cloud), or OpenAI (cloud).
- No auto-execution. The bot proposes; the user decides. Forever.
- Trade data lives in three files that are git-ignored and never push:
  - `TradingBot/app/data/signals.jsonl` (LLM proposals + journal updates)
  - `TradingBot/app/data/feed.db` (NS-published bars + ticks)
  - `Trade_Perf/trades.db` (mirror of NT8 fills)

## Status

Private. Pre-distribution. Not licensed for redistribution.
