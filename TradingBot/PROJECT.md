# The Helm (TradeBot) — Project Context

> **Brand:** "The Helm", offered as a service from **Lodestone & Purser**. (Earlier docs and session-log entries below reference the previous working title "FYF Analysis" / "VQR Ventures LLC" — historical, not current.) Module/file names remain neutral (`pipeline.py`, etc.).

> **Purpose:** Seed any fresh conversation about this project with the locked v1 scope, hardware constraints, and working agreement. Read this first — but check the **Current state** block immediately below before relying on any prescriptive section deeper in the doc.

## Current state — read this first (2026-05-08)

The body of this document captures the **original v1 design** from the build sessions of 2026-05-05 → 2026-05-08. Several cross-cutting decisions have shifted since; the source of truth for what's actually running today is [`MIGRATION.md`](MIGRATION.md) and [`NT8_Trade_Perf/PROJECT.md`](../NT8_Trade_Perf/PROJECT.md).

- **AI inference is offloaded to Ollama.** Default localhost; can be pointed at a LAN GPU host via the Settings page. A 7B Q4 vision model on a 4060 Ti turns calls around in 0.7–5 s; an iGPU host runs 15–45 s/call. Anything in §3 about ≥1-minute cadence assumes the slow path.
- **The Flask dashboard described in §4–§5 is retired.** A unified FastAPI + React app at `NT8_Trade_Perf/` now serves all three pages (Home / Trade Performance / Signal Analysis) from `:8000`. Both apps read/write the same `signals.jsonl` via a sys.path bridge, so this project's `app/src/` (`pipeline.py`, `local_llm_analyzer.py`, `signal_storage.py`, `instruments.py`, `screenshot_capturer.py`) is still the engine.
- **NS indicator renamed.** `ninjascript/TradeBotTrigger.cs` → `ninjascript/_Helm Locker/HelmAnalyzer.cs`. POSTs to `:8000/api/capture-from-nt`. Multi-tab guard added.
- **Autostart in place.** A watchdog (PowerShell + Task Scheduler at user logon) brings the dashboard up while NinjaTrader is running and stops it when NT exits. See `NT8_Trade_Perf/runtime/`.
- **Branding.** "The Helm" / "Lodestone & Purser" replaces "FYF Analysis" / "VQR Ventures LLC". Class names like `HelmAnalyzer` follow this.

Sections that are still authoritative: §1 Working Agreement, §6 Output Contract, the Tick-Size / Confidence-Floor / Reconciliation / Trade-Sizing subsections, the NinjaScript Bridge subsection (paths/names updated above), and §13–§14 history.

---

## 1. Working Agreement

Non-negotiable rules for how to work on this project:

- **I build it. You guide and teach.** The architect explains; the engineer types. No drop-in finished systems.
- **Local-first, fully offline.** Internet is acceptable only for one-time installs (Python, Ollama, models). Nothing in the runtime path may reach the internet.
- **Be precise about code scope.** Every code block must say up front: full file, partial patch, or new file.
- **Pragmatic over perfect.** Right tool for the job. Native and simple beats clever and complex. Working beats elegant.
- **Less-code tools welcome** where they earn their place — Task Scheduler, AutoHotkey, `.bat` launchers, etc.
- **One concept per turn.** Don't bundle three unrelated changes into one response.

---

## 2. Project Goal

Build a **local, offline, AI-powered trading proposal tool** that:

1. Captures a screenshot of a NinjaTrader chart on demand.
2. Sends the screenshot to a local vision LLM (Ollama / Qwen2-VL 7B).
3. Returns a full, structured trade proposal: entry / stop / target with reasoning.
4. Stores the proposal + screenshot for later review.

That's the whole product. Advisory only — the bot proposes, the user decides. No autonomous execution in v1.

---

## 3. Hardware Envelope

