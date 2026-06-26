# TradingBot — The Helm

> Local-only NT8 chart-analysis bot by **Lodestone & Purser**. Brand = "The Helm".
> The "TradeBot" name persists in module names; the surface brand is The Helm.

## Read first

- [`PROJECT.md`](PROJECT.md) — design + history; **the "Current state" block at the top is canonical** (the body describes original v1; later decisions overrode parts of it).
- [`MIGRATION.md`](MIGRATION.md) — what's changed since v1, what's outstanding. The "Outstanding (next session pickup)" list at the top is the source of truth for what's next.

## One-breath architecture

NinjaScript indicator (`HelmFeed.cs`, the single Helm chart indicator since v1.1.0-beta.1) publishes bars+ticks+screenshot+rich context to FastAPI on `:8000` (hosted by `Trade_Perf/`) on each bar close → headless analyzer (or Ctrl+Shift+F manual capture) → Ollama (local or LAN, per Settings) → proposal in `app/data/signals.jsonl` → unified React dashboard renders.

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

- Project canonical: `ninjascript/_Helm Locker/HelmFeed.cs` (single indicator: live feed + screenshot + rich context + Ctrl+Shift+F hotkey). `HelmAutoTrader.cs` is the auto-exec strategy. `HelmAnalyzer.cs` was merged into HelmFeed and deleted (v1.1.0-beta.1).
- NT compiles from: `~/Documents/NinjaTrader 8/bin/Custom/Indicators/_Helm Locker/`.
- **Two-copy gotcha:** edit project file, **copy to NT's path**, F5 in NS Editor. Forgetting = NT runs stale code despite "Compile succeeded."
- Hotkey: **Ctrl+Shift+F**. POSTs to `:8000/api/capture-from-nt`.

## NS gotchas (hard-won)

- `AddDataSeries()` **must** be in `State.Configure`, never `State.SetDefaults` (silent type-discovery exclusion).
- `HttpClient` static field initializer can fail in NS sandbox; lazy-init inside a property.
- Hotkey: hook on the parent `Window` (via `System.Windows.Window.GetWindow(...)`), not `ChartControl` — chart panels rarely hold focus.
- JSON `NaN`/`Infinity` break FastAPI's JSON parsing → emit `null` instead.
- `Bars` implements `ISeries<double>`, so `EMA(BarsArray[idx], n)` / `ADXR/DonchianChannel(BarsArray[idx], ...)` all bind to the `(ISeries, ...)` overload. `GetCurrentBid(int)`/`GetCurrentAsk(int)` exist on `NinjaScriptBase` (indicators too, not just strategies) — use the index to pin a multi-series quote to the primary.
- On a multi-series indicator, guard `OnMarketData` with `if (BarsInProgress != 0) return;` or each tick fires once per added series.

## Versioning & releases (since v1.1.0-beta.1)

Semantic versioning, two channels — see [`VERSIONING.md`](../VERSIONING.md).
- **`main` = production.** The bot's version-check + in-place updater track `origin/main` (`version.py`, SHA compare). Only **validated** work lands here, tagged `vX.Y.Z`.
- **`beta` branch = staging.** Unvalidated work, tagged `vX.Y.Z-beta.N`; the production updater does NOT pull it. Validate (Sim/Playback/live) → merge `beta`→`main` → tag stable.
- **Bump `VERSION` on EVERY push** (+ a `CHANGELOG.md` line). Operator's product-centric semver: **MAJOR** = major system overhaul / breaking contract; **MINOR** = a new page/feature/tool introduced; **PATCH** = an update to an existing page/tool. Bump by the highest-order change in the push. Conventional commits. Don't push unvalidated work to `main`. Don't click the in-app updater while the local checkout is on `beta` (it resets to `origin/main`).

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

**Implication for HelmFeed's context builder:** the bot's emitted context must match what the user sees on screen. Don't compute and emit indicator values the user doesn't have on chart (e.g., EMA-20 / 50 / 200) — the LLM will reference them in its reasoning and the user can't validate against the chart. Keep the bot's indicator set ≤ the chart's indicator set. (Current emitted set per timeframe: EMA90, ADXR14, Donchian14, 20-bar swings; plus pivots/session levels + 3-lens BOS/CHoCH structure.)

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

## Auto-analysis rules (durable, 2026-06-03; ATM made OPTIONAL 2026-06-18 / v2.0.0)

