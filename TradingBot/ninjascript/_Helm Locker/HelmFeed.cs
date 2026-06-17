// =====================================================================
//  HelmFeed — live bar/tick publisher + market-context dispatcher
//  Project: Lodestone & Purser / The Helm
//  Companion to: %USERPROFILE%\Documents\Projects\TradingBot\app
// =====================================================================
//
//  WHAT THIS DOES (merged HelmFeed + HelmAnalyzer, 2026-06-05)
//  ----------------------------------------------------------
//  One indicator per fed chart. Three streams flow to the dashboard
//  FastAPI on 127.0.0.1:8000:
//
//      POST /api/feed/bar         — one closed bar + screenshot + RICH
//                                   market context (auto path)
//      POST /api/feed/ticks       — batch of trade prints, every 250 ms
//      POST /api/capture-from-nt  — on hotkey (Ctrl+Shift+F): context +
//                                   screenshot for the manual snip flow
//
//  The rich context (EMA90, ADXR, Donchian, pivots, session levels, and
//  Smart-Money market structure at 3 retrace lenses) used to live only in
//  HelmAnalyzer (hotkey-only). It now rides every realtime bar close so the
//  AUTO analyzer reasons over the same verified context the manual path
//  gets -- closing the structure gap between the two paths.
//
//  Bars come via OnBarUpdate (Calculate.OnBarClose) on the primary series.
//  Ticks come via OnMarketData filtered to MarketDataType.Last. The 4 HTF
//  AddDataSeries (30m/1h/daily/weekly) feed the context builder only; their
//  OnBarUpdate calls (BarsInProgress != 0) are ignored.
//
//  Multi-chart safe: the bot dedupes bars on (instrument, period, ts) and
//  ticks on (instrument, ts_ms, price). The bot also dedupes the analysis
//  dispatch per bar, so a re-sent bar never double-fires.
//
//  Historical bars/ticks are skipped for publishing (only State.Realtime),
//  but the structure lenses ARE fed historical bars so structure is warm
//  before the first realtime publish.
//
//  INSTALL
//  -------
//  1. Copy to:
//       %USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\Indicators\_Helm Locker\HelmFeed.cs
//  2. NinjaScript Editor -> F5 to compile. (HelmAnalyzer.cs is retired;
//     its structure classes now live here -- keeping both would be a
//     duplicate-class CS0101 compile error.)
//  3. On any chart you want fed + analyzed: Indicators (Ctrl+I) -> HelmFeed.
//
//  NOTES FOR FUTURE EDITS
//  ----------------------
//    - HttpClient is lazy-initialized (static-field-initializer exceptions
//      silently exclude us from NS type discovery).
//    - Hand-rolled JSON to avoid pulling serializers into the NS sandbox.
//    - Instrument published as MasterInstrument.Name (e.g. "MES").
//    - AddDataSeries order MUST match the IDX_* constants.
// =====================================================================

