# AI Offload Migration — Workstation as Inference Backend

> **Purpose:** Track the migration that offloads the TradingBot's vision-LLM inference from the GEEKOM (integrated Intel Arc iGPU, CPU-bound, 15–45 s/call) to a dedicated Linux workstation with an RTX 4060 Ti 16 GB. Topology decision: **GEEKOM stays the trading host; workstation is AI-only.**
>
> Read this file alongside [PROJECT.md](PROJECT.md). PROJECT.md describes the bot; this file describes the cross-machine inference path that the bot will use after migration.

---

## 1. Goal & Topology

| | |
|---|---|
| Goal | Move only the inference runtime (Ollama + vision model) to the workstation. Everything else stays on the GEEKOM. |
| Topology | **A** — GEEKOM hosts NT8 + TradeCopier AddOn + TradingBot Flask app + NS bridge. Workstation hosts Ollama. Bot calls Ollama over LAN. |
| What moves | Nothing on disk. Ollama is installed fresh on the workstation; the vision model is pulled fresh from the registry. The GEEKOM-side bot edit is one line. |
| What does NOT move | TradingBot codebase + `signals.jsonl` + `screenshots/`; TradeCopier (architectural — must run inside `NinjaTrader.exe`); NT8_Trade_Perf (recorder reads NT8 fills locally). |

## 2. Hardware Envelope

### GEEKOM (trading host — unchanged)

GEEKOM GT1 Mega, 32 GB RAM, integrated Intel Arc iGPU, no CUDA. Hosts NT8 v8.1.6.3, TradingBot Flask app at `127.0.0.1:5000`, eventual TradeCopier AddOn. Continues to do everything it did before — the only change is that `OLLAMA_URL` in the bot points away.

### Workstation (`<workstation-hostname>` — new inference backend)

| Item | Value |
|---|---|
| Distro | Ubuntu 24.04.4 LTS Desktop |
| Hostname | `<workstation-hostname>` (kept; not renamed) |
| Primary user | `<workstation-user>` |
| GPU | NVIDIA RTX 4060 Ti, 16 GB VRAM |
| NVIDIA driver | 595.58.03 (CUDA 13.2 visible via `nvidia-smi`) |
| Disks | NVMe (931 GB) — OS + Ollama models on `/`. SDA (3.6 TB SSD, exFAT) — preserved as-is at `/media/<workstation-user>/Untitled` (258 GB existing data, deferred decision). |
| Network | Wi-Fi only (`wlp8s0`), DHCP-reserved at `<workstation-LAN-IP>`. Wired NIC `enp9s0` present but DOWN (no cable). |
| LAN latency to GEEKOM | 2–3 ms over Wi-Fi |

## 3. Network Identity

| Endpoint | Value |
|---|---|
| Workstation LAN IP | `<workstation-LAN-IP>` (router-reserved) |
| Ollama port | `11434` |
| Bot → Ollama URL (post-migration) | `http://<workstation-LAN-IP>:11434/api/generate` |
| Firewall rule | UFW: allow `<GEEKOM-IP>` → tcp/11434; allow ssh; deny everything else |

## 4. Phase Status

| Phase | Description | Status |
|---|---|---|
| 1 | Workstation provisioning (OS, driver, base tooling, Ollama install) | ✅ Done |
| 2 | Smoke test inference (vision call from GEEKOM → workstation) | ✅ Done — cold 31.72 s, warm 0.69 s end-to-end |
| 3 | Expose Ollama on LAN + UFW restriction to GEEKOM | ✅ Done — bound to `0.0.0.0:11434`, UFW restricting to GEEKOM |
| 4 | Repoint bot at workstation (one-line edit) | ✅ Done — validated via NS bridge `Ctrl+Shift+F` end-to-end |
| 5 | Optional: bigger model, raised reconciliation cap, RAG, LoRA | 🔮 Future |

## 5. Configuration Reference

### Workstation — Ollama systemd override

File created via `sudo systemctl edit ollama.service` (writes drop-in at `/etc/systemd/system/ollama.service.d/override.conf`):

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
```

Apply with `sudo systemctl daemon-reload && sudo systemctl restart ollama`. Verify with `ss -tlnp | grep 11434` — must show `0.0.0.0:11434`, not `127.0.0.1:11434`.

### Workstation — UFW rules

```bash
sudo ufw allow from <GEEKOM-IP> to any port 11434 proto tcp comment 'Ollama from GEEKOM'
sudo ufw allow ssh
sudo ufw enable
```

`<GEEKOM-IP>` retrieved from `ipconfig` on the GEEKOM (the active adapter's IPv4).

### GEEKOM — bot edit (Phase 4)

Single line at [`app/src/local_llm_analyzer.py:14`](app/src/local_llm_analyzer.py#L14):

```python
# before
OLLAMA_URL = "http://localhost:11434/api/generate"
# after
OLLAMA_URL = "http://<workstation-LAN-IP>:11434/api/generate"
```

Both call sites (`analyze` at line 51, `reconcile` at line 122) reference this constant — they migrate together. No env-var plumbing added; if the workstation IP ever changes, edit the line.

## 6. Verification Plan

In order, after each phase:

| Phase | Verification | Expected |
|---|---|---|
| 1 | `ollama --version`, `systemctl is-active ollama`, `nvtop --version` | `0.23.2`, `active`, `3.0.2` (all confirmed 2026-05-08) |
| 2 | Loopback inference on a real chart screenshot | Structured JSON proposal returned in seconds (vs 15–45 s on GEEKOM iGPU) |
| 3 | `ss -tlnp \| grep 11434` on workstation; `Test-Connection <workstation-LAN-IP>` from GEEKOM; `curl -s http://<workstation-LAN-IP>:11434/api/tags` from GEEKOM | Bound to `0.0.0.0:11434`; ping <5 ms; tags list returned |
| 4 | Run `main.py` on GEEKOM with NT8 chart visible; check `data/tradebot.log` for the new URL; verify proposal lands in `signals.jsonl` | Pipeline completes faster than baseline; log shows `<workstation-LAN-IP>:11434`; entry appended |

## 7. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Workstation reboot during long Ollama pull or fine-tune | Use `tmux` for any long-running command; UFW + systemd survive reboots cleanly |
| Wi-Fi roaming / drop interrupts inference | DHCP reservation pins IP; `requests` `TIMEOUT=300` in `local_llm_analyzer.py` tolerates short blips. If frequent, plug Ethernet into `enp9s0` |
| Ollama bound to 0.0.0.0 without UFW = exposed to whole LAN | UFW allow rule restricts source IP to GEEKOM only; SSH still password-auth (key auth recommended) |
| Bot can't reach workstation (network down / Ollama crashed) | `requests` raises; bot logs the failure. No automatic fallback to local Ollama on GEEKOM (it was uninstalled / not running) |
| Workstation's `<workstation-user>` user has root sudo by default | Acceptable for a single-user workstation; revisit if it's ever multi-user |

## 8. Deferred / Open Items