The host is a **GEEKOM GT1 Mega**: 32 GB RAM, **integrated Intel Arc iGPU** (not a discrete Arc A-series card). No CUDA. No dedicated VRAM.

What this means for every model and cadence decision:

| | Reality |
|---|---|
| 7B vision inference (Q4) | Works. ~15–45 s per call on CPU. |
| 11B+ vision models | Don't reach for them. |
| Cadence | ≥1 minute realistic; manual trigger preferred for v1. |
| RAG (embeddings + vector search) | Fine, future option. |
| LoRA / QLoRA fine-tune | **Off the table on this hardware**, not deferred. The maturity path ends at RAG. |

If hardware ever changes (discrete GPU, second box), revisit this section before anything else.

---

## 4. v1 Scope (Locked)

| Decision | Choice |
|---|---|
| Chart source | NinjaTrader 8 chart on screen — captured by **invoking the Windows Snipping overlay from code** (`ms-screenclip:` URI), then waiting for a new image to land in the clipboard. User clicks "Snip & Analyze" on the dashboard or runs `main.py`; the script opens the overlay, the user drags a rectangle, the snip is processed. No NinjaScript / ATI bridge in v1. |
| Output | Full order proposal as structured JSON (see §6) |
| Trigger | Manual / on-demand (CLI, optionally via AutoHotkey hotkey) |
| Model | Qwen2.5-VL 7B via Ollama (`qwen2.5vl:7b`, ~6 GB). MiniCPM-V (`minicpm-v:latest`, ~5.5 GB) as fallback |
| Storage | `signals.jsonl` (append-only). SQLite deferred until query needs justify it |
| Dashboard | **Added in v1.1**: localhost-only Flask app at `127.0.0.1:5000`. Headless capture pipeline shared with `main.py` (`src/pipeline.run_pipeline`). Index page has a **Snip & Analyze** button that opens the Windows Snipping overlay, then runs the pipeline against the resulting snip. Detail view shows screenshot + proposal, lets the user edit the journal verdict/note, add an outcome (`target`/`stop`/`breakeven`/`partial`/`no_fill`/`not_watched`/`other` + free-text note), and **soft-delete** bad snips (`deleted: true` update — hidden from dashboard, recoverable by editing the JSONL). Updates append to `signals.jsonl` with same timestamp; `load_signals()` merges them latest-wins. |
| Scheduler | **Not in v1.** No `schedule` lib, no Task Scheduler |

If a request implies adding the scheduler, dashboard, or autonomous execution back into v1, it's a v2 conversation.

---

## 5. Architecture (v1)

```
┌──────────────────────────────────────────────────────────┐
│                   Windows 11 Host                        │
│                                                          │
│   ┌──────────────┐                                       │
│   │ NinjaTrader  │                                       │
│   │ chart window │                                       │
│   └──────┬───────┘                                       │
│          │ window-targeted screenshot (mss + pygetwindow)│
│          ▼                                               │
│   ┌─────────────────────┐                                │
│   │ screenshot_         │                                │
│   │ capturer.py         │                                │
│   └─────────┬───────────┘                                │
│             │ cropped PNG (chart panel + price axis)     │
│             ▼                                            │
│   ┌─────────────────────┐    HTTP                        │
│   │ local_llm_          │◄────────► Ollama (Qwen2-VL Q4) │
│   │ analyzer.py         │           localhost:11434      │
│   └─────────┬───────────┘                                │
│             │ structured JSON proposal                   │
│             ▼                                            │
│   ┌─────────────────────┐                                │
│   │ signal_storage.py   │──► data\signals.jsonl          │
│   └─────────────────────┘                                │
│                                                          │
│   Entrypoint: run.bat (optional AutoHotkey hotkey)       │
└──────────────────────────────────────────────────────────┘
```

Per invocation: **capture → analyze → store → print result**. Each module is independently testable.

---

## 6. Output Contract

The vision model is prompted to emit exactly this JSON shape:

