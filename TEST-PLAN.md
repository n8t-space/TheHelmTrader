# The Helm - Test Plan

> Dev tooling. Lives in the repo, does NOT ship with the app. The runtime is the
> FastAPI service + built `web/dist` + NinjaScript; the `tests/`, `scripts/`, and
> this doc are excluded from any distributed build.

## Purpose

Confirm the app's trading-correctness and integrity behavior before every push,
so a regression in P&L math, trade derivation, or fill reconciliation cannot ship.

## Prerequisites

- Python 3.12+ with the backend deps and `pytest` / `pytest-asyncio` installed
- Node + npm for the frontend build
- Run from the repo root (`TheHelmTrader/`)

## Two tiers

| Tier | What | When | Speed |
|---|---|---|---|
| **Core** (default gate) | Deterministic unit tests + frontend build | Every push | ~3 s |
| **Integration** | Live `feed.db` warmup/arming + worker spawns (marked `integration`) | On demand / pre-release | minutes, timing-coupled |

The core is the push gate because a gate must be fast and reliable. The
integration tests touch live data and have real time waits, so they run
separately -- never silently block a push on a flaky timing test.

## How to run

```powershell
# Default gate: deterministic core + frontend build (~3 s)
powershell -File scripts/preflight.ps1

# Include the slow live-data integration tests (pre-release)
powershell -File scripts/preflight.ps1 -Full

# Backend core only (fast inner loop)
powershell -File scripts/preflight.ps1 -SkipBuild

# Raw pytest (each tree uses its own pytest.ini)
python -m pytest Trade_Perf/tests -m "not integration" -q
python -m pytest TradingBot/app/tests -q
python -m pytest Trade_Perf/tests -m integration -q     # just the slow ones
```

## Pre-push enforcement

`scripts/pre-push` shells out to `preflight.ps1`; a non-zero exit blocks the push.
Install it once per clone:

```powershell
pwsh -File scripts/install-hooks.ps1
```

Emergency override (use sparingly): `git push --no-verify`.

## Coverage

| Area | File | What it locks |
|---|---|---|
| Trade derivation | `Trade_Perf/tests/test_trades.py` | Short read from signed position (BuyToCover trap), scale-out exit legs, long-loss net incl. commissions, aggregate win/loss stats |
| Signal metrics | `TradingBot/app/tests/test_instruments_metrics.py` | Multi-bracket sizing from real legs, point value / tick lookup, **auditor override beats paper legs**, stored leg P&L trusted, tick snapping, flat = no P&L |
| Integrity auditor | `Trade_Perf/tests/test_auditor.py` | Mismatch corrected to broker net, in-sync left alone, **unlinked filled signal flagged not guessed**, unfilled skipped |
| Auto-analysis queue | `TradingBot/app/tests/test_auto_analyzer.py` | Lazy worker start, coalescing, run counters (order-independent) |
| Outcome resolver | `TradingBot/app/tests/test_outcome_resolver.py` | Tick-walk leg resolution |
| Feed store | `TradingBot/app/tests/test_feed_store.py` | Bar/tick persistence + prune |
| Routers (`integration`) | `Trade_Perf/tests/test_*_router.py` | Feed warmup/arm gate, auto-analysis API (excluded from the default gate) |

## Verification

A clean run ends with `PREFLIGHT PASSED` and exit code 0. The correctness core
(`test_trades`, `test_instruments_metrics`, `test_auditor`) is deterministic and
fast; treat any failure there as a real P&L bug, not flake.

## Troubleshooting

| Symptom | Cause -> Fix |
|---|---|
| `test_submit_starts_worker_lazily` fails when both trees run together | A prior router test left the analyzer worker running. The fixture now resets on setup + teardown; if it recurs, a new test started the worker without using `reset_analyzer`. |
| Integration run is slow / hangs | They exercise the live `feed.db` with real sleeps and worker threads; one open >10 min lets the auditor sweep fire mid-test. They are `integration`-marked and excluded from the default gate -- only `-Full` runs them. |
| Integration tests touch live data | They snapshot/restore `auto_analysis_config` and clean test bars. Run `-Full` when not mid-trade. |

## References

- Auditor engine: `Trade_Perf/dashboard/api/auditor.py`
- P&L metrics: `TradingBot/app/src/instruments.py`
- Trade derivation: `Trade_Perf/dashboard/api/trades.py`
