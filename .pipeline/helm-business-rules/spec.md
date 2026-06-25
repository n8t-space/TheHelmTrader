# Spec: helm-business-rules

Single source of truth for the pipeline. ASCII only. All paths are repo-relative to
`TheHelmTrader/` unless noted. Items are independently buildable by the coder unless a
cross-link says otherwise.

## Work items (this round)

ALL seven items are IN SCOPE and built this round, one pass. Item 2 is a framework whose
core dimensions are PROMOTED into Items 3/4/5 (built); its remaining dimensions stay a named
backlog (design only). Nothing in Items 1-7 is deferred.

1A. Remove the ATM requirement on directional proposals (Python + prompts).
1B. Build the ATM-less auto-execution path: bare LIMIT entry + separate stop/target OCO
    (NinjaScript). Quantity now comes from per-account risk sizing (Item 3), NOT a static
    `default_qty`.
2.  Business-grade trade rules. Core dimensions are PROMOTED to this round (per-trade risk
    sizing, per-instrument allocation caps, drawdown governance tier 1 -> Items 3/4/5). The
    remaining dimensions stay a named backlog (design only, not deferred items 1-7).
3.  Strategy tab redesign: remove the ATM-strategies list; add per-account custom configs
    for LIVE + EVAL accounts ONLY (friendly name, live cash readout, USER-ENTERED trailing-DD
    limit with high-water-mark tracking, risk-per-trade, max daily loss, max
    concurrent/instrument, max contracts/instrument, stop-if-below). Sim accounts get NO
    config card and fall back to the GLOBAL default config.
4.  Auto-Trader enforces the SAME per-account config (Item 3). Per-account overrides the
    global guardrails; global becomes the default fallback -- and is the ONLY source for Sim
    accounts (Auto-Trader is Sim-only in v1, and Sim has no card).
5.  Accounts tab: remove the OLD MANUAL multi-field drawdown-tracking feature
    (`DrawdownConfig` with starting_balance/trailing_drawdown/daily_drawdown/profit_target,
    `accounts.drawdowns`, `drawdown.py`, the Accounts-tab block, the Home `DrawdownsCard`/
    `DrawdownRow`). It is REPLACED by Item 3's single user-entered trailing-DD limit +
    high-water-mark tracking on `AccountConfig` -- NOT by a passive "live readout".
6.  Merge the Auto-Trader and Automation settings tabs into one tab/section set.
7.  News: user-configurable additional sources (name, url, type, enabled) with per-source
    parsing adapters.

> Architecture note (verified): there is NO separate "Strategy page", "Auto-Trader page",
> or "Automation page" route. They are TABS inside `dashboard/web/src/pages/SettingsPage.tsx`
> (`Tab` union at `:21`, tab bar at `:127-139`). "Strategy page" == the `strategy` tab;
> "Auto-Trader page" == the `autotrader` tab; "Automation page" == the `automation` tab.
> Auto-Analysis config is a CARD on the Home page (`HomePage.tsx:55` -> `AutoAnalysisCard`),
> NOT the "Automation" tab. Requirement D (merge Auto-Trader + Automation) therefore merges
> the two SETTINGS TABS, not the Home Auto-Analysis card. The only top-level routes are in
> `App.tsx:93-102` (Home / Performance / Signals / Health / Settings / Support).

---

## Version target & migration

- **Target: `2.0.0` on the `beta` branch.** The Helm's 2nd major version. BREAKING on
  several settings-shape axes (each item below names its field changes). Per the project
  versioning policy (`TradingBot/CLAUDE.md` "Versioning & releases", `VERSIONING.md`):
  build + tag `v2.0.0-beta.N` on `beta`, validate on Sim/Playback, then merge `beta` ->
  `main` and tag `v2.0.0`. The in-app updater tracks `origin/main` only; do NOT click it
  while the checkout is on `beta`.

- **Breaking settings-shape changes in 2.0.0 (write into `CHANGELOG.md` + `MIGRATION.md`
  at the cut):**
  1. New `auto_trader.require_atm_for_directional` (bool, default `False`). (Item 1A)
  2. New per-account config map `account_configs` keyed by NT account id, holding the
     friendly name + user-entered limits INCLUDING the new user-entered trailing-DD limit
     (Item 3). Stored in `settings.json` (secret-free; ids already live in `credentials.json`
     under `accounts`). NEW top-level section.
  3. `auto_trader` global guardrails (`max_contracts_per_order`, `max_concurrent`,
     `daily_loss_cutoff`, `min_account_balance`) become DEFAULTS; per-account values in
     `account_configs` override them when present (Item 4). The global fields REMAIN in the
     schema (back-compat + fallback); they are not deleted. They are also the SOLE source for
     Sim accounts (no per-account card for Sim).
  4. `accounts.drawdowns` (the `dict[str, DrawdownConfig]` manual tracker) is REMOVED, along
     with `DrawdownConfig`. (Item 5) Existing `settings.json` files carrying `drawdowns`
     must load without error -- Pydantic ignores unknown keys by default, so a stale
     `drawdowns` key is dropped silently on the next save. Call this out so a fresh load
     dropping the old card is expected behavior, not a regression. NOTE: the manual trailing-
     DD intent is NOT lost -- it moves to the single `account_configs[id].trailing_dd_limit`
     field (Item 3), with the system computing the high-water mark instead of the user.
  5. `news.sources` (list of source configs) ADDED; the legacy `forexfactory_enabled` /
     `econoday_enabled` booleans are migrated into two default `sources` entries (Item 7).
     The two booleans stay READABLE for the whole 2.0.x line (rollback) and are dropped in a
     later minor; Item 7 specifies the path.
- **Migration mechanics:** every new field gets a Pydantic default so a missing/old
  `settings.json` loads unchanged except where a section is intentionally dropped (4 above).
  Provide a one-shot `_migrate_*` helper in `settings.py` mirroring the existing
  `_migrate_credentials` pattern (`settings.py:270-292`) for (3)->per-account seeding and
  (5)->news.sources seeding. Bump `VERSION` to `2.0.0`; conventional commit `feat!:`.
- **Default risk posture stays conservative:** ATM-optional default OFF on enforcement
  (`require_atm_for_directional=False` means ATM is OPTIONAL); per-account configs absent ->
  fall back to global guardrails; risk sizing absent -> qty defaults to 1 (Item 3 sizing
  cascade bottoms out at 1, never an unbounded size).

---

## Gate 1 decisions (locked)

- **D1 = Option A (confirmed).** ATM is OPTIONAL. When no template is named, trust the LLM's
  explicit numeric `stop`/`target` (validate: numeric, correct side of `entry` for the
  direction, snap to tick, recompute RR via `_compute_rr`, keep the 2:1 floor; fall back to
  the existing 1:2 tick default + log if invalid). Reversible `require_atm_for_directional`
  toggle, default `False` (ATM-optional).
- **D2 = BUILD THE OCO PATH NOW.** The Auto-Trader MUST auto-execute an ATM-less directional
  proposal via a bare LIMIT entry plus a separate stop-loss and take-profit forming an OCO
  bracket in `HelmAutoTrader.cs` (Item 1B).
- **D3 = Per-account config supersedes global guardrails (NEW, Gate 1 round 2).** The
  Strategy tab gains per-account custom configs (Item 3); the Auto-Trader reads them
  (Item 4). Per-account values OVERRIDE the matching global `auto_trader` fields; the global
  fields become the default for accounts without an entry.
- **D4 = ATM-less quantity comes from RISK SIZING (NEW, supersedes the prior
  `default_qty=1` cascade).** "Risk per trade (% of account | price)" supplies the contract
  count for ATM-less trades (Item 3 formula). This PROMOTES Item-2 dimension 2 (per-trade
  risk sizing) from backlog to this round. `default_qty` is demoted to the final fallback
  only (see Item 1B sizing).
- **D5 = USER-ENTERED trailing-DD limit + HIGH-WATER-MARK tracking replaces the OLD MANUAL
  multi-field tracker (UPDATED Gate 1 final fold).** The Strategy-tab per-account config gets
  a SINGLE user-entered trailing max-drawdown limit (e.g. $2,500). The system tracks it
  against the account's equity HIGH-WATER MARK, computed from the live `NetLiquidation` cash
  channel already reported by the NS strategy. A breach forces the Auto-Trader OFF for that
  account (same passive fail-safe as stop-if-below; NO auto-flatten). The OLD manual feature
  (`DrawdownConfig` starting_balance/trailing_drawdown/daily_drawdown/profit_target,
  `accounts.drawdowns`, `drawdown.py`, the cards) is removed in Item 5. This is NOT a passive
  "not reported" readout -- the user enters the limit and the server enforces it.
- **D6 = Config scope = LIVE + EVAL accounts only (NEW Gate 1 final fold).** Per-account
  config cards render ONLY for accounts in the LIVE and EVAL visibility buckets. Sim accounts
  get NO card. Because Auto-Trader is Sim-only in v1, a Sim trade falls back to the GLOBAL
  default config (the legacy `auto_trader` global fields) for every guardrail and for risk
  sizing inputs. KNOWN SEAM to revisit when live trading is enabled.

---

## Item 1A: Remove the ATM requirement (Python + prompts)

### Goal

A directional (long/short) proposal must no longer be auto-dismissed solely because
`atm_strategy` is blank. ATM becomes optional, reversible via one guarded toggle.

### Current state (verified)

ATM is mandatory through a chain of enforcement points:

1. **Sanity gate (hard reject).** `app/src/proposal_sanity.py:50-51`:
   ```python
   if not str(proposal.get("atm_strategy") or "").strip():
       return False, "directional proposal has no ATM strategy"
   ```
   A `False` return makes the headless analyzer auto-dismiss (soft-delete) the record.