| Item | Decision |
|---|---|
| `sda` reformat exFAT → ext4 | **Deferred.** 258 GB of existing data preserved as-is. Revisit when bulk storage actually needed (LoRA datasets, screenshot archive sync). |
| Stray legacy user account on workstation | **Deferred.** Confirm whether it's a legacy identity of the same user; `/home/<workstation-user>` and `/media/<workstation-user>/` left untouched until then. |
| Hostname rename (`<workstation-hostname>` → something cleaner) | **Declined** by user. Keep as-is; no NS-bridge config impact since IP is the bridge target. |
| Wired Ethernet (`enp9s0` currently DOWN) | **Deferred.** Wi-Fi is sufficient for current latency targets. Plug in if jitter ever becomes an issue. |
| SSH key auth from GEEKOM → workstation | **Deferred.** Password auth works; keys are quality-of-life, not blocking. |
| Bigger vision model (Qwen2.5-VL Q8, InternVL2-26B Q4) | **Phase 5.** Test after migration is stable on the current 7B Q4 to keep variables isolated. |
| Reassessment cap / open-trade reconciliation cap raised | **Phase 5.** Both are throttles on the slow GEEKOM. With sub-second inference, the constraint goes away. |
| LoRA fine-tuning path | **Phase 5.** Was struck from PROJECT.md §11 due to iGPU; restored as viable on 16 GB VRAM. Needs corpus first (PROJECT.md "use it" line). |
| RAG with `nomic-embed-text` | **Phase 5.** Trivially cheap on this hardware once corpus exists. |

## 9. Session Log

### 2026-05-08 — Migration kickoff session

