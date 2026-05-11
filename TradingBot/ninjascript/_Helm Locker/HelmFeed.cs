// =====================================================================
//  HelmFeed — live bar/tick publisher to The Helm bot
//  Project: Lodestone & Purser / The Helm
//  Companion to: %USERPROFILE%\Documents\Projects\TradingBot\app
// =====================================================================
//
//  WHAT THIS DOES
//  --------------
//  Apply this indicator to any chart you want piped into the bot. Two
//  streams flow to the dashboard FastAPI on 127.0.0.1:8000:
//
//      POST /api/feed/bar    — one closed bar (chart's native period)
//      POST /api/feed/ticks  — batch of trade prints, flushed every 250 ms
//
//  Bars come via OnBarUpdate (Calculate.OnBarClose). Ticks come via
//  OnMarketData filtered to MarketDataType.Last (trade prints, not
//  bid/ask quote updates) — the two event streams are independent in
//  NS8 so we don't need to choose.
//
//  Multi-chart safe: the bot dedupes bars on (instrument, period, ts)
//  and ticks on (instrument, ts_ms, price). Two charts on MES 5m fire
//  twice; the bot keeps one.
//
//  Historical bars/ticks are skipped (only State.Realtime) — chart load
//  would otherwise flood the bot with weeks of useless past data.
//
//  INSTALL
//  -------
//  1. Copy this file to:
//       %USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\Indicators\_Helm Locker\HelmFeed.cs
//  2. NinjaScript Editor → F5 to compile.
//  3. On any chart you want fed: Indicators (Ctrl+I) → HelmFeed → Apply.
//     The chart's period (5m / 15m / 1h ...) becomes the published period.
//
//  NOTES FOR FUTURE EDITS
//  ----------------------
//    - HttpClient is lazy-initialized — same rationale as HelmAnalyzer
//      (static field initializer exceptions silently exclude us from
//      type-discovery).
//    - Hand-rolled JSON to avoid pulling serializers into the NS sandbox.
//    - Instrument is published as MasterInstrument.Name (e.g., "MES"),
//      so contract-month suffixes ("MES 06-26") never reach the bot.
//    - Tick batching uses System.Threading.Timer firing every 250 ms.
//      Buffer is swap-on-flush so OnMarketData never blocks on IO.
// =====================================================================