#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media.Imaging;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    // =====================================================================
    //  StructureSwing — one confirmed pivot. Named StructureSwing (not
    //  "Swing") because NT8 ships a built-in `Swing` indicator in this
    //  namespace; a second `Swing` symbol is a CS0101 compile error.
    // =====================================================================
    public class StructureSwing
    {
        public string   Type;
        public string   Label;
        public double   Price;
        public DateTime Time;
        public bool     IsConfirmed;
    }

    // =====================================================================
    //  MarketStructureLens — one Smart-Money-Concepts state machine at one
    //  retrace sensitivity. While in an up leg we track the running high;
    //  the leg confirms (and the high becomes a swing high) when price
    //  retraces by RetracePct of the leg range. Labels HH/HL/LH/LL drive
    //  BOS (continuation) / CHoCH (reversal) events. See full notes in the
    //  pre-merge HelmAnalyzer history.
    // =====================================================================
    public class MarketStructureLens
    {
        public readonly double RetracePct;

        public string         Trend              = "Up";
        public string         Structure          = "Transitional";
        public string         LastStructureEvent = null;
        public double         BreakPrice         = 0.0;
        public StructureSwing LastSwing          = null;
        public StructureSwing LastConfirmedHigh  = null;
        public StructureSwing LastConfirmedLow   = null;

        private double   currentLegHigh = double.MinValue;
        private double   currentLegLow  = double.MaxValue;
        private DateTime currentLegHighTime;
        private DateTime currentLegLowTime;
        private double   legStartPrice;
        private bool     initialized = false;

        public MarketStructureLens(double retracePct)
        {
            RetracePct = retracePct;
        }

        public void OnBar(double high, double low, double close, DateTime barTime)
        {
            if (!initialized)
            {
                currentLegHigh     = high;
                currentLegLow      = low;
                currentLegHighTime = barTime;
                currentLegLowTime  = barTime;
                legStartPrice      = close;
                initialized        = true;
                return;
            }

            if (high > currentLegHigh) { currentLegHigh = high; currentLegHighTime = barTime; }
            if (low  < currentLegLow)  { currentLegLow  = low;  currentLegLowTime  = barTime; }

            if (Trend == "Up")
            {
                double legRange = currentLegHigh - legStartPrice;
                if (legRange <= 0) return;

                double retraceLevel = currentLegHigh - legRange * RetracePct;
                BreakPrice = retraceLevel;

                if (low <= retraceLevel)
                {
                    ConfirmSwingHigh();
                    Trend              = "Down";
                    legStartPrice      = currentLegHigh;
                    currentLegHigh     = high;
                    currentLegHighTime = barTime;
                    currentLegLow      = low;
                    currentLegLowTime  = barTime;
                }
            }
            else
            {
                double legRange = legStartPrice - currentLegLow;
                if (legRange <= 0) return;

                double retraceLevel = currentLegLow + legRange * RetracePct;
                BreakPrice = retraceLevel;

                if (high >= retraceLevel)
                {
                    ConfirmSwingLow();
                    Trend              = "Up";
                    legStartPrice      = currentLegLow;
                    currentLegLow      = low;
                    currentLegLowTime  = barTime;
                    currentLegHigh     = high;
                    currentLegHighTime = barTime;
                }
            }
        }

        private void ConfirmSwingHigh()
        {
            var s = new StructureSwing
            {
                Type        = "High",
                Price       = currentLegHigh,
                Time        = currentLegHighTime,
                IsConfirmed = true
            };

            bool isHH = (LastConfirmedHigh == null) || (s.Price > LastConfirmedHigh.Price);
            s.Label = isHH ? "HH" : "LH";

            if (isHH && LastConfirmedLow != null)
            {
                if      (LastConfirmedLow.Label == "HL") LastStructureEvent = "BullishBOS";
                else if (LastConfirmedLow.Label == "LL") LastStructureEvent = "BullishCHoCH";
            }

            LastSwing         = s;
            LastConfirmedHigh = s;
            RecomputeStructure();
        }

        private void ConfirmSwingLow()
        {
            var s = new StructureSwing
            {
                Type        = "Low",
                Price       = currentLegLow,
                Time        = currentLegLowTime,
                IsConfirmed = true
            };

            bool isLL = (LastConfirmedLow == null) || (s.Price < LastConfirmedLow.Price);
            s.Label = isLL ? "LL" : "HL";

            if (isLL && LastConfirmedHigh != null)
            {
                if      (LastConfirmedHigh.Label == "LH") LastStructureEvent = "BearishBOS";
                else if (LastConfirmedHigh.Label == "HH") LastStructureEvent = "BearishCHoCH";
            }

            LastSwing        = s;
            LastConfirmedLow = s;
            RecomputeStructure();
        }

        private void RecomputeStructure()
        {
            bool hasHH = LastConfirmedHigh != null && LastConfirmedHigh.Label == "HH";
            bool hasLH = LastConfirmedHigh != null && LastConfirmedHigh.Label == "LH";
            bool hasLL = LastConfirmedLow  != null && LastConfirmedLow.Label  == "LL";
            bool hasHL = LastConfirmedLow  != null && LastConfirmedLow.Label  == "HL";

            if (LastStructureEvent != null && LastStructureEvent.EndsWith("CHoCH"))
                Structure = "Transitional";
            else if (hasHH && hasHL)
                Structure = "Bullish";
            else if (hasLL && hasLH)
                Structure = "Bearish";
            else if ((hasLH && hasHL) || (hasHH && hasLL))
                Structure = "Range";
            else
                Structure = "Transitional";
        }
    }


    public class HelmFeed : Indicator
    {
        // =================================================================
        //  CONFIG
        // =================================================================
        private const string FEED_BAR_URL         = "http://127.0.0.1:8000/api/feed/bar";
        private const string FEED_TICKS_URL       = "http://127.0.0.1:8000/api/feed/ticks";
        private const string CAPTURE_URL          = "http://127.0.0.1:8000/api/capture-from-nt";
        private const int    HTTP_TIMEOUT_SECONDS = 5;
        private const int    FLUSH_INTERVAL_MS    = 250;
        private const int    BACKFILL_BAR_COUNT   = 100;

        // Hotkey: Ctrl+Shift+F (manual snip + analyze).
        private const Key          HOTKEY_KEY       = Key.F;
        private const ModifierKeys HOTKEY_MODIFIERS = ModifierKeys.Control | ModifierKeys.Shift;

        // Timeframe series indices. Order MUST match the AddDataSeries calls
        // in State.Configure.
        private const int IDX_PRIMARY = 0;
        private const int IDX_30M     = 1;
        private const int IDX_1H      = 2;
        private const int IDX_DAILY   = 3;
        private const int IDX_WEEKLY  = 4;

        // Structure lenses: 0.5 fast / 1.0 medium / 2.0 slow.
        private static readonly double[] LENS_RETRACE_PCTS = { 0.5, 1.0, 2.0 };
        private List<MarketStructureLens> lenses;

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

        // ChartControl + host Window: needed for the BitBlt capture and the
        // window-level hotkey listener (chart panels rarely hold focus).
        private NinjaTrader.Gui.Chart.ChartControl chartControl;
        private System.Windows.Window hostWindow;
        private bool hotkeyAttached = false;   // guard against double-subscribe on a Historical re-fire

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
            try { Print($"[HelmFeed] State -> {State}"); } catch { }

            if (State == State.SetDefaults)
            {
                Description              = "The Helm: publish closed bars + ticks + rich market context, and snip-on-hotkey, to the local bot.";
                Name                     = "HelmFeed";
                IsOverlay                = true;
                Calculate                = Calculate.OnBarClose;
                // false so an armed chart keeps feeding + emitting context even
                // when its tab is in the background (true suspended background
                // tabs -- the cause of "one instrument fed, the others didn't").
                IsSuspendedWhileInactive = false;
            }
            else if (State == State.Configure)
            {
                // HTF context series. Order MUST match the IDX_* constants.
                AddDataSeries(BarsPeriodType.Minute, 30);
                AddDataSeries(BarsPeriodType.Minute, 60);
                AddDataSeries(BarsPeriodType.Day, 1);
                AddDataSeries(BarsPeriodType.Week, 1);

                lenses = new List<MarketStructureLens>(LENS_RETRACE_PCTS.Length);
                foreach (var pct in LENS_RETRACE_PCTS)
                    lenses.Add(new MarketStructureLens(pct));
            }
            else if (State == State.DataLoaded)
            {
                flushTimer = new System.Threading.Timer(
                    _ => FlushTicks(), null, FLUSH_INTERVAL_MS, FLUSH_INTERVAL_MS);

                try { chartControl = ChartControl; }
                catch { /* unattached / non-chart context; capture stays null */ }
            }
            else if (State == State.Historical)
            {
                // Wire the hotkey at the host-window level (ChartControl
                // panels rarely hold keyboard focus). chartControl was
                // captured in DataLoaded.
                if (chartControl == null)
                {
                    try { chartControl = ChartControl; } catch { }
                }
                if (chartControl != null && !hotkeyAttached)
                {
                    chartControl.Dispatcher.InvokeAsync(() =>
                    {
                        try
                        {
                            hostWindow = System.Windows.Window.GetWindow(chartControl);
                            if (hostWindow != null)
                            {
                                hostWindow.PreviewKeyDown += OnChartKeyDown;
                                hotkeyAttached = true;
                                Print($"[HelmFeed] Hotkey listener attached to window: {hostWindow.Title}");
                            }
                            else
                            {
                                Print("[HelmFeed] WARNING: could not resolve host window -- hotkey will not fire.");
                            }
                        }
                        catch (Exception ex)
                        {
                            Print($"[HelmFeed] Failed to attach hotkey: {ex.Message}");
                        }
                    });
                }
            }
            else if (State == State.Realtime)
            {
                // Backfill last N closed bars so feed.db is warm immediately.
                // Bot tags stale ts and stores them WITHOUT triggering
                // analysis, so flooding bars doesn't fire N LLM calls.
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
                FlushTicks();   // final flush of anything buffered

                if (hostWindow != null)
                {
                    var hw = hostWindow;
                    hw.Dispatcher.InvokeAsync(() =>
                    {
                        try { hw.PreviewKeyDown -= OnChartKeyDown; } catch { }
                    });
                    hostWindow = null;
                }
                hotkeyAttached = false;
                chartControl = null;
            }
        }

        protected override void OnBarUpdate()
        {
            // Primary series only -- the 4 HTF series also fire here.
            if (BarsInProgress != IDX_PRIMARY) return;
            if (CurrentBar < 1) return;

            // Feed the structure lenses on EVERY closed bar (Historical +
            // Realtime) so SMC structure is warm before the first publish.
            if (lenses != null)
            {
                double lh = High[0], ll = Low[0], lc = Close[0];
                DateTime lt = Time[0];
                foreach (var lens in lenses) lens.OnBar(lh, ll, lc, lt);
            }

            // Publish only realtime closes (historical bars come via backfill).
            if (State != State.Realtime) return;

            string body;
            try { body = BuildBarJson(); }
            catch (Exception ex)
            {
                Print($"[HelmFeed] BuildBarJson failed: {ex.Message}");
                return;
            }

            // Strip the closing brace so we can append context + screenshot.
            body = body.Substring(0, body.Length - 1);

            // Rich NS context (best-effort: the bar still publishes without it).
            try { body += ",\"context\":" + BuildContextJson(); }
            catch (Exception ex) { Print($"[HelmFeed] BuildContextJson failed: {ex.Message}"); }

            // Chart bitmap for the vision LLM (latest per combo on the bot side).
            string shot = CaptureChartBase64();
            if (!string.IsNullOrEmpty(shot))
                body += ",\"screenshot_b64\":\"" + shot + "\"";

            string json = body + "}";
            Task.Run(() => PostAsync(FEED_BAR_URL, json));
        }

        protected override void OnMarketData(MarketDataEventArgs e)
        {
            // With the 4 HTF AddDataSeries, OnMarketData fires once per series
            // for the same instrument -- take only the primary so each trade
            // print is buffered once (the bot also dedupes, but don't 5x the
            // POST volume).
            if (BarsInProgress != IDX_PRIMARY) return;
            if (e.MarketDataType != MarketDataType.Last) return;
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
        //  HOTKEY -> manual snip + analyze (POST /api/capture-from-nt)
        // =================================================================
        private void OnChartKeyDown(object sender, KeyEventArgs e)
        {
            if (e.Key != HOTKEY_KEY) return;
            if (e.KeyboardDevice.Modifiers != HOTKEY_MODIFIERS) return;

            // Multi-tab guard: only the visible chart's instance handles the
            // hotkey (the listener is window-level, so every tab's instance
            // would otherwise race to POST).
            if (chartControl == null || !chartControl.IsVisible) return;

            e.Handled = true;
            Print($"[HelmFeed] Hotkey caught -- snapshot for {Instrument.FullName}");
            TriggerSnapshot();
        }

        private void TriggerSnapshot()
        {
            string screenshotB64 = CaptureChartBase64();

            string json;
            try { json = BuildContextJson(); }
            catch (Exception ex)
            {
                Print($"[HelmFeed] BuildContextJson failed: {ex.Message}");
                return;
            }

            if (!string.IsNullOrEmpty(screenshotB64))
            {
                json = json.Substring(0, json.Length - 1)
                     + ",\"screenshot_b64\":\"" + screenshotB64 + "\"}";
            }

            Task.Run(() => PostAsync(CAPTURE_URL, json));
            Print(screenshotB64 != null
                ? "[HelmFeed] Context POSTed with embedded chart bitmap."
                : "[HelmFeed] Context POSTed (no screenshot; bot falls back to snip overlay).");
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
        //  BAR / TICK JSON BUILDERS
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

        // Backfill runs once on State.Realtime. Plain bars only (no context /
        // screenshot) -- the bot recognizes stale ts and stores without
        // triggering analysis.
        private void BackfillHistoricalBars(int count)
        {
            if (Bars == null) return;
            int total = Bars.Count;
            if (total < 2) return;

            int endIdx   = total - 2;   // total-1 is the still-forming bar
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
                catch { continue; }

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

        // =================================================================
        //  CONTEXT JSON BUILDER (folded in from HelmAnalyzer)
        // =================================================================
        private string BuildContextJson()
        {
            var sb = new StringBuilder(1024);
            sb.Append('{');

            WriteKv(sb, "instrument", Instrument.FullName, leading: false);
            WriteKv(sb, "timestamp",  DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture));
            WriteKv(sb, "schema_version", "1");

            // Current price. Bid/ask pinned to the PRIMARY series (the
            // index-less overloads return whatever series BarsInProgress last
            // touched -> a stale/foreign quote). Fall back to last close.
            sb.Append(",\"current\":{");
            double bid  = GetCurrentBid(IDX_PRIMARY);
            double ask  = GetCurrentAsk(IDX_PRIMARY);
            double last = Closes[IDX_PRIMARY][0];
            WriteKv(sb, "bid",  bid > 0 ? bid : last, leading: false);
            WriteKv(sb, "ask",  ask > 0 ? ask : last);
            WriteKv(sb, "last", last);
            sb.Append('}');

            sb.Append(",\"timeframes\":{");
            AppendTimeframe(sb, "primary", IDX_PRIMARY, leading: false);
            AppendTimeframe(sb, "30m",     IDX_30M);
            AppendTimeframe(sb, "1h",      IDX_1H);
            AppendTimeframe(sb, "daily",   IDX_DAILY);
            AppendTimeframe(sb, "weekly",  IDX_WEEKLY);
            sb.Append('}');

            sb.Append(",\"daily_levels\":{");
            AppendPivots(sb);
            AppendSessionLevels(sb);
            sb.Append('}');

            sb.Append(",\"market_structure\":[");
            if (lenses != null)
            {
                bool first = true;
                foreach (var lens in lenses)
                {
                    if (!first) sb.Append(',');
                    AppendLens(sb, lens);
                    first = false;
                }
            }
            sb.Append(']');

            sb.Append('}');
            return sb.ToString();
        }

        // Per-timeframe block:
        //   "5m":{"ema90":4519.8,"adxr":27.4,...,"swing_high_20":4523.5}
        private void AppendTimeframe(StringBuilder sb, string label, int idx, bool leading = true)
        {
            if (leading) sb.Append(',');
            sb.Append('"').Append(label).Append("\":{");

            var bars = BarsArray[idx];

            // The user's chart stack uses ONLY EMA(90); adding lines invites
            // the LLM to cite levels not on screen.
            WriteKv(sb, "ema90",  EMA(bars,  90)[0],  leading: false);
            // ADXR (trend-strength rating) replaced ATR14 2026-06-05: the ATM
            // templates own stop/target sizing, so trend strength is the more
            // useful signal. >25 trending, <20 chop.
            WriteKv(sb, "adxr",   ADXR(bars, 14, 14)[0]);

            var dc = DonchianChannel(bars, 14);
            WriteKv(sb, "donchian_upper",  dc.Upper[0]);
            WriteKv(sb, "donchian_lower",  dc.Lower[0]);
            WriteKv(sb, "donchian_middle", dc.Mean[0]);

            WriteKv(sb, "swing_high_20", MAX(Highs[idx], 20)[0]);
            WriteKv(sb, "swing_low_20",  MIN(Lows[idx],  20)[0]);

            sb.Append('}');
        }

        // Per-lens block inside market_structure[].
        private void AppendLens(StringBuilder sb, MarketStructureLens lens)
        {
            sb.Append('{');
            WriteKv(sb, "retrace_pct",          lens.RetracePct, leading: false);
            WriteKvString(sb, "trend",          lens.Trend);
            WriteKvString(sb, "structure",      lens.Structure);
            WriteKvString(sb, "last_structure_event", lens.LastStructureEvent);
            WriteKv(sb, "break_price",          lens.BreakPrice);

            sb.Append(",\"last_swing\":");
            AppendSwing(sb, lens.LastSwing);
            sb.Append(",\"last_confirmed_high\":");
            AppendSwing(sb, lens.LastConfirmedHigh);
            sb.Append(",\"last_confirmed_low\":");
            AppendSwing(sb, lens.LastConfirmedLow);

            sb.Append('}');
        }

        private void AppendSwing(StringBuilder sb, StructureSwing s)
        {
            if (s == null) { sb.Append("null"); return; }
            sb.Append('{');
            WriteKvString(sb, "type",  s.Type,  leading: false);
            WriteKvString(sb, "label", s.Label);
            WriteKv(sb, "price", s.Price);
            WriteKvString(sb, "time", s.Time.ToString("o", CultureInfo.InvariantCulture));
            sb.Append(",\"is_confirmed\":").Append(s.IsConfirmed ? "true" : "false");
            sb.Append('}');
        }

        // Standard floor-trader pivots from yesterday's daily H/L/C. Manual
        // math (NT8's Pivots plot names vary across versions). Absolute
        // GetHigh/GetLow index (barsAgo can throw off the dispatcher thread).
        private void AppendPivots(StringBuilder sb)
        {
            try
            {
                var bars = BarsArray[IDX_DAILY];
                int n = bars.Count;
                if (n < 2) return;

                int yIdx = n - 2;
                double yh = bars.GetHigh(yIdx);
                double yl = bars.GetLow(yIdx);
                double yc = bars.GetClose(yIdx);
                double pp = (yh + yl + yc) / 3.0;
                double range = yh - yl;

                WriteKv(sb, "pivot_p",  Math.Round(pp,                  2), leading: false);
                WriteKv(sb, "pivot_r1", Math.Round(2 * pp - yl,         2));
                WriteKv(sb, "pivot_s1", Math.Round(2 * pp - yh,         2));
                WriteKv(sb, "pivot_r2", Math.Round(pp + range,          2));
                WriteKv(sb, "pivot_s2", Math.Round(pp - range,          2));
                WriteKv(sb, "pivot_r3", Math.Round(yh + 2 * (pp - yl),  2));
                WriteKv(sb, "pivot_s3", Math.Round(yl - 2 * (yh - pp),  2));
            }
            catch (Exception ex)
            {
                Print($"[HelmFeed] Pivots calculation failed: {ex.Message}");
            }
        }

        private void AppendSessionLevels(StringBuilder sb)
        {
            try
            {
                var bars = BarsArray[IDX_DAILY];
                int n = bars.Count;

                if (n >= 1)
                {
                    int tIdx = n - 1;
                    WriteKv(sb, "today_high", bars.GetHigh(tIdx));
                    WriteKv(sb, "today_low",  bars.GetLow(tIdx));
                }
                if (n >= 2)
                {
                    int yIdx = n - 2;
                    WriteKv(sb, "yesterday_high",  bars.GetHigh(yIdx));
                    WriteKv(sb, "yesterday_low",   bars.GetLow(yIdx));
                    WriteKv(sb, "yesterday_close", bars.GetClose(yIdx));
                }
            }
            catch (Exception ex)
            {
                Print($"[HelmFeed] Session levels unavailable: {ex.Message}");
            }
        }

        // =================================================================
        //  JSON helpers (invariant culture; NaN/Inf -> null)
        // =================================================================
        private static void WriteKv(StringBuilder sb, string key, double value, bool leading = true)
        {
            if (leading) sb.Append(',');
            sb.Append('"').Append(key).Append("\":");
            if (double.IsNaN(value) || double.IsInfinity(value))
                sb.Append("null");
            else
                sb.Append(value.ToString("0.########", CultureInfo.InvariantCulture));
        }

        private static void WriteKv(StringBuilder sb, string key, string value, bool leading = true)
        {
            if (leading) sb.Append(',');
            sb.Append('"').Append(key).Append("\":\"")
              .Append(EscapeJson(value))
              .Append('"');
        }

        // String key/value that emits `null` (not "") for null values.
        private static void WriteKvString(StringBuilder sb, string key, string value, bool leading = true)
        {
            if (leading) sb.Append(',');
            sb.Append('"').Append(key).Append("\":");
            if (value == null) sb.Append("null");
            else sb.Append('"').Append(EscapeJson(value)).Append('"');
        }

        private static string EscapeJson(string s)
        {
            if (string.IsNullOrEmpty(s)) return string.Empty;
            var sb = new StringBuilder(s.Length);
            foreach (var c in s)
            {
                switch (c)
                {
                    case '\\': sb.Append("\\\\"); break;
                    case '"':  sb.Append("\\\""); break;
                    case '\n': sb.Append("\\n");  break;
                    case '\r': sb.Append("\\r");  break;
                    case '\t': sb.Append("\\t");  break;
                    default:
                        if (c < 0x20) sb.AppendFormat("\\u{0:x4}", (int)c);
                        else          sb.Append(c);
                        break;
                }
            }
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
        //  CHART CAPTURE — desktop framebuffer at the chart's screen rect,
        //  via Win32 BitBlt + WPF PNG encode. Returns null if not ready.
        // =================================================================
        [DllImport("user32.dll")]  private static extern IntPtr GetDesktopWindow();
        [DllImport("user32.dll")]  private static extern IntPtr GetWindowDC(IntPtr hWnd);
        [DllImport("user32.dll")]  private static extern int    ReleaseDC(IntPtr hWnd, IntPtr hDC);
        [DllImport("gdi32.dll")]   private static extern IntPtr CreateCompatibleDC(IntPtr hdc);
        [DllImport("gdi32.dll")]   private static extern IntPtr CreateCompatibleBitmap(IntPtr hdc, int w, int h);
        [DllImport("gdi32.dll")]   private static extern IntPtr SelectObject(IntPtr hdc, IntPtr obj);
        [DllImport("gdi32.dll", SetLastError = true)]
                                   private static extern bool   BitBlt(IntPtr dst, int x, int y, int w, int h, IntPtr src, int sx, int sy, uint rop);
        [DllImport("gdi32.dll")]   private static extern bool   DeleteObject(IntPtr obj);
        [DllImport("gdi32.dll")]   private static extern bool   DeleteDC(IntPtr hdc);
        private const uint SRCCOPY = 0x00CC0020;

        private string CaptureChartBase64()
        {
            if (chartControl == null) return null;
            try
            {
                return (string)chartControl.Dispatcher.Invoke(new Func<string>(() =>
                {
                    if (!chartControl.IsVisible) return null;
                    int w = (int)chartControl.ActualWidth;
                    int h = (int)chartControl.ActualHeight;
                    if (w <= 0 || h <= 0) return null;

                    var origin = chartControl.PointToScreen(new System.Windows.Point(0, 0));
                    int sx = (int)origin.X;
                    int sy = (int)origin.Y;

                    IntPtr hwndDesk = GetDesktopWindow();
                    IntPtr srcDc    = GetWindowDC(hwndDesk);
                    IntPtr dstDc    = CreateCompatibleDC(srcDc);
                    IntPtr dib      = CreateCompatibleBitmap(srcDc, w, h);
                    IntPtr oldObj   = SelectObject(dstDc, dib);
                    try
                    {
                        if (!BitBlt(dstDc, 0, 0, w, h, srcDc, sx, sy, SRCCOPY)) return null;

                        var source = Imaging.CreateBitmapSourceFromHBitmap(
                            dib, IntPtr.Zero, Int32Rect.Empty,
                            BitmapSizeOptions.FromEmptyOptions());
                        var encoder = new PngBitmapEncoder();
                        encoder.Frames.Add(BitmapFrame.Create(source));
                        using (var ms = new MemoryStream())
                        {
                            encoder.Save(ms);
                            return Convert.ToBase64String(ms.ToArray());
                        }
                    }
                    finally
                    {
                        SelectObject(dstDc, oldObj);
                        DeleteObject(dib);
                        DeleteDC(dstDc);
                        ReleaseDC(hwndDesk, srcDc);
                    }
                }));
            }
            catch (Exception ex)
            {
                Print($"[HelmFeed] Chart capture failed: {ex.GetType().Name}: {ex.Message}");
                return null;
            }
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
                        Print($"[HelmFeed] {url} -> {(int)response.StatusCode} {response.ReasonPhrase}");
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
