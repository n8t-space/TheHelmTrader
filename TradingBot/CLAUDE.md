# TradingBot — The Helm

> Local-only NT8 chart-analysis bot by **Lodestone & Purser**. Brand = "The Helm".
> The "TradeBot" name persists in module names; the surface brand is The Helm.

## Read first

- [`PROJECT.md`](PROJECT.md) — design + history; **the "Current state" block at the top is canonical** (the body describes original v1; later decisions overrode parts of it).
- [`MIGRATION.md`](MIGRATION.md) — what's changed since v1, what's outstanding. The "Outstanding (next session pickup)" list at the top is the source of truth for what's next.

## One-breath architecture

NinjaScript indicator (`HelmAnalyzer.cs`) on hotkey → POSTs HTF context to FastAPI on `:8000` (hosted by `Trade_Perf/`) → bot opens snip overlay → snip + context → Ollama (local or LAN, per Settings) → proposal in `app/data/signals.jsonl` → unified React dashboard renders.

NT fills are mirrored independently into `Trade_Perf/trades.db` via `recorder.py`.

## Working agreement (durable)

- **I build it, you guide and teach.** Hand over explanations + step-by-step direction, not finished systems. The user types to learn. (Override with explicit "build it for me" / "keep going".)
- **Be precise about code scope.** Every code block declares: full file, partial patch, or new file.
- **Local-first, fully offline.** Internet allowed only for one-time installs. Nothing in the runtime path may reach the cloud.
- **One concept per turn.** Don't bundle three unrelated changes.
- **Pragmatic over perfect.** Native + simple beats clever + complex.
- **Planning brevity.** Decide minor sub-choices yourself; ask simple yes/no questions when judgment is required.

## Path discipline

