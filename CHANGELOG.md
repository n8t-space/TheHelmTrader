# Changelog

All notable changes are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](VERSIONING.md).

## [2.0.0] - 2026-06-18

> 2nd major version. BREAKING on several settings-shape axes -- see
> [MIGRATION-2.0.0.md](MIGRATION-2.0.0.md). Build + tag `v2.0.0-beta.N` on
> `beta`, validate on Sim/Playback, then merge `beta` -> `main` and tag
> `v2.0.0`. The in-app updater tracks `origin/main` only; do NOT click it while
> the checkout is on `beta`.

### Added
- **ATM is now OPTIONAL on directional proposals (Item 1A).** New
  `auto_trader.require_atm_for_directional` toggle (default `False` = ATM
  optional). With ATM absent, the LLM's own numeric stop/target are trusted
  (validated side-of-entry, tick-snapped, RR recomputed; 1:2 tick fallback if
  invalid). Both prompts (`analyzer.txt`, `headless_analyzer.txt`) relaxed to
  parity, still enforcing >= 2:1.
- **ATM-less auto-execution OCO path (Item 1B).** `HelmAutoTrader.cs` places a
  bare managed LIMIT entry plus a StopMarket + Limit OCO bracket (same
  `fromEntrySignal`) for blank-ATM signals; the named-ATM `AtmStrategyCreate`
  path is unchanged. Queue payload now carries `stop`/`target`.
- **Per-account trading config (Item 3).** New top-level `account_configs` map
  keyed by NT account id (secret-free), rendered as a card on the Strategy tab
  for LIVE + EVAL accounts only. Holds friendly name, risk-per-trade
  (percent|price), max daily loss, max concurrent/instrument, max
  contracts/instrument, stop-if-below, and a user-entered trailing-DD limit.
  Live cash + server-computed trailing-DD high-water-mark readout via
  `GET /api/account-configs/live`.
- **Per-trade risk sizing.** ATM-less order qty is computed from the per-account
  config (% of live cash | fixed $) using `instruments.json` tick_value, clamped
  to the per-account contract cap; falls back to `proposal.qty` ->
  `auto_trader.default_qty` (new) -> 1.
- **User-configurable news sources (Item 7).** New `news.sources` list
  ({name, url, type[xml|scrape|ai-extract], enabled}) with per-source parsing
  adapters; editable on the News tab. Seeded from the legacy booleans.

### Changed
- **Auto-Trader enforces per-account guardrails (Item 4).** Per-account values
  override the matching global `auto_trader` fields via `effective_guardrails`;
  the global fields remain the default and the SOLE source for Sim accounts.
  `max_concurrent_per_instrument > 1` allows up to N open trades on an instrument
  (default 1 preserves today's lock).
- **Merged the Auto-Trader + Automation settings tabs (Item 6)** into one
  "Auto-Trader & Automation" tab; both feature sets intact, no settings-shape
  change. Global Auto-Trader limits relabeled as "defaults".

### Removed
- **Old manual drawdown tracker (Item 5).** Deleted `DrawdownConfig`,
  `accounts.drawdowns`, `drawdown.py` (+ its `/api/drawdown/accounts` route),
  the Accounts-tab Drawdown block, and the Home/Trade-Performance `DrawdownsCard`.
  Replaced by Item 3's single user-entered trailing-DD limit with
  server-computed HWM tracking. A stale `drawdowns` key in an existing
  settings.json loads without error and is dropped on next save.

### Migration notes
- Every new field has a Pydantic default; a missing/old settings.json loads
  unchanged except the intentionally-dropped `accounts.drawdowns`.
- `news.forexfactory_enabled` / `econoday_enabled` stay READABLE through 2.0.x
  for rollback (UI writes only `sources`); dropped in a later minor.
- Trailing-DD intent is NOT lost: it moved to
  `account_configs[id].trailing_dd_limit` (system-tracked HWM), so old
  multi-field drawdown configs are NOT field-migrated.

## [1.1.4] - 2026-06-16

### Changed
- **HelmFeed `IsSuspendedWhileInactive = false`** so an armed chart keeps feeding
  bars/ticks + emitting context while its tab is in the background (true
  suspended background tabs -- the cause of "one instrument fed, the others
  didn't"). Requires an F5 recompile.

### Security
- **Purged the leaked broker account IDs from all git history** via
  `git filter-repo` + force-push (HEAD was already clean as of 1.1.3). A fresh
  clone of origin now contains zero account IDs across all commits. Note: old
  unreachable commit SHAs may linger in GitHub's cache until their GC.

## [1.1.3] - 2026-06-16

### Added
- **AI-config disclaimer on Signal Analysis.** A banner reads `/api/health/bot-stats`
  and warns (red) when no AI provider is configured -- "no new signals will be
  generated until you configure it" -- with a link to Settings -> AI Backend;
  a subtle note when AI is configured. Surfaces the silent no-signals state.

### Security
- **Scrubbed broker account IDs from `MIGRATION.md` at HEAD** (`<redacted-acct>`).
  Keys/secrets were never tracked (they live in `~/.helm/credentials.json`,
  outside the repo); this removes the remaining plaintext account IDs from the
  public tree. Full git-history rewrite of those IDs still pending.

## [1.1.2] - 2026-06-16

### Changed
- **Friendly account names across all of Trade Performance.** Extended the
  `accountLabel` lookup beyond the Tax + Drawdown cards to the trades table,
  fills table, and the account filter checkboxes -- every account rendering on
  the page now shows the friendly name (falling back to the raw ID).

## [1.1.1] - 2026-06-16

### Added
- **Friendly account names.** Settings -> Accounts has a name field per account
  (ID -> display name); the Estimated Tax and Account Drawdowns cards show the
  name, falling back to the raw ID. Display-only (`Accounts.names`).

### Changed
- **Analyzer prompt** (`prompts/analyzer.txt`) revised; dropped the unused
  `range_top`/`range_bottom` output fields. Read per call -- no restart.

### Fixed
- **Recorder garbage-expiry guard.** A rolled contract that came in from NT8
  with a bogus expiry (e.g. `199211`) rendered "MCL NOV92". `expiry_to_contract`
  now rejects implausible years and falls back to the bare master symbol;
  existing mislabeled rows cleaned. P&L was never affected (keys on master
  symbol). Requires a restart of the standalone recorder process.

## [1.1.0] - 2026-06-15

Promoted to production from beta (beta.1 + beta.2). Manual context path validated
in Playback; the live auto-path NS context surfaces on the next live bar.

### Added
- **Estimated tax per account** (IRC Section 1256 60/40). New `GET /api/tax-estimate`
  computes per-account tax on realized P&L for the current calendar year: each
  account taxed on its own positive net, the total netted across accounts (losses
  offset gains, as on one Form 6781). Rates configurable in **Settings -> Tax**
  (default 20% LT / 37% ST / 0% state = 26.8% blended). Surfaced as a card on the
  Trade Performance page. Estimate only -- excludes year-end mark-to-market of open
  positions and loss carrybacks; not tax advice.
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