#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net.Http;
using System.Text;
using System.Threading.Tasks;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    public class HelmFeed : Indicator
    {
        // =================================================================
        //  CONFIG
        // =================================================================
        private const string FEED_BAR_URL         = "http://127.0.0.1:8000/api/feed/bar";
        private const string FEED_TICKS_URL       = "http://127.0.0.1:8000/api/feed/ticks";
        private const int    HTTP_TIMEOUT_SECONDS = 5;
        private const int    FLUSH_INTERVAL_MS    = 250;
        private const int    BACKFILL_BAR_COUNT   = 100;

        private static readonly DateTime UNIX_EPOCH =
            new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);

        // =================================================================
        //  TICK BATCH STATE
        // =================================================================
        private class Tick
        {
            public long   TsMs;
            public double Price;
            public long   Volume;
        }

        private List<Tick> tickBuffer = new List<Tick>(512);
        private readonly object tickBufferLock = new object();
        private System.Threading.Timer flushTimer;

        // =================================================================
        //  HTTP — lazy singleton
        // =================================================================
        private static HttpClient httpInstance;
        private static readonly object httpLock = new object();
        private static HttpClient Http
        {
            get
            {
                if (httpInstance == null)
                {
                    lock (httpLock)
                    {
                        if (httpInstance == null)
                        {
                            httpInstance = new HttpClient
                            {
                                Timeout = TimeSpan.FromSeconds(HTTP_TIMEOUT_SECONDS)
                            };
                        }
                    }
                }
                return httpInstance;
            }
        }

        // =================================================================
        //  LIFECYCLE
        // =================================================================
        protected override void OnStateChange()
        {
            try { Print($"[HelmFeed] State → {State}"); } catch { }

            if (State == State.SetDefaults)
            {
                Description              = "The Helm: publish closed bars + trade ticks to the local bot's feed endpoints.";
                Name                     = "HelmFeed";
                IsOverlay                = true;
                Calculate                = Calculate.OnBarClose;
                IsSuspendedWhileInactive = true;
            }
            else if (State == State.DataLoaded)
            {
                // Start the tick-flush timer once data is loaded. Firing
                // earlier than first tick is harmless (empty buffer = no-op).
                flushTimer = new System.Threading.Timer(
                    _ => FlushTicks(), null, FLUSH_INTERVAL_MS, FLUSH_INTERVAL_MS);
            }
            else if (State == State.Realtime)
            {
                // Historical backfill: publish the last N closed bars so the
                // bot's feed.db is warm immediately, instead of waiting ~100
                // minutes for 20 live 5m bars to accumulate. The bot tags
                // bars whose ts is materially older than wall-clock now as
                // "stale" and stores them WITHOUT triggering auto-analysis,
                // so flooding 100 bars at once doesn't fire 100 LLM calls.
                try { BackfillHistoricalBars(BACKFILL_BAR_COUNT); }
                catch (Exception ex) { Print($"[HelmFeed] backfill failed: {ex.Message}"); }
            }
            else if (State == State.Terminated)
            {
                if (flushTimer != null)
                {
                    flushTimer.Dispose();
                    flushTimer = null;
                }
                // Final flush so anything still buffered makes it out.
                FlushTicks();
            }
        }

        protected override void OnBarUpdate()
        {
            // Only the chart's native series; ignore any future AddDataSeries.
            if (BarsInProgress != 0) return;
            if (State != State.Realtime) return;
            if (CurrentBar < 1) return;

            string json;
            try { json = BuildBarJson(); }
            catch (Exception ex)
            {
                Print($"[HelmFeed] BuildBarJson failed: {ex.Message}");
                return;
            }
            Task.Run(() => PostAsync(FEED_BAR_URL, json));
        }

        protected override void OnMarketData(MarketDataEventArgs e)
        {
            // Trade prints only — skip bid/ask quote noise.
            if (e.MarketDataType != MarketDataType.Last) return;

            // Belt-and-suspenders; OnMarketData generally only fires live.
            if (State != State.Realtime) return;

            var t = new Tick
            {
                TsMs   = ToUnixMillis(e.Time),
                Price  = e.Price,
                Volume = e.Volume,
            };

            lock (tickBufferLock)
            {
                tickBuffer.Add(t);
            }
        }

        // =================================================================
        //  TICK FLUSH — swap-on-flush so OnMarketData never blocks
        // =================================================================
        private void FlushTicks()
        {
            List<Tick> snapshot;
            lock (tickBufferLock)
            {
                if (tickBuffer.Count == 0) return;
                snapshot = tickBuffer;
                tickBuffer = new List<Tick>(Math.Max(512, snapshot.Count));
            }

            string json;
            try { json = BuildTicksJson(snapshot); }
            catch (Exception ex)
            {
                Print($"[HelmFeed] BuildTicksJson failed: {ex.Message}");
                return;
            }
            Task.Run(() => PostAsync(FEED_TICKS_URL, json));
        }

        // =================================================================
        //  JSON BUILDERS
        // =================================================================
        private string BuildBarJson()
            => BuildBarJsonAt(ToUnixSeconds(Time[0]),
                              Open[0], High[0], Low[0], Close[0],
                              (long)Volume[0]);

        private string BuildBarJsonAt(long ts, double o, double h, double l, double c, long v)
        {
            string sym    = Instrument.MasterInstrument.Name;
            string period = PeriodLabel();

            var ic = CultureInfo.InvariantCulture;
            var sb = new StringBuilder(160);
            sb.Append('{');
            sb.AppendFormat(ic, "\"instrument\":\"{0}\",", sym);
            sb.AppendFormat(ic, "\"period\":\"{0}\",", period);
            sb.AppendFormat(ic, "\"ts\":{0},", ts);
            sb.AppendFormat(ic, "\"o\":{0},", o);
            sb.AppendFormat(ic, "\"h\":{0},", h);
            sb.AppendFormat(ic, "\"l\":{0},", l);
            sb.AppendFormat(ic, "\"c\":{0},", c);
            sb.AppendFormat(ic, "\"v\":{0}", v);
            sb.Append('}');
            return sb.ToString();
        }

        // -----------------------------------------------------------------
        //  Historical backfill — runs once on State.Realtime transition.
        //  Walks the chart's loaded historical bars (absolute index, NOT
        //  barsAgo — same access pattern HelmAnalyzer uses for pivots, to
        //  stay compatible with the WPF dispatcher thread quirks) and POSTs
        //  each closed bar to /api/feed/bar. The bot recognizes stale ts
        //  and stores them without triggering auto-analysis.
        // -----------------------------------------------------------------
        private void BackfillHistoricalBars(int count)
        {
            if (Bars == null) return;
            int total = Bars.Count;
            if (total < 2) return;

            // Bars.Count - 1 is the still-forming current bar; back off one
            // so we only publish CLOSED bars.
            int endIdx   = total - 2;
            int startIdx = Math.Max(0, endIdx - count + 1);

            int published = 0;
            for (int i = startIdx; i <= endIdx; i++)
            {
                long ts;
                double o, h, l, c;
                long v;
                try
                {
                    ts = ToUnixSeconds(Bars.GetTime(i));
                    o  = Bars.GetOpen(i);
                    h  = Bars.GetHigh(i);
                    l  = Bars.GetLow(i);
                    c  = Bars.GetClose(i);
                    v  = (long)Bars.GetVolume(i);
                }
                catch
                {
                    // Skip bars NT refuses to deliver at absolute index;
                    // they're typically session-boundary placeholders.
                    continue;
                }

                string json = BuildBarJsonAt(ts, o, h, l, c, v);
                Task.Run(() => PostAsync(FEED_BAR_URL, json));
                published++;
            }
            Print($"[HelmFeed] backfill: posted {published} historical bar(s)");
        }

        private string BuildTicksJson(List<Tick> ticks)
        {
            string sym = Instrument.MasterInstrument.Name;
            var ic = CultureInfo.InvariantCulture;
            var sb = new StringBuilder(64 + ticks.Count * 56);
            sb.Append('{');
            sb.AppendFormat(ic, "\"instrument\":\"{0}\",", sym);
            sb.Append("\"ticks\":[");
            for (int i = 0; i < ticks.Count; i++)
            {
                if (i > 0) sb.Append(',');
                var t = ticks[i];
                sb.AppendFormat(ic,
                    "{{\"ts_ms\":{0},\"price\":{1},\"volume\":{2}}}",
                    t.TsMs, t.Price, t.Volume);
            }
            sb.Append("]}");
            return sb.ToString();
        }

        private string PeriodLabel()
        {
            var bp = BarsPeriod;
            int v = bp.Value;
            switch (bp.BarsPeriodType)
            {
                case BarsPeriodType.Minute:
                    if (v % 60 == 0) return (v / 60) + "h";
                    return v + "m";
                case BarsPeriodType.Second: return v + "s";
                case BarsPeriodType.Tick:   return v + "t";
                case BarsPeriodType.Day:    return v + "d";
                case BarsPeriodType.Week:   return v + "w";
                default:                    return bp.BarsPeriodType + ":" + v;
            }
        }

        private static long ToUnixSeconds(DateTime t)
        {
            return (long)(t.ToUniversalTime() - UNIX_EPOCH).TotalSeconds;
        }

        private static long ToUnixMillis(DateTime t)
        {
            return (long)(t.ToUniversalTime() - UNIX_EPOCH).TotalMilliseconds;
        }

        // =================================================================
        //  HTTP — fire-and-forget; swallow errors so the chart isn't disrupted
        // =================================================================
        private async Task PostAsync(string url, string json)
        {
            try
            {
                using (var content = new StringContent(json, Encoding.UTF8, "application/json"))
                using (var response = await Http.PostAsync(url, content))
                {
                    if (!response.IsSuccessStatusCode)
                    {
                        Print($"[HelmFeed] {url} → {(int)response.StatusCode} {response.ReasonPhrase}");
                    }
                }
            }
            catch (TaskCanceledException)
            {
                Print($"[HelmFeed] POST timed out after {HTTP_TIMEOUT_SECONDS}s. Dashboard running?");
            }
            catch (Exception ex)
            {
                Print($"[HelmFeed] POST failed: {ex.GetType().Name}: {ex.Message}");
            }
        }
    }
}
