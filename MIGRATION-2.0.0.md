# Migration to 2.0.0

> The Helm's 2nd major version. BREAKING on several settings-shape axes. All
> changes are backward-compatible on load (every new field has a Pydantic
> default) except one intentionally-dropped section. ASCII only.

## Release mechanics

- Build + tag `v2.0.0-beta.N` on `beta`. Validate on Sim/Playback.
- Merge `beta` -> `main`, then tag `v2.0.0`.
- The in-app updater tracks `origin/main` only; do NOT click it while the local
  checkout is on `beta` (it resets to `origin/main`).

## Settings-shape changes

1. **`auto_trader.require_atm_for_directional`** (bool, default `False`). ATM is
   OPTIONAL by default; set `True` to restore the legacy "blank ATM directional
   -> rejected" behavior. Read live per-call (no restart). (Item 1A)
2. **`auto_trader.default_qty`** (int, default `1`). Final fallback contract
   count for an ATM-less trade when risk sizing can't run and the proposal
   carried no explicit qty. (Item 1B)
3. **`account_configs`** (NEW top-level map, default `{}`) keyed by NT account
   id. Per-account trading config for LIVE + EVAL accounts: friendly name,
   risk-per-trade (value + mode percent|price), max_daily_loss,
   max_concurrent_per_instrument, max_contracts_per_instrument,
   stop_if_balance_below, trailing_dd_limit. Secret-free (ids stay in
   `credentials.json`). Per-account values override the matching global
   `auto_trader` guardrails; the global fields remain the default fallback and
   the SOLE source for Sim accounts (no card). (Items 3 + 4)
4. **`accounts.drawdowns` + `DrawdownConfig` REMOVED** (Item 5). A stale
   `drawdowns` key in an existing `settings.json` loads without error -- Pydantic
   ignores unknown keys -- and is dropped on the next save. A fresh load no
   longer rendering the old Drawdown card is EXPECTED, not a regression.
   - The manual trailing-DD intent is NOT lost: it moves to
     `account_configs[id].trailing_dd_limit`, a single user-entered limit the
     server enforces against an equity high-water mark it computes from the
     NetLiquidation cash channel. Old multi-field drawdown configs are NOT
     field-migrated (different shape); re-enter one trailing-DD limit per
     LIVE/EVAL account on the Strategy tab.
5. **`news.sources`** (NEW list, default seeded). Each `{name, url,
   type[xml|scrape|ai-extract], enabled}`. On first load `news.sources` seeds
   from the legacy `forexfactory_enabled` / `econoday_enabled` booleans (ForexFactory
   xml + Econoday scrape). The two booleans stay READABLE for the whole 2.0.x
   line (rollback); the UI writes only `sources` going forward; they are dropped
   in a later minor. (Item 7)

## Runtime state seams (in-memory, documented)

- The per-account equity **high-water mark** (`auto_trader._equity_hwm`) and the
  last-reported cash (`_last_balance`) are in-memory. A uvicorn restart clears
  them; the next NS balance report re-seeds. Trailing-DD is briefly
  under-counted after a restart, and live cash shows "not reported yet" until the
  strategy reports again. Acceptable v1.
- HWM updates only on NetLiquidation reports (the NS balance-report cadence), not
  a continuous stream -- enforcement granularity equals that cadence.

## NinjaScript

- `HelmAutoTrader.cs` gained the ATM-less OCO path. Two-copy gotcha: the project
  file was copied to `bin/Custom/Strategies/_Helm Locker/`; F5-recompile in the
  NS editor before running. `IsUnmanaged` stays `false`.
- VERIFY on Sim/Playback before merging to `main`: managed OCO auto-cancel of the
  sibling on fill, partial-fill auto-resize, and the auditor reconciling OCO
  entry+exit fills to the signal.