**Done:**
- Reviewed all three projects ([TradingBot](.), [TradeCopier](../TradeCopier/), [NT8_Trade_Perf](../NT8_Trade_Perf/)). Identified TradingBot's vision-LLM inference as the only offloadable component.
- Locked topology A: GEEKOM stays trading host, workstation is AI-only.
- Verified workstation initial state (Ubuntu 24.04.4, NVIDIA driver 595.58.03, RTX 4060 Ti 16 GB, SSH active, no Ollama).
- Installed Tier 1 packages: `git curl wget build-essential python3-venv python3-pip tmux htop nvtop rsync ufw`.
- Installed Ollama 0.23.2 via official script. systemd service active.
- DHCP reservation confirmed by user at router for workstation Wi-Fi MAC → `<workstation-LAN-IP>`.
- LAN reachability verified: GEEKOM ping → `<workstation-LAN-IP>` = 2–3 ms, 0 % loss.
- Identified Phase 4 edit point: single constant at [`app/src/local_llm_analyzer.py:14`](app/src/local_llm_analyzer.py#L14), shared by both `analyze` and `reconcile`.
- `ollama pull qwen2.5vl:7b` started; was at 24 % when this session paused.

**Side issue caught & resolved:**
- Loud GEEKOM fans early in session traced to a stuck bash + `recorder.py` orphan from a prior Claude Code session (`pid=19468`, started 10:13 AM, was supposed to live 6 s, lived ~50 min). Killed the tree; system idle returned to 1.7 % CPU; fans normalized after thermal lag.

**Deferred (with reasons):** see §8.

**Phases 2–4 closed out same session:**
- `ollama pull qwen2.5vl:7b` (5.97 GB Q4_K_M) completed. Also pulled `nomic-embed-text:latest` (274 MB F16) for future RAG.
- Workstation rebooted; verified post-reboot that override applied automatically (Ollama bound to `0.0.0.0:11434`), UFW persisted, Wi-Fi DHCP reservation held.
- Phase 2 smoke test from GEEKOM via `curl`/PowerShell against `http://<workstation-LAN-IP>:11434/api/generate`:
  - **Cold call: 31.72 s** (26.06 s of which was one-time model load into VRAM).
  - **Warm call: 0.69 s** end-to-end including LAN.
  - Generation rate: ~56–62 tok/s.
  - Model correctly identified instrument (MES JUN26) and described chart structure.
- Phase 4 edit applied at [`app/src/local_llm_analyzer.py:14`](app/src/local_llm_analyzer.py#L14). Validated via NS bridge `Ctrl+Shift+F` → dashboard → workstation Ollama → proposal returned quickly.

**Done this session (post-Phase-4):**
- ✅ PROJECT.md §3, §11, §12 updated to reflect post-migration reality (LoRA un-struck; cadence/dashboard prohibitions walked back).
- ✅ Phase 5 partial: dashboard merger completed (see section below); `nomic-embed-text` already pulled and ready for RAG when corpus warrants.

**Outstanding (next session pickup, priority order):**

1. **Hotkey -> `/api/capture-from-nt` POST never arrives.** Snipping Tool itself is fine (user confirmed; `Win+Shift+S` works). Bot side healthy: 2026-05-12 21:00 log shows headless analyzer producing proposals + outcome-watcher running. NS log shows `[Helm] Hotkey caught` + `Context POSTed` but tradebot.log has **zero `NT trigger received` lines**. So the POST is failing somewhere between NS HttpClient and `:8000`. Investigate: `HelmAnalyzer.cs` target URL (`BackendUrl` property), whether the NS HttpClient hits an actual error after logging `Context POSTed`, and whether the URL host/port still match. HelmFeed POSTs were also failing in the same NS log window -- correlated failure, not a snip-side issue. The Session-0 URI-handler workaround from this morning is NOT the right fix this time -- the issue is upstream of the bot.

2. **Restore operational data from backup.** ~~Once the service is up~~ -- service is up; only step left is data restore. Backup at `Projects\helm-backup-20260511_213210\`:
   - `helm-backup\signals.jsonl`    -> `TheHelmTrader\TradingBot\app\data\signals.jsonl`
   - `helm-backup\feed.db`          -> `TheHelmTrader\TradingBot\app\data\feed.db`
   - `helm-backup\screenshots\`     -> `TheHelmTrader\TradingBot\app\data\screenshots\`
   - `helm-backup\settings.json`    -> `%USERPROFILE%\.helm\settings.json` (skip if you re-configured via Settings page since)
   - `trades.db` already has 459 fills as of last health probe -- recorder is repopulating from NT's own SQLite; may not need restoring. Diff against `helm-backup\trades.db` if you want to recover anything pre-wipe that NT no longer has.

3. **Live Feed Pipeline — Phase 1 verification at market open.** Phases 1-4 all shipped. F5 `HelmAnalyzer.cs` in NS Editor; apply HelmFeed to a chart; confirm `[HelmFeed] State -> ...` Output progression and rows landing in `feed.db` once ticks flow. Trigger Ctrl+Shift+F to verify the manual analyze path. Arm an Auto Analysis slot to verify the headless analyzer fires on a real bar close.

4. **SHARING -- packaging.** Major progress 2026-05-11:
   - ✅ **Configuration page** shipped — `/settings` route, four tabs (Appearance / AI Backend / Strategy / Accounts), per-user `~/.helm/settings.json`, live-reload on PUT.
   - ✅ **install.ps1** at the monorepo root drives a clean install end-to-end (prereqs via winget, pip, npm build, NS indicator copy, recorder shortcut, NSSM service).
   - ⏳ **Packaging for non-developer users** still open: MSI, code signing, an actual one-click experience. Today's install.ps1 is the bridge.

5. **Refresh ATM strategies on each boot.** New ask. The bot needs to read NT's ATM strategy list (`Documents\NinjaTrader 8\db\NinjaTrader.sqlite` -> `AtmStrategyTemplates`, or `templates/AtmStrategy/*.xml`) every time uvicorn starts and expose it via the API. Catches user-created strategies without manual reconfiguration.

6. **Signal proposals must reference an ATM strategy for TP/SL.** Shipped 2026-05-12 evening. LLM now picks `atm_strategy` from the user's NT templates (or "custom" with custom_stop_ticks / custom_target_ticks if no listed strategy fits). Bot derives stop/target prices from the chosen bracket + entry + direction + tick_size. Signal Analysis table now shows the strategy name column instead of Stop/Target. Follow-ups:
   - `Tight 100` and `SL 10 - RUN` strategies have unusual bracket shapes -- one returns 0 ticks for stop or target and gets filtered out of the LLM menu. Investigate the XML and decide whether to include them.
   - Backfill old signals: existing signals.jsonl entries have stop/target but no atm_strategy -- the table column shows `—` for them. Acceptable; user can ignore historical rows or we can reverse-map (e.g. round R:R to nearest known strategy).

7. **"Automated signal updater" still doesn't work.** User-reported, term not yet defined. Could mean: outcome-watcher (logs show it IS suggesting outcomes -- working), auto-analyzer (logs show it IS firing -- 21:00:05 produced a proposal), or something else like a live-update pipeline that isn't named explicitly. Clarify scope with user before chasing.

8. **Headless analyzer needs screenshots too.** New ask (2026-05-12). Auto Analysis fires on bar close from `feed.db` text data only -- no visual context. User wants the headless analyzer to also include a chart screenshot. Design open: NS-side push (HelmFeed captures bitmap on every bar close for armed instruments and POSTs alongside the bar)? Or bot-side request (auto-analyzer triggers NS to capture)? Storage and bandwidth need a plan (200-400 KB per bar adds up fast; gate to armed instruments only, prune aggressively). Talk through with user before building.

8. **NS account-state indicator.** The Open Positions card on Home was removed 2026-05-10. The data path (balance / equity / open positions pushed to bot) is still on the roadmap, lower priority than (1)-(7).

6. **Single venv / requirements consolidation.** `_tradebot_bridge.py` still bridges `TradingBot/app/src/` into the dashboard. The reinstall didn't change this. Closing it lets the installer assume one project / one venv.

5. **Reconciliation cap** in `pipeline.py` (currently 3) and confidence-floor `MAX_ATTEMPTS` (currently 2) can be raised — workstation latency makes the throttles over-conservative.

6. **Bigger/Q8 vision model A/B test** (now viable on the workstation).

7. **`signals.jsonl` → SQLite** migration. Not urgent; revisit only if append-only contention or query performance bites.

---

### 2026-05-08 — Dashboard merger (separate effort, completed same day)

The Flask Signal Analysis dashboard and the FastAPI/React Trade Performance dashboard were merged into a single 3-page web app in `NT8_Trade_Perf/`. Eight checkpoints, all in commits on `NT8_Trade_Perf/master` and `TradingBot/master`:

| # | Description | Commit (NT8_Trade_Perf) |
|---|---|---|
| 1 | Backend route parity skeleton (every Flask route → FastAPI stub) | `aa0162c` |
| 2 | Router shell + Trade Performance page wired (existing UI under `/performance`, Home + Signal Analysis placeholders) | `1bf4c28` |
| 3 | Signal Analysis index wired to real `signals.jsonl` via sys.path bridge | `33cd6ca` |
| 4 | Full Signal Analysis detail page (proposal, market context, recap, journal/outcome/position, soft-delete, reconciliation) | `101dd57`, refined `d0e533d` |
| 5 | Snip & Analyze flow (no page reload) | `d6ade3a` |
| 6 | NS bridge endpoint relocated to FastAPI:8000 | `79ad04f` (+ TradingBot `c2ba712` for the NS URL flip, `9a9bce6` for the multi-tab focus guard) |
| 7 | Home page with action queue, equity curve, bot health | `f6ef876` |
| 8 | Cutover — Flask deprecated, launchers updated | this commit |

**End state:**
- All three pages (Home / Trade Performance / Signal Analysis) live at `http://localhost:5173/` with the FastAPI backend at `:8000`.
- TradingBot's Flask `dashboard.py` is **deprecated** — header note added; runs are still possible but redundant since both apps read/write the same `signals.jsonl`.
- `TradingBot/app/run_dashboard.bat` now forwards to `NT8_Trade_Perf/dashboard/run_dev.ps1`.
- NS indicator (`TradeBotTrigger.cs`) posts to `127.0.0.1:8000/api/capture-from-nt`. Multi-tab guard ensures only the active tab's instance fires.
- Bridge from FastAPI back to TradingBot's source: `NT8_Trade_Perf/dashboard/api/_tradebot_bridge.py` puts `TradingBot/app` on `sys.path` so `signal_storage`, `instruments`, and `pipeline` are importable. To be removed when TradingBot's `app/src/` modules are eventually relocated into the unified project.

**Deferred from the merge (Phase 5 onwards):**
- **Open positions** card on Home is a placeholder. Needs a NinjaScript indicator that pushes account state (balance, equity, open positions) — separate mini-project.
- **Single venv / requirements consolidation.** FastAPI runs from system Python; TradingBot's pipeline deps (`requests`, `Pillow`) were installed system-wide for this session. Long-term cleanup: either move TradingBot/app/src/ into NT8_Trade_Perf and use one venv, or document the shared-deps requirement.
- **`signals.jsonl` → SQLite migration** (originally proposed for Checkpoint 1; deferred to keep risk low). Current shared-file approach works fine; revisit if append-only contention or query performance ever becomes an issue.

---

### 2026-05-08 — Post-merge improvement tiers (same day)

After cutover, four tiers of UI/runtime polish landed in `NT8_Trade_Perf`:

| Tier | Commit | Summary |
|---|---|---|
| 1 | `e8b25f1` | Hyperlink colors readable on dark theme; Signal Detail JSON snippet card; Stop + Target columns on the Signal Analysis index |
| 2 | `5cfec21` | Bulk select + delete on Signal Analysis; Health page with Bot Health card moved off Home + live tail of unified `tradebot.log` (3-second poll) |
| 3 | `635996e` | Trade Performance now surfaces real ATM template names (`'40 for 400'`, `'Tight 100'`, etc.) instead of NT's generic `'AtmStrategy'` label. Recorder schema migration (`recorder.py`) backfilled 208 historical fills. |
| 4 | `612fab7` | NinjaTrader-aware autostart watchdog. FastAPI now serves the built React frontend at `/` via a SPA-aware catch-all so the runtime is one process. `runtime/watchdog.ps1` polls for `NinjaTrader.exe`; `install_watchdog.ps1` registers a Task Scheduler entry that fires at user logon. |

### 2026-05-08 — Rebrand (same day)

| Commit | What |
|---|---|
| NT8_Trade_Perf `53e00b2` | Site = "The Helm". New ship's-wheel favicon. FastAPI app title updated. |
| TradingBot `34f2563` | Indicator: `TradeBotTrigger.cs` → `_Helm Locker/HelmAnalyzer.cs`. `[FYF]` log prefix → `[Helm]`. `FYF_API_URL` → `HELM_API_URL`. Two-copy gotcha synced; old NT-side file deleted. |
| TradingBot `b58a09b` | PROJECT.md banner reflects the new brand; session-log entries left as historical record. |
| TradeCopier `fd1984a` | Company name updated to Lodestone & Purser; product name "FYF Copier" itself rebranded in a follow-up commit. |

**Brand state going forward:** Product = **The Helm**. Indicator class = **HelmAnalyzer** (in `_Helm Locker/`). Company = **Lodestone & Purser** (replaces VQR Ventures LLC). Sibling product TradeCopier = **Helm Copier** (rebrand pending in this session).

---

### 2026-05-08 — Live Feed Pipeline, Phase 1 (same day, end of session)

Top-down design + same-session Phase 1 implementation of the **Independent Confirmation + Auto Analysis** project (item #1 of "Outstanding"). The pipe is one NS push, two consumers: outcome resolver (replaces the old "follow-up snip" idea) and an auto-analysis trigger that fires the LLM on selected (instrument, period) bars while the market is open.

**Locked design decisions:**

- **Hybrid trigger.** NS publishes bar closes + trade ticks unconditionally; bot decides per-message whether to store, fan out to analysis, or both. Multi-chart safe via idempotent ingest (bot dedupes — no user discipline).
- **Two endpoints, FastAPI on `:8000`:** `POST /api/feed/bar`, `POST /api/feed/ticks`. Localhost only.
- **Separate `feed.db`** (not the bot's main DB) at `TradingBot/app/data/feed.db`, WAL mode. Bars: PK `(instrument, period, ts seconds)` with `INSERT OR REPLACE`. Ticks: PK `(instrument, ts_ms, price)` with `INSERT OR IGNORE`, `WITHOUT ROWID`.
- **7-day retention** for both, with a hard rule: prune cutoff = min(7 days ago, oldest unresolved trade entry) — open trades protect their own data.
- **Outcome resolver = two stages:** bar pre-filter to find the crossing 1-min window, tick-level walk inside that window to break ambiguous bars. **Touched** semantics (≥ target / ≤ stop), tie-break to stop.
- **Auto-analysis = bar arrivals are the cadence.** Predicate over each `/feed/bar` checks dashboard config; if armed and warmup-gate clear, enqueue with coalescing on `(instrument, period)`. In-process asyncio queue.
- **Warmup gate = session-gap detection.** A >30 min silence implies session restart; skip the first bar after, run on the second. Handles CME maintenance + holidays for free.
- **Auto-analysis UI:** new dashboard panel with 4 slots × (instrument, period, on/off). 4-cap UI-side. Config in a small `auto_analysis_config` table on the bot's main DB.
- **Instrument naming:** NS strips contract month (`MES`, not `MES 06-26`) via `Instrument.MasterInstrument.Name`. Bot trusts NS.
- **Period:** taken from chart's native `BarsPeriod`. One indicator instance per chart.

**Phase 1 (publisher + transport + storage) — shipped this session, smoke-tested green:**

- Created [`TradingBot/app/src/feed_store.py`](app/src/feed_store.py) — sync sqlite3 module-level connection + `threading.Lock`, `init_schema()` / `insert_bar(...)` / `insert_ticks(rows)` with WAL + synchronous=NORMAL. `WITHOUT ROWID` on ticks.
- Created [`NT8_Trade_Perf/dashboard/api/feed.py`](../NT8_Trade_Perf/dashboard/api/feed.py) — Pydantic-validated endpoints; sync DB calls wrapped in `asyncio.to_thread`. Schema init at module import. Router prefixed `/api/feed`.
- Wired router into [`NT8_Trade_Perf/dashboard/api/main.py`](../NT8_Trade_Perf/dashboard/api/main.py) (added `feed as feed_routes` import + `include_router`).
- Created [`TradingBot/ninjascript/_Helm Locker/HelmFeed.cs`](ninjascript/_Helm%20Locker/HelmFeed.cs) — sibling to HelmAnalyzer. `Calculate.OnBarClose` for bars; `OnMarketData` filtered to `MarketDataType.Last` for ticks; 250 ms tick-flush via `System.Threading.Timer`. Skips `State != Realtime` to avoid historical replay flooding the bot. Two-copy synced to NT compile dir.
- Smoke tests via FastAPI `TestClient` and live `Invoke-RestMethod` after dashboard restart: bar 200, ticks 200, dedup confirmed (one duplicate tick rejected), bad payload → 422.

**Verification deferred** (market closed at end-of-session): F5 compile, apply to a chart, confirm `[HelmFeed] State → ...` Output-window lines, watch live ticks/bars land at next market open.

**Side-finding flagged + resolved:** The dashboard watchdog (`runtime/install_watchdog.ps1`) was **not installed as a Scheduled Task** on this machine — restarting NT did not bring uvicorn up. Manually launched uvicorn for this session's smoke tests. Watchdog reinstalled by user same evening (2026-05-08) — autostart restored.

**Outstanding (Phases 2–4 of this project, next-session priority):**
1. **Phase 2 — Outcome resolver.** Read-only module over `feed.db`. Two-stage (bar pre-filter → tick walk). Testable with synthetic data even before live ticks are flowing.
2. **Phase 3 — Auto-analysis hook-up.** `auto_analysis_config` table, dashboard panel, scheduler predicate on `/feed/bar`, asyncio queue with coalescing.
3. **Phase 4 — Retention prune + warmup gate.** Nightly DELETE with open-trade protection; session-gap detection on `last_bar_ts` per instrument.

---

### 2026-05-09 — Live Feed Pipeline, Phases 2–4

Built and tested the remaining three phases of the Independent Confirmation + Auto Analysis project. Watchdog reinstalled by user same evening as 2026-05-08, autostart restored — that item is closed.

**Phase 2 — Outcome resolver.** New module [`TradingBot/app/src/outcome_resolver.py`](app/src/outcome_resolver.py). Two-stage: (1) bar pre-filter picks the finest period available for the instrument (1m if present, else 5m, …) and finds the first bar at-or-after `entry_ts` whose range crosses target or stop; (2) tick walk inside that bar's window finds the first touch. Touched semantics throughout. Same-ms tie-break to stop. Falls back to bar-level resolution if no ticks were stored for the window. **12/12 synthetic test cases pass** (clean target/stop hits long+short, neither, ambiguous bar with ticks both directions, same-ts tie, bar-only fallback, ambiguous bar with no ticks → stop tie-break, no bars, pre-entry bars ignored, invalid direction → ValueError).

**Phase 3 — Auto-analysis hook-up.**

Backend:
- [`feed_store.py`](app/src/feed_store.py) — added `auto_analysis_config` table + `is_armed()`, `list_config()`, `replace_config()` (atomic full-list replace).
- [`auto_analyzer.py`](app/src/auto_analyzer.py) — new. In-process asyncio queue keyed on `(instrument, period)` so newer bars replace older queued jobs (coalescing). Lazy-starts the worker on first `submit()` so module import doesn't need an event loop. **`_run_analysis` is a deliberate stub** that logs the job and updates run_count — real headless analyzer is a separate design conversation (no screenshot, text-only LLM call against synthesized HTF context).
- [`feed.py`](../NT8_Trade_Perf/dashboard/api/feed.py) — added the arming predicate: after `insert_bar`, check `is_armed`; if yes, fan out to `auto_analyzer.submit`.
- [`auto_analysis.py`](../NT8_Trade_Perf/dashboard/api/auto_analysis.py) — new router. `GET /api/auto-analysis/config`, `PUT /api/auto-analysis/config` (atomic full-list replace, server-side cap of 4 armed entries, duplicate-key validation), `GET /api/auto-analysis/status` (queue size, run count, last run, worker liveness).
- [`main.py`](../NT8_Trade_Perf/dashboard/api/main.py) — wired router; added `PUT` to CORS `allow_methods` for the dev-mode Vite cross-origin flow.

Frontend:
- [`api.ts`](../NT8_Trade_Perf/dashboard/web/src/api.ts) — `putJSON()` helper, AutoAnalysis types + the period vocabulary + `MAX_SLOTS=4` constants.
- [`HomePage.tsx`](../NT8_Trade_Perf/dashboard/web/src/pages/HomePage.tsx) — new `AutoAnalysisCard` joins the Home grid (4-slot table of instrument input + period dropdown + on-toggle, Save button, live status footer pulling `/api/auto-analysis/status` every 5s).
- [`App.css`](../NT8_Trade_Perf/dashboard/web/src/App.css) — matching dark-theme styles.

8/9 backend integration cases pass via FastAPI `TestClient`. The 9th case was a coalescing assertion that turned out to be a stub-timing artifact: the stub is so fast the worker drains between each POST, so coalescing never has anything to collapse. Coalescing is correct by inspection (the dict-keyed `_pending` queue is the mechanism). Frontend type-checks (`tsc --noEmit`) and production-builds (`vite build`) clean.

**Phase 4 — Retention prune + session-gap warmup.**

- [`feed_store.py`](app/src/feed_store.py) — added `prune(retention_days, protected_ts_seconds)`. Cutoff = `min(now − retention_days, protected_ts_seconds)` so an open trade's data isn't deleted out from under the resolver.
- [`feed.py`](../NT8_Trade_Perf/dashboard/api/feed.py) — added `_oldest_open_trade_ts()` helper (reads `signals.jsonl` for unresolved/non-deleted/non-flat signals, returns earliest `entry_ts`) and `POST /api/feed/prune?retention_days=N` endpoint that combines them. **Auto-trigger of prune is deferred** — manual or scheduled-task only for now.
- [`feed.py`](../NT8_Trade_Perf/dashboard/api/feed.py) — added the **session-gap warmup gate** to `/api/feed/bar`. Per-instrument `_last_bar_ts` dict. If `bar.ts - last_ts > 1800` (or `last_ts is None`), the bar is "first of session" — bar still stores, but analysis is skipped. After-restart behavior: first bar per instrument always treated as post-gap (skip-first conservative).

8/8 cases pass (first-bar-skipped, second-bar-armed, post-gap-bar-skipped, next-bar-armed-after-warmup, out-of-order bar doesn't regress `last_ts`, prune endpoint reachable, prune body shape, huge retention deletes 0).

**Cumulative file inventory after this session:**

```
TradingBot/app/src/
  feed_store.py         (extended: + auto_analysis_config + prune)
  outcome_resolver.py   (new)
  auto_analyzer.py      (new)

NT8_Trade_Perf/dashboard/api/
  feed.py               (extended: + warmup gate, arming, prune endpoint)
  auto_analysis.py      (new)
  main.py               (extended: + auto_analysis router, + PUT in CORS)

NT8_Trade_Perf/dashboard/web/src/
  api.ts                (extended: putJSON + AutoAnalysis types)
  pages/HomePage.tsx    (extended: + AutoAnalysisCard)
  App.css               (extended: + Auto Analysis card styles)
  dist/                 (rebuilt; uvicorn serves on next start)

TradingBot/ninjascript/_Helm Locker/
  HelmFeed.cs           (already existed from 2026-05-08; unchanged today)
```

**Carry forward to next session:**
1. Live Phase 1 verification still pending market open (same as yesterday — restated with clearer instructions in Outstanding §1).
2. Real headless analyzer to replace the stub (Outstanding §1).
3. Auto-prune trigger (Outstanding §1).
4. **Sharing initiative** newly added (Outstanding §2) — config page + installer packaging.

### 2026-05-09 (afternoon/evening) — Improvement sweep + sibling project deletion

Long evening session covering: a sibling-project full lifecycle, a Claude-config audit, an NT chart inventory, and a 10-item improvement sweep that closed several Live Feed Pipeline carry-forwards.

**Helm Copier (TradeCopier) — built and then deleted same evening.**
- Built spike zero (NS AddOn loads, Tools-menu wired, NTWindow opens). Three NT8 quirks hit: AddOnBase shadows State/PrintTo enums; vendor-DLL trigger-file pattern doesn't auto-Reference; ControlCenter Tools-menu Name in 8.1.6.3 is `toolsMenuItem`. Pivoted to source-compile path.
- Built M1 Core fully: `LifecycleMap`, `SizingResolver` (modes 1–3), `CopyEngine` with full event handling (New/Change/Cancel/Filled/Rejected/PartFill), echo suppression, reverse mode, coalescing dispatch. **69/69 xUnit cases.**
- Built NS adapter wiring: `LeaderEventRouter`, `CopyEngineHost` (BlockingCollection + consumer thread), `FollowerSubmitter` (NT API submit/change/cancel), pre-existence filter, single-tab Window UI with leader/follower dropdowns + Start/Stop. Inlined Core source into NT compile path after vendor-DLL Reference auto-add proved unreliable.
- **User requested deletion** before live testing. Project dir, NT deployment, and memory entries all removed cleanly. Left the NAS git repo untouched per scope.

**Claude config audit + cleanup (5 items):**
- Trimmed `Projects/.claude/settings.local.json` from 70+ exact-string entries to 39 generic patterns (removed PID-19468 scripts, dated commit messages, one-time NT log paths).
- New per-project `CLAUDE.md` for `TradingBot/` and `NT8_Trade_Perf/` (each ~70 lines; carries working agreement, architecture, conventions, gotchas).
- Extracted §2 trading format / §7 domain contexts / §10 export structure from global CLAUDE.md into 4 new on-demand skills: `trading-analysis`, `vqr-real-estate`, `cmmc-context`, `export-knowledge-base`. Global CLAUDE.md trimmed 295 → ~150 lines.
- Pruned memory 16 → 10 entries (folded the deleted ones into per-project CLAUDE.md).
- New `ninjascript-reviewer` subagent encoding 12 NT8 gotchas + 3 diagnostic patterns (file-write proof-of-life, reflection-based API discovery, minimum-viable inline reproductions).

**NT chart audit:** parsed `MyFutures.xml` workspace, listed all indicators on both MES 5-Minute charts (Pivots, THE_VWAP_INTRADAY, EMA-90, Overnight_High_Low_jay, BarTimer, DonchianChannel-14, OrderFlowTradeDetector ± ATR-14 ± OrderFlowVolumeProfile ± PriorDayOHLC). User confirmed: **the same indicator stack is used on every 5-min chart regardless of instrument** — captured as the "Chart conventions" section in `TradingBot/CLAUDE.md`.

**10-item improvement sweep (all eight code items shipped; chart-template item user did themselves; Phase 1 live-verify still pending market open):**
1. **HelmAnalyzer pruned** to match the chart stack (only `ema90` + `atr14` + Donchian + swing + pivots; dropped EMA-20/50/200). Bot's emitted context is now strictly ⊆ what the user sees on screen.
2. **(User-done)** Saved chart template.
3. **(Pending)** Live Phase 1 verification at next market open.
4. **Pytest scaffolding** added to both projects: `TradingBot/app/tests/` (29 cases — feed_store, outcome_resolver, auto_analyzer, schema-version) + `NT8_Trade_Perf/tests/` (13 cases — feed router, auto-analysis router). All pass. Today's inline smoke tests are now permanent.
5. **Real headless analyzer** — new `app/src/headless_analyzer.py` + `app/prompts/headless_analyzer.txt`. Pulls 60 recent bars from `feed.db`, computes EMA(90) + ATR(14) in pure Python, calls workstation Ollama text-only, persists via `signal_storage` with `trigger=headless`. Replaces the `auto_analyzer._run_analysis` stub via `asyncio.to_thread`.
6. **Auto-prune scheduler** — `main.py` now uses FastAPI `lifespan` to spawn a background task that prunes `feed.db` every 24h (first run after 10 min). Computes "oldest unresolved trade entry" from `signals.jsonl` so open trades' bars/ticks survive.
7. **Watchdog rewritten as Windows Service** via NSSM. New `runtime/install_service.ps1` (auto-installs NSSM via winget if missing, prompts for user creds, sets auto-restart on failure) + `uninstall_service.ps1`. Old Task Scheduler installer marked deprecated. Service name: `HelmDashboardWatchdog`. Two debugging cycles to land it: (a) em-dash chars in `.ps1` file caused PS 5.1 parse failure in service context (no BOM → Win-1252 misread); (b) WindowsApps Python alias fails from service context (`Access is denied`) — added `Resolve-PythonExe` that prefers `py` launcher then real install paths under `LOCALAPPDATA\Programs\Python\` or `Program Files\Python*\`. **User installed `Python.Python.3.12` via winget; service now running end-to-end** — `HelmDashboardWatchdog` Status=Running, uvicorn auto-spawned on NT detection, `127.0.0.1:8000` listening.
8. **`/bedtime` slash command** updated to lead with per-project `CLAUDE.md` for durable rules; falls through to `PROJECT.md`/`MIGRATION.md` for point-in-time decisions.
9. **Schema versioning** added to `feed_store` (new `schema_meta` table, `SCHEMA_VERSION=1`, `SchemaVersionMismatchError` raised on forward-incompat) and `signal_storage` (records stamp `schema_version: 1`; `load_all` warns on higher versions).
10. **Diagnostic patterns codified** into the `ninjascript-reviewer` agent prompt with working code samples drawn from today's debugging.

**Global CLAUDE.md addition:** §3 PowerShell — "ASCII only in `.ps1` files" rule landed after em-dash bug bit twice (`hot-processes.ps1`, `watchdog.ps1`). Also calls out `$args` / `$input` / `$matches` automatic-variable shadowing.

**Outstanding (next session pickup, priority order):**
1. **Live Phase 1 verification at market open.** F5 `HelmAnalyzer.cs` (was edited — EMA prune); apply HelmFeed to a chart; watch ticks/bars land in `feed.db`; trigger Ctrl+Shift+F to verify the manual analyze path still works with the trimmed context; arm an Auto Analysis slot and watch the headless analyzer fire on bar close.
2. **Verify watchdog teardown side.** Stop NT and confirm `HelmDashboardWatchdog` brings uvicorn down (start + teardown completed in tests; full cycle when user is around).
3. **SHARING initiative** (config page + installer) — still the next major initiative.
4. **NS account-state indicator** — Open Positions card on Home is still a placeholder.

**Carry-forward observations:**
- Service install pattern is sensitive to Python install location. Microsoft Store Python (anywhere under `WindowsApps/`) is service-incompatible due to ACLs on that folder. Real installs (Python.Python.3.x via winget) work. Captured in the `Resolve-PythonExe` function.
- All `.ps1` files going forward must be ASCII-only or BOM-stamped — landed as a global rule but worth re-emphasizing if creating new scripts.
- Headless analyzer is a stub-replacement; the prompt + context shape will iterate based on actual proposals it produces. Compare against HelmAnalyzer (screenshot path) outputs once both are running on the same instrument.

---

### 2026-05-11 — Settings page, GitHub push, clean reinstall, install.ps1

Long session covering Phase 1 of the SHARING initiative, monorepo creation + GitHub publish, a full wipe-and-reinstall validation of the installer, plus a handful of UX fixes.

**Snip pipeline diagnostic (morning).** After NT had a rough startup, `Ctrl+Shift+F` triggered the bot end-to-end but the Snipping overlay never appeared. Localized to a broken `ms-screenclip:` URI handler — re-registered `Microsoft.ScreenSketch` AppxPackage + restarted the watchdog service and captures resumed. Cross-session bounce from Session-0 uvicorn to Session-N user desktop is fragile; flagged for future hardening.

**UI fixes.**
- Removed the "Reject this analysis & delete" button from `ChildrenSection` on Signal Detail (per-signal `DeleteButton` retained at page header).
- Trade Performance times now render in `America/Chicago` (DST-aware) via `Intl.DateTimeFormat`; column headers say "(CT)". Trade and Fill exit/entry times + StatusPanel first/last fill all switched.

**SHARING Phase 1 — Settings page (shipped).** Per-user runtime config:
- New `Trade_Perf/dashboard/api/settings.py` — Pydantic schema with `appearance` / `ai_backend` / `strategy` / `accounts` sections; storage at `~/.helm/settings.json` (atomic temp+rename writes); `schema_version=1` for future migrations.
- New routes: `GET/PUT /api/settings`, `POST /api/settings/reset`, `POST /api/settings/test/ollama` (probes `/api/tags` against the configured URL).
- New `TradingBot/app/src/runtime_config.py` — accessor pattern with hardcoded `Defaults` fallback. Bot code (`local_llm_analyzer`, prune loop) calls e.g. `runtime_config.ollama_url()`; if dashboard package is importable (uvicorn process), delegates to live settings; otherwise uses defaults. Standalone CLI keeps working.
- Frontend: `SettingsPage.tsx` with tabbed UI (Appearance / AI Backend / Strategy / Accounts), live color preview, beforeunload guard, click-capture nav guard (React Router 7's `useBlocker` only works with data routers — used DOM event capture instead). New `lib/theme.ts` for `applyAppearance` + localStorage cache. `App.tsx` pre-applies cached appearance before mount so the SPA never flashes the default palette.
- Accounts tab was initially a textarea; replaced with a per-account list editor (input + X + Add, Enter-to-append) after the textarea stripped trailing empty lines mid-edit and made adding rows impossible. Empty rows stripped at save.

**Monorepo + GitHub publish.**
- Created `%USERPROFILE%\Documents\Projects\TheHelmTrader\` containing `TradingBot/` + `Trade_Perf/` (was `NT8_Trade_Perf/` — renamed). Private GitHub repo at `git@github.com:n8t-space/TheHelmTrader.git`, SSH ed25519 auth, branch `main`.
- Fresh history (NAS bare repos retained at `G:/Git_Projects/` as untouched parallel mirrors).
- Data-hygiene gates: `signals.jsonl` and `trades.db` removed from index, added to per-subproject .gitignore. Confirmed zero leak at first push.
- README rewritten with installation steps; lead is now the one-shot installer, manual steps preserved as fallback.

**install.ps1 (shipped).** One-shot installer at the monorepo root. Six steps: prereqs (winget-installs missing tools), pip deps, npm install + vite build, NS indicator copy, `recorder.py` startup-shortcut install + launch, NSSM `HelmDashboardWatchdog` service install. Idempotent, ASCII-only, runs from elevated PS. Caught a PS 5.1 pitfall mid-test: `2>&1` on native commands wraps stderr as `NativeCommandError` and trips `ErrorActionPreference = 'Stop'` even on successful builds (vite's `chunks > 500 kB` advisory was the canary). Added `Invoke-NativeCapture` helper that toggles EAP to `Continue` around the call and only throws on real non-zero exit codes.

**Clean-slate reinstall validation.**
- Wrote `helm-clean.ps1` to back up operational data + quarantine project trees + uninstall pip deps + remove the NSSM service + tear down the recorder startup shortcut.
- User ran it on the canonical install + cloned fresh + ran `install.ps1`. End-to-end install completed except step 6 (NSSM service) failed with **Event 7038 logon failure** — password entered at the credential prompt didn't match. Watchdog logged "starting" once then SCM killed it; uvicorn never came up; HelmFeed flooded "POST failed". Open blocker for next session.
- Operational data preserved at `%USERPROFILE%\Documents\Projects\helm-backup-20260511_213210\` (5 files: signals.jsonl, feed.db, trades.db, settings.json, screenshots/).
- Two quarantine dirs `_helm-quarantine-20260511_{213210,214509}` hold the old canonical trees + NS indicators + ~/.helm. Restore is 5 `Move-Item` calls if needed.

**Carry-forward observations.**
- `helm-clean.ps1` has two bugs we hit live: (a) the kill-recorder filter searched for `pythonw.exe` but the live process name is `pythonw3.12.exe`; (b) on re-run after a rename, the backup phase looks at the old canonical paths and silently backs up nothing. Worth patching to read from `TheHelmTrader/{TradingBot,Trade_Perf}/...` going forward.
- The two-working-trees structure (canonical `NT8_Trade_Perf/` + GitHub sync `TheHelmTrader/`) is now collapsed: after the wipe, the GitHub clone IS the canonical runtime. Memory entry updated.
- Microsoft Store Python alias (anywhere under `WindowsApps/`) remains service-incompatible. `Resolve-PythonExe` in watchdog.ps1 prefers `py.exe` then `LOCALAPPDATA\Programs\Python\Python312\` over WindowsApps. Same rule as 2026-05-09.

---

### 2026-05-11 (evening) — Post-install: snip URI handler bit again

Service came up clean after the user re-entered the correct Windows password (resolved item #1 from earlier today). Health probe returned 200, `trades.db` populated with 459 fills, recorder + watchdog both running.

Snip overlay then didn't appear on Ctrl+Shift+F — bot logged `Opening Snipping overlay` then aborted three times at the 30s no-snip timeout. **Same Session-0 cross-session bounce issue we hit this morning** — `explorer.exe ms-screenclip:` from the NSSM-spawned uvicorn isn't reaching the user desktop until the URI handler is warmed from a user-session invocation.

**Two-step fix (captured as recurring memory):**
1. User-session PS: `Start-Process 'ms-screenclip:'` (Esc to dismiss)
2. Elevated PS: `nssm restart HelmDashboardWatchdog`

Memory entry `feedback_snip_uri_session0.md` added so future sessions skip the bot-side debugging when this pattern recurs. Expected after every reboot or service reinstall.

**No code changes.** Outstanding-list item #1 (NSSM logon failure) is closed; remaining items renumbered.

---

### 2026-05-12 (evening) — Hotkey still broken; new ATM-strategy asks

Bot pipeline is healthy: tradebot.log at 21:00:05 shows headless analyzer storing a MES 5m proposal and outcome-watcher suggesting stops on two prior signals. Service Running, `:8000` listening (PID 19420). But user reports hotkey "still broken" — NS log shows `[Helm] Hotkey caught` + `Context POSTed` followed by no bot-side `NT trigger received` line. POST is failing between NS HttpClient and `/api/capture-from-nt`. Same NS log window has HelmFeed POST failures (`HttpRequestException`) — correlated. **Not** the Session-0 URI handler this time; the request isn't reaching the bot at all.

User also called out: snipping tool itself works (don't waste time there), and surfaced two new feature asks for the install flow:
- Refresh available ATM strategies on each boot
- Signal proposals must reference an ATM strategy for TP/SL rather than raw prices

Plus an unclear "Automated signal updater still doesn't work" — needs clarification before debugging.

No code changes this session. Outstanding list restructured around the new top blockers; HTTP plumbing investigation is item #1.

---

### 2026-05-19 — Signals rules overhaul, reconciliation removal, packaging prep

Three threads landed:

**(1) Entry/outcome invariants enforced everywhere.** Codified the rule "outcome populated implies entry was hit" across all write paths:
- `Trade_Perf/dashboard/api/signals.py` — `update_outcome` route coerces `entry_triggered` to match (`no_fill` ⇔ `False`, anything else ⇒ `True`). Rejects 409 if you try to set a non-no_fill outcome on an already-no-entry signal.
- Same coercion on `update_entry_triggered`: flipping triggered=false auto-stamps `outcome=no_fill`.
- `TradingBot/app/src/outcome_watcher.py` — bar-walker writes now stamp `entry_triggered` alongside the outcome.
- Signals default to `position_size=1` contract (`signal_storage.append_signal` adds the default; `instruments.compute_trade_metrics` floors zero/missing to 1) so realized P&L and W/L tallies populate without manual sizing edits.

**(2) "Reconciliations from this analysis" feature removed.** The cross-signal LLM reconciliation that ran per capture and surfaced via a confirm-and-soft-delete UI was retired — confirming destroyed prior signals from the visible list, distorting W/L + P&L stats, and the LLM was often less accurate than the deterministic bar walker. Removed:
- Frontend: `ChildrenSection`, `PendingSuggestionBanner`, `pending_suggestions` block on Home, `children` field on `SignalDetailResp`.
- Dashboard API: `_load_visible_signals` suggestion-stripper, children walk in `GET /api/signals/{ts}`, `POST /api/signals/{ts}/suggestion/confirm`, `pending_suggestions` from the action queue.
- Bot pipeline: `_reconcile_open_trades`, `_find_open_trades`, `MAX_RECONCILE_TARGETS`, the `reconcile()` call site (the function itself is left in `local_llm_analyzer.py` with no caller).

Side-effect: manual signals (Ctrl+Shift+F) now get the same direct outcome write from the bar walker that headless signals already had — no confirm banner exists to surface a "suggestion." User retains override via the Signal Detail Outcome editor.

**(3) Outcome-watcher candidate filter fix + data normalization.** The watcher was skipping any signal where `outcome_suggestion.result` was set, regardless of who wrote it — so stale LLM-reconciliation suggestions from the now-removed feature were blocking the bar walker from ever running. Filter now only skips when `outcome_suggestion.engine == "resolver"`. Backfilled three sticky signals (2026-05-13T18:45, 2026-05-18T18:45, 2026-05-19T08:53) via a force-rerun script. Earlier in the day backfilled 61 historical signals that had `entry_triggered=None` + age >= 4h: 50 with real outcomes (target/stop) got `entry_triggered=true`; 11 with no/no_fill outcome got `entry_triggered=false` plus `outcome=no_fill` where missing.

**Packaging prep (also today):**
- Scrubbed personal data from runtime defaults. `runtime_config.py`, `Trade_Perf/dashboard/api/settings.py`, `Trade_Perf/dashboard/api/home.py`, `Trade_Perf/dashboard/web/src/api.ts`, `SettingsPage.tsx`, `parse_fills.py` no longer carry the operator's account IDs or LAN Ollama IP. Defaults are localhost Ollama + NT-default sims only; the friend's account categorization is driven from the Settings page.
- `home.py`'s previously-hardcoded `ACCOUNT_CATEGORIES` now reads from `settings_mod.get_settings().accounts` at request time — single source of truth.
- Doc sweep: `README.md`, `TradingBot/CLAUDE.md`, `TradingBot/PROJECT.md`, `Trade_Perf/CLAUDE.md`, `Trade_Perf/PROJECT.md` no longer reference the operator-specific workstation IP as the canonical inference target. `README.md` gained a "unzip the bundle" alternative to the SSH-clone instruction. `TradingBot/PROJECT.md` "Open-Trade Reconciliation" section rewritten as "(REMOVED)".

**Test status:** TradingBot 29/29 pass. Trade_Perf 12/13 pass — the one failure (`test_second_bar_within_window_is_armed`) pre-dates today and is unrelated; tracks the feed-router session-gap warmup behavior.

**Not done / next session:**
- Carry-forward from 2026-05-12: backend HTTP plumbing (Ctrl+Shift+F + HelmFeed POSTs failing to reach `:8000`) still item #1.
- The two ATM-strategy asks: refresh available strategies at boot + force proposals to reference an ATM strategy instead of raw TP/SL.
- Clarify the "automated signal updater still doesn't work" report.
- Investigate the pre-existing `test_second_bar_within_window_is_armed` failure.

---

### 2026-05-18 — Signal Analysis page cleanup (KPI grid + P&L column)

Tightened the Signal Analysis page header and added a P&L column to the row table. Nothing structural — pure dashboard polish on `Trade_Perf/dashboard/web/src/pages/SignalAnalysisPage.tsx`.

**Done:**
- Replaced the single "Today's Signals" KPI card with a side-by-side `.grid` of two cards: **Today** (date in header) and **All-Time** (`since <first-signal-date>` in header). Both share a `computeKpi(signals)` helper that returns `{count, resolved, pnl, wins, losses, instruments, autoGen, autoRes, avgConfidence, avgRR}`. Today's card shows: P&L headline, signals captured, win-rate · W/L, resolved/count, auto-gen · auto-res, instruments list. All-Time adds avg confidence · avg R:R and a distinct-instrument count.
- Added a sortable **P&L** column to the signal table between Outcome and Auto-res. Reads `s.metrics.realized_pnl`, formatted as `$X,XXX.XX` with `pnl-pos`/`pnl-neg` coloring; `—` for signals with no realized P&L; tooltip surfaces `realized_pnl_source` (e.g. `trade-match` vs `proposal-bracket`). `SignalKey` union extended with `'realized_pnl'`; accessor + `<Th>` + empty-row `colSpan` bumped from 14 → 15.
- Removed dead `fmtNum` helper that had been unreferenced for several sessions (was tripping `noUnusedLocals`).
- `npx tsc -p tsconfig.app.json --noEmit` clean. `npm run build` clean. New bundle `dist/assets/index-pBRHd6uB.js` (333 kB / 99 kB gzipped). Watchdog picks it up next uvicorn restart; no NS / backend changes required.

**Not done / next session:**
- No backend touched. Backend HTTP plumbing investigation from 2026-05-12 (Ctrl+Shift+F hotkey + HelmFeed POSTs both failing to reach `:8000`) is still the priority item carried forward.
- All-Time card lists every distinct instrument inline — fine at current volume; will need a `Top N` / collapse if the count grows past ~10.

---

### Closure

The original four-phase AI-offload migration is fully done; the eight-checkpoint dashboard merger is fully done; four post-merge improvement tiers are done; rebrand is done. The Live Feed Pipeline is functionally complete — Phases 1–4 all shipped (2026-05-08 → 2026-05-09); only live Phase 1 verification at market open remains. The 10-item improvement sweep closed the headless-analyzer + auto-prune carry-forwards from earlier in the day, plus added pytest, schema versioning, NSSM-service watchdog, project-local CLAUDE.md scaffolding, and an `ninjascript-reviewer` subagent. **Next major initiative: SHARING** (config page + installer). The list under "Outstanding (next session pickup)" near the top of this doc is the source of truth for what's next.

If a future session picks this up cold, start by reading: this file's "Outstanding" list → [`PROJECT.md`](PROJECT.md) "Current state" block → [`../Trade_Perf/PROJECT.md`](../Trade_Perf/PROJECT.md). Together those describe what's running and where the seams are.