```json
{
  "instrument": "ES 03-26",
  "direction": "long | short | flat",
  "entry": 4521.25,
  "stop": 4515.00,
  "target": 4540.00,
  "risk_reward": 3.0,
  "confidence": 0.65,
  "reasoning": "≤3 sentences explaining why."
}
```

`flat` is mandatory as a valid direction — without it the model will invent setups in noise.

The parser must be **defensive**: local 7B models often wrap JSON in prose or markdown fences. Extract the first JSON object; do not require strict bracket-only output.

### NinjaScript Bridge (added 2026-05-06)

[`ninjascript/TradeBotTrigger.cs`](ninjascript/TradeBotTrigger.cs) is a NinjaTrader 8 indicator that, on **Ctrl+Shift+F**, snapshots the chart's multi-timeframe context (5m primary / 30m / 1h / daily / weekly) plus key indicators (EMA 20/50/90/200, ATR(14), Donchian(14) upper/lower/`Mean`, 20-bar swings) plus daily-derived levels (manual floor pivots, today's H/L, yesterday's H/L/C) and POSTs the JSON to `http://127.0.0.1:5000/api/capture-from-nt`.

**Bot side** (`dashboard.capture_from_nt` + `pipeline.run_pipeline(market_context=...)`):
1. Endpoint receives the payload, persists to `data/market_context.json` for audit, returns `202 Accepted` immediately so NS isn't held on the HTTP connection.
2. Background thread runs `run_pipeline` with `market_context` set: opens the Snipping overlay (same `ms-screenclip:` path), polls clipboard for the snip, prepends a formatted **Authoritative Market Context** block to the analyzer prompt, calls the LLM with image + context.
3. The full context dict is stored on the signal record. Detail page renders a "Market Context (NinjaTrader)" section with the headline values + a `<details>` toggle for the full JSON.

**Why a bridge instead of multi-image snipping or a public API:**
- Stays offline — only network call is to localhost.
- Authoritative prices — model uses NS-supplied numbers instead of OCRing the price axis (the foreseeable problem flagged in PROJECT.md).
- Cheap on this hardware — text injection adds negligible latency vs adding 3+ images to the vision call.

**SMC market-structure layer (added 2026-05-06).** `TradeBotTrigger` now also runs three `MarketStructureLens` state machines at retrace sensitivities 0.5 / 1.0 / 2.0. Each lens tracks trend (Up/Down), structure (Bullish/Bearish/Range/Transitional), last structure event (BOS for continuation, CHoCH for reversal), and most recent confirmed swings on each side. Output is a `market_structure[]` array in the JSON payload — one block per lens. Inspired by the reference `mtMarketStructure` output but uses a simplified rule set (events fire only at swing confirmation, not mid-leg). Three lenses chosen so the LLM sees fast/medium/slow disagreement, which is itself meaningful signal — user is still figuring out which retrace level matters for their style. Configurable via `LENS_RETRACE_PCTS` constant in the `.cs` file.

**Two-copy gotcha.** `ninjascript/TradeBotTrigger.cs` is the project canonical, but NinjaTrader compiles from `%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\Indicators\TradeBotTrigger.cs`. After every edit to the project copy, **`cp` to NT's location and recompile (F5)**, otherwise NT runs stale code. Symptom: changes don't take effect even though "Compile succeeded" shows.

**Not yet covered:** custom indicators on the user's charts (Order Flow Trade Detector, Overnight_High_Low_jay, THE_VWAP_INTRADAY). Add by either including their source in `bin\Custom\Indicators\` and emitting their values, or by referencing them via NS's indicator API. NT8's base API doesn't ship a plain `VWAP` indicator — drop in any free VWAP NS file to add it.

### Tick-Size Handling

The model often proposes prices that don't respect the instrument's minimum price increment (e.g. `target: 7299.9` for MES, which moves in 0.25 ticks). Two-layer mitigation:

