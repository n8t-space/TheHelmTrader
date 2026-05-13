// =====================================================================
//  HelmAnalyzer — The Helm context dispatcher
//  Project: Lodestone & Purser / The Helm
//  Companion to: %USERPROFILE%\Documents\Projects\TradingBot\app
// =====================================================================
//
//  WHAT THIS DOES
//  --------------
//  This is a NinjaTrader 8 *Indicator* (not a Strategy) that snapshots
//  your chart's higher-timeframe context and standard indicator values,
//  then POSTs them as JSON to the The Helm Flask app running on
//  http://127.0.0.1:5000 — but only when YOU press the hotkey.
//
//  It does NOT:
//    - run continuously (you said no always-listening)
//    - place trades (it's an Indicator, not a Strategy)
//    - reach the internet (the only network call is to localhost)
//
//  WORKFLOW
//  --------
//    1. You attach HelmAnalyzer to a chart (any chart on the
//       instrument you want to analyze).
//    2. With that chart focused, you press the hotkey
//       (default: Ctrl+Shift+F — see HOTKEY_KEY / HOTKEY_MODIFIERS).
//    3. NS reads its multi-timeframe series in memory, formats a JSON
//       payload, and POSTs to the bot at HELM_API_URL.
//    4. The bot opens the Windows Snipping overlay; you drag a
//       rectangle around the chart; the bot's pipeline runs the
//       vision LLM with your image AND the numeric context the
//       indicator just sent.
//    5. The new signal appears on the dashboard.
//
//  HIGHER-TIMEFRAME DATA
//  ---------------------
//  NinjaScript supports subscribing to additional timeframes inside
//  one indicator via AddDataSeries(). The chart's native timeframe is
//  always BarsInProgress index 0. Each AddDataSeries() call adds
//  another series at the next index. We add: 30m, 1h, daily, weekly.
//
//  We DO NOT calculate anything in OnBarUpdate — instead, we let
//  NinjaScript's indicator-on-series pattern auto-update the values,
//  and we read them on demand at trigger time. This keeps the
//  indicator cheap (no work per bar) and makes the trigger snapshot
//  reflect the latest possible values.
//
//  INDICATORS COVERED (v1)
//  -----------------------
//  Per timeframe (5m primary / 30m / 1h / daily / weekly):
//    - EMA(20), EMA(50), EMA(90), EMA(200)
//    - ATR(14)
//    - Donchian Channel (14) — upper / lower / middle (NT8 calls
//      the midline `Mean` rather than `Middle`)
//    - 20-bar swing high / swing low
//
//  VWAP is intentionally NOT emitted in v1: NinjaTrader 8 does not
//  ship a plain `VWAP` indicator in its base API (OrderFlowVWAP needs
//  the Order Flow license). To add: drop a free VWAP NinjaScript file
//  into bin\Custom\Indicators\, recompile, and emit per intraday TF.
//
//  Daily-derived:
//    - Pivots (P, R1, R2, R3, S1, S2, S3) — computed MANUALLY from
//      yesterday's H/L/C using standard floor-trader formulas. We
//      skip NT8's built-in Pivots indicator because its plot-property
//      names (PP vs PivotPoint vs Values[i]) differ across NT8
//      versions; manual math is portable.
//    - Today's session high / low (from the live daily bar)
//    - Yesterday's high / low / close
//
//  Market structure (SMC, 3 retrace lenses at 0.5 / 1.0 / 2.0):
//    - Per-lens trend (Up/Down), structure (Bullish/Bearish/Range/
//      Transitional), last structure event (BullishBOS / BearishBOS /
//      BullishCHoCH / BearishCHoCH), break price, and the last
//      confirmed swings on each side. See MarketStructureLens class
//      doc comment above for the algorithm.
//
//  Custom indicators on your chart that aren't covered here
//  (Order Flow Trade Detector, Overnight_High_Low_jay, etc.) need
//  separate handling — either share their source so we can compute
//  them, or expose their values via NinjaScript's indicator API.
//  See the comment block above BuildContextJson() for how to add
//  more.
//
//  INSTALL
//  -------
//  1. Copy this file to:
//       %USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\Indicators\_Helm Locker\HelmAnalyzer.cs
//  2. In NinjaTrader: New → NinjaScript Editor → Compile (F5)
//     — NS will tell you about any syntax errors at the bottom of
//     the editor window.
//  3. On any chart of an instrument you want to analyze:
//     Indicators (Ctrl+I) → HelmAnalyzer → Apply.
//  4. Click on the chart so it has keyboard focus, then press
//     Ctrl+Shift+F to trigger.
//
//  NOTES FOR FUTURE EDITS
//  ----------------------
//    - HttpClient is static so we don't leak sockets across triggers.
//    - PreviewKeyDown (not KeyDown) so the hotkey fires before NT's
//      own bindings can swallow it.
//    - All work is fire-and-forget: we don't block the chart UI
//      waiting for the bot's response. The bot processes async.
//    - JSON is hand-built rather than serialized via a library to
//      avoid pulling in extra dependencies into NinjaScript's
//      sandbox. The shape is simple enough that this is fine.
// =====================================================================