2. **Auto-dismiss consumers.** `app/src/headless_analyzer.py` calls `sanity_check` on the
   vision path (~line 261) and the text path (~line 318); a failed check sets the dismiss
   reason before `signal_storage.append_signal`.
3. **Stop/target derivation.** `app/src/local_llm_analyzer.py:395-496` (`_derive_stop_target`).
   - LLM picks a template; stop/target DERIVE from the template ticks vs `entry`/`direction`
     (`:494-496`). The LLM's own stop/target are advisory and overwritten.
   - Unknown / wrong-instrument picks CLEAR `atm_strategy` (`:451-461`) -> trips the gate.
   - A missing `atm_strategy` already has a soft 1:2 fallback (10/20 ticks) at `:421-426`,
     currently unreachable because the gate rejects blank-ATM first.
4. **Prompt enforcement.** `app/prompts/analyzer.txt:13`, `:38`, schema at `:51`; mirror
   language in `app/prompts/headless_analyzer.txt`. Read per-call (no restart).
5. **Arm / exec path (ATM-shaped sizing + placement).** Detailed in Item 1B current-state.

### Proposed change

Introduce `auto_trader.require_atm_for_directional` (bool, default `False` per D1) in
`Trade_Perf/dashboard/api/settings.py` (the `AutoTrader` model, near `:165-181`) and a
matching `runtime_config.py` knob. Gate every enforcement point on it.

1. **proposal_sanity.py:50-51** -- only reject blank ATM when the flag is on:
   ```python
   if require_atm and not str(proposal.get("atm_strategy") or "").strip():
       return False, "directional proposal has no ATM strategy"
   ```
   `sanity_check` reads the flag (inject via arg or read `runtime_config`). Keep the
   price-drift validation unchanged for ATM-less proposals.
2. **local_llm_analyzer.py `_derive_stop_target`** -- when ATM is absent AND the flag is
   off, do NOT clear/derive from a template. Use the LLM `stop`/`target` directly (validate
   numeric + correct side of `entry`; snap to tick; on invalid, fall back to the 1:2 tick
   default + log). Set `atm_strategy_resolved=False`, `atm_brackets=[]`, `atm_total_qty=1`.
   Leave the template path untouched when an ATM IS named.
3. **Prompts** -- relax mandatory-template language in `analyzer.txt` (`:13`, `:38`, `:51`)
   and `headless_analyzer.txt`: ATM template OPTIONAL; if none chosen the LLM must emit
   numeric `stop`/`target` honoring `>= 2:1`. Schema: `atm_strategy` may be `""` for a
   directional trade when explicit stop/target are provided. Keep both paths at parity.
4. **Auto-Trader (Python)** -- ATM-less directional proposals are now OFFERED and ARMABLE
   (no longer skipped). See Items 1B + 3/4 for the queue payload + sizing changes.

### Affected files

- `app/src/proposal_sanity.py`
- `app/src/local_llm_analyzer.py`
- `app/prompts/analyzer.txt`, `app/prompts/headless_analyzer.txt`
- `app/src/runtime_config.py`
- `Trade_Perf/dashboard/api/settings.py`
- `TradingBot/CLAUDE.md` ("Auto-analysis rules" line that states ATM is mandatory)
- Tests: `app/tests/` sanity/analyzer tests

### Acceptance criteria

- [ ] With `require_atm_for_directional=False`, a long/short proposal with blank
      `atm_strategy` + valid numeric stop/target passes `sanity_check` -> persisted, NOT
      auto-dismissed.
- [ ] The same proposal still FAILS `sanity_check` on price drift > 5% (existing check
      intact for ATM-less proposals).
- [ ] With the flag `True`, blank-ATM directional proposals are rejected exactly as today
      (fully reversible).
- [ ] `_derive_stop_target` on an ATM-less directional proposal keeps the LLM stop/target
      (snapped, correct side of entry), sets `atm_strategy_resolved=False`,
      `atm_brackets=[]`, `atm_total_qty=1`; `_compute_rr` recomputes RR.
- [ ] A named valid ATM template still derives stop/target from ticks (no regression).
- [ ] Prompts no longer say "rejected"/"required" for a missing template but still enforce
      `>= 2:1` and require explicit stop/target when none chosen.
- [ ] `TradingBot/CLAUDE.md` "Auto-analysis rules" updated to ATM-optional.
- [ ] No secrets touched; settings.json stays secret-free; default ATM-optional.

### Risks

- Outcome watcher / auditor assume `stop`/`target` exist; Option A keeps them populated --
  verify on an ATM-less sample.
- LLM stop/target no longer template-validated; mitigate with side-of-entry + numeric
  validation + `_compute_rr` floor logging.
- Two prompt paths must stay at parity (project rule). Edit both.
- Flag must read live (per-call) like other runtime knobs; no restart.

---

## Item 1B: ATM-less auto-execution -- bare LIMIT entry + OCO stop/target (NinjaScript)

### Goal

Auto-execute an ATM-less directional proposal: bare LIMIT entry, then on fill a stop-loss
and a take-profit forming an OCO pair. Engaged ONLY when the queued signal has no
`atm_strategy`; the ATM-template path is preserved with zero regression.

### Current state (verified)

- `HelmAutoTrader.cs:180` sets `IsUnmanaged = false` (MANAGED mode). Current placement is
  via `AtmStrategyCreate` (`:420-422`), independent of the managed order layer.
- `Place(...)` (`:382-444`) hard-rejects an empty `it.AtmStrategy` (`:405-411`).
- `Tracked` POCO (`:97-107`) keys off `AtmId` (== `exec_tag`) and `OrderId`
  (`exec_tag + "-E"`); `MonitorTracked` (`:447-504`) uses ATM getters.
- `QueueItem` (`:111-122`) has no stop/target fields; `ParseQueue` (`:612-637`) reads
  `entry/limit_price/atm_strategy/qty/expires_at`.
- Threading rule (`:27-32`, `:310-311`): the poll timer is a worker thread doing HTTP only;
  ALL order/strategy interaction is marshalled onto the strategy thread via
  `TriggerCustomEvent(o => ProcessOnStrategyThread(...))`. The OCO path MUST obey this.
- Server queue payload (`auto_trader.py:485-497`) sends `atm_strategy` + `qty`; no
  stop/target. `_template_qty` (`:120-128`) reads `atm_total_qty`/`position_size`, default 1.

### Placement primitive decision (locked)

The strategy is MANAGED. Use the **managed entry/exit methods** (not unmanaged):

- **Entry:** `EnterLongLimit(qty, limitPrice, signalName)` /
  `EnterShortLimit(qty, limitPrice, signalName)`, `signalName = it.ExecTag + "-OCOE"`
  (distinct from the ATM `orderId`/`atmId` namespace).
- **OCO stop + target (after entry fills):** `ExitLongStopMarket(qty, stopPrice,
  fromEntrySignal)` + `ExitLongLimit(qty, targetPrice, fromEntrySignal)` for a long (mirror
  with `ExitShort*` for a short), where `fromEntrySignal` == the entry's `signalName`. NT8
  auto-OCOs a stop+target pair sharing the same `fromEntrySignal` and auto-resizes to the
  live position. Stop is StopMarket (guaranteed exit); target is Limit.

NOTE for the coder: confirm the `Enter*Limit` / `Exit*StopMarket` / `Exit*Limit` overloads
carry the `signalName`/`fromEntrySignal` args, and do NOT use `SetStopLoss`/`SetProfitTarget`
(they fire on EVERY entry, including ATM-path entries). Keep `IsUnmanaged = false`.

### Quantity for an ATM-less trade (UPDATED -- now risk-sized, cross-link Item 3)

> SUPERSEDES the prior `proposal.qty -> default_qty -> 1` static cascade.

The contract count for an ATM-less trade is computed by the per-account RISK SIZING model
in Item 3, server-side in `auto_trader.py` at queue-build time, using the proposal's
`stop_distance` and `instruments.json` `tick_value`. The cascade is:

1. **Risk sizing (primary).** If a per-account config (Item 3) exists for the locked
   account AND the proposal carries a numeric `stop` (so a stop distance is known), compute
   contracts via the Item-3 formula (`% of account` or `price` mode). Clamp to the
   per-account "max contracts per instrument". (For a Sim account there is no per-account
   card per D6, so risk sizing draws on the GLOBAL default config instead.)
2. **Explicit `proposal.qty`/`position_size`** if the pipeline set one and no per-account
   config applies.
3. **`auto_trader.default_qty`** (new Pydantic field, default `1`) -- global fallback.
4. **Hard default `1`.**

The resolved qty then passes the HARD CEILING gate: per-account "max contracts per
instrument" if set, else global `max_contracts_per_order`. An over-ceiling resolved qty is
REFUSED at arm and not offered (existing gate at `auto_trader.py:207-213`, `:437`). On the
NS side, `ProcessOnStrategyThread:369-376` already gates `it.Qty > MaxContractsPerOrder`.

`_template_qty` is renamed in intent to "resolved order qty": for ATM signals it keeps
reading `atm_total_qty`; for ATM-less it runs the cascade above.

### Server-side changes (auto_trader.py)

- Add `stop` and `target` to the queue payload (`:485-497`) from `proposal.get("stop")` /
  `proposal.get("target")` (present per Item 1A). ATM signals may omit/ignore them.
- Resolved-qty cascade above (calls into the Item-3 sizing helper) feeds the `qty` field.
- No change to the arm/disarm/exec lifecycle routes; the OCO path reuses the same `state`
  transitions and `exec_tag`.

### NinjaScript changes (HelmAutoTrader.cs)

1. **QueueItem + ParseQueue:** add `double Stop;` `double Target;`; parse `stop`/`target` in
   `ParseQueue` (`:623-634`).
