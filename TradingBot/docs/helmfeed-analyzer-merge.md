# HelmFeed + HelmAnalyzer Merge

## Purpose
Collapse the two NinjaScript indicators into one so the **live auto-analysis path gets the same rich market context (incl. BOS/CHoCH market structure) the manual hotkey path already gets** -- eliminating the three-way context drift between HelmAnalyzer.cs (rich, manual), pipeline.py (manual render), and headless `_build_context` (thin, auto).

## Background: why the gap exists
- **HelmFeed.cs** -- continuous publisher on every armed chart. Sends `bars + ticks + screenshot` only. No `AddDataSeries`, no indicators, no structure.
- **HelmAnalyzer.cs** -- hotkey-only. On keypress it computes the full context (EMA90, ADXR, Donchian, swings, pivots, market-structure at 3 retrace lenses) + screenshot and POSTs to `/api/capture-from-nt` (pipeline.py).
- The auto path therefore can't see structure: it runs off HelmFeed's bars and rebuilds a **thin** context in Python (`headless_analyzer._build_context`: price/high/low/EMA/ATR/ADXR). No structure, no pivots, no HTF.

## Chosen approach: M1 -- one indicator (HelmFeed absorbs HelmAnalyzer)
HelmFeed becomes the single chart indicator. On **primary-series bar close (Realtime)** it builds the rich context and attaches it to the bar POST. It also keeps the **hotkey** manual-capture path. HelmAnalyzer.cs is retired.

Rejected alternatives:
- **M2 (context-on-feed, keep HelmAnalyzer for hotkey):** duplicates the structure/context C# across two files -> reintroduces drift at the NS level.
- **M3 (shared AddOn class):** cleanest DRY in theory, but keeps two indicators the user must both apply; the user explicitly wants one.

### Load note
HelmAnalyzer already carries the 4 HTF `AddDataSeries` (30m/1h/daily/weekly). If a chart currently runs both indicators, merging is load-neutral (one set of HTF series instead of two indicators). Charts running only HelmFeed gain the HTF series. Heavy context is gated to **primary bar-close, armed, Realtime** -- same cadence as the existing auto dispatch. Ticks stay lightweight (no context).

## Data flow after merge
```
HelmFeed (one indicator/chart)
  - OnMarketData            -> /api/feed/ticks            (unchanged)
  - OnBarUpdate (primary)   -> /api/feed/bar  {ohlcv, screenshot_b64, context}   (NEW: context)
  - Hotkey (PreviewKeyDown) -> /api/capture-from-nt {context, image}             (preserved)

feed.py /api/feed/bar
  - store bar (feed.db)                                   (unchanged)
  - save screenshot auto_{i}_{p}.png                      (unchanged)
  - NEW: save context_{i}_{p}.json (latest per combo, bar_ts embedded)
  - dispatch auto-analysis                                (unchanged, deduped)

headless_analyzer.analyze()
  - NEW: read context_{i}_{p}.json; if bar_ts matches -> use NS rich context
  - else -> fall back to thin _build_context (back-compat w/ old HelmFeed)
  - render via shared formatter (structure included)
```

## Phasing (each phase independently shippable; rollback = restore the backup)
**Backup:** `C:\Users\pilot\Documents\Helm-Backups\2026-06-05_170923_pre-merge` (full source tree, pre-merge).

### Phase 1 -- Bot side, additive + backward-compatible (NO NS recompile)
- `feed.py`: `Bar.context: dict | None = None`; write `context_{i}_{p}.json` (with embedded `bar_ts`) right before dispatch when present + fresh.
- `headless_analyzer.py`: `_latest_auto_context(instrument, period, bar_ts)`; in `analyze()` prefer NS context, else thin `_build_context`. Visual + text blocks consume whichever.
- Shared renderer: one `format_context_block(ctx)` used by both pipeline.py (manual) and headless (auto) so **structure renders on both**.
- Safe with the CURRENT HelmFeed (no `context` field -> falls back). Tests cover both paths.

### Phase 2 -- NinjaScript merge (needs F5)
- Move `MarketStructureLens` + `StructureSwing` + the context builder (`BuildContextJson`, `AppendTimeframe`, `AppendPivots`, `AppendSessionLevels`, `AppendLens`, `AppendSwing`) into HelmFeed.cs.
- `Configure`: add the 4 HTF `AddDataSeries`. `DataLoaded`: init lenses. `Historical`: wire the hotkey (PreviewKeyDown, multi-tab guard).
- `OnBarUpdate` (BarsInProgress==0, Realtime): build context, attach to the bar JSON as `"context":{...}`.
- Hotkey handler: reuse the same builder -> POST `/api/capture-from-nt` (manual flow unchanged).
- Two-copy deploy: canonical `_Helm Locker/` -> `bin/Custom/Indicators/_Helm Locker/`. F5.

### Phase 3 -- Retire HelmAnalyzer
- Delete HelmAnalyzer.cs (canonical + deployed copy) once HelmFeed proves out in Sim/Playback.
- Remove the Python thin `_build_context` ADXR/ATR math only if we decide NS context is mandatory; keep as fallback otherwise.

### Phase 4 -- Validation
- Sim/Playback: confirm one indicator emits bars+ticks+context+screenshot; auto-signal records carry `market_context` with `market_structure`; manual hotkey still fires.
- Pre-push gate green; `helm restart` for the Python; F5 for the NS.

## Rollback
Restore from the backup dir above (robocopy back), F5, `helm restart`. Phase 1 is reversible by reverting the 3 Python files; Phase 2 by restoring HelmFeed.cs/HelmAnalyzer.cs from backup.