#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui.Chart;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    // =====================================================================
    //  StructureSwing — small POCO representing one confirmed pivot.
    //
    //  Named StructureSwing rather than the more obvious "Swing" because
    //  NT8 already ships a built-in indicator called `Swing` in this same
    //  namespace, and dropping a second `Swing` symbol here causes
    //  CS0101 ("namespace already contains a definition") at compile time.
    //
    //  Type:  "High" or "Low"
    //  Label: HH / HL / LH / LL  (or null for the very first swing where
    //         we have nothing prior to compare against)
    //  IsConfirmed: always true once it lands here; the field exists so
    //         the JSON output mirrors the source indicator's schema.
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
    //  MarketStructureLens — a single Smart-Money-Concepts state machine
    //  at one retrace sensitivity.
    //
    //  HOW IT WORKS
    //  ------------
    //  At any moment we are in either an "Up leg" (Trend = Up) or a "Down
    //  leg" (Trend = Down). While in an up leg we track the running high
    //  (currentLegHigh). The leg is over — and the running high is
    //  CONFIRMED as a swing high — when price retraces by RetracePct of
    //  the leg's range from its start point.
    //
    //    legRange    = currentLegHigh - legStartPrice
    //    retraceLevel = currentLegHigh - legRange * RetracePct
    //    confirm when low <= retraceLevel
    //
    //  RetracePct semantics:
    //    0.5 — leg confirmed at 50% retrace (sensitive, lots of swings)
    //    1.0 — leg confirmed at 100% retrace (price fully gives back leg)
    //    2.0 — leg confirmed at 200% retrace (deep reversal required)
    //
    //  LABELS at swing confirmation:
    //    HH — new swing high higher than the prior confirmed high
    //    LH — new swing high lower-than-or-equal to prior
    //    LL — new swing low lower than prior confirmed low
    //    HL — new swing low higher-than-or-equal to prior
    //
    //  STRUCTURE EVENTS (BOS = continuation, CHoCH = reversal):
    //    HH after HL  ->  BullishBOS    (uptrend continuation)
    //    HH after LL  ->  BullishCHoCH  (reversal from down to up)
    //    LL after LH  ->  BearishBOS    (downtrend continuation)
    //    LL after HH  ->  BearishCHoCH  (reversal from up to down)
    //    LH or HL alone — no structure event (just notes inside a range)
    //
    //  This is a simplified rule set vs. the reference indicator which
    //  also fires structure events MID-LEG when price breaks a prior
    //  confirmed swing before its own retrace completes. We trade some
    //  fidelity for code clarity — easy to tighten later.
    // =====================================================================
    public class MarketStructureLens
    {
        public readonly double RetracePct;

        // Snapshot fields read by the JSON serializer at trigger time
        public string   Trend              = "Up";              // "Up" or "Down"
        public string   Structure          = "Transitional";    // Bullish / Bearish / Range / Transitional
        public string   LastStructureEvent = null;              // BullishBOS / BearishBOS / BullishCHoCH / BearishCHoCH / null
        public double   BreakPrice         = 0.0;               // the retrace level that would currently confirm the in-progress leg
        public StructureSwing    LastSwing          = null;              // most recent confirmed swing (either side)
        public StructureSwing    LastConfirmedHigh  = null;
        public StructureSwing    LastConfirmedLow   = null;

        // Internal leg tracking
        private double   currentLegHigh     = double.MinValue;
        private double   currentLegLow      = double.MaxValue;
        private DateTime currentLegHighTime;
        private DateTime currentLegLowTime;
        private double   legStartPrice;
        private bool     initialized        = false;

        public MarketStructureLens(double retracePct)
        {
            RetracePct = retracePct;
        }

        /// <summary>
        ///   Feed one closed bar to the lens. Updates running extremes,
        ///   fires confirmations + structure events when retrace targets
        ///   are hit, and flips the leg direction.
        /// </summary>
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

            // Extend leg extremes as the running max/min
            if (high > currentLegHigh) { currentLegHigh = high; currentLegHighTime = barTime; }
            if (low  < currentLegLow)  { currentLegLow  = low;  currentLegLowTime  = barTime; }

            if (Trend == "Up")
            {
                double legRange = currentLegHigh - legStartPrice;
                if (legRange <= 0) return;  // no positive leg yet

                double retraceLevel = currentLegHigh - legRange * RetracePct;
                BreakPrice = retraceLevel;

                if (low <= retraceLevel)
                {
                    ConfirmSwingHigh();
                    // Flip into a Down leg starting from the just-confirmed high
                    Trend              = "Down";
                    legStartPrice      = currentLegHigh;
                    currentLegHigh     = high;
                    currentLegHighTime = barTime;
                    currentLegLow      = low;
                    currentLegLowTime  = barTime;
                }
            }
            else  // Trend == "Down"
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

            // Structure event: only HHs trigger one. The prior LOW's label
            // tells us whether this HH is a continuation (HL) or a reversal (LL).
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

        // -----------------------------------------------------------------
        //  Structure state derivation:
        //    Bullish      — uptrend with a HH+HL pattern
        //    Bearish      — downtrend with a LL+LH pattern
        //    Range        — last confirmed pair is HL+LH (consolidation)
        //    Transitional — most recent event was a CHoCH (state flipping)
        // -----------------------------------------------------------------
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



    public class HelmAnalyzer : Indicator
    {
        // =================================================================
        //  CONFIG — change these if your local setup differs
        // =================================================================

        /// <summary>
        ///   The The Helm bot's local endpoint. The bot binds to
        ///   127.0.0.1 only (loopback), so this never leaves your machine.
        ///   Port 8000 = Trade_Perf FastAPI (unified dashboard, post-merge).
        /// </summary>
        private const string HELM_API_URL = "http://127.0.0.1:8000/api/capture-from-nt";

        /// <summary>
        ///   How long to wait for the bot to acknowledge the POST before
        ///   giving up. The bot returns immediately (it triggers the snip
        ///   tool then processes async), so 5s is plenty.
        /// </summary>
        private const int HTTP_TIMEOUT_SECONDS = 5;

        /// <summary>
        ///   Hotkey: Ctrl+Shift+F. Adjust if these collide with anything
        ///   you use in NinjaTrader.
        /// </summary>
        private const Key HOTKEY_KEY = Key.F;
        private const ModifierKeys HOTKEY_MODIFIERS = ModifierKeys.Control | ModifierKeys.Shift;

        // =================================================================
        //  TIMEFRAME SERIES INDICES
        //  ------------------------------------------------------------------
        //  The chart's native period is always BarsInProgress index 0
        //  (Closes[0], Highs[0], etc.). Each AddDataSeries() call in
        //  State.SetDefaults adds another series at the next index, in the
        //  order they were added. Keep this constants list in sync with the
        //  AddDataSeries calls below or bad things happen silently.
        // =================================================================
        private const int IDX_PRIMARY = 0;  // chart's native timeframe (typically 5m for this user)
        private const int IDX_30M     = 1;
        private const int IDX_1H      = 2;
        private const int IDX_DAILY   = 3;
        private const int IDX_WEEKLY  = 4;

        // =================================================================
        //  MARKET STRUCTURE LENSES
        //  ------------------------------------------------------------------
        //  Each lens is an independent SMC state machine running at a
        //  different retrace sensitivity. 0.5 = fast (lots of swings,
        //  intraday rhythm), 1.0 = medium (primary trade structure), 2.0
        //  = slow (macro, only deep reversals). Output is one block per
        //  lens in the JSON payload — the LLM gets to see when fast and
        //  slow lenses disagree, which is itself a meaningful signal.
        // =================================================================
        private static readonly double[] LENS_RETRACE_PCTS = { 0.5, 1.0, 2.0 };
        private List<MarketStructureLens> lenses;

        // =================================================================
        //  INTERNAL STATE
        // =================================================================

        // One HttpClient for the lifetime of the process. Lazy-initialized
        // on first use rather than as a static field initializer — type-load
        // exceptions in NinjaScript's sandbox can silently exclude the
        // indicator from NT's discovery scan, so we keep type init trivial.
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

        // We keep references to ChartControl AND its host WPF Window. NT8
        // routes keystrokes through the parent Window, so attaching the
        // PreviewKeyDown handler to ChartControl directly is unreliable —
        // the chart panel rarely holds keyboard focus. We hook the Window
        // and filter so the keystroke only fires when this chart's tab
        // is the visible/focused one.
        private NinjaTrader.Gui.Chart.ChartControl chartControl;
        private System.Windows.Window hostWindow;

        // =================================================================
        //  LIFECYCLE — OnStateChange runs at every state transition.
        //  We use:
        //    - SetDefaults  : declare property defaults only. NT calls this
        //                     once at type-discovery time WITHOUT an
        //                     instrument context, so anything that touches
        //                     bars (AddDataSeries, indicator factories) MUST
        //                     NOT live here — exceptions during discovery
        //                     silently exclude the indicator from NT's
        //                     picker list.
        //    - Configure    : per-instance setup. Subscribe to extra
        //                     timeframes here (NT now has an instrument).
        //    - DataLoaded   : (nothing — indicators are referenced lazily
        //                     in BuildContextJson via the EMA(...)/ATR(...)
        //                     factory calls, which is the NS idiomatic way)
        //    - Historical   : ChartControl is now available — wire hotkey
        //    - Terminated   : unwire hotkey to avoid leaks
        // =================================================================
        protected override void OnStateChange()
        {
            // Diagnostic: every state transition prints to NinjaScript Output.
            // Helps confirm Configure / Historical / Terminated all run.
            try { Print($"[Helm] State → {State}"); } catch { }

            if (State == State.SetDefaults)
            {
                Description    = "The Helm: snapshot HTF context + indicators and POST to local bot on hotkey.";
                Name           = "HelmAnalyzer";
                IsOverlay      = true;     // doesn't draw anything but visible in chart's indicator stack
                Calculate      = Calculate.OnBarClose;
                IsSuspendedWhileInactive = true;
            }
            else if (State == State.Configure)
            {
                // Subscribe to higher timeframes. Order MUST match the
                // IDX_* constants above. NinjaScript will fetch historical
                // bars for each at chart load time.
                AddDataSeries(BarsPeriodType.Minute, 30);
                AddDataSeries(BarsPeriodType.Minute, 60);
                AddDataSeries(BarsPeriodType.Day, 1);
                AddDataSeries(BarsPeriodType.Week, 1);

                // Allocate one lens per configured retrace sensitivity.
                // Configure (not SetDefaults) is the right place — we now
                // have an instrument context, and the lenses don't need
                // bar data yet (they'll get fed in OnBarUpdate).
                lenses = new List<MarketStructureLens>(LENS_RETRACE_PCTS.Length);
                foreach (var pct in LENS_RETRACE_PCTS)
                    lenses.Add(new MarketStructureLens(pct));
            }
            else if (State == State.Historical)
            {
                // ChartControl becomes available here. We walk up the WPF
                // visual tree from ChartControl to find the host Window
                // and attach our key listener there — chart panels rarely
                // hold focus directly so a ChartControl-level hook misses
                // most keystrokes.
                if (ChartControl != null)
                {
                    chartControl = ChartControl;
                    chartControl.Dispatcher.InvokeAsync(() =>
                    {
                        try
                        {
                            hostWindow = System.Windows.Window.GetWindow(chartControl);
                            if (hostWindow != null)
                            {
                                hostWindow.PreviewKeyDown += OnChartKeyDown;
                                Print($"[Helm] Hotkey listener attached to window: {hostWindow.Title}");
                            }
                            else
                            {
                                Print("[Helm] WARNING: Could not resolve host window — hotkey will not fire.");
                            }
                        }
                        catch (Exception ex)
                        {
                            Print($"[Helm] Failed to attach hotkey: {ex.Message}");
                        }
                    });
                }
            }
            else if (State == State.Terminated)
            {
                if (hostWindow != null)
                {
                    var hw = hostWindow;
                    hw.Dispatcher.InvokeAsync(() =>
                    {
                        try { hw.PreviewKeyDown -= OnChartKeyDown; } catch { }
                    });
                    hostWindow = null;
                }
                chartControl = null;
            }
        }

        // OnBarUpdate runs on every bar close. Built-in indicators (EMA,
        // ATR, Donchian) are still read lazily at trigger time — we don't
        // touch them here. The work in this method is feeding closed bars
        // to each MarketStructureLens so its state stays current.
        //
        // We only feed the PRIMARY series. Multi-timeframe series fire
        // OnBarUpdate too (with BarsInProgress != 0) — we ignore those
        // because the lenses operate on the chart's native timeframe.
        protected override void OnBarUpdate()
        {
            if (BarsInProgress != IDX_PRIMARY) return;
            if (CurrentBar < 1)               return;  // need a previous bar for retrace math
            if (lenses == null)               return;  // shouldn't happen, but defensive

            double h = High[0];
            double l = Low[0];
            double c = Close[0];
            DateTime t = Time[0];

            foreach (var lens in lenses)
                lens.OnBar(h, l, c, t);
        }

        // =================================================================
        //  HOTKEY HANDLER
        //  Fires for every keystroke routed through the host window. We
        //  filter for the configured chord; everything else is left alone
        //  so we don't break NT's own keyboard shortcuts.
        //
        //  Diagnostic Print on every keypress (commented out by default —
        //  uncomment to verify keystrokes are reaching this handler at all).
        // =================================================================
        private void OnChartKeyDown(object sender, KeyEventArgs e)
        {
            // Diagnostic — uncomment if hotkey isn't firing to see what
            // the handler is actually receiving:
            // Print($"[Helm] KeyDown: {e.Key}  Mods: {e.KeyboardDevice.Modifiers}");

            if (e.Key != HOTKEY_KEY) return;
            if (e.KeyboardDevice.Modifiers != HOTKEY_MODIFIERS) return;

            // Multi-tab guard: each instance only handles the hotkey when
            // its own chart is currently visible. The hotkey listener is
            // attached at the window level, so without this check every
            // indicator instance in the window's tabs races to POST and
            // whichever wrote last to market_context.json wins — even if
            // its tab isn't the one the user is looking at.
            if (chartControl == null || !chartControl.IsVisible) return;

            e.Handled = true;  // tell WPF/NT we consumed this keystroke
            Print($"[Helm] Hotkey caught — triggering snapshot for {Instrument.FullName}");
            TriggerSnapshot();
        }

        // =================================================================
        //  TRIGGER — capture the chart bitmap, build the JSON with the
        //  screenshot embedded, POST it. Bot uses the embedded image
        //  directly -- no Snipping overlay needed. Bypasses the Session-0
        //  URI-handler issue that breaks ms-screenclip: when uvicorn runs
        //  as a service.
        // =================================================================
        private void TriggerSnapshot()
        {
            // Capture FIRST -- while the user's chart is still visible.
            // Returns null on failure; bot will fall back to snip overlay
            // (legacy path).
            string screenshotB64 = CaptureChartBase64();

            string json;
            try
            {
                json = BuildContextJson();
            }
            catch (Exception ex)
            {
                Print($"[Helm] BuildContextJson failed: {ex.Message}");
                return;
            }

            // Surgical insertion: the JSON ends in '}'; replace that with
            // ',"screenshot_b64":"<b64>"}' so we don't touch BuildContextJson.
            if (!string.IsNullOrEmpty(screenshotB64))
            {
                json = json.Substring(0, json.Length - 1)
                     + ",\"screenshot_b64\":\"" + screenshotB64 + "\"}";
            }

            // Fire-and-forget POST. UI thread is free to do whatever.
            Task.Run(() => PostAsync(json));
            Print(screenshotB64 != null
                ? "[Helm] Context POSTed with embedded chart bitmap."
                : "[Helm] Context POSTed (no screenshot; bot will fall back to snip overlay).");
        }

        // =================================================================
        //  CHART CAPTURE — grab from the screen via System.Drawing.
        //  ----------------------------------------------------------------
        //  We previously tried WPF's RenderTargetBitmap.Render(chartControl)
        //  but it produces a 4-KB mostly-blank PNG -- NT8's chart paints
        //  via DirectX (an HWND child of the WPF tree), so WPF's render
        //  pipeline can't see the D3D surface beneath it.
        //
        //  Graphics.CopyFromScreen reads the actual desktop framebuffer
        //  at the chart's on-screen coordinates, which DOES include the
        //  D3D content. Requires the chart to be on-screen at the moment
        //  of capture -- enforced by the IsVisible check in the hotkey
        //  handler that calls us.
        // =================================================================
        private string CaptureChartBase64()
        {
            if (chartControl == null) return null;
            try
            {
                return (string)chartControl.Dispatcher.Invoke(new Func<string>(() =>
                {
                    int w = (int)chartControl.ActualWidth;
                    int h = (int)chartControl.ActualHeight;
                    if (w <= 0 || h <= 0) return null;

                    // WPF point (chart-local 0,0) -> screen coords. Honors
                    // DPI scaling, multi-monitor offsets, and window position.
                    var origin = chartControl.PointToScreen(new System.Windows.Point(0, 0));
                    int sx = (int)origin.X;
                    int sy = (int)origin.Y;

                    using (var bmp = new System.Drawing.Bitmap(w, h, System.Drawing.Imaging.PixelFormat.Format32bppArgb))
                    using (var g = System.Drawing.Graphics.FromImage(bmp))
                    {
                        g.CopyFromScreen(sx, sy, 0, 0, new System.Drawing.Size(w, h),
                                         System.Drawing.CopyPixelOperation.SourceCopy);
                        using (var ms = new MemoryStream())
                        {
                            bmp.Save(ms, System.Drawing.Imaging.ImageFormat.Png);
                            return Convert.ToBase64String(ms.ToArray());
                        }
                    }
                }));
            }
            catch (Exception ex)
            {
                Print($"[Helm] Chart capture failed: {ex.GetType().Name}: {ex.Message}");
                return null;
            }
        }

        private async Task PostAsync(string json)
        {
            try
            {
                using (var content = new StringContent(json, Encoding.UTF8, "application/json"))
                using (var response = await Http.PostAsync(HELM_API_URL, content))
                {
                    if (!response.IsSuccessStatusCode)
                    {
                        Print($"[Helm] Bot returned {(int)response.StatusCode} {response.ReasonPhrase}");
                    }
                }
            }
            catch (TaskCanceledException)
            {
                Print($"[Helm] POST timed out after {HTTP_TIMEOUT_SECONDS}s. Is the dashboard running?");
            }
            catch (Exception ex)
            {
                Print($"[Helm] POST failed: {ex.GetType().Name}: {ex.Message}");
                var inner = ex.InnerException;
                while (inner != null)
                {
                    Print($"[Helm]   inner ({inner.GetType().Name}): {inner.Message}");
                    inner = inner.InnerException;
                }
            }
        }

        // =================================================================
        //  JSON BUILDER
        //  ------------------------------------------------------------------
        //  Hand-rolled JSON to avoid pulling extra serializers into the NS
        //  sandbox. The shape is small + flat enough that this is fine.
        //
        //  How to add an indicator:
        //    1. If it's a built-in NS indicator (EMA, RSI, MACD, etc.),
        //       just call EMA(BarsArray[IDX_x], period)[0] and write
        //       the value via WriteKv. The Bars array index picks the
        //       timeframe; period is the indicator's parameter.
        //    2. If it's a custom indicator with source code available,
        //       drop its source into Documents\NinjaTrader 8\bin\Custom\
        //       Indicators\, recompile, then call it the same way.
        //    3. If it's a custom indicator from another vendor with no
        //       source, we can sometimes reach it via
        //       NinjaTrader.NinjaScript.Indicators.<Namespace>.<Name>(...)
        //       — depends on whether they exposed it publicly.
        //
        //  How to add a timeframe:
        //    1. Add an IDX_* constant.
        //    2. Add an AddDataSeries() call in SetDefaults (in order!).
        //    3. Emit a new tf block below.
        // =================================================================
        private string BuildContextJson()
        {
            var sb = new StringBuilder(1024);
            sb.Append('{');

            // --- top-level metadata ---
            // NOTE: first key after the opening { MUST pass leading: false
            // so we don't emit a stray leading comma like `{,"instrument":...`.
            WriteKv(sb, "instrument", Instrument.FullName, leading: false);
            WriteKv(sb, "timestamp",  DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture));
            WriteKv(sb, "schema_version", "1");

            // --- current price (use the most recent close on primary
            //     timeframe; bid/ask are only available on live data,
            //     so we fall back to last close if those are zero). ---
            sb.Append(",\"current\":{");
            double bid  = GetCurrentBid();
            double ask  = GetCurrentAsk();
            double last = Closes[IDX_PRIMARY][0];
            WriteKv(sb, "bid",  bid > 0 ? bid : last, leading: false);
            WriteKv(sb, "ask",  ask > 0 ? ask : last);
            WriteKv(sb, "last", last);
            sb.Append('}');

            // --- per-timeframe blocks ---
            sb.Append(",\"timeframes\":{");
            AppendTimeframe(sb, "primary", IDX_PRIMARY, leading: false);
            AppendTimeframe(sb, "30m",     IDX_30M);
            AppendTimeframe(sb, "1h",      IDX_1H);
            AppendTimeframe(sb, "daily",   IDX_DAILY);
            AppendTimeframe(sb, "weekly",  IDX_WEEKLY);
            sb.Append('}');

            // --- daily-derived levels ---
            sb.Append(",\"daily_levels\":{");
            AppendPivots(sb);
            AppendSessionLevels(sb);
            sb.Append('}');

            // --- SMC market structure (one block per lens) ---
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

        // -----------------------------------------------------------------
        //  Per-lens block:
        //  emits one object inside market_structure[] of the form:
        //    {"retrace_pct":1.0,
        //     "trend":"Up","structure":"Bullish",
        //     "last_structure_event":"BullishBOS","break_price":4517.5,
        //     "last_swing":      {"type":"High","label":"HH",...},
        //     "last_confirmed_high":{"label":"HH","price":4525.0,...},
        //     "last_confirmed_low": {"label":"HL","price":4517.5,...}}
        // -----------------------------------------------------------------
        private void AppendLens(StringBuilder sb, MarketStructureLens lens)
        {
            sb.Append('{');
            WriteKv(sb, "retrace_pct",          lens.RetracePct,          leading: false);
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
            if (s == null)
            {
                sb.Append("null");
                return;
            }
            sb.Append('{');
            WriteKvString(sb, "type",  s.Type,  leading: false);
            WriteKvString(sb, "label", s.Label);
            WriteKv(sb, "price", s.Price);
            WriteKvString(sb, "time", s.Time.ToString("o", CultureInfo.InvariantCulture));
            sb.Append(",\"is_confirmed\":").Append(s.IsConfirmed ? "true" : "false");
            sb.Append('}');
        }

        // String key/value that emits `null` for null values rather than
        // an empty quoted string. (The plain WriteKv string overload below
        // does not handle nulls — adding this variant rather than changing
        // existing behavior to keep call sites predictable.)
        private static void WriteKvString(StringBuilder sb, string key, string value, bool leading = true)
        {
            if (leading) sb.Append(',');
            sb.Append('"').Append(key).Append("\":");
            if (value == null)
            {
                sb.Append("null");
            }
            else
            {
                sb.Append('"').Append(EscapeJson(value)).Append('"');
            }
        }

        // -----------------------------------------------------------------
        //  Per-timeframe block:
        //  emits e.g. "5m":{"ema90":4519.8,"atr14":1.25,...,"swing_high_20":4523.5}
        // -----------------------------------------------------------------
        private void AppendTimeframe(StringBuilder sb, string label, int idx, bool leading = true)
        {
            if (leading) sb.Append(',');
            sb.Append('"').Append(label).Append("\":{");

            var bars = BarsArray[idx];

            // The user's chart stack uses ONLY EMA(90); 20/50/200 dropped
            // 2026-05-09 to keep the bot's emitted context strictly ⊆ what
            // appears on screen. Adding more lines here invites the LLM to
            // cite levels the user can't visually validate.
            WriteKv(sb, "ema90",  EMA(bars,  90)[0],  leading: false);
            WriteKv(sb, "atr14",  ATR(bars,  14)[0]);

            // Donchian Channel — NT8's class exposes Upper / Lower / Mean
            // (the "Mean" plot is the midline; same value as (upper+lower)/2
            // but using the indicator's own series stays consistent with
            // however a future NT version draws it).
            var dc = DonchianChannel(bars, 14);
            WriteKv(sb, "donchian_upper",  dc.Upper[0]);
            WriteKv(sb, "donchian_lower",  dc.Lower[0]);
            WriteKv(sb, "donchian_middle", dc.Mean[0]);

            // Recent 20-bar swing high/low (cheap, no extra indicator)
            WriteKv(sb, "swing_high_20", MAX(Highs[idx], 20)[0]);
            WriteKv(sb, "swing_low_20",  MIN(Lows[idx],  20)[0]);

            // (VWAP intentionally omitted — see top-of-file note.)

            sb.Append('}');
        }

        // -----------------------------------------------------------------
        //  Standard floor-trader pivots, computed MANUALLY from yesterday's
        //  H/L/C on the daily series. We skip NinjaTrader's built-in Pivots
        //  indicator because its plot-property names (PP vs PivotPoint vs
        //  Values[i]) differ across NT8 versions; manual math is portable.
        //
        //  IMPORTANT: We use Bars.GetHigh(absoluteIndex) instead of the
        //  barsAgo indexer (e.g. Highs[i][1]). The barsAgo path can throw
        //  "'barsAgo' needed to be between 0 and N" when called from outside
        //  OnBarUpdate (our hotkey handler runs on the WPF dispatcher
        //  thread). GetHigh / GetLow / GetClose with absolute indices read
        //  the underlying bar storage directly and don't have that issue.
        //
        //  Formulas (Hyd / Lyd / Cyd = yesterday's high / low / close):
        //    PP = (Hyd + Lyd + Cyd) / 3                          pivot point
        //    R1 = 2*PP - Lyd          S1 = 2*PP - Hyd            1st levels
        //    R2 = PP + (Hyd - Lyd)    S2 = PP - (Hyd - Lyd)      2nd levels
        //    R3 = Hyd + 2*(PP - Lyd)  S3 = Lyd - 2*(Hyd - PP)    3rd levels
        // -----------------------------------------------------------------
        private void AppendPivots(StringBuilder sb)
        {
            try
            {
                var bars = BarsArray[IDX_DAILY];
                int n = bars.Count;
                if (n < 2) return;  // need yesterday's closed bar

                int yIdx = n - 2;  // second-to-last bar = yesterday
                double yh = bars.GetHigh(yIdx);
                double yl = bars.GetLow(yIdx);
                double yc = bars.GetClose(yIdx);
                double pp = (yh + yl + yc) / 3.0;
                double range = yh - yl;

                WriteKv(sb, "pivot_p",  pp,                       leading: false);
                WriteKv(sb, "pivot_r1", 2 * pp - yl);
                WriteKv(sb, "pivot_s1", 2 * pp - yh);
                WriteKv(sb, "pivot_r2", pp + range);
                WriteKv(sb, "pivot_s2", pp - range);
                WriteKv(sb, "pivot_r3", yh + 2 * (pp - yl));
                WriteKv(sb, "pivot_s3", yl - 2 * (yh - pp));
            }
            catch (Exception ex)
            {
                Print($"[Helm] Pivots calculation failed: {ex.Message}");
            }
        }

        // -----------------------------------------------------------------
        //  Today's session high/low + yesterday's H/L/C, sourced from the
        //  daily series. Same absolute-index pattern as AppendPivots — last
        //  bar is "today" (may be developing), second-to-last is yesterday.
        // -----------------------------------------------------------------
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
                Print($"[Helm] Session levels unavailable: {ex.Message}");
            }
        }

        // =================================================================
        //  JSON helpers
        //  - We always write numbers in invariant culture so we get
        //    "4521.25" regardless of the user's locale (no commas-as-
        //    decimal-separator surprises).
        //  - `leading` controls whether to prepend a comma; first key in
        //    each object passes leading: false.
        // =================================================================
        private static void WriteKv(StringBuilder sb, string key, double value, bool leading = true)
        {
            if (leading) sb.Append(',');
            sb.Append('"').Append(key).Append("\":");
            // NaN / Infinity are NOT valid JSON. If we emit them as bare
            // tokens (e.g. "NaN"), Flask's get_json() rejects the whole
            // payload with a 400. Emit `null` instead so the rest of the
            // context survives.
            if (double.IsNaN(value) || double.IsInfinity(value))
            {
                sb.Append("null");
            }
            else
            {
                sb.Append(value.ToString("0.########", CultureInfo.InvariantCulture));
            }
        }

        private static void WriteKv(StringBuilder sb, string key, string value, bool leading = true)
        {
            if (leading) sb.Append(',');
            sb.Append('"').Append(key).Append("\":\"")
              .Append(EscapeJson(value))
              .Append('"');
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
    }
}