2. **Tracked:** add `bool IsOco;`, `string EntrySignal;`, `double Stop;`, `double Target;`;
   reuse `Qty`. Keep `AtmId`/`OrderId` for the ATM path.
3. **Place(...) branch (`:382`):** if `string.IsNullOrEmpty(it.AtmStrategy)` -> OCO branch
   instead of the `:405-411` reject. DryRun logs "WOULD place OCO: <entry> + stop <s> +
   target <t>" and reports `working`. Live OCO entry: `EnterLongLimit`/`EnterShortLimit`
   with resolved qty, `it.LimitPrice`, `EntrySignal = it.ExecTag + "-OCOE"`. Record a
   `Tracked` with `IsOco=true`. Report `working`.
4. **Fill -> place OCO exits:** detect the entry fill in `OnExecutionUpdate` keyed on
   `execution.Order.Name == EntrySignal`, then place stop + target exits bound to
   `EntrySignal` on the strategy thread. Report `filled` with `avgFillPrice` + qty (same
   PostExec shape as the ATM path `:465`).
5. **OCO resolution:** when stop OR target fills, the managed layer auto-cancels the
   sibling. Detect Flat -> tally managed realized P&L into `sessionRealized`, stop tracking.
   Terminal state resolves via the outcome watcher / auditor (real fills).
6. **Entry-window / assessment expiry:** cancel an unfilled OCO LIMIT entry like the ATM
   path (`:484-493`) via `CancelOrder(entryOrder)` (cache the `Order` returned by
   `Enter*Limit`). Report `cancelled` with the same `why` notes.
7. **Partial fill:** place OCO exits for the FILLED qty only; managed exits auto-resize to
   the live position. Verify in Sim/Playback. Report `filled` with cumulative filled qty.
8. **Flatten-on-disarm / master-OFF / loss-cutoff:** do NOT auto-flatten an open OCO; its
   stop/target stay live (mirrors the ATM path). Document like `report_balance`
   (`auto_trader.py:303-304`).
9. **Thread-safety:** all OCO placement/cancel/exit on the strategy thread via the existing
   `TriggerCustomEvent` marshalling (`:310-311`). `OnExecutionUpdate`/`OnOrderUpdate` fire
   on the strategy thread.

### exec.state / exec_tag mapping (OCO path)

Identical contract to the ATM path: `armed` -> `working` (entry placed) -> `filled` (entry
fill) -> NS stops tracking on stop/target fill; outcome watcher + auditor resolve final
P&L. Unfilled entry expiry/cancel -> `cancelled` -> server `no_fill` +
`entry_triggered=False`. `exec_tag` is the dashboard linkage key; `EntrySignal = exec_tag +
"-OCOE"` is the NS-internal managed signal name.

### Outcome watcher / fill linker impact (verified)

Both key off REAL fills, reading `signal.stop`/`signal.target` (populated by Item 1A) and
`trades.db` fills. They should work UNCHANGED for the OCO path. VERIFY on a Sim OCO
round-trip: the auditor links OCO entry+exit fills to the signal via `exec.account` +
price/time and realized P&L matches. Note any gap rather than assuming.

### Keep the ATM template path intact

- OCO branch reached ONLY when `it.AtmStrategy` is empty. Non-empty -> `AtmStrategyCreate`
  (`:420-422`) + existing ATM `MonitorTracked`, unchanged.
- Do NOT introduce `SetStopLoss`/`SetProfitTarget`. `Tracked.IsOco` selects monitor logic so
  ATM and OCO trackeds coexist.

### Affected files

- `TradingBot/ninjascript/_Helm Locker/HelmAutoTrader.cs` (copy to
  `bin/Custom/Strategies/_Helm Locker/` per the two-copy gotcha)
- `Trade_Perf/dashboard/api/auto_trader.py` (queue `stop`/`target`; risk-sized resolved qty;
  ceiling gate)
- `Trade_Perf/dashboard/api/settings.py` (new `auto_trader.default_qty`, default 1)
- `app/src/runtime_config.py` (matching `default_qty` knob if read bot-side)
- Tests: `Trade_Perf/tests/test_auto_trader.py` (queue payload + sizing cascade + gate)

### Acceptance criteria

- [ ] Queue payload for an ATM-less directional signal includes numeric `stop`, `target`,
      `qty` (risk-sized per Item 3); ATM signals unchanged.
- [ ] Resolved qty for ATM-less follows the cascade: Item-3 risk sizing -> `proposal.qty` ->
      `default_qty` -> `1`; an over-ceiling resolved qty is REFUSED at arm and not offered.
- [ ] In DryRun, an ATM-less armed signal logs "WOULD place OCO ..." and reports `working`.
- [ ] Live (Sim): ATM-less armed signal places a bare LIMIT entry; on fill places a stop
      (StopMarket) + target (Limit) OCO pair; one filling cancels the other.
- [ ] Unfilled OCO entry past the window / assessment-expiry is cancelled -> server
      `no_fill` + `entry_triggered=False`.
- [ ] Partial fill: OCO exits placed for filled qty and auto-resize; position always covered
      by a protective stop.
- [ ] A named-ATM signal in the same session still executes via `AtmStrategyCreate`
      unchanged (both tracked types coexist).
- [ ] exec.state transitions + `exec_tag` linkage match the ATM path; dashboard renders the
      OCO signal identically.
- [ ] Outcome watcher resolves the OCO trade from `signal.stop`/`signal.target`; auditor
      reconciles P&L from real `trades.db` fills -- verified on one Sim round-trip.
- [ ] All OCO order calls run on the strategy thread.

### Risks

- Managed entry + managed OCO exits is standard NT8, but partial-fill auto-resize and
  OCO auto-cancel MUST be VERIFIED in Sim/Playback before merge. Highest-risk item.
- Do NOT mix `SetStopLoss`/`SetProfitTarget` with the per-entry `Exit*` approach.
- `IsUnmanaged` stays `false`.
- Cache the `Order` returned by `Enter*Limit` for a reliable cancel handle.
- Two-copy gotcha: edit project file, COPY to `bin/Custom/Strategies/_Helm Locker/`, F5.
- Loss-cutoff/disarm leaves an open OCO holding its own stop (by design).

---

## Item 2: Business-grade trade rules (framework; core dimensions PROMOTED, rest backlog)

### Status

The CORE dimensions are PROMOTED to this round and BUILT in Items 3/4/5 (per-trade risk
sizing, per-instrument allocation caps, drawdown governance tier 1 -- the user-entered
trailing-DD limit + max-daily-loss + balance-floor). The REMAINING dimensions stay DESIGN /
RESEARCH backlog for later sessions; do NOT author those rules now. This Item delivers a
FRAMEWORK: dimensions the cohort fills in, each a task with a clear "done looks like". The
backlog remainder being deferred does NOT make Items 1-7 deferred -- Items 1-7 all ship.

### Promotions this round (cross-links)

- **Dimension 2 (per-trade risk sizing) -> PROMOTED to Item 3.** The Strategy-tab
  "Risk per trade (% of account | price)" makes sizing concrete and feeds the ATM-less OCO
  quantity (Item 1B). Built this round.
- **Dimension 3 (capital-allocation discipline) -> PARTIALLY PROMOTED to Item 3.** The
  per-account "max concurrent per instrument" + "max contracts per instrument" are the
  first concrete allocation caps. The broader instrument/strategy allocation TABLE stays
  backlog.
- **Dimension 4 (drawdown governance) -> PROMOTED to Items 3 + 5.** "Max daily loss" +
  "stop if balance below" + the USER-ENTERED trailing-DD limit with high-water-mark tracking
  (Item 3) are the first tier and ship this round, REPLACING the old manual tracker (Item 5).
  The tiered drawdown LADDER (reduce-size -> pause -> halt) stays backlog.

### Current-state inputs to read first (each later session)

- `TheHelmTrader/Documentation/the-helm-operating-plan.html` -- current-state operating plan.
- Current rules live in: `app/prompts/analyzer.txt`, `app/src/proposal_sanity.py`, and the
  settings schema `Trade_Perf/dashboard/api/settings.py`. As of this round the quantified
  guardrails are BOTH global (`auto_trader` fields) AND per-account (`account_configs`,
  Item 3).

### Retail-flavored gaps (file:line evidence; what remains after this round)

- **Edge / expectancy undocumented.** Nothing records win rate, avg R, or expectancy; RR is
  enforced per-trade (>= 2:1, `analyzer.txt`) but never aggregated.
- **Drawdown is single-tier, not a ladder.** After Items 3/5, daily-loss + balance-floor +
  the user-entered trailing-DD limit are per account, but there's still no graduated
  reduce/pause/halt ladder (`auto_trader.py:299-318` is a single hard kill-switch).
- **Capital allocation is per-instrument caps only.** Item 3 adds per-account per-instrument
  caps; there's no cross-instrument capital-allocation table or reserve model.
- **Research and execution are blended** in the prompt; no separation of research artifacts
  from live execution policy.
- **No reporting cadence / governance** beyond the live dashboard.

### Rule-definition dimensions (remaining backlog tasks)

> Format: dimension -- one-line intent -- acceptance ("done looks like").

1. **Treasury / risk-budget framing** -- total trading capital, reserve, risk budget the
   system draws against (not flat dollar floors). Done: a written capital model + the
   settings fields it maps to, with how the balance floor + per-account daily-loss are
   superseded by a budget framework.
2. **(PROMOTED -> Item 3) Per-trade risk sizing.** Backlog remainder: fixed-fractional R
   that scales the risk budget over time (vs the static per-account % set in Item 3), and
   position-as-inventory carrying-cost framing. Done: a sizing formula that scales with a
   rolling risk budget + an acceptance test that sizing tracks account growth.
