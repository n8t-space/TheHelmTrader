# Changelog

All notable changes are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](VERSIONING.md).

## [1.1.0-beta.2] - 2026-06-15 (beta)

### Added
- **Estimated tax per account** (IRC Section 1256 60/40). New `GET /api/tax-estimate`
  computes per-account tax on realized P&L for the current calendar year: each
  account taxed on its own positive net, the total netted across accounts (losses
  offset gains, as on one Form 6781). Rates configurable in **Settings -> Tax**
  (default 20% LT / 37% ST / 0% state = 26.8% blended). Surfaced as a card on the
  Trade Performance page. Estimate only -- excludes year-end mark-to-market of open
  positions and loss carrybacks; not tax advice.

## [1.1.0-beta.1] - 2026-06-05 (beta)

Beta channel -- not yet promoted to production. Pending live validation of the
auto-analysis context path (manual hotkey path validated in Playback).

### Added
- **Shared NinjaScript-context renderer** (`context_format.py`): the manual and
  auto paths now render the same authoritative-context block, including
  Smart-Money market structure (BOS/CHoCH), so the two flows can't drift.
- **Market structure in the prompt**: BOS/CHoCH lenses, previously computed and
  dropped, are now rendered into the analyzer prompt.
- **Auto path reads rich NS context**: `feed.py` stores the NS context per bar
  (`context_{i}_{p}.json`, bar_ts-keyed); the headless analyzer prefers it and
  tags `context_source`, falling back to the thin context when absent.

### Changed
- **HelmFeed + HelmAnalyzer merged into one indicator.** HelmFeed now publishes
  `bars + ticks + screenshot + rich context` on every realtime primary bar
  close and keeps the Ctrl+Shift+F manual-capture hotkey. The 4 HTF data series,
  the SMC structure engine, pivots, and session levels moved into HelmFeed.
  **HelmAnalyzer.cs is retired** -- re-apply HelmFeed to charts after compiling.
- **ADXR(14) replaces ATR(14)** in the chart context (trend strength; the ATM
  templates already own stop/target sizing). ATR retained in the text-only
  fallback where it sizes stops.
- **Bid/ask pinned to the primary series** in the NS context (fixes a stale /
  foreign-series quote that read ~165 pts off).

### Fixed
- **Duplicate-order safeguards** (the 14:00 MES double-submit):
  - Dispatch dedup in `feed.py` -- a re-sent/duplicate bar is stored but never
    re-analyzed (one analysis per bar).
  - Execution dedup in `auto_trader.py` -- at most one order per
    (instrument, bar), even if the first filled and closed fast.

### Migration
- Compile NinjaScript (F5). HelmAnalyzer is gone; re-apply **HelmFeed** to each
  charted instrument.
- `helm restart` for the Python changes.
- Decide `IsSuspendedWhileInactive` (HelmFeed) before relying on background
  charts feeding while inactive.

## [1.0.0] - baseline

The production baseline at the adoption of semantic versioning (commit
`8f67725`). Includes the data-integrity auditor, automation blackout windows,
fee-aware P&L, multi-contract/reversal trade resolution, per-instrument
concurrency, and the service-unreachable alert.
