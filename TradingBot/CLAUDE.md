# TradingBot — The Helm

> Local-only NT8 chart-analysis bot by **Lodestone & Purser**. Brand = "The Helm".
> The "TradeBot" name persists in module names; the surface brand is The Helm.

## Read first

- [`PROJECT.md`](PROJECT.md) — design + history; **the "Current state" block at the top is canonical** (the body describes original v1; later decisions overrode parts of it).
- [`MIGRATION.md`](MIGRATION.md) — what's changed since v1, what's outstanding. The "Outstanding (next session pickup)" list at the top is the source of truth for what's next.

## One-breath architecture

NinjaScript indicator (`HelmAnalyzer.cs`) on hotkey → POSTs HTF context to FastAPI on `:8000` (hosted by `Trade_Perf/`) → bot opens snip overlay → snip + context → workstation Ollama (`<workstation-LAN-IP>:11434`) → proposal in `app/data/signals.jsonl` → unified React dashboard renders.

NT fills are mirrored independently into `Trade_Perf/trades.db` via `recorder.py`.

## Working agreement (durable)

- **I build it, you guide and teach.** Hand over explanations + step-by-step direction, not finished systems. The user types to learn. (Override with explicit "build it for me" / "keep going".)
- **Be precise about code scope.** Every code block declares: full file, partial patch, or new file.
- **Local-first, fully offline.** Internet allowed only for one-time installs. Nothing in the runtime path may reach the cloud.
- **One concept per turn.** Don't bundle three unrelated changes.
- **Pragmatic over perfect.** Native + simple beats clever + complex.
- **Planning brevity.** Decide minor sub-choices yourself; ask simple yes/no questions when judgment is required.

## Path discipline

- Project root: `%USERPROFILE%\Documents\Projects\TradingBot\`. **Documents, not OneDrive.**
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
- Confidence floor: 0.75. Below-floor proposals re-attempt up to 2x.

## AI inference offloaded to workstation

`local_llm_analyzer.py` calls `http://<workstation-LAN-IP>:11434/api/generate`. Workstation = Ubuntu 24.04 + RTX 4060 Ti 16GB. Cold call ~30s, warm <1s. UFW restricts the port to GEEKOM IP only.

## Live feed pipeline (in flight)

NS-driven publish of bars + ticks → bot ingestion at `/api/feed/{bar,ticks}` → outcome resolver + auto-analysis. Phase 1 done 2026-05-08; Phases 2–4 outstanding. See [`MIGRATION.md`](MIGRATION.md) latest session-log entries for design decisions and current state.

## Don't propose

- Cloud/SaaS endpoints. Not negotiable.
- Re-litigating the v1 scope. If a request implies adding scheduler/dashboard/auto-execution back into v1, treat it as a v2 conversation.
- The vendor-DLL distribution path for NT8 AddOns (separate Helm Copier project — deleted 2026-05-09, but pattern was: trigger files don't auto-add Reference; pivot to source-compile if it ever comes up).