3. **(PARTIAL -> Item 3) Capital-allocation discipline.** Backlog remainder: cross-
   instrument/strategy allocation table + per-bucket caps + reserve. Done: an allocation
   table (instrument/strategy -> max capital / max concurrent) extending the per-account
   caps from Item 3.
4. **(PROMOTED -> Items 3/5) Drawdown governance.** Backlog remainder: the tiered ladder
   (reduce size -> pause -> halt) above this round's single-tier cutoff. Done: a ladder
   (thresholds -> actions) + which settings/automation enforce each tier, building on the
   user-entered trailing-DD limit + high-water-mark tracking from Item 3.
5. **Edge / expectancy documentation** -- living doc of hypothesis, sample, win rate, avg R,
   expectancy, review trigger. Done: a template + the data source (`signals.jsonl` /
   `trades.db` / auditor) + a "minimum sample before trusting" rule.
6. **Separation of research vs execution** -- split research artifacts from live execution
   policy. Done: a defined boundary (which files/prompts are "execution policy" vs
   "research") + a change-control note.
7. **Entry / invalidation policy as written rules** -- promote in-prompt entry/stand-aside
   logic into a reviewable policy doc the prompt references. Done: a policy doc enumerating
   valid setups, mandatory confluences, stand-aside conditions, with the prompt pointing at it.
8. **Reporting cadence & governance** -- what gets reviewed, how often, by what artifact,
   who signs off. Done: a cadence table (report -> frequency -> source -> decision).
9. **Trade journal / decision log** -- rationale + outcome per trade beyond auto-resolved
   P&L. Done: the fields to log + where (extend signal record vs separate store) + how it
   feeds dimension 5.
10. **Compliance / kill-switch policy** -- formalize the equity-floor + trailing-DD ->
    master-OFF fail-safe into a written operating-limits policy with manual re-enable
    governance. Done: a doc mapping each guardrail (per-account daily-loss, balance-floor,
    trailing-DD limit, blackout) to trigger, action, re-arm authority.

### Acceptance criteria (planning round)

- [ ] The 10 dimensions exist as discrete tasks, each with intent + "done".
- [ ] Promotions (2, 3, 4) are cross-linked to Items 3/5 with the backlog remainder named.
- [ ] Each dimension references the current-state artifact(s) it reconciles with.
- [ ] Retail-flavored gaps are named with file:line evidence (the ones remaining after this
      round).
- [ ] No backlog rules are authored this round.

---

## Item 3: Strategy tab redesign -- remove ATM list, add per-account custom configs

### Goal

Replace the read-only ATM-strategies list on the Strategy tab with per-account custom
configurations for LIVE + EVAL accounts only: a friendly name, a USER-ENTERED base cash that
the server ADJUSTS by realized trades into a computed current cash, a USER-ENTERED trailing-DD
limit tracked against a server-computed high-water mark, and the risk limits per account.
This is the single source of truth the Auto-Trader enforces (Item 4) and the source of
ATM-less quantity sizing (Item 1B). Sim accounts get NO card and fall back to the GLOBAL
default config (D6).

> CASH SOURCE UPDATE (post-Gate-2 fix): the config card's cash is NO LONGER the broker
> NetLiquidation pull (`report_balance` -> `_last_balance`), which was wrong/empty whenever no
> Auto-Trader strategy was running on the account. It is now a USER-ENTERED `base_cash`
> ("cash now") stamped with a `cash_basis_ts` on save, plus the cumulative realized P&L of
> that account's trades that closed AFTER the basis (sourced from Trade Performance /
> trades.db via the existing `/api/trades` round-trip aggregation -- not re-implemented).
> Risk sizing, the trailing-DD high-water mark, and the balance floor ALL read this computed
> current cash. NetLiquidation reporting is retained only for back-compat on
> `GET /api/auto-trader/account`; it is no longer the cash source.

### Current state (verified)

- **Strategy tab** = `SettingsPage.tsx` `StrategyTab` (`:969-1015`): three numeric knobs
  (reconciliation_cap, retention_days, stale_bar_seconds) backed by `Strategy`
  (`settings.py:112-115`), THEN `<ExistingStrategiesBlock />` (`:1012`).
- **ATM list to remove** = `ExistingStrategiesBlock` (`:1116-1218`) + its helpers
  `parseStrategyName` (`:1032`), `describeStrategy` (`:1053`), `describeBracketBehavior`
  (`:1096`), `bracketHasManagement` (`:1092`), `STYLE_BLURBS` (`:1046`), the
  `ParsedStrategyName` interface (`:1019`). It calls `GET /api/atm-strategies`
  (`api.ts` `AtmStrategiesResp`, `:601-607`; backend `atm_strategies.py`).
- **What depends on `/api/atm-strategies`:** only `ExistingStrategiesBlock`. (Verified:
  the only frontend match for `atm-strategies` is `SettingsPage.tsx`.) The ATM TEMPLATES
  themselves are still used end-to-end by the ATM execution path (proposals reference them,
  `HelmAutoTrader.cs` `AtmStrategyCreate`); only the read-only LIST UI is removed. Keep the
  `atm_strategies.py` router (Item 1B's ATM path still works); just stop rendering the list.
- **Live equity channel (reuse for "current cash" + HWM):** `HelmAutoTrader.cs:288` reads
  `Account.Get(AccountItem.NetLiquidation, ...)` and POSTs to `/api/auto-trader/balance`
  (`auto_trader.py:299-318`), cached in `_last_balance` (`auto_trader.py:274`) and surfaced
  by `GET /api/auto-trader/account` (`auto_trader.py:277-291`, returns `balance` +
  `balance_at`). This is per the locked account only today.
- **No live trailing-DD channel exists today.** The OLD manual tracker required the user to
  type starting_balance/trailing_drawdown/etc. The NEW design keeps ONE user-entered limit
  and derives the high-water mark server-side from NetLiquidation (below).
- **tick_value source:** `app/src/instruments.py` `lookup_tick_size` (`:53`) +
  `lookup_point_value` (`:77`); `tick_value = tick_size * point_value`
  (`instruments.py:155`). Data in `app/instruments.json` (`instruments` = tick sizes,
  `point_values`).
- **Friendly names already exist** at the account level: `Accounts.names`
  (`settings.py:147`, `api.ts:526`) -- display-only. Item 3's per-account config gets its
  OWN friendly name field for the CONFIG (a config is 1:1 with an account in v1, so the
  coder MAY reuse `accounts.names` for display and add only a config-specific label if the
  user wants the config named separately from the account; default: the config's friendly
  name is its own field on `AccountConfig`, falling back to `accounts.names[id]` then id).
- **Account visibility buckets (for scoping cards to LIVE + EVAL):** the Accounts tab
  classifies accounts into visibility buckets (live / eval / sim). Render config cards only
  for the LIVE and EVAL buckets per D6. Confirm the bucket source the Accounts tab uses and
  reuse it.

### Data model (settings.py)

Add a new top-level section `account_configs: dict[str, AccountConfig]` on `Settings`
(`settings.py:249-259`), keyed by NT account id. Secret-free (ids live in
`credentials.json`; this map only holds limits + a label). New Pydantic model:

```python
class AccountConfig(BaseModel):
    # User-chosen label for this account's trading config. Display falls back to
    # accounts.names[id] then the raw id.
    name: str = Field(default="")
    # USER-ENTERED base cash ("cash now" as of cash_basis_ts). Current cash =
    # base_cash + realized P&L of trades closed AFTER cash_basis_ts.
    base_cash: float = Field(default=0.0, ge=0.0)
    # UTC ISO stamp of when base_cash was saved (server-managed, not user-edited).
    cash_basis_ts: str = Field(default="")
    # Risk per trade. interpretation: "percent" -> value is % of CURRENT cash
    # (base_cash + realized since basis); "price" -> a fixed risk amount in dollars.
    risk_per_trade_value: float = Field(default=0.0, ge=0.0)
    risk_per_trade_mode: str = Field(default="percent", pattern="^(percent|price)$")
    max_daily_loss: float = Field(default=0.0, ge=0.0)            # 0 => off
    max_concurrent_per_instrument: int = Field(default=1, ge=1, le=20)
    max_contracts_per_instrument: int = Field(default=1, ge=1, le=50)
    stop_if_balance_below: float = Field(default=0.0, ge=0.0)     # 0 => off
    # USER-ENTERED trailing max drawdown limit in account-currency dollars (e.g. 2500.0).
    # Tracked against a server-computed equity high-water mark. 0 => off.
    trailing_dd_limit: float = Field(default=0.0, ge=0.0)
```

- `base_cash` + `cash_basis_ts` ARE stored here (user enters base_cash; the server stamps
  cash_basis_ts on save). Current cash is COMPUTED from them + trades.db, not stored. The
  trailing-DD LIMIT IS user-entered and stored here; the HIGH-WATER MARK it is measured
  against is computed server-side from the current cash (not a settings field -- see below).
- The user-entered limits ARE stored here. They map onto / override the global
  `auto_trader` fields per Item 4.

### Computed current cash + trailing-DD high-water-mark tracking (D5, cash-source updated)

- **Current cash (COMPUTED, not pulled):** `current_cash(account)` = `base_cash` +
  cumulative realized P&L of that account's trades that closed AFTER `cash_basis_ts`.
  Realized P&L is sourced from Trade Performance via the existing `/api/trades` round-trip
  aggregation (`db.fetch_fills_for_derivation(account=[id])` + `trades.derive_trades`), NOT
  re-implemented. Filter rule: fetch ALL of the account's fills (a round-trip's ENTRY fill
  can predate the basis while its EXIT is after, and derive_trades needs both legs), then sum
  `net_pnl` over trades whose `exit_time >= cash_basis_ts` (lexical compare on UTC ISO-Z
  stamps; `cash_basis_ts` is stamped in the same UTC-Z format). Returns `None` when the
  account has no config OR no base_cash basis set (percent-mode sizing then falls back to the
  Item-1B cascade). With a basis but no trades since it, returns `base_cash` unchanged.