- Canonical path: `%USERPROFILE%\Documents\Projects\TheHelmTrader\TradingBot\`. **Documents, not OneDrive** — OneDrive-redirected paths break NT8 + watchdog runtimes.
- Sibling dashboard project at `../Trade_Perf/`.
- Always use `pathlib`, never `os.path`. Build paths from a project-root anchor.
- NT user folder: `%USERPROFILE%\Documents\NinjaTrader 8\` (also non-OneDrive).

## Python venv

`app/trading_env/` exists. **Do NOT activate it** — call `app\trading_env\Scripts\python.exe` directly. Avoids cmd-vs-PS activation confusion. Note: the dashboard (Trade_Perf) actually runs on **system Python**, not this venv.

## NinjaScript bridge

- Project canonical: `ninjascript/_Helm Locker/HelmAnalyzer.cs` (and `HelmFeed.cs` for the live-feed pipeline).
- NT compiles from: `~/Documents/NinjaTrader 8/bin/Custom/Indicators/_Helm Locker/`.
- **Two-copy gotcha:** edit project file, **copy to NT's path**, F5 in NS Editor. Forgetting = NT runs stale code despite "Compile succeeded."
- Hotkey: **Ctrl+Shift+F**. POSTs to `:8000/api/capture-from-nt`.

## NS gotchas (hard-won)

- `AddDataSeries()` **must** be in `State.Configure`, never `State.SetDefaults` (silent type-discovery exclusion).
- `HttpClient` static field initializer can fail in NS sandbox; lazy-init inside a property.
- Hotkey: hook on the parent `Window` (via `System.Windows.Window.GetWindow(...)`), not `ChartControl` — chart panels rarely hold focus.
- JSON `NaN`/`Infinity` break FastAPI's JSON parsing → emit `null` instead.

## Chart conventions (instrument-invariant)

The user's 5-minute charts use the **same indicator stack regardless of instrument** (MES, MCL, ES, CL, etc.). The stack — verified from `MyFutures.xml` workspace, 2026-05-09:

- **Pivots** — daily, classic floor formula. PP gold, R1–R3 dodger blue, S1–S3 crimson.
- **THE_VWAP_INTRADAY** — daily timeframe, StDev1=1, StDev2=2, slope-based VWAP color, dashed ±1σ, solid ±2σ.
- **EMA(90)** — slate blue, single MA (no 20/50/200).
- **Overnight_High_Low_jay** — session 17:00–08:29, current + previous + midline + daily open visible.
- **BarTimer** — bottom-right.
- **DonchianChannel(14)** — mean gold, bands dodger blue.
- **OrderFlowTradeDetector** — ≥200 contracts trigger, BidAsk basis, no alerts.
- **ATR(14)** — sub-panel, dark cyan. (On some charts; not all.)
- **PriorDayOHLC** — open dashed steel blue, high dark cyan, low crimson, close dashed slate blue. (On some charts; not all.)
- **OrderFlowVolumeProfile** — sessions, letters, value area 68%. (On some charts; not all.)

**Implication for HelmAnalyzer:** the bot's emitted context must match what the user sees on screen. Don't compute and emit indicator values the user doesn't have on chart (e.g., EMA-20 / 50 / 200) — the LLM will reference them in its reasoning and the user can't validate against the chart. Keep the bot's indicator set ≤ the chart's indicator set.

## v1 scope (locked)

- Chart capture via Windows Snipping overlay (`ms-screenclip:` URI).
- Output: structured JSON proposal — `instrument, direction (long/short/flat), entry, stop, target, risk_reward, confidence, reasoning`.
- Model: `qwen2.5vl:7b` via Ollama. Fallback: `minicpm-v:latest`.
- Storage: `app/data/signals.jsonl` (append-only). Updates merge latest-wins on read.
- Tick rounding: every proposal snaps `entry/stop/target` to the instrument's tick size via `src/instruments.py`.
- (Confidence score removed 2026-06-02 — proposals generate in a single LLM pass; no floor / no reassessment retries.)

## AI inference offloaded to workstation

`local_llm_analyzer.py` calls Ollama via `runtime_config.ollama_url()`, default `http://127.0.0.1:11434/api/generate`. The URL, model, timeout, and num_ctx are all overridable via the Settings page (stored at `~/.helm/settings.json`). Source of truth is `runtime_config.py` — adds a knob = add a field there + a matching Pydantic field in `Trade_Perf/dashboard/api/settings.py`. If pointing at a LAN GPU host, firewall the inference port to the bot machine.

## Live feed pipeline (in flight)

NS-driven publish of bars + ticks → bot ingestion at `/api/feed/{bar,ticks}` → outcome resolver + auto-analysis. Phase 1 done 2026-05-08; Phases 2–4 outstanding. See [`MIGRATION.md`](MIGRATION.md) latest session-log entries for design decisions and current state.

## Auto-analysis rules (durable, 2026-06-03)