1. **Prompt-side reminder** in `prompts/analyzer.txt` listing common instrument tick sizes — reduces (but doesn't eliminate) the failure rate.
2. **Code-side enforcement** in `src/instruments.py`: after parsing the model's JSON, `apply_tick_rounding()` looks up the instrument's tick size and snaps `entry/stop/target` to the nearest valid tick using banker's rounding (Decimal-precise).

**Lookup priority:**
1. Explicit symbol map in `instruments.json` (CME futures + crypto + user-added).
2. Forex pattern (6-letter pair) → JPY check → `rules.forex_jpy_tick` or `rules.forex_other_tick`.
3. Stock-like pattern (1–5 letters, optional `.X` suffix) → `rules.stock_default_tick`.
4. Unknown → pass-through, log warning, dashboard shows a hint to add to `instruments.json`.

**Annotated, not silent.** The proposal record gets `tick_size_applied`, `tick_source`, and `tick_adjustments` (list of `{field, from, to}`). The detail page surfaces a red banner when adjustments were made — that's the model-failure signal worth watching during prompt iteration.

**`instruments.json` includes bonds (ZB/ZN/ZF/ZT)** even though the user doesn't trade them — they're there for completeness so the eventual training corpus has full coverage.

### Confidence Floor & Reassessment (added 2026-05-05)

`local_llm_analyzer.analyze_with_floor()` is the entry point used by the pipeline (not `analyze()` directly). If the first attempt returns confidence < `CONFIDENCE_FLOOR` (default 0.75), the model is called again with the same image+prompt, up to `MAX_ATTEMPTS` (default 2). The proposal with the highest confidence wins.

The proposal is annotated with:
- `attempts: int` — how many model calls
- `reassessed: bool` — whether at least one retry ran
- `attempt_confidences: list[float]` — confidence from each attempt
- `confidence_floor: float` — the threshold that was in effect

Detail page surfaces a small `↻ N attempts` badge and the confidence trail; if the final confidence is still below the floor, a `below floor` flag appears next to it.

### Open-Trade Reconciliation (REMOVED 2026-05-19)

The LLM-driven cross-signal reconciliation feature was retired. Each new capture used to fire a separate `reconcile()` call per open trade on the same instrument, storing verdicts as `outcome_suggestion` updates that the user confirmed (and soft-deleted) from the dashboard. In practice the confirm-and-delete flow distorted the W/L + P&L tallies (resolved prior signals disappeared from the visible list) and the LLM's reconciliations were often less accurate than the deterministic feed.db bar walker.

Outcomes are now managed per-signal:
- The **outcome_watcher** walks feed.db ticks/bars and auto-stamps `outcome.result` + `entry_triggered` on any signal where the entry was hit and a stop or target was subsequently touched. Works for both manual (Ctrl+Shift+F) and headless (auto-bar-close) signals.
- The user can override via the Signal Detail page's Outcome editor at any time.
- An **entry/outcome invariant** is enforced everywhere: `outcome=no_fill` ⇔ `entry_triggered=False`; any other outcome implies `entry_triggered=True`. The dashboard write endpoints coerce the pair so they can't drift.

Code references retained in [`local_llm_analyzer.py`](app/src/local_llm_analyzer.py) (the `reconcile` function is still defined but has no caller) and the `outcome_suggestion` field on signal records (now produced only by the bar walker as an audit trail, `engine: "resolver"`).

### Trade Sizing & P/L

`instruments.json` also has a `point_values` map (dollars per point per contract/share) parallel to the tick map: ES=$50, MES=$5, CL=$1000, MGC=$10, etc. `src/instruments.compute_trade_metrics()` derives:

- `risk_per_contract` = |entry−stop| × point_value
- `reward_per_contract` = |target−entry| × point_value
- `total_risk` / `total_reward` = ×position_size
- `realized_pnl` from outcome: `target` → +total_reward; `stop` → −total_risk; `breakeven` → 0; everything else → null

Position size is stored as an update field on each signal (`POST /signal/<ts>/position`); the dashboard's detail page has a Contracts/Shares input. Index columns "Size" and "P/L" surface the computed values per row. Forex P/L assumes a standard lot (100,000 base) — user adjusts position_size for mini/micro lots.

**Display unit:** for futures (anything in the explicit `instruments` map), the Trade Recap shows risk/reward in **ticks × $/tick** instead of points × $/point — that's how futures traders think. Stocks/forex still display in points.

**Closing-price-driven P/L:** the outcome form has an optional `closing_price` input. When set, realized P/L = `direction × (close − entry) × point_value × position_size`, overriding the result-based default. This makes partial fills and manual exits accurate.

### Foreseeable Problem — Reading Prices Off Pixels

Vision LLMs read **structure** (trend, pullback, S/R) much better than they read **specific prices** off chart axes. Tiny fonts and dense scales are where they fail. v1 mitigations:

- Crop to chart panel + price axis only — drop the rest of the NT8 window.
- Render the price scale legibly (wide, large font).
- Prompt the model to identify visible levels first, then express entry/stop/target relative to those levels.
- **Parked for v2:** a NinjaScript indicator that writes current bid/ask + recent swing levels to a text file, injected into the prompt as authoritative price context. Don't build this until the pixel-only path is proven inadequate.

---

## 7. Project Layout

```
%USERPROFILE%\Documents\Projects\TradingBot\app\
├── trading_env\              ← Python virtual environment (do not commit)
├── src\
│   ├── screenshot_capturer.py
│   ├── local_llm_analyzer.py
│   └── signal_storage.py
├── data\
│   ├── screenshots\          ← captured frames
│   └── signals.jsonl         ← append-only proposals
├── prompts\
│   └── analyzer.txt          ← the system/user prompt for the vision model
├── run.bat                   ← single CLI entrypoint
├── tradebot.ahk              ← optional AutoHotkey hotkey wrapper
├── requirements.txt
└── .gitignore
```

Path discipline:

- App root is `%USERPROFILE%\Documents\Projects\TradingBot\app\`. Verified that `Documents\` is **not** OneDrive-redirected on this machine (resolves to the literal disk path), so JSONL/SQLite are safe here.
- **Never** move it under `Program Files` (UAC headaches) or into any cloud-synced folder (OneDrive, iCloud, Dropbox) — sync conflicts will corrupt files.
- In Python, use `pathlib.Path` everywhere. Never hardcode `\\` or `/`.

---

## 8. Stack

| Concern | Choice | Notes |
|---|---|---|
| Python | 3.11 or 3.12 | Widest wheel support on Windows. Skip 3.13/3.14. |
| Virtual env | `python -m venv trading_env` | Don't activate — call `trading_env\Scripts\python.exe` directly. Avoids cmd-vs-PowerShell confusion. |
| LLM runtime | Ollama for Windows | Native installer. Service on `127.0.0.1:11434`. |
| Vision model | `qwen2.5vl:7b` | Fallback: `minicpm-v:latest` |
| Screen capture | **Code-triggered Snipping overlay** via `subprocess.Popen(["explorer.exe", "ms-screenclip:"])` + `PIL.ImageGrab.grabclipboard()` polling for a new image. `mss` + `pygetwindow` reserved for future automated window-targeting. |
| HTTP client | `requests` | |
| Storage | JSONL via stdlib | SQLite deferred |
| Editor | VS Code | Python extension |
| Trigger | `run.bat` + AutoHotkey | No scheduler in v1 |

---

## 9. Setup — Minimal OS-Touch Path

The two things that *might* require OS-level changes are sidestepped:

- **PowerShell execution policy**: avoided by using a `.bat` launcher (no `.ps1`) and calling `python.exe` directly.
- **Task Scheduler**: not used in v1. Manual trigger only.

What's required:

1. `winget install Python.Python.3.12` (per-user, no admin). Or python.org installer → "Install just for me."
2. Install **Ollama for Windows** (standard installer, runs as user-level service).
3. `ollama pull qwen2.5vl:7b` (primary). Optional fallback: `ollama pull minicpm-v`.
4. Create the app folder, then `python -m venv trading_env`, then install deps via the venv python directly:
   ```
   trading_env\Scripts\python.exe -m pip install mss pygetwindow requests Pillow
   ```
   No activation step. `run.bat` will hard-code the venv python path so this stays invisible.
5. Verify Ollama: hit `http://localhost:11434/api/tags` from a browser or `curl`.

Nothing else touched at the OS level.

---

## 10. First Tasks (in order)

Don't skip ahead. Each step de-risks the next one.

1. **Walking skeleton (~40 lines, one file)**: user snips the chart with Win+Shift+S → script reads clipboard via `PIL.ImageGrab.grabclipboard()` → POST image + JSON-output prompt to Ollama Qwen2.5-VL → print the response. Iterate the prompt until parseable JSON returns 8/10 times.
2. **(Future) Automated window targeting**: replace the snip workflow with `pygetwindow` + `mss` for hotkey-driven, zero-interaction runs. Park until the snip workflow is proven and feels limiting.
3. **Module split**: extract `screenshot_capturer.py` and `local_llm_analyzer.py`. Defensive JSON parsing.
4. **Storage**: `signal_storage.py` appends `{timestamp, proposal, screenshot_path}` to `signals.jsonl`.
5. **CLI entrypoint**: `run.bat` activates venv and runs the pipeline end-to-end. Output proposal to terminal.
6. **AutoHotkey hotkey** (optional): keybind triggers `run.bat` against the focused chart.

Stop at each step and verify before moving on.

---

## 11. Maturity Path

Tackle in order. Don't skip ahead.

1. **Prompt optimization** — biggest ROI for least effort. Iterate `prompts\analyzer.txt` against the corpus in `signals.jsonl`, using each entry's `journal.verdict` (agree/disagree/skip) and `journal.note` as the labels. The journaling step in `main.py` is what makes this stage measurable rather than vibes-based — without it, prompt iteration is guesswork.
2. **Few-shot learning** — include 3–5 example (screenshot → correct proposal) pairs in the prompt.
3. **RAG** — when the prompt gets too long, move examples to a local vector store (sqlite-vec or Chroma) with `nomic-embed-text` embeddings, retrieve the most relevant per call.
4. **Fine-tune (LoRA/QLoRA)** — **Restored 2026-05-08** after the workstation migration. 16 GB VRAM is sufficient for LoRA on a 7B vision model. Open path; needs corpus first (the journaling step from item 1 builds it).

Track proposal accuracy in `signals.jsonl` (manual labeling field) so each stage's lift is measurable, not guessed.

---

## 12. Things to Explicitly Not Do

> Some original v1 prohibitions have been deliberately walked back since 2026-05-08; current standing is:

- ~~Don't add a scheduler, dashboard, or autonomous execution to v1.~~ — **Walked back.** A unified dashboard ships at `NT8_Trade_Perf/`, and an autostart watchdog manages its lifecycle alongside NinjaTrader. Autonomous *execution* (placing orders) is still off the table.
- **Don't auto-place trades.** This stays the bot's hard line — proposals only, the user decides.
- ~~Don't reach for an 11B+ vision model on this hardware.~~ — **Walked back** post-workstation migration. 16 GB VRAM fits 7B Q8 comfortably and 14B–26B-class vision models at Q4. Bigger is fair game when there's evidence it helps.
- Don't use Python 3.13/3.14 — wheel support on Windows is still patchy as of writing. 3.12 remains the target.
- Don't put the project in OneDrive / iCloud / Dropbox — sync conflicts will corrupt the JSONL file.
- ~~Don't bind any future dashboard to `0.0.0.0`.~~ — **Still loopback.** FastAPI binds to `127.0.0.1:8000`. Workstation Ollama binds to `0.0.0.0:11434` but is firewalled to GEEKOM only — that's a deliberate exception, not a relaxation of this rule.
- Don't ask the model to invent prices freely — always relative to visible chart levels (or NS-supplied authoritative prices via the bridge).
- Don't add cloud dependencies "just for one thing." The whole point is offline.

---

## 14. Session Log

### 2026-05-06 (NinjaScript bridge + SMC layer)

Built the NinjaScript bridge end-to-end and added a Smart-Money-Concepts market-structure layer. NS now opens the snipping overlay on Ctrl+Shift+F, posts authoritative prices/indicators/HTF context to the bot, and the bot fold the context into the prompt + stores it on the signal record. Late in the session built a 3-lens BOS/CHoCH state machine inside the indicator. Project also moved to git: local repo initialized + initial commit, but push to network-share bare repo blocked by Windows safe.directory check.

**Outstanding for next session (priority order):**
1. **Unblock git push.** User needs to run once in their shell: `git config --global --add safe.directory G:/TraderBot/TraderBot.git`. Then retry the push. (CLAUDE.md forbids me from modifying git config.)
2. **Bot side: surface `market_structure` in the prompt + dashboard.** NS now emits a `market_structure[]` array in the JSON payload (3 lenses × trend/structure/last_event/last_swing/etc.) but the bot's `pipeline._format_context_for_prompt` doesn't render it yet — LLM can't see the new data. Add a "Market Structure" section to the prompt formatter and a small surface on `signal.html`.
3. **Verify NS compiles + runs after `StructureSwing` rename.** Last unfinished test was: F5 in NS Editor → remove + re-add indicator → Ctrl+Shift+F → confirm JSON parses cleanly on bot side and `market_structure` appears in `tradebot.log`.

**Other deferred work (no rush):**
- Stop-distance rules + `quality_flags` — explicitly parked earlier this session.
- Custom NT8 indicators (Order Flow Trade Detector, Overnight_High_Low_jay, THE_VWAP_INTRADAY, source-less mtMarketStructure) — added to deferred list.
- Naming "FYF Analysis" still placeholder; trademark research outstanding.

**Known issues / observations:**
- `bid`/`ask`/`last` from NS sometimes disagree on quiet contracts (e.g., MCL outside RTH) — `GetCurrentBid/Ask` return last-quoted, `Close[0]` is the last-traded price. Worth normalizing later.
- Two-copy gotcha: `ninjascript/TradeBotTrigger.cs` (project) vs `%USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\Indicators\TradeBotTrigger.cs` (NT compiles from here). I `cp` after every edit; if changes seem to ignore F5, that's the symptom.
- Swing detection in our SMC layer uses simplified rules (events fire only at swing confirmation, not mid-leg) compared to the source `mtMarketStructure` reference. Tighten later if needed.

**Files modified or added this session:**
- `ninjascript/TradeBotTrigger.cs` (new + many edits — now ~952 lines)
- `app/dashboard.py` (`/api/capture-from-nt`, threading, file logging, diagnostic warnings)
- `app/src/pipeline.py` (`run_pipeline(..., market_context=)`, `_format_context_for_prompt`)
- `app/templates/signal.html` (Market Context subsection)
- `app/static/style.css` (market-context styling)
- `PROJECT.md` (this file — NinjaScript bridge section, SMC layer, two-copy gotcha)
- `.gitignore` (new; excludes venv, screenshots, runtime caches, backups)
- `backups/TradingBot-pre-smc-20260506-2137.tar.gz` (~1.5 MB tarball before SMC work)
- `references/Unknown_Indicator_JSON_Output.txt` (user-provided reference for SMC inspiration)

---

### 2026-05-05 → 2026-05-06 (initial build session)

**Started from:** empty `app/` folder, only `old project.md` present.

**Shipped end-to-end in this session:**
- **Walking skeleton** → `main.py` calling `pipeline.run_pipeline()` (capture → vision LLM → JSONL append).
- **Modules:** `screenshot_capturer.py`, `local_llm_analyzer.py`, `signal_storage.py`, `pipeline.py`, `instruments.py`, `dashboard.py`. Plus `templates/` (`_topbar.html`, `index.html`, `signal.html`, `error.html`), `static/style.css`, `prompts/analyzer.txt`, `instruments.json`.
- **Entrypoints:** `run.bat` (terminal flow with journaling), `run_dashboard.bat` (Flask dashboard at `127.0.0.1:5000`).
- **Capture mechanism:** code-triggered Windows Snipping overlay via `subprocess.Popen(["explorer.exe", "ms-screenclip:"])`, then poll clipboard for a new image (hash-compared, OSError-safe).
- **Storage:** append-only `data/signals.jsonl` with merge-on-read for journal/outcome/position_size/outcome_suggestion/deleted updates. `data/tradebot.log` shared by `main.py` and `dashboard.py`.
- **Tick-size snapping:** `instruments.json` (CME futures + crypto explicit map + forex/stock fallback rules); UI surfaces adjustments + unknown-instrument warnings.
- **Trade Recap:** futures show ticks × $/tick (not points); closing-price-driven realized P/L; per-contract risk/reward + totals. Index columns include Entry, Stop, Target, R:R, Conf, Size, P/L.
- **Open-trade reconciliation:** retired 2026-05-19. Outcomes now auto-resolve via the deterministic feed.db bar walker; the user overrides on the Signal Detail page if needed. See the "Open-Trade Reconciliation (REMOVED)" section above.
- **Confidence floor 0.75** with auto-reassessment (max 2 attempts); proposal annotated with `attempts`, `reassessed`, `attempt_confidences`.
- **UI rebrand:** FYF Analysis (VQR Ventures LLC working title); dark/cyan theme; Inter + JetBrains Mono with system fallbacks (no CDN — stays offline).

**Observations carried into next session:**
- First-pass confidence is frequently 0.60–0.70 — reassessment helps, but the *prompt* is the higher-ROI lever. Worth iterating `prompts/analyzer.txt` once 20–30 real captures are in `signals.jsonl`.
- No live errors observed in the dashboard watch-tail beyond Flask's standard dev-server startup banner.
- Reconciliation per open trade is one extra LLM call (~30–90 s on this hardware); capped at 3 to stay under the browser timeout envelope.

**Open for next session (in priority order):**
1. **Use it.** Capture 20–50 real charts. Build the journaling corpus.
2. Iterate `prompts/analyzer.txt` once patterns in `signals.jsonl` surface (especially common confidence-floor failures).
3. Open the JSONL and look at every `disagree` journal entry — the *why* notes are the seed of strategy articulation.
4. **Naming**: "FYF Analysis" is a placeholder. Trademark search + final pick before any external use.
5. Defer until earlier items plateau: few-shot examples in the prompt; NinjaScript-fed authoritative price context (parked for v2); RAG over historical trades.

**Permanently ruled out on this hardware:** LoRA/QLoRA fine-tuning (no CUDA, integrated Intel Arc iGPU). Reconsider only if a discrete-GPU box becomes available.

---

## 13. History

Earlier macOS+Parallels build context lives in [`old project.md`](./old%20project.md). It's preserved for reference but is **not** the current plan. This file (`PROJECT.md`) is canonical for v1.

Key things from `old project.md` that are now obsolete:

- Mistral 7B as the LLM (text-only, replaced with Qwen2-VL).
- Five-module architecture (collapsed to three for v1).
- Scheduler-driven cadence (replaced with manual trigger).
- Dashboard module (deferred past v1).
- Stage-4 fine-tune in the maturity path (removed for hardware).

---

*End of project context.*