- **Read endpoint:** `GET /api/account-configs/live?account=<id>` ->
  `{account, cash, base_cash, cash_basis_ts, realized_since, high_water_mark,
  trailing_dd_used, trailing_dd_limit, dd_breached}`. `cash` is the computed current cash
  (`null` when no basis); `realized_since` is the adjustment so the UI can render the
  "= base + realized since <date>" hint.
- **High-water mark + trailing-DD enforcement (server-side):**
  1. The per-account equity HIGH-WATER MARK ratchets to the latest COMPUTED current cash
     (`_update_hwm`), called wherever cash is computed (the live readout + the queue/arm
     path), NOT on NetLiquidation reports. In-memory still acts as the monotonic ceiling
     within a process; a restart re-seeds it from the next read. This is now DERIVABLE from
     trades.db (base_cash + realized is durable), so the floor survives a restart.
  2. Compute `trailing_dd_used = hwm[account] - current_cash`. If `trailing_dd_limit > 0` and
     `trailing_dd_used >= trailing_dd_limit`, the account is BREACHED.
  3. **Breach action (same passive fail-safe as stop-if-below):** force the Auto-Trader OFF
     for that account -- stop offering/arming new signals on it. NO auto-flatten of open
     positions. Surfaced via `dd_breached=true`. Enforced in `report_balance` AND re-checked
     in `exec_queue` (`_enforce_fail_safe`), so it trips even with no NetLiquidation report.
  - The trailing-DD limit is a USER-ENTERED field on `AccountConfig`; the HWM and used-amount
    are SERVER-COMPUTED from the COMPUTED current cash. This is NOT a passive readout.
  - RISK/ASSUMPTION: current cash reflects realized (closed) trades only -- OPEN-position MTM
    is not counted (the broker NetLiquidation that included it is no longer the source). The
    trailing-DD figure therefore moves on trade closes, not continuously. Documented seam.

### API changes

- `Trade_Perf/dashboard/api/settings.py`: add `AccountConfig` (incl. `base_cash`,
  `cash_basis_ts`, `trailing_dd_limit`) + `account_configs` to `Settings`; include in GET/PUT
  (already generic); add `account_config(account_id) -> AccountConfig | None`. Stamp
  `cash_basis_ts` (UTC-Z) in the settings write path (`_stamp_cash_basis` in `_replace`)
  whenever `base_cash` changes, preserving the prior stamp when it doesn't.
- `Trade_Perf/dashboard/api/auto_trader.py`: add `current_cash(account)` (base_cash +
  `_realized_since` from trades.db) + `_update_hwm` (HWM ratchets to current cash) + the
  trailing-DD used/breach computation in `_trailing_dd_state`; `report_balance` keeps caching
  NetLiquidation in `_last_balance` (back-compat only) and re-checks the floor/DD on the
  COMPUTED cash via `_enforce_fail_safe`; `/api/account-configs/live` returns the computed
  cash + base_cash + realized_since + HWM + trailing-DD used/limit + breach.
- Sizing helper `_risk_sized_qty` in `auto_trader.py` computes contracts from a config +
  COMPUTED current cash + stop distance + tick_value (formula below), consumed by
  `exec_queue` and the arm gate.

### Risk sizing formula (the load-bearing math; cross-link Item 1B)

Inputs: `cfg` (AccountConfig; for a Sim account, the GLOBAL default config per D6), `cash`
(the COMPUTED current cash `current_cash(account)` = base_cash + realized since basis; NOT
NetLiquidation), `entry`, `stop`, instrument root. Derive
`tick_size`/`point_value`/`tick_value` from `instruments.py`.
`stop_distance_ticks = round(abs(entry - stop) / tick_size)`.

- **percent mode:** `risk_dollars = cash * (risk_per_trade_value / 100.0)`
- **price mode:** `risk_dollars = risk_per_trade_value` (already account-currency dollars)
- `per_contract_risk = stop_distance_ticks * tick_value`
- `contracts = floor(risk_dollars / per_contract_risk)` (guard `per_contract_risk > 0`)
- `contracts = clamp(contracts, 1, max_contracts_per_instrument)` (min 1 so a valid signal
  is never sized to 0; if the user wants no trade they disable the account/master switch)
- HARD CEILING: also clamp to the effective per-order ceiling (per-account
  `max_contracts_per_instrument`, else global `max_contracts_per_order`).
- Edge cases: missing `cash` (no base_cash basis set, incl. the global/Sim default config
  which has none) -> cannot size in percent mode -> fall back to the Item-1B cascade
  (`proposal.qty` -> `default_qty` -> 1) and LOG; price mode still works (no cash needed);
  missing/zero `stop_distance_ticks` -> same fallback + log; unknown instrument tick_value ->
  same.

### Frontend changes (SettingsPage.tsx Strategy tab)

- DELETE `ExistingStrategiesBlock` + the ATM-name parsing helpers + `AtmStrategiesResp`
  usage from the Strategy tab. (Leave `AtmStrategiesResp` in `api.ts` only if still used
  elsewhere -- verified it is NOT, so remove the interface too.)
- ADD a `PerAccountConfigBlock` rendering one card per LIVE + EVAL account (D6) drawn from
  the Accounts visibility buckets. Sim accounts are EXCLUDED (no card; they use the global
  default config at runtime). Each card:
  - Friendly name input (`name`), defaulting to `accounts.names[id]`.
  - Base cash INPUT (`base_cash`) + a COMPUTED current-cash readout (from the live endpoint;
    refetch on the 5s interval) with a "= base + realized since <date>" hint.
  - Trailing-DD readout: the user-entered LIMIT input + a server-computed "used / high-water
    mark" display (and a BREACHED badge when `dd_breached`).
  - Risk-per-trade: a numeric input + a `<select>` mode dropdown (`% of account` | `price`).
  - Max daily loss (number).
  - Max concurrent trades per instrument (number).
  - Max contracts per instrument (number).
  - Stop if balance below (number).
- Wire into `draft.account_configs` like other tabs (`setDraft({ ...draft,
  account_configs: ... })`).
- Add `AccountConfig` (incl. `base_cash`, `cash_basis_ts`, `trailing_dd_limit`) +
  `account_configs` to `api.ts` (`SettingsDoc`); add `AccountConfigLive` fields
  `base_cash`, `cash_basis_ts`, `realized_since`.
- Copy note: state plainly that the trailing-DD limit is user-entered and enforced against a
  high-water mark computed from each account's current cash (base cash + realized P&L from
  Trade Performance), so it works even when no strategy is running on the account.

### Affected files

- `Trade_Perf/dashboard/api/settings.py` (AccountConfig model w/ trailing_dd_limit +
  account_configs section + helper + migration seed)
- `Trade_Perf/dashboard/api/auto_trader.py` (live endpoint + `current_cash` /
  `_realized_since` from trades.db + HWM map derived from current cash + trailing-DD breach
  fail-safe + sizing helper; `_last_balance` retained for back-compat only)
- `Trade_Perf/dashboard/web/src/pages/SettingsPage.tsx` (remove ATM list block; add
  PerAccountConfigBlock scoped to LIVE + EVAL)
- `Trade_Perf/dashboard/web/src/api.ts` (AccountConfig type; remove AtmStrategiesResp if
  unused after removal)
- Tests: `Trade_Perf/tests/` for the sizing formula (both modes, clamps, fallbacks), the HWM
  + trailing-DD breach logic, and the live endpoint shape

### Acceptance criteria

- [ ] The Strategy tab no longer renders the "Existing ATM strategies (NT8)" list; no call
      to `/api/atm-strategies` from the Strategy tab.
- [ ] `settings.json` gains `account_configs` (secret-free, incl. `base_cash`,
      `cash_basis_ts`, `trailing_dd_limit`); GET/PUT round-trips it; a missing section loads
      as `{}` without error.
- [ ] On save, the server stamps `cash_basis_ts` (UTC-Z) whenever `base_cash` changes and
      preserves the prior stamp when it does not.
- [ ] Config cards render ONLY for LIVE + EVAL accounts; Sim accounts show NO card.
- [ ] Each visible (LIVE/EVAL) account shows a config card with friendly name + a base-cash
      INPUT + the editable limits (incl. trailing-DD limit) + the risk-mode dropdown.
- [ ] Computed current cash = base_cash + cumulative realized P&L of the account's trades
      that closed AFTER `cash_basis_ts` (from `/api/trades` aggregation); trades before the
      basis are NOT counted; no trades since basis -> current cash == base_cash; no basis ->
      cash is null. The card shows the current cash + a "= base + realized since <date>" hint.
- [ ] The server tracks a per-account equity high-water mark from the COMPUTED current cash
      and computes `trailing_dd_used = hwm - cash`; the card surfaces used + limit + a breach
      badge.
- [ ] When `trailing_dd_used >= trailing_dd_limit` (> 0), the Auto-Trader is forced OFF for
      that account (no new offers/arms) with NO auto-flatten -- same fail-safe as
      stop-if-below.
- [ ] Risk sizing (percent mode) uses the COMPUTED current cash, NOT NetLiquidation; both
      modes compute `floor(risk_dollars / (stop_distance_ticks * tick_value))`, clamped to
      max-contracts-per-instrument and the hard per-order ceiling; min 1.
- [ ] `stop_if_balance_below` compares against the COMPUTED current cash.
- [ ] Sizing falls back to the Item-1B cascade (+log) when cash (no basis), stop distance, or
      tick_value is missing; price mode still works with no cash.
- [ ] Sim / unconfigured sizing uses the GLOBAL default config (no base_cash) per D6 ->
      percent mode degrades to default_qty/price-mode (documented).