- **ATM is OPTIONAL on directional proposals (v2.0.0, Item 1A).** `auto_trader.require_atm_for_directional` (default `False`) gates it; read live via `runtime_config.require_atm_for_directional()`. With it OFF, `proposal_sanity.sanity_check` no longer rejects a blank-ATM long/short -- the trade executes via the bare-LIMIT OCO path using the LLM's own stop/target. Set the flag `True` to restore the legacy "blank ATM -> auto-dismissed" behavior. Flat still clears `atm_strategy`/brackets.
- **The ATM menu is injected by code**, not the prompt file. Both `analyze()` (visual) and `analyze_text()` (text-only) prepend `_format_atm_block()`; both run `_derive_stop_target()`. When the LLM names a template, stop/target derive from it (LLM's are advisory). When ATM is blank AND the flag is off, `_derive_stop_target` calls `_apply_llm_stop_target` -- it keeps the LLM's numeric stop/target (validated side-of-entry, snapped to tick), sets `atm_strategy_resolved=False` / `atm_brackets=[]` / `atm_total_qty=1`, and falls back to the 1:2 tick default if invalid. Keep the two paths at parity.
- **Auto-analysis skips an instrument that holds a live trade** (`headless_analyzer._has_active_trade` — entry_triggered OR exec working/filled, not resolved). This is **per-instrument, not per-timeframe**: an open MCL trade blocks ALL MCL auto-analysis regardless of armed period. A hung/unresolved trade therefore starves new signals — watch the outcome-watcher.
- **Prompt files** (`app/prompts/`): `analyzer.txt` (vision; used by manual snip AND visual auto-analysis), `headless_analyzer.txt` (text-only fallback, no fresh screenshot). Read **per-call** → edits are live with NO restart. `.format()`-rendered? `headless_analyzer.txt` yes (double literal braces); `analyzer.txt` is concatenated (single braces OK). As of v2.0.0 both say the ATM template is OPTIONAL: if none chosen, emit explicit numeric stop/target at >= 2:1. Keep both at parity.
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
- **"Open" includes an unflattened RUNNER (durable, 2026-06-05).** `exec_queue` locks an instrument while a filled signal there is still open: unresolved outcome OR a scale-out leg still open after TP1 (`_trade_still_open`). A `partial` (TP1 filled) with an open runner leg keeps the lock so the next signal can't stack on the live runner (entangled position, inflated qty); a fully-resolved signal frees the instrument. `headless_analyzer._has_active_trade` mirrors this. **Gate on the signal's LEG state, NOT the raw NT8 `position` column** — that column is garbled for ATM reversal/scale-out fills (same-ms conflicting values; a signed re-walk disagrees wildly), so gating on it deadlocks the queue on phantom stale positions. Legs are watcher-maintained and auditor-corrected to real fills, so a closed runner reliably frees the lock.
- **One NS HelmAutoTrader instance per instrument.** Each instance trades only its chart's instrument (ignores other roots) and self-caps at `MaxConcurrent=1`. MES + MCL = two instances, both on the locked account.
- **Account is dashboard-driven** (single source of truth = Settings > Auto-Trader > Account, via `GET /api/auto-trader/account`). The strategy fetches it on the **poll tick** (worker thread) and uses it as the allowed account; the `AllowedAccount` property is only the offline fallback. Do NOT fetch in `State.Realtime` (boot race -> permanent disable) and do NOT treat the property as the source of truth.
- **Queue**: <=1 signal per instrument, superseded/older ones expire to `no_fill`. Cancel-on-chart of an unfilled entry -> `no_fill` (kept on board, excluded from P&L); a filled position is a normal close, not a clear.

## Auto-Trader: per-account config + ATM-less OCO (durable, 2026-06-18 / v2.0.0)

- **Per-account guardrails override the globals.** `settings.account_configs[id]` (LIVE + EVAL only) supersedes the matching global `auto_trader` fields via `settings.effective_guardrails(account)`. Sim accounts (no card) resolve entirely to the globals (D6). Every enforcement point in `auto_trader.py` (arm gate, queue ceilings, balance floor, offer filter) reads through the resolver, not the global field directly.
- **Risk sizing supplies the ATM-less qty.** `auto_trader._resolved_qty` cascade: Item-3 risk sizing (% of live cash | fixed $, from `instruments.json` tick_value + stop distance, clamped to per-account max_contracts_per_instrument) -> explicit `proposal.qty` -> `auto_trader.default_qty` -> 1. ATM-template signals keep their template-fixed size. The per-order ceiling gate still refuses oversize at arm.
- **Trailing-DD = user-entered limit + server HWM.** `account_configs[id].trailing_dd_limit` is enforced against an equity high-water mark `auto_trader._equity_hwm` computed from NetLiquidation reports. Breach forces the master switch OFF for that account (no auto-flatten), same fail-safe as the balance floor. HWM is in-memory (re-seeds after a restart).
- **ATM-less OCO path in `HelmAutoTrader.cs`.** Blank-ATM signals -> managed `EnterLong/ShortLimit` entry, then on fill `ExitLong/ShortStopMarket` + `ExitLong/ShortLimit` sharing the entry's `fromEntrySignal` (NT8 auto-OCOs + auto-resizes). Named-ATM path (`AtmStrategyCreate`) unchanged; `Tracked.IsOco` selects the monitor. Do NOT use `SetStopLoss`/`SetProfitTarget`. `IsUnmanaged` stays `false`.

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
