# The Helm — Ideal Configuration

> Recommended baseline settings for a fresh install. Everything here can be tuned later from the dashboard's **Settings** page; this doc explains *why* each value matters and what to consider before changing it.

## Purpose

One-page reference operators can follow when configuring The Helm for the first time, or auditing an existing install. The same content is mirrored on the dashboard's **Support → Configuration** tab so it's reachable without leaving the browser.

## Prerequisites

- The Helm installed via `install.ps1` and the `HelmDashboardWatchdog` service running
- NinjaTrader 8 (8.1.6.3+) running with at least one chart open
- An AI provider available (Ollama locally / on LAN, or a paid Claude / OpenAI key)

## 1. AI Backend

The vision LLM that turns chart screenshots into trade proposals. Picked on **Settings → AI Backend**.

### Provider comparison

| Provider | Cost | Latency (warm) | Privacy | Quality | Best for |
|---|---|---|---|---|---|
| **Ollama** (local / LAN) | $0 | 5–15 s | Stays on your network | Good with `qwen2.5vl:7b` | High-volume analysis, sensitive charts |
| **Claude** (sonnet-4-6) | ~$0.01–0.03 / snip | 2–4 s | Anthropic cloud | Best overall reasoning | Important / one-shot decisions |
| **OpenAI** (gpt-4o) | ~$0.005–0.02 / snip | 2–5 s | OpenAI cloud | Strong, faster than Claude | Balanced cost/quality |

**Recommended:** Ollama for volume, Claude for conviction trades. Switch providers per session via the Settings page.

### Ollama (recommended default)

| Field | Recommended value | Notes |
|---|---|---|
| Provider | `ollama` | |
| Ollama URL | `http://127.0.0.1:11434/api/generate` | If offloaded to a LAN GPU (e.g. workstation 4060 Ti), use `http://<host>:11434/api/generate` |
| Model | `qwen2.5vl:7b` | Run `ollama pull qwen2.5vl:7b` first |
| Fallback model | `qwen2.5vl:3b` | Used if the primary times out |
| num_ctx | `8192` | 4096 is fine for tight charts; 16384+ only if you stuff a lot of context into the prompt |
| Request timeout (s) | `180` | Cold start on iGPU can take ~30–60s |

### Claude

| Field | Recommended value | Notes |
|---|---|---|
| Provider | `claude` | |
| API key | `sk-ant-...` | From [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) |
| Model | `claude-sonnet-4-6` | Use `claude-opus-4-7` if you want the smartest model and accept higher cost |
| Max tokens | `2048` | Enough for a structured proposal + reasoning |
| Request timeout (s) | `60` | Cloud is fast; if you see timeouts, network is the issue |

### OpenAI