- [ ] Unit tests cover the computed-cash math (base + realized since basis; before-basis and
      wrong-account trades excluded; no-trades == base), risk sizing on computed cash, and a
      trailing-DD breach driven by current cash.

### Risks

- The HWM is in-memory runtime state; a uvicorn restart clears it and the next read re-seeds
  it from the running high of current cash. Because current cash itself is DERIVABLE from
  durable trades.db (base_cash + realized), the floor is reconstructable -- the only loss on
  restart is an intraday peak that has since drawn down. Acceptable v1. (Persist the HWM if
  stricter is wanted later.)
- Current cash reflects realized (closed) trades only; OPEN-position MTM is not counted, so
  the figure moves on trade closes, not continuously. State it in the UI copy.
- `cash_basis_ts` and trades.db `exit_time` must share the UTC-Z format for the lexical
  exit_time >= basis filter to be correct -- the basis is stamped in that format server-side.
- Sizing to 0 must be prevented (min 1) or a valid signal silently never trades.
- Sim / unconfigured accounts have no base_cash, so percent-mode sizing cannot run; they
  degrade to default_qty / price-mode and must still clamp to the global per-order ceiling.

---

## Item 4: Auto-Trader enforces the per-account config

### Goal

The Auto-Trader reads and enforces the SAME per-account config from Item 3 at every existing
enforcement point. Per-account values OVERRIDE the matching global `auto_trader` fields;
global remains the default for accounts without a config (D3). For SIM accounts there is no
per-account card (D6), so the GLOBAL default config is the sole source for every guardrail
AND for risk-sizing inputs.

### Current state (verified) -- global fields the per-account config supersedes

- `max_contracts_per_order` (`settings.py:165`) -- arm gate `auto_trader.py:207-213`, offer
  filter `:437`, NS gate `ProcessOnStrategyThread:369-376`.
- `max_concurrent` (`settings.py:166`) -- queue ceiling `auto_trader.py:403-406`.
- `daily_loss_cutoff` (`settings.py:170`) -- session-loss disarm (enforced NS-side; the
  server documents it; see `auto_trader.py` queue/disarm flow).
- `min_account_balance` (`settings.py:171-175`) -- balance floor in `report_balance`
  (`auto_trader.py:307-318`) + queue hold (`:347-349`).
- Sizing today: `_template_qty` (`auto_trader.py:120-128`) -- ATM template size, default 1.
- NEW this round: a per-account trailing-DD limit (Item 3) enforced via the server-computed
  HWM, with the same force-OFF fail-safe as the balance floor.

### Migration: override vs layer (RECOMMENDED -- per-account overrides global)

For an account WITH a config (LIVE/EVAL), resolve each guardrail as:
`account_configs[account].<field>` if set (> 0 / non-default), ELSE the global `auto_trader`
default. For a SIM account (no config per D6), use the global default for every field.
Mapping:

| Per-account (Item 3)             | Global default (supersedes / Sim source) | Enforcement point |
|----------------------------------|-------------------------------------------|-------------------|
| `risk_per_trade_*`               | (new sizing; replaces `_template_qty` for ATM-less; Sim uses global default cfg) | `exec_queue` qty + arm gate |
| `max_daily_loss`                 | `daily_loss_cutoff`                        | session-loss disarm (NS + server) |
| `max_concurrent_per_instrument`  | `max_concurrent`                          | `auto_trader.py:403-406` ceiling + per-instrument lock `:395-402` |
| `max_contracts_per_instrument`   | `max_contracts_per_order`                 | arm gate `:207-213`, offer filter `:437`, NS gate |
| `stop_if_balance_below`          | `min_account_balance`                     | `report_balance` floor `:307-318`, queue hold `:347-349` |
| `trailing_dd_limit`              | (no global equivalent; off when unset)    | HWM breach in `report_balance` -> force account OFF (Item 3) |

- Add a resolver helper, e.g. `effective_guardrails(account) -> ResolvedGuardrails`, in
  `auto_trader.py` (or `settings.py`), reading `account_config(account)` then falling back
  to `auto_trader_config()`. A Sim account (no config) resolves entirely to the global
  defaults. Every enforcement point calls the resolver instead of reading the global field
  directly.
- `max_concurrent_per_instrument`: note the existing semantics -- the server already locks
  ONE open trade per instrument (`auto_trader.py:395-402`) and uses `max_concurrent` as the
  cross-instrument ceiling (durable rule in `Trade_Perf/CLAUDE.md`). The new per-account
  "max concurrent per instrument" lets the user raise the per-instrument lock above 1 for
  that account; reconcile carefully: if > 1, the per-instrument open-trade lock must allow
  up to N open trades for that instrument (changes the `open_instruments` set logic from a
  boolean lock to a count). Keep default 1 = today's behavior.
- `trailing_dd_limit`: enforced via the Item-3 HWM computation in `report_balance`; on breach,
  treat the account like a balance-floor breach (force OFF, no auto-flatten). Sim has no
  trailing-DD limit (no card).

### Critical interlock with Item 1B (OCO quantity)

Risk-per-trade SUPPLIES THE QUANTITY for ATM-less trades (Item 1B sizing cascade + Item 3
formula), REPLACING the prior `default_qty=1` cascade. Keep `max_contracts_per_instrument`
(per account) / `max_contracts_per_order` (global; also the Sim ceiling) as the HARD CEILING
clamp. ATM-template trades keep their template-fixed size (`_template_qty` reads
`atm_total_qty`); only the qty CEILING gate is resolved per-account for them.

### Affected files

- `Trade_Perf/dashboard/api/auto_trader.py` (resolver helper; route every guardrail read
  through it; per-instrument count logic for `max_concurrent_per_instrument`; risk-sized qty;
  trailing-DD breach fail-safe)
- `Trade_Perf/dashboard/api/settings.py` (`account_config` helper from Item 3;
  `effective_guardrails` may live here)
- `TradingBot/ninjascript/_Helm Locker/HelmAutoTrader.cs` (NS reads its caps from the queue
  payload / `/api/auto-trader/account`; if per-account caps must reach NS, surface them on
  `GET /api/auto-trader/account` so the strategy's `MaxContractsPerOrder`/`MaxConcurrent`
  reflect the per-account values; two-copy gotcha)
- Tests: `Trade_Perf/tests/test_auto_trader.py` (override-vs-global resolution per field;
  Sim falls back to global; per-instrument count > 1; risk-sized qty + ceiling; trailing-DD
  breach forces OFF)

### Acceptance criteria

- [ ] With a per-account config set for a LIVE/EVAL account, the Auto-Trader uses the
      per-account `max_contracts_per_instrument`, `max_concurrent_per_instrument`,
      `max_daily_loss`, `stop_if_balance_below`, `trailing_dd_limit`, and risk sizing -- NOT
      the global values.
- [ ] With NO per-account config (incl. every Sim account per D6), behavior is byte-for-byte
      today's (global fields apply).
- [ ] ATM-less qty is risk-sized (Item 3) and clamped to the per-account contract ceiling
      (or the global ceiling on Sim).
- [ ] The balance floor / daily-loss / trailing-DD kill-switches trip on the per-account
      thresholds when set, else the global ones (trailing-DD has no global equivalent: off
      when unset / on Sim).
- [ ] `max_concurrent_per_instrument > 1` allows up to N open trades for that instrument on
      that account; default 1 preserves today's one-per-instrument lock.
- [ ] No autonomous-firing behavior changes; only the threshold SOURCE changes.

### Risks

- The per-instrument lock today is a boolean (`open_instruments` set membership). Allowing
  N > 1 changes it to a count -- careful with the dedup-per-bar guard (`:415-422`) so N
  concurrent trades on one instrument don't all come from the SAME bar.
- Global fields must remain valid defaults; do not delete them (breaks accounts without a
  config AND all Sim accounts).
- Surfacing per-account caps to the NS strategy needs the `/api/auto-trader/account`
  contract extended without breaking the strategy's existing parse.
- The Sim-uses-global fallback is a KNOWN SEAM (D6): when live trading is enabled, revisit
  whether Sim should carry its own config.

---

## Item 5: Accounts tab -- remove the OLD MANUAL drawdown tracking

### Goal

Remove the OLD MANUAL multi-field drawdown-tracking feature entirely. It is REPLACED by
Item 3's SINGLE user-entered trailing-DD limit + server-computed high-water-mark tracking on
`AccountConfig` (D5) -- NOT by a passive live readout. The two are distinct: old = a
multi-field user-typed config (`DrawdownConfig`: starting_balance, trailing_drawdown,
daily_drawdown, profit_target) + a derived-from-trades.db card; new = one user-entered
trailing-DD limit on the Strategy tab, enforced against an equity HWM the server computes.

### Current state (verified) -- every reference to remove

- **Settings schema:** `DrawdownConfig` model (`settings.py:118-126`) and
  `Accounts.drawdowns: dict[str, DrawdownConfig]` (`settings.py:139-143`).
- **Backend router:** `Trade_Perf/dashboard/api/drawdown.py` (entire file -- `_classify`,
  `_account_drawdown`, `GET /api/drawdown/accounts`). Registered in `main.py` (router
  include -- remove the import + `include_router`).
- **Frontend Accounts tab:** `DrawdownTrackingBlock` (`SettingsPage.tsx:1347-1469`) +
  its render at `:1339-1342`; `DrawdownConfig` import (`:13`).
- **Frontend Home card:** `DrawdownsCard` (`panels.tsx:344+`) + `DrawdownRow`
  (`panels.tsx:457+`); usage in `HomePage.tsx:12` (import) + `:57` (`<DrawdownsCard />`).
- **api.ts types:** `DrawdownConfig` (`:511-516`), `DrawdownState` (`:536-556`),
  `DrawdownResp` (`:558-563`), and `drawdowns` field on `SettingsAccounts` (`:525`).