- **A directional proposal MUST carry an `atm_strategy`.** `proposal_sanity.sanity_check` rejects long/short with an empty ATM → auto-dismissed (never "entered"). Flat clears `atm_strategy`/brackets. ATM is empty ONLY on flat.
- **The ATM menu is injected by code**, not the prompt file. Both `analyze()` (visual) and `analyze_text()` (text-only) prepend `_format_atm_block()`; both run `_derive_stop_target()`. The LLM picks a real template; stop/target derive from it (LLM's stop/target are advisory). Keep the two paths at parity.
- **Auto-analysis skips an instrument that holds a live trade** (`headless_analyzer._has_active_trade` — entry_triggered OR exec working/filled, not resolved). This is **per-instrument, not per-timeframe**: an open MCL trade blocks ALL MCL auto-analysis regardless of armed period. A hung/unresolved trade therefore starves new signals — watch the outcome-watcher.
- **Prompt files** (`app/prompts/`): `analyzer.txt` (vision; used by manual snip AND visual auto-analysis), `headless_analyzer.txt` (text-only fallback, no fresh screenshot). Read **per-call** → edits are live with NO restart. `.format()`-rendered? `headless_analyzer.txt` yes (double literal braces); `analyzer.txt` is concatenated (single braces OK).
- **Stale-bar gate**: `/api/feed/bar` drops bars arriving >120s after their close ts (backfill) and the first bar after any >30 min gap (post-gap warmup, skips chaotic session open). A "no signals" report is often one of these or the active-trade skip — check before assuming a bug.

## Outcome resolution (durable, 2026-06-05)

- **Only EXECUTED signals are tracked.** `outcome_watcher` skips any signal whose `exec.state is None` — if the auto-trader never placed a real trade, there is no fill to confirm against and a paper outcome would be a pure hypothetical (or a phantom — an entry the market never reached) that never appears in Trade Performance. Real (executed) signals are reconciled to broker fills by the **auditor** (`Trade_Perf/dashboard/api/auditor.py`); the watcher only fills the interim gap before a trade closes.
- **Never resolve stop/target before the entry is CONFIRMED hit.** The watcher gates on `entry_triggered is True` (and walks from `entry_hit_ts`, not the signal ts). `resolve_brackets` assumes a fill at `entry_price`, so resolving a pending entry books a phantom (e.g. a long limit below a market that ran straight up). Pending entries wait; at window expiry the entry resolver writes `no_fill`.
- **The `audit` block MUST be in `signal_storage.MERGEABLE_FIELDS`.** `load_all` only merges whitelisted fields; if `audit` is dropped, the auditor's real-fill P&L override silently vanishes from the dashboard AND the auditor re-corrects every pass (file bloat). Any new top-level signal field that must survive updates goes in that tuple.

## Secrets / credentials split (durable, 2026-06-04)

- **`~/.helm/credentials.json` holds the sensitive sections only** — `ai_backend` (API keys, inference URLs, models) + `accounts` (broker IDs). `~/.helm/settings.json` holds everything else and is secret-free, safe to share/commit/bundle. Both live outside the repo; `credentials.json` is also git-ignored.
- Split happens in `Trade_Perf/dashboard/api/settings.py`: `_save_to_disk` writes the two files; `_load_from_disk` overlays credentials over settings; `_migrate_credentials` (runs every load, incl. post-update restart) moves any inline secrets out of a legacy settings.json. The credentials path is **derived from `SETTINGS_PATH`** (`_credentials_path()`) so test redirection isolates it too.
- **Do NOT** put API keys or account IDs back into `settings.json`, default seeds, docs, session logs, or commit messages. `_CREDENTIAL_SECTIONS` is the source of truth for what's sensitive. install/update never overwrite `credentials.json`.

## Auto-Trader: concurrency & account (durable, 2026-06-04)

- **Concurrency is PER-INSTRUMENT, not global.** `exec_queue` skips only instruments that already have an open trade (one open per instrument); `auto_trader.account` `max_concurrent` is the overall ceiling across instruments. Do NOT reinstate a global "one trade total" hold.
- **"Open" includes an unflattened RUNNER (durable, 2026-06-05).** `exec_queue` unions the signal-derived open set with `_instruments_with_open_position(account)` — instruments where the real NT8 net position (last fill's `position`) is non-zero. A scale-out runner after TP1 sets `outcome='partial'` but the position is STILL OPEN; gating on outcome alone released the lock and stacked the next entry on the runner (entangled position, inflated derived qty). `headless_analyzer._has_active_trade` likewise treats a `partial` with any open leg as active. Lookup fails OPEN (empty set) so a db error never deadlocks the queue.
- **One NS HelmAutoTrader instance per instrument.** Each instance trades only its chart's instrument (ignores other roots) and self-caps at `MaxConcurrent=1`. MES + MCL = two instances, both on the locked account.
- **Account is dashboard-driven** (single source of truth = Settings > Auto-Trader > Account, via `GET /api/auto-trader/account`). The strategy fetches it on the **poll tick** (worker thread) and uses it as the allowed account; the `AllowedAccount` property is only the offline fallback. Do NOT fetch in `State.Realtime` (boot race -> permanent disable) and do NOT treat the property as the source of truth.
- **Queue**: <=1 signal per instrument, superseded/older ones expire to `no_fill`. Cancel-on-chart of an unfilled entry -> `no_fill` (kept on board, excluded from P&L); a filled position is a normal close, not a clear.

## AI providers (durable, 2026-06-04)

- **Per-component provider**: `ai_backend.news_provider` / `signal_provider` ("" = inherit `provider`). Resolve via `runtime_config.provider(component)` / `is_provider_configured(component)`. News runs on Claude (big Econoday HTML overflows Ollama's num_ctx -> returns `{`); signals can stay local.
- **`claude-opus-4-8` does NOT support assistant-message prefill** (400 "conversation must end with a user message"). Don't prefill the assistant turn for any Claude call; rely on the prompt + a defensive parse.

## Dev environment (durable, 2026-06-04)

- Run an isolated instance with `HELM_HOME` (settings/credentials/news/version honor it; unset = `~/.helm`). `Trade_Perf/runtime/setup-dev-env.ps1` makes a `dev` git worktree + seeds `~/.helm-dev` + a live-data snapshot; `run-dev.ps1` launches it foreground on :8001 (`--reload`, NOT the NSSM service). Live :8000 stays untouched.

## Operational gotcha (2026-06-04)

- **NT8 hang freezes feeds.** If an instrument's bars freeze while its ticks stay 0-min-old, NinjaTrader/the chart is hung (ticks ride the market-data subscription; bar publishing stops) -- not a Helm bug. Reboot NT. The reconnect backfills bars as a burst that the 120s stale-gate skips, so analysis only resumes on the next CLEAN live bar.

## Restart mechanism (durable, 2026-06-03)

- **In-app "Restart Helm" button works** (`POST /api/version/restart`): uvicorn **self-exits** (`os._exit`), watchdog respawns. Do NOT revert to `Stop-Process` against the uvicorn PID — it runs Session-0 as the NSSM service account and refuses with Access denied.
- **`watchdog.ps1` `Write-Log` must use `Write-Host`, never `Write-Output`** — Write-Output leaks onto the success pipeline, so any function that logs then returns an object (`Start-Dashboard`) returns an array and downstream `.HasExited`/`.WaitForExit()` blow up on a `[String]`.
- **Bootstrap after editing version.py/watchdog.ps1**: one elevated `Restart-Service HelmDashboardWatchdog` (reloads the watchdog + a fresh uvicorn). After that, the in-app button suffices. Out-of-process `Stop-Process`/`Restart-Service` still need elevation (NSSM ACL).

## Don't propose

- Cloud/SaaS endpoints. Not negotiable.
- Re-litigating the v1 scope. If a request implies adding a scheduler or autonomous trading back into v1, treat it as a v2 conversation. **Exception (2026-06-02):** an opt-in **Auto-Trader** exists — per-signal manual arm only, locked to ONE user-selected account, Sim-only in v1, master switch OFF by default. The executor is the NT8 `HelmAutoTrader` Strategy (`ninjascript/_Helm Locker/HelmAutoTrader.cs`, compiles from `bin/Custom/Strategies/`). This is NOT autonomous firing; the human arms each trade.
- The vendor-DLL distribution path for NT8 AddOns (separate Helm Copier project — deleted 2026-05-09, but pattern was: trigger files don't auto-add Reference; pivot to source-compile if it ever comes up).
- Re-introducing the cross-signal LLM reconciliation pipeline (`_reconcile_open_trades` + `outcome_suggestion` + the "Confirm & remove previous" UI). Removed 2026-05-19 — distorted W/L stats via confirm-and-soft-delete, less accurate than the deterministic bar walker. `local_llm_analyzer.reconcile()` still exists with no caller. Outcomes auto-resolve via `outcome_watcher` walking `feed.db` ticks/bars for BOTH manual and headless signals; the user overrides on the Signal Detail page if needed.