| Field | Recommended value | Notes |
|---|---|---|
| Provider | `openai` | |
| API key | `sk-proj-...` | From [platform.openai.com/api-keys](https://platform.openai.com/api-keys) |
| Model | `gpt-4o` | `gpt-4o-mini` is ~5x cheaper but noticeably weaker on chart reading |
| Max tokens | `2048` | |
| Request timeout (s) | `60` | |

After saving, click **Test connection** — green badge with latency + model present means you're good.

> **Where credentials live.** Your API keys and broker account IDs are stored in `~/.helm/credentials.json`, kept separate from `~/.helm/settings.json` (which holds only non-sensitive config). `credentials.json` is git-ignored, is never overwritten by install/update, and is excluded from support bundles. On first run after an update, any keys/accounts still inline in `settings.json` are migrated into `credentials.json` automatically. Back up `credentials.json` if you reinstall.

## 2. Strategy thresholds

**Settings → Strategy**. Controls when the bot accepts a proposal and how aggressively it cleans up old data.

| Field | Recommended | Why |
|---|---|---|
| Confidence floor | `0.65` | Rejects sub-0.65 proposals and forces a retry. Tune up if you get too much noise, down if good setups get filtered out |
| Max attempts | `3` | Retry budget when below floor. More attempts = more cost / latency per snip |
| Reconciliation cap | `5` | Max in-flight trades the reconciliation pass touches per manual snip. 5 is plenty for one operator |
| Retention (days) | `14` | Feed.db (live bars + ticks) auto-pruned after this. 14d covers the outcome-resolver window with margin |
| Stale bar (s) | `300` | Skip auto-analysis if the most recent bar is older than this. Prevents weekend backfill from triggering analyses |

## 3. Accounts

**Settings → Accounts**. Categorizes your NT account IDs into Live / Evals / Simulation buckets. Drives:
- The Home page **Cumulative Earnings** card (one line per bucket)
- The Trade Performance page quick-filter buttons

Pre-listed simulation accounts ship under the Simulation bucket: `Sim101`, `Playback101`, `Backtest`, `SimBetaSIM`. Add your own live broker account IDs to **Live**, and any prop-firm evaluation accounts to **Evals**.

**Tip:** Account IDs are exactly as NT8 reports them in the Control Center's Accounts tab — copy/paste to avoid typos.

## 4. Auto Analysis (Home page)

Headless analysis runs without you pressing Ctrl+Shift+F. Configured on the Home page → **Auto Analysis** card. Up to 4 instrument/period slots.

| Slot | Instrument | Period | Purpose |
|---|---|---|---|
| 1 | `MES 03-26` | `5m` | Primary scalping timeframe |
| 2 | `MES 03-26` | `15m` | Intraday context |
| 3 | `MCL 04-26` | `5m` | Crude scalping |
| 4 | `MCL 04-26` | `15m` | Crude intraday |

Use the active front-month contract for each instrument. Slots are checked against the `HelmFeed` data store, so the corresponding chart must have `HelmFeed` running.

## 5. NinjaScript indicators

Both Helm indicators live under `_Helm Locker` after install. Add them to each chart you trade:

| Indicator | Add to | Purpose |
|---|---|---|
| `HelmAnalyzer` | Every chart you might Ctrl+Shift+F | Captures the chart bitmap + market context and POSTs to the dashboard |
| `HelmFeed` | Every chart used in Auto Analysis | Streams live bars + ticks into `feed.db` for the headless pipeline |

After adding, compile via NinjaScript Editor (F11 → F5). **Compile succeeded** at the bottom = ready.

## 6. Appearance (optional)

**Settings → Appearance**. Cosmetic — defaults are fine. Worth setting:

| Field | Recommended | Why |
|---|---|---|
| Theme | `Dark` | Easier on the eyes during long sessions |
| Timezone (IANA) | `America/Chicago` | CME session timing — RTH open/close, daily candles align |
| Table page size | `100` | Comfortable for Trade Performance / Signal Analysis tables |

## Verification

1. Open `http://127.0.0.1:8000/`
2. Settings → AI Backend → **Test connection** → green
3. Press Ctrl+Shift+F on a NinjaTrader chart → drag a rectangle → within 30s a new card appears on the Signal Analysis page
4. Make a paper trade in NT → within ~5s the new fill shows on Trade Performance
5. Health page log tail shows `[feed.bar]` POSTs every minute (or per your bar period) if `HelmFeed` is on a chart

## Troubleshooting

See the dashboard's **Support → Troubleshooting** tab for FAQ entries covering the common configuration issues (Ollama not reachable, NinjaScript not compiled, snip overlay broken, etc.).

## References

- `README.md` — install / uninstall / update procedures
- `Trade_Perf/PROJECT.md` — dashboard architecture
- `TradingBot/PROJECT.md` — pipeline architecture
- [Ollama vision models](https://ollama.com/library?c=vision)
- [Anthropic Claude pricing](https://www.anthropic.com/pricing)
- [OpenAI pricing](https://openai.com/api/pricing/)