- **CLAUDE.md references:** `Trade_Perf/CLAUDE.md` "Routers" entry for `drawdown.py`, the
  "User accounts" / Accounts-tab description mentioning the Drawdown tracker, and the
  `home.py`/Home-card mentions.

### Reconcile (do NOT remove what the new feature needs)

- The NEW trailing-DD control (Item 3 / D5) is a SEPARATE feature: a user-entered
  `trailing_dd_limit` on `AccountConfig`, enforced server-side via a HWM computed from the
  COMPUTED current cash (base_cash + realized P&L from trades.db) in `auto_trader.py`. It does
  NOT use `drawdown.py`, `DrawdownConfig`, or `accounts.drawdowns`. Removing all of the OLD
  references does not break Item 3 -- the trailing-DD intent simply moved to a single lean
  field with system-tracked HWM.
- `drawdown.py` consumes `trades.db` via `db.fetch_fills_for_derivation` +
  `trades.derive_trades`. Those modules are used elsewhere (Trade Performance) -- do NOT
  remove them, only the `drawdown.py` router and its registration.
- `accounts.names` (friendly names) STAYS -- it is independent of `drawdowns`.

### Proposed change

- Delete `DrawdownConfig` + `Accounts.drawdowns` from `settings.py`. A stale `drawdowns`
  key in an existing `settings.json` is ignored by Pydantic and dropped on next save
  (document in MIGRATION.md). Note in MIGRATION.md that trailing-DD now lives at
  `account_configs[id].trailing_dd_limit` (Item 3) and is system-tracked, so old per-account
  drawdown configs are NOT auto-migrated field-for-field (different shape; user re-enters one
  trailing-DD limit per LIVE/EVAL account).
- Delete `drawdown.py` and its `include_router` in `main.py`.
- Delete `DrawdownTrackingBlock` + its render + the `DrawdownConfig` import in
  `SettingsPage.tsx`.
- Delete `DrawdownsCard` + `DrawdownRow` from `panels.tsx` and the import/usage in
  `HomePage.tsx`.
- Delete `DrawdownConfig` / `DrawdownState` / `DrawdownResp` from `api.ts` and the
  `drawdowns` field from `SettingsAccounts`.
- Update `Trade_Perf/CLAUDE.md` (Routers + User accounts sections).

### Affected files

- `Trade_Perf/dashboard/api/settings.py`
- `Trade_Perf/dashboard/api/drawdown.py` (delete)
- `Trade_Perf/dashboard/api/main.py` (remove include)
- `Trade_Perf/dashboard/web/src/pages/SettingsPage.tsx`
- `Trade_Perf/dashboard/web/src/panels.tsx`
- `Trade_Perf/dashboard/web/src/pages/HomePage.tsx`
- `Trade_Perf/dashboard/web/src/api.ts`
- `Trade_Perf/CLAUDE.md`
- Tests: remove/adjust any drawdown tests

### Acceptance criteria

- [ ] No `DrawdownConfig` / `accounts.drawdowns` in the settings schema; a settings.json
      carrying a legacy `drawdowns` key loads without error and is dropped on save.
- [ ] `GET /api/drawdown/accounts` returns 404 (route removed); no `drawdown.py` import in
      `main.py`.
- [ ] The Accounts tab no longer shows the "Drawdown tracking" block; friendly names + the
      visibility radios remain intact.
- [ ] The Home page no longer renders the "Account drawdowns" card.
- [ ] No dangling `DrawdownConfig`/`DrawdownState`/`DrawdownResp` types or imports; the
      frontend builds clean (`npm run build`).
- [ ] The NEW per-account trailing-DD limit + HWM tracking (Item 3) works (separate feature);
      MIGRATION.md documents that old multi-field drawdown configs are not field-migrated.

### Risks

- `main.py` router-include order / other modules importing `drawdown` -- grep before delete.
- Frontend build fails on any leftover import; remove all references atomically.
- Don't accidentally remove `accounts.names` or the trades.db derivation helpers that
  `drawdown.py` happened to use.
- Communicate clearly that trailing-DD is NOT gone -- it moved to one lean user-entered field
  (Item 3) so users don't think the protection was dropped.

---

## Item 6: Merge the Auto-Trader and Automation settings tabs

### Goal

Combine the `autotrader` and `automation` settings tabs into ONE tab, keeping both feature
sets intact, so all execution + automation-pause controls live together.

### Current state (verified)

- Two tabs in `SettingsPage.tsx`: `'autotrader'` and `'automation'` in the `Tab` union
  (`:21`), the tab bar list (`:128`), the render switch (`:165-178`), and `tabLabel`
  (`:221-222`).
- `AutoTraderTab` (`:457-571`): master switch, locked account, max contracts/order, max
  concurrent, daily loss cutoff, stop-if-below, poll interval, entry window. Backed by
  `auto_trader` (`SettingsAutoTrader`).
- `AutomationTab` (`:274-325`): blackout-window editor. Backed by `automation`
  (`SettingsAutomation`, blackout_windows).
- (Auto-Analysis config is a Home-page CARD, `HomePage.tsx:124 AutoAnalysisCard`, NOT a tab
  -- out of scope for this merge; do not move it.)

### Proposed change

- Replace the two `Tab` entries with one, e.g. `'execution'` (label "Auto-Trader &
  Automation" or "Execution"). Keep `AutoTraderTab` + `AutomationTab` as COMPONENTS but
  render them stacked inside the single tab as two `<section>`s ("Auto-Trader" and
  "Automation / Blackout windows"). No prop-shape changes; both still bind to
  `draft.auto_trader` and `draft.automation` respectively.
- Update the `Tab` union, tab-bar array, render switch, and `tabLabel`.
- After Item 3 lands, note that per-account contract/concurrent caps live on the Strategy
  tab; relabel the global fields on the Auto-Trader section as "defaults" (cross-link Item 4)
  so the user understands per-account overrides AND that Sim accounts use these global
  defaults (D6).
- No backend change (these are pure frontend tabs over existing settings sections).

### Affected files

- `Trade_Perf/dashboard/web/src/pages/SettingsPage.tsx` (Tab union, tab bar, render switch,
  tabLabel; wrap the two components in one tab)

### Acceptance criteria

- [ ] The settings tab bar shows ONE combined tab instead of separate "Auto-Trader" and
      "Automation" tabs.
- [ ] Both feature sets render under it: all Auto-Trader fields AND the blackout-window
      editor, fully functional (save/round-trip unchanged).
- [ ] No settings-shape change; `auto_trader` and `automation` sections persist as before.
- [ ] The Home Auto-Analysis card is untouched.

### Risks

- The unsaved-settings dirty guard (`App.tsx:50-67`) keys on navigation, not tabs --
  unaffected, but verify dirty state still tracks edits in the merged tab.
- Don't drop the `automation` binding when collapsing the switch -- both sections must keep
  their own `onChange` into the correct draft slice.

---

## Item 7: News -- user-configurable additional sources

### Goal

Let the user ADD news sources beyond the built-in ForexFactory + Econoday: a configurable
list (name, url, type, enabled), with per-source parsing adapters. The two existing sources
stay as defaults.

### Current state (verified)

- **Settings schema:** `News` model (`settings.py:184-202`): `enabled`,
  `forexfactory_enabled`, `econoday_enabled`, `impact_filter`, `currency_filter`,
  `refresh_interval_minutes`. Two sources are HARD-CODED.
- **Backend router:** `news.py`. Hard-coded `FF_FEED_URL` (`:51`), `ECONODAY_URL` (`:52`);
  `_fetch_forexfactory` (`:123`, XML parse), `_fetch_econoday` (`:345`, scrape +
  `_ai_extract_econoday` `:202`). `_refresh_once` (`:450`) branches on
  `forexfactory_enabled` / `econoday_enabled`. `_dedupe` (`:358`), `_apply_filters`
  (`:388`). Events carry `source` + `sources` fields.
- **Frontend:** `NewsTab` (`SettingsPage.tsx:1471-1567`): two fixed checkboxes for FF +
  Econoday, impact/currency chips, refresh cadence. `SettingsNews` type (`api.ts:609-616`).
  The Home card is `NewsPanel.tsx` (renders `/api/news/today`; reads `sources` map for
  status). Backend `news_today` (`news.py:528`) returns a `sources` status map.

### Data model (settings.py)

Add a `sources: list[NewsSource]` to the `News` model. New Pydantic model:

```python
class NewsSource(BaseModel):
    name: str = Field(min_length=1)            # unique key + display label
    url: str = Field(default="")
    type: str = Field(pattern="^(xml|scrape|ai-extract)$")
    enabled: bool = True
```

- `type` semantics:
  - `xml` -> fetch + XML-parse adapter (the ForexFactory adapter, generalized: takes a URL,
    parses the FF schema). NOTE: a generic XML source needs a parse mapping; v1 supports the
    ForexFactory XML schema specifically (document that an arbitrary XML feed needs a
    matching adapter). Mark FF's default source `type="xml"`.
  - `scrape` -> fetch HTML then hand to the AI extractor (the Econoday path). Econoday's
    default source is `type="scrape"` (it scrapes THEN ai-extracts; "scrape" implies the AI
    step for HTML pages that aren't structured).
  - `ai-extract` -> same as scrape but explicitly for pages that REQUIRE AI extraction
    (alias; the coder may collapse scrape+ai-extract into one "scrape" path if cleaner --
    decide and document). Keep three values in the enum for forward flexibility but two
    code paths (structured-XML vs fetch-then-AI) are acceptable.
- Secret-free: URLs only, no keys (AI keys stay in `credentials.json` `ai_backend`).

### Migration (legacy booleans -> sources; one-version rollback CONFIRMED)

On load, if `news.sources` is empty/absent, seed it from the two booleans:
- `{name:"ForexFactory", url:FF_FEED_URL, type:"xml", enabled:forexfactory_enabled}`
- `{name:"Econoday", url:ECONODAY_URL, type:"scrape", enabled:econoday_enabled}`

CONFIRMED PATH (Gate 1 final fold): KEEP `forexfactory_enabled` / `econoday_enabled` in the
schema and READABLE for the WHOLE 2.0.x line. Behavior:
- 2.0.0 first load reads the old booleans to SEED `news.sources` (the migration helper).
- Throughout 2.0.x they remain readable so a rollback to a pre-2.0 build still finds them
  (the UI writes ONLY `sources` going forward; the booleans are not updated by the UI and are
  marked deprecated in a comment).
- DROP the two booleans in a LATER MINOR (post-2.0.x), once rollback is no longer a concern.
Provide `_migrate_news_sources` mirroring `_migrate_credentials`.

### Per-source parsing adapter design (news.py)

- Define an adapter dispatch: `fetch_source(src: NewsSource) -> tuple[list[event], err]`
  that branches on `src.type`:
  - `xml` -> `_fetch_xml(src.url)` (generalize `_fetch_forexfactory` to take a URL; keep the
    FF field mapping).
  - `scrape` / `ai-extract` -> `_fetch_scrape_ai(src.url)` (generalize `_fetch_econoday`:
    fetch HTML, call `_ai_extract_*` with a generic calendar prompt). The Econoday-specific
    prompt (`ECONODAY_PROMPT` `:170`) becomes a generic "extract economic-calendar events
    from this HTML" prompt parameterized by source name.
- `_refresh_once` (`:450`) iterates `cfg.sources` (enabled only) instead of the two
  hard-coded branches; each event tags `source = src.name`. The `sources` status map keys
  by `src.name` (so the Home card status reflects each configured source).
- `_dedupe` (`:358`) already keys on (hour, currency, title-prefix) -> works across N
  sources unchanged. `_ai_reachable` precheck still gates any `scrape`/`ai-extract` source.

### Frontend changes (NewsTab)

- Replace the two fixed checkboxes with an editable source LIST: rows of {name input, url
  input, type dropdown (xml | scrape | ai-extract), enabled checkbox, remove button} + an
  "Add source" button. Keep impact/currency chips + refresh cadence as-is.
- Default rows are the two seeded sources; the user can edit/disable/remove them or add new.
- Update `SettingsNews` in `api.ts`: add `sources: NewsSource[]` (+ `NewsSource` interface);
  keep the two booleans in the type (deprecated) for the 2.0.x rollback window.
- `NewsPanel.tsx` Home card: the `sources` status map is already generic (keyed by source
  name) -- verify it renders N sources, not just the two hard-coded names.

### Affected files

- `Trade_Perf/dashboard/api/settings.py` (NewsSource model + News.sources + migration seed;
  keep legacy booleans readable for 2.0.x)
- `Trade_Perf/dashboard/api/news.py` (adapter dispatch; generalize FF/Econoday fetchers;
  iterate sources in `_refresh_once`; generic AI prompt)
- `Trade_Perf/dashboard/web/src/pages/SettingsPage.tsx` (NewsTab source-list editor)
- `Trade_Perf/dashboard/web/src/api.ts` (NewsSource type; News.sources; keep deprecated
  booleans)
- `Trade_Perf/dashboard/web/src/NewsPanel.tsx` (verify N-source status rendering)
- `Trade_Perf/CLAUDE.md` (news.py Routers entry)
- Tests: `Trade_Perf/tests/` for the migration seed + the adapter dispatch (xml vs scrape)

### Acceptance criteria

- [ ] `news.sources` exists in the schema; an old settings.json with only the two booleans
      migrates to two seeded `sources` entries on load.
- [ ] The two legacy booleans remain readable through 2.0.x (rollback) and the UI writes only
      `sources`; they are marked deprecated and dropped in a later minor.
- [ ] The News tab lets the user add/edit/remove/disable sources (name, url, type, enabled);
      the two defaults appear pre-seeded.
- [ ] `_refresh_once` pulls every enabled source via the type adapter; an `xml` source
      XML-parses, a `scrape`/`ai-extract` source fetches HTML + AI-extracts.
- [ ] The Home News card status map shows one entry per configured source (ok/error/count).
- [ ] Disabling a source removes its events on the next refresh; merge/dedupe still works
      across N sources.
- [ ] No AI keys or secrets land in `settings.json` (URLs only).

### Risks

- A generic XML source other than ForexFactory needs a matching field mapping; v1 supports
  the FF XML schema -- document that arbitrary XML feeds may not parse without an adapter.
- Background refresh loop must not crash on one bad source (wrap per-source fetch in
  try/except; a failing source records an error in the status map and others still load --
  mirror the existing per-source error capture).
- AI cost: each `scrape`/`ai-extract` source is a full AI call per refresh; document the
  cost implication of adding many.
- Econoday's `str.replace`-not-`str.format` HTML-brace gotcha (`news.py:216-224`) must be
  preserved in the generalized scrape adapter.

---

## Out of scope (this round)

> Items 1-7 are ALL in scope and built this round. "Out of scope" below = backlog/design
> remainder ONLY; it does NOT defer any of Items 1-7.

- Writing the business RULES themselves (Item 2 backlog dimensions 1, 5-10; partials of
  2/3 remain backlog). Dimension 4 (drawdown) tier 1 IS built via Items 3/5.
- A tiered drawdown LADDER (reduce-size -> pause -> halt) -- only the single per-account
  daily-loss + balance-floor + user-entered trailing-DD limit (with HWM tracking) ship this
  round.
- Cross-instrument capital-allocation TABLE / reserve model -- only per-account
  per-instrument caps ship.
- Fixed-fractional R sizing that scales with a rolling risk budget -- this round's sizing is
  a static per-account % or fixed price-distance, not a growing-budget model.
- Per-account config cards for SIM accounts -- Sim uses the global default config (D6); a
  KNOWN SEAM to revisit when live trading is enabled.
- Durable persistence of the equity high-water mark -- v1 keeps it in-memory (re-seeds from
  the next computed-cash read after a restart; current cash itself is durable via trades.db).
- Switching the strategy to `IsUnmanaged = true` (OCO path is MANAGED mode).
- Moving the Home Auto-Analysis card into the merged settings tab (Item 6 merges only the
  two settings tabs).
- Native broker enforcement of a prop-firm trailing-DD figure -- this round enforces a
  USER-ENTERED trailing-DD limit against a server-computed HWM from the computed current cash
  (base_cash + realized P&L), not a broker-native trailing-DD item.
- Generic XML parsing for arbitrary (non-ForexFactory-schema) feeds (Item 7 v1).
- Dropping the legacy `forexfactory_enabled` / `econoday_enabled` booleans -- kept readable
  through 2.0.x for rollback; removed in a later minor.
- Cloud/SaaS endpoints for the BOT runtime (TradingBot project rule). NOTE: the Trade_Perf
  dashboard project permits cloud per its CLAUDE.md, but inference + the bot runtime stay
  local/LAN.

## Assumptions

- D1 = Option A: ATM optional, LLM supplies stop/target when absent; default
  `require_atm_for_directional=False`.
- D2 = BUILD the OCO path now (Item 1B), MANAGED mode, ATM-less only, ATM path preserved.
- D3 = Per-account config overrides global guardrails; global is the default fallback.
- D4 = ATM-less qty is RISK-SIZED (Item 3 formula) from the COMPUTED current cash (base_cash
  + realized since basis) + stop distance + `instruments.json` tick_value; falls back to
  `proposal.qty` -> `default_qty` -> 1 when an input is missing (percent mode needs a base_cash
  basis; price mode does not).
- D5 = A SINGLE user-entered trailing-DD limit on `AccountConfig`, tracked against a
  server-computed equity high-water mark from the COMPUTED current cash (base_cash + realized
  P&L; NOT NetLiquidation), REPLACES the old manual multi-field `accounts.drawdowns` tracker.
  Breach forces the Auto-Trader OFF for that account (no auto-flatten), same fail-safe as
  stop-if-below.
- D6 = Per-account config cards render for LIVE + EVAL accounts only; Sim accounts have no
  card and use the GLOBAL default config for guardrails + risk-sizing inputs (Auto-Trader is
  Sim-only in v1). KNOWN SEAM to revisit when live trading is enabled.
- All seven items (1A/1B, 2, 3, 4, 5, 6, 7) are IN SCOPE and built this round; Item 2's
  backlog remainder is design-only and does not defer any of Items 1-7.
- The "Strategy / Auto-Trader / Automation pages" are SETTINGS TABS, not routes; Item 6
  merges the autotrader + automation tabs.
- Version target = `2.0.0` on `beta`; validate on Sim/Playback before merge to main.
- Per-account config is 1:1 with an NT account id in v1 (one config per account).
- Account ids remain in `credentials.json` `accounts`; `account_configs` (limits + label +
  trailing-DD limit) and `news.sources` (URLs) stay secret-free in `settings.json`.
- Inference stays local/LAN per project CLAUDE.md; no endpoint changes.
- NT8 managed `Enter*Limit` + `Exit*StopMarket`/`Exit*Limit` auto-OCO + auto-resize holds
  as documented -- to be VERIFIED in Sim before main merge.
- The config card's current cash is COMPUTED (base_cash + realized P&L since basis from
  trades.db), NOT pulled from broker NetLiquidation; it works with no strategy running. The
  equity HWM is in-memory and re-seeds from the next computed-cash read after a restart, but
  current cash itself is durable via trades.db. `_last_balance` (NetLiquidation) is retained
  for back-compat on `GET /api/auto-trader/account` only.
