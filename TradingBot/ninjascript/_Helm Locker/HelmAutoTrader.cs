// =====================================================================
//  HelmAutoTrader.cs  -- The Helm auto-execution strategy (Sim-only v1)
//  by Lodestone & Purser
//
//  WHAT IT DOES
//    Polls the Helm dashboard (FastAPI on 127.0.0.1:8000) for signals the
//    user has ARMED for execution, and places the proposal's ATM entry on
//    the account this strategy is running on. Per-signal manual arm only --
//    nothing fires unless the user armed it on the dashboard AND the master
//    switch + account are set in Settings.
//
//  WHAT IT IS NOT
//    - Not autonomous. It never decides to trade; the human arms each signal.
//    - Not multi-account. It refuses to run unless its own account name equals
//      the AllowedAccount parameter (defense-in-depth on top of the server-side
//      account lock).
//    - Not real-money in v1. Intended for Sim101 / Playback. DryRun defaults
//      true: it logs "WOULD place ..." and reports a dry-run working state
//      without touching the order book until you flip DryRun off.
//
//  DEPLOYMENT (two-copy gotcha)
//    Project canonical:  ninjascript/_Helm Locker/HelmAutoTrader.cs
//    NT compiles from:   Documents/NinjaTrader 8/bin/Custom/Strategies/_Helm Locker/
//    Edit here -> copy to the Strategies path -> F5 in the NS editor. Strategies
//    compile from bin/Custom/Strategies, NOT Indicators.
//
//  THREADING (load-bearing)
//    ATM methods (AtmStrategyCreate / GetAtmStrategyMarketPosition /
//    AtmStrategyCancelEntryOrder ...) MUST run on the strategy's own thread.
//    The poll timer fires on a worker thread and only does HTTP; it then
//    marshals the ATM work onto the strategy thread via TriggerCustomEvent.
// =====================================================================

#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.Linq;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Timers;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Strategies;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public class HelmAutoTrader : Strategy
    {
        // =================================================================
        //  One HttpClient for the process. Lazy-initialized (not a static
        //  field initializer): type-load exceptions in NinjaScript's sandbox
        //  can silently drop the type from discovery, so keep type init trivial.
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
                        if (httpInstance == null)
                            httpInstance = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
                }
                return httpInstance;
            }
        }

        // =================================================================
        //  Runtime state
        // =================================================================
        private System.Timers.Timer pollTimer;
        private int   pollBusy;            // Interlocked reentrancy guard for the timer
        private bool  running;             // true between Realtime and Terminated
        private bool  disabled;            // account-lock failure -> hard no-op
        private bool  haltedByLoss;        // daily-loss cutoff tripped
        private double sessionRealized;    // running realized P&L of ATMs we placed (for the cutoff)
        private string effectiveAllowedAccount;  // account this strategy may trade; resolved from the dashboard (Setup > Account) on the poll tick, falls back to the AllowedAccount property when offline
        private bool   loggedAccountWait;         // throttles the "dashboard unreachable, holding" log to once per wait

        // Tracked ATM strategies we created, keyed by exec_tag.
        private readonly Dictionary<string, Tracked> tracked = new Dictionary<string, Tracked>();
        // exec_tags we've already acted on this session, so a duplicate queue
        // entry can't double-place even before the server drops it.
        private readonly HashSet<string> handled = new HashSet<string>();
        // exec_tags we've already logged an instrument-mismatch skip for, so the
        // 3s poll doesn't spam the same "wrong instrument" line every tick.
        private readonly HashSet<string> loggedSkips = new HashSet<string>();

        private sealed class Tracked
        {
            public string   ExecTag;     // == AtmId; the dashboard linkage key
            public string   AtmId;       // atmStrategyId for GetAtmStrategy* + close
            public string   OrderId;     // entry orderId for AtmStrategyCancelEntryOrder
            public string   SignalTs;
            public int      Qty;
            public DateTime PlacedAt;
            public DateTime ExpiresAt;   // assessment-expiry cancel time; MinValue = none
            public bool     Filled;
        }

        // One queue item from /api/exec/queue. Plain POCO -- parsed by the
        // hand-rolled JSON reader below (NT8 doesn't reference Newtonsoft).
        private sealed class QueueItem
        {
            public string Ts;
            public string ExecTag;
            public string Instrument;
            public string Direction;
            public double Entry;
            public double LimitPrice;
            public string AtmStrategy;
            public int    Qty;
            public double ExpiresAt;   // unix seconds; 0 = no assessment-expiry
        }

        // =================================================================
        //  Parameters
        // =================================================================
        [NinjaScriptProperty]
        [TypeConverter(typeof(HelmAccountNameConverter))]
        [Display(Name = "Allowed account", Order = 1, GroupName = "Helm Auto-Trader",
                 Description = "Account this strategy is allowed to trade. Pick from the connected accounts; the strategy refuses to act unless its own account name matches exactly.")]
        public string AllowedAccount { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Bot base URL", Order = 2, GroupName = "Helm Auto-Trader")]
        public string BotBaseUrl { get; set; }

        [NinjaScriptProperty]
        [Range(1, 60)]
        [Display(Name = "Poll seconds", Order = 3, GroupName = "Helm Auto-Trader")]
        public int PollSeconds { get; set; }

        [NinjaScriptProperty]
        [Range(1, 50)]
        [Display(Name = "Max contracts / order", Order = 4, GroupName = "Helm Auto-Trader")]
        public int MaxContractsPerOrder { get; set; }

        [NinjaScriptProperty]
        [Range(1, 20)]
        [Display(Name = "Max concurrent", Order = 5, GroupName = "Helm Auto-Trader")]
        public int MaxConcurrent { get; set; }

        [NinjaScriptProperty]
        [Range(0, double.MaxValue)]
        [Display(Name = "Daily loss cutoff ($)", Order = 6, GroupName = "Helm Auto-Trader",
                 Description = "Stop placing + disarm the queue once session realized loss hits this. 0 = off.")]
        public double DailyLossCutoff { get; set; }

        [NinjaScriptProperty]
        [Range(1, 1440)]
        [Display(Name = "Entry window (min)", Order = 7, GroupName = "Helm Auto-Trader",
                 Description = "Cancel an unfilled LIMIT entry after this many minutes.")]
        public int EntryWindowMinutes { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Dry run (no orders)", Order = 8, GroupName = "Helm Auto-Trader",
                 Description = "Log what it WOULD do and report a dry-run state without placing orders.")]
        public bool DryRun { get; set; }

        // =================================================================
        //  Lifecycle
        // =================================================================
        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Helm auto-execution: places ATM entries for dashboard-armed signals on a single locked account (Sim-only v1).";
                Name        = "HelmAutoTrader";
                Calculate   = Calculate.OnBarClose;   // logic is timer-driven; no per-tick work
                IsUnmanaged = false;               // ATM methods manage their own orders independently
                BarsRequiredToTrade = 1;

                AllowedAccount       = "Sim101";
                BotBaseUrl           = "http://127.0.0.1:8000";
                PollSeconds          = 3;
                MaxContractsPerOrder = 2;
                MaxConcurrent        = 1;
                DailyLossCutoff      = 0;
                EntryWindowMinutes   = 240;
                DryRun               = true;
            }
            else if (State == State.Realtime)
            {
                // The locked account's single source of truth is the dashboard
                // (Settings > Auto-Trader > Account). We do NOT fetch it here:
                // blocking HTTP belongs on the timer worker thread (see header),
                // and NT often reaches Realtime before the dashboard is up, so a
                // fetch here would race and could cache a permanent disable.
                // Seed provisionally from the property; the poll tick resolves the
                // real allowed account from the dashboard and gates placement.
                effectiveAllowedAccount = AllowedAccount;
                running = true;
                string acct0 = Account != null ? Account.Name : "(null)";
                Print($"[HelmAuto] armed on account '{acct0}' (resolving allowed account from dashboard; "
                    + $"provisional='{effectiveAllowedAccount}', dryRun={DryRun}, poll={PollSeconds}s, "
                    + $"maxQty={MaxContractsPerOrder}, maxConc={MaxConcurrent}). Per-signal manual arm only.");
                StartTimer();
            }
            else if (State == State.Terminated)
            {
                running = false;
                StopTimer();
            }
        }

        private void StartTimer()
        {
            StopTimer();
            pollTimer = new System.Timers.Timer(Math.Max(1, PollSeconds) * 1000.0) { AutoReset = true };
            pollTimer.Elapsed += OnPollTick;
            pollTimer.Start();
        }

        private void StopTimer()
        {
            if (pollTimer != null)
            {
                pollTimer.Stop();
                pollTimer.Elapsed -= OnPollTick;
                pollTimer.Dispose();
                pollTimer = null;
            }
        }

        // =================================================================
        //  Poll tick -- WORKER THREAD. HTTP only; marshal NS work via
        //  TriggerCustomEvent so ATM methods run on the strategy thread.
        // =================================================================
        private void OnPollTick(object sender, ElapsedEventArgs e)
        {
            if (!running || disabled) return;
            if (Interlocked.CompareExchange(ref pollBusy, 1, 0) != 0) return;  // skip overlapping ticks
            try
            {
                string acct = Account != null ? Account.Name : null;
                if (string.IsNullOrEmpty(acct)) return;

                // Resolve the allowed account from the dashboard (source of truth);
                // fall back to the AllowedAccount property only while it's
                // unreachable. Worker thread, so this blocking GET is by design.
                string fetched = FetchDashboardAccount();
                string allowed = !string.IsNullOrEmpty(fetched) ? fetched : AllowedAccount;
                effectiveAllowedAccount = allowed;
                if (!string.Equals(acct, allowed, StringComparison.Ordinal))
                {
                    // Hard-disable ONLY on a definitive dashboard answer. A transient
                    // fetch miss just holds and retries next tick, so the lock self-
                    // heals when the dashboard comes up after NT.
                    if (!string.IsNullOrEmpty(fetched))
                    {
                        if (!disabled)
                        {
                            disabled = true;
                            Print($"[HelmAuto] DISABLED: running account '{acct}' != dashboard account '{allowed}'. "
                                + "Set Settings > Auto-Trader to this account, or attach the strategy to that account. No orders will be placed.");
                        }
                    }
                    else if (!loggedAccountWait)
                    {
                        loggedAccountWait = true;
                        Print($"[HelmAuto] holding: dashboard unreachable and running account '{acct}' != fallback '{allowed}'. Retrying each poll.");
                    }
                    return;
                }
                loggedAccountWait = false;  // account matches -> clear the wait throttle

                // Fail-safe telemetry: report live account equity so the dashboard
                // can enforce the balance floor (it forces auto-trading OFF if
                // equity <= the configured floor). NetLiquidation = total account
                // value incl. open P&L, in the account's own currency.
                try
                {
                    if (Account != null)
                    {
                        // NetLiquidation reads 0.0 before the broker's first account-
                        // item push (reachable at Realtime / under Sim+Playback). Only
                        // report a plausible POSITIVE equity, so a not-ready 0 can't
                        // false-trip the "stop if balance <= floor" kill-switch.
                        double eq = Account.Get(AccountItem.NetLiquidation, Account.Denomination);
                        if (eq > 0 && !double.IsNaN(eq) && !double.IsInfinity(eq))
                            ReportBalance(acct, eq);
                    }
                }
                catch (Exception bex) { Print($"[HelmAuto] balance read failed: {bex.Message}"); }

                string url = BotBaseUrl.TrimEnd('/') + "/api/exec/queue?account=" + Uri.EscapeDataString(acct);
                string body;
                try
                {
                    body = Http.GetStringAsync(url).GetAwaiter().GetResult();
                }
                catch (Exception ex)
                {
                    Print($"[HelmAuto] queue fetch failed: {ex.GetType().Name}: {ex.Message}");
                    return;
                }

                List<QueueItem> items;
                try { items = ParseQueue(body); }
                catch (Exception ex) { Print($"[HelmAuto] queue parse failed: {ex.Message}"); return; }
                // Marshal onto the strategy thread for all ATM interaction.
                TriggerCustomEvent(o => ProcessOnStrategyThread((List<QueueItem>)o), items);
            }
            finally
            {
                Interlocked.Exchange(ref pollBusy, 0);
            }
        }

        // =================================================================
        //  STRATEGY THREAD -- safe to call ATM methods here.
        // =================================================================
        private void ProcessOnStrategyThread(List<QueueItem> items)
        {
            if (!running || disabled) return;

            MonitorTracked();

            // Daily-loss cutoff: stop placing + actively disarm the queue.
            if (DailyLossCutoff > 0 && sessionRealized <= -DailyLossCutoff)
            {
                if (!haltedByLoss)
                {
                    haltedByLoss = true;
                    Print($"[HelmAuto] DAILY LOSS CUTOFF hit (realized {sessionRealized:0.00} <= -{DailyLossCutoff:0.00}). "
                        + "Halting placement and disarming the queue.");
                }
                foreach (var it in items)
                    PostFireAndForget(DisarmUrl(it.Ts), null);
                return;
            }

            string myRoot = Instrument != null && Instrument.MasterInstrument != null
                ? Instrument.MasterInstrument.Name : null;

            foreach (var it in items)
            {
                if (it == null || string.IsNullOrEmpty(it.ExecTag)) continue;
                if (handled.Contains(it.ExecTag)) continue;
                // This strategy instance trades exactly one instrument; ignore
                // armed signals for other roots (run another instance for those).
                if (!string.IsNullOrEmpty(myRoot) && !string.Equals(it.Instrument, myRoot, StringComparison.OrdinalIgnoreCase))
                {
                    if (loggedSkips.Add(it.ExecTag))
                        Print($"[HelmAuto] skip {it.ExecTag}: armed signal is '{it.Instrument}' but this strategy runs '{myRoot}'. "
                            + "Add a HelmAutoTrader instance on the '" + it.Instrument + "' instrument to auto-trade it.");
                    continue;
                }

                int openCount = tracked.Count;
                if (openCount >= MaxConcurrent)
                {
                    Print($"[HelmAuto] skip {it.ExecTag}: at max concurrent ({openCount}/{MaxConcurrent}).");
                    continue;
                }

                // Contract cap is a GATE, not a clamp: AtmStrategyCreate has no
                // qty arg, so the ATM template fixes the order size. Refuse an
                // oversize template rather than placing it under-reported.
                if (it.Qty > MaxContractsPerOrder)
                {
                    Print($"[HelmAuto] reject {it.ExecTag}: ATM template places {it.Qty}c, over max {MaxContractsPerOrder}.");
                    PostExec(it.Ts, "rejected", it.ExecTag, null, null, false,
                             $"template qty {it.Qty} > max {MaxContractsPerOrder}");
                    handled.Add(it.ExecTag);
                    continue;
                }

                Place(it, it.Qty);
            }
        }

        private void Place(QueueItem it, int qty)
        {
            string action = (it.Direction ?? "").ToLowerInvariant() == "short" ? "SellShort" : "Buy";

            // NT8 requires the entry orderId and the atmStrategyId to be two
            // DISTINCT strings. atmId stays == exec_tag (the dashboard linkage
            // key); the entry order gets a derived id.
            string atmId   = it.ExecTag;
            string orderId = it.ExecTag + "-E";
            DateTime expiresAt = it.ExpiresAt > 0
                ? DateTimeOffset.FromUnixTimeSeconds((long)it.ExpiresAt).LocalDateTime
                : DateTime.MinValue;

            if (DryRun)
            {
                Print($"[HelmAuto] WOULD place: {action} LIMIT {qty} {it.Instrument} @ {it.LimitPrice} "
                    + $"template='{it.AtmStrategy}' atm={atmId} order={orderId}");
                handled.Add(it.ExecTag);
                tracked[it.ExecTag] = new Tracked { ExecTag = it.ExecTag, AtmId = atmId, OrderId = orderId, SignalTs = it.Ts, Qty = qty, PlacedAt = DateTime.Now, ExpiresAt = expiresAt, Filled = false };
                PostExec(it.Ts, "working", it.ExecTag, null, null, dryRun: true, note: "dry-run: no order placed");
                return;
            }

            if (string.IsNullOrEmpty(it.AtmStrategy))
            {
                Print($"[HelmAuto] reject {it.ExecTag}: no ATM template on the proposal.");
                PostExec(it.Ts, "rejected", it.ExecTag, null, null, dryRun: false, note: "no ATM template");
                handled.Add(it.ExecTag);
                return;
            }

            OrderAction oa = action == "SellShort" ? OrderAction.SellShort : OrderAction.Buy;
            handled.Add(it.ExecTag);
            tracked[it.ExecTag] = new Tracked { ExecTag = it.ExecTag, AtmId = atmId, OrderId = orderId, SignalTs = it.Ts, Qty = qty, PlacedAt = DateTime.Now, ExpiresAt = expiresAt, Filled = false };

            string ts = it.Ts;
            try
            {
                AtmStrategyCreate(
                    oa, OrderType.Limit, it.LimitPrice, 0,
                    TimeInForce.Day, orderId, it.AtmStrategy, atmId,
                    (errorCode, callbackId) =>
                    {
                        if (errorCode == ErrorCode.NoError)
                        {
                            Print($"[HelmAuto] placed {action} LIMIT {qty} {it.Instrument} @ {it.LimitPrice} atm={atmId}");
                            PostExec(ts, "working", atmId, null, null, dryRun: false, note: null);
                        }
                        else
                        {
                            Print($"[HelmAuto] AtmStrategyCreate error {errorCode} for {atmId}");
                            PostExec(ts, "rejected", atmId, null, null, dryRun: false, note: errorCode.ToString());
                            tracked.Remove(atmId);
                        }
                    });
            }
            catch (Exception ex)
            {
                Print($"[HelmAuto] place threw for {atmId}: {ex.GetType().Name}: {ex.Message}");
                PostExec(ts, "rejected", atmId, null, null, dryRun: false, note: ex.Message);
                tracked.Remove(atmId);
            }
        }

        // Fill detection + entry-expiry cancel + closed-position realized P&L.
        private void MonitorTracked()
        {
            if (DryRun || tracked.Count == 0) return;

            var done = new List<string>();
            foreach (var kv in tracked)
            {
                var t = kv.Value;
                // Getters + close take the atmStrategyId; cancel takes the orderId.
                MarketPosition pos = GetAtmStrategyMarketPosition(t.AtmId);

                if (!t.Filled)
                {
                    if (pos != MarketPosition.Flat)
                    {
                        t.Filled = true;
                        double avg = GetAtmStrategyPositionAveragePrice(t.AtmId);
                        Print($"[HelmAuto] FILLED {t.AtmId} @ {avg} x{t.Qty}");
                        PostExec(t.SignalTs, "filled", t.ExecTag, avg, t.Qty, dryRun: false, note: null);
                    }
                    else
                    {
                        // Manual cancel on the chart: the entry order's state goes
                        // Cancelled while unfilled. GetAtmStrategyEntryOrderStatus
                        // returns [avgFillPrice, filledQty, orderState]; the state
                        // string distinguishes Cancelled from a still-Working order,
                        // so a normal pending entry does NOT false-fire. (Scan the
                        // array for "Cancelled" to stay robust to element order.)
                        string[] est = GetAtmStrategyEntryOrderStatus(t.OrderId);
                        bool cancelledOnChart = est != null
                            && est.Any(e => string.Equals(e, "Cancelled", StringComparison.OrdinalIgnoreCase));
                        if (cancelledOnChart)
                        {
                            Print($"[HelmAuto] entry cancelled on chart for {t.AtmId}");
                            PostExec(t.SignalTs, "cancelled", t.ExecTag, null, null, dryRun: false, note: "cancelled on chart");
                            done.Add(t.ExecTag);
                        }
                        else if ((t.ExpiresAt != DateTime.MinValue && DateTime.Now >= t.ExpiresAt)
                                 || (DateTime.Now - t.PlacedAt).TotalMinutes > EntryWindowMinutes)
                        {
                            bool assessment = t.ExpiresAt != DateTime.MinValue && DateTime.Now >= t.ExpiresAt;
                            string why = assessment ? "assessment-expiry (next read due)" : "entry window expired";
                            bool cancelled = AtmStrategyCancelEntryOrder(t.OrderId);
                            Print($"[HelmAuto] {why} for {t.AtmId}; cancel={cancelled}");
                            PostExec(t.SignalTs, "cancelled", t.ExecTag, null, null, dryRun: false, note: why);
                            done.Add(t.ExecTag);
                        }
                    }
                }
                else if (pos == MarketPosition.Flat)
                {
                    // Filled then flat -> the ATM closed. Tally realized for the cutoff.
                    sessionRealized += GetAtmStrategyRealizedProfitLoss(t.AtmId);
                    done.Add(t.ExecTag);
                }
            }
            foreach (var tag in done) tracked.Remove(tag);
        }

        // =================================================================
        //  Fetch the dashboard's configured auto-trade account (the single
        //  source of truth). Returns null on any failure so the caller can
        //  fall back to the AllowedAccount property.
        // =================================================================
        private string FetchDashboardAccount()
        {
            try
            {
                string url = BotBaseUrl.TrimEnd('/') + "/api/auto-trader/account";
                string body = Http.GetStringAsync(url).GetAwaiter().GetResult();
                string acct = ExtractJsonString(body, "account");
                return string.IsNullOrEmpty(acct) ? null : acct;
            }
            catch { return null; }
        }

        // Pull "<key>":"<value>" from a flat JSON object. Returns null if the key
        // is missing or its value isn't a string (e.g. null). Avoids pulling in a
        // full parser for a one-field read.
        private static string ExtractJsonString(string json, string key)
        {
            if (string.IsNullOrEmpty(json)) return null;
            string needle = "\"" + key + "\"";
            int i = json.IndexOf(needle, StringComparison.Ordinal);
            if (i < 0) return null;
            i = json.IndexOf(':', i + needle.Length);
            if (i < 0) return null;
            i++;
            while (i < json.Length && char.IsWhiteSpace(json[i])) i++;
            if (i >= json.Length || json[i] != '"') return null;
            i++;
            var sb = new StringBuilder();
            while (i < json.Length && json[i] != '"')
            {
                if (json[i] == '\\' && i + 1 < json.Length) i++;  // skip escape
                sb.Append(json[i]);
                i++;
            }
            return sb.ToString();
        }

        // =================================================================
        //  HTTP reporting -- fire-and-forget so the strategy thread never
        //  blocks on the network.
        // =================================================================
        private string DisarmUrl(string ts) =>
            BotBaseUrl.TrimEnd('/') + "/api/signals/" + Uri.EscapeDataString(ts) + "/disarm";

        // Fail-safe telemetry: post the account's live equity. The dashboard
        // forces auto-trading OFF if equity is at/below the configured floor.
        private void ReportBalance(string acct, double equity)
        {
            string url = BotBaseUrl.TrimEnd('/') + "/api/auto-trader/balance";
            string json = "{\"account\":\"" + JsonStr(acct) + "\",\"balance\":"
                + equity.ToString("0.##", CultureInfo.InvariantCulture) + "}";
            PostFireAndForget(url, json);
        }

        private void PostExec(string ts, string state, string execTag,
                              double? fillPrice, int? fillQty, bool dryRun, string note)
        {
            var sb = new StringBuilder();
            sb.Append("{\"state\":\"").Append(state).Append("\"");
            if (!string.IsNullOrEmpty(execTag)) sb.Append(",\"exec_tag\":\"").Append(JsonStr(execTag)).Append("\"");
            if (fillPrice.HasValue) sb.Append(",\"fill_price\":").Append(fillPrice.Value.ToString(CultureInfo.InvariantCulture));
            if (fillQty.HasValue)   sb.Append(",\"fill_qty\":").Append(fillQty.Value);
            if (dryRun)             sb.Append(",\"dry_run\":true");
            if (!string.IsNullOrEmpty(note)) sb.Append(",\"note\":\"").Append(JsonStr(note)).Append("\"");
            sb.Append("}");

            string url = BotBaseUrl.TrimEnd('/') + "/api/signals/" + Uri.EscapeDataString(ts) + "/exec";
            PostFireAndForget(url, sb.ToString());
        }

        private void PostFireAndForget(string url, string json)
        {
            Task.Run(async () =>
            {
                try
                {
                    using (var content = new StringContent(json ?? "{}", Encoding.UTF8, "application/json"))
                    using (var r = await Http.PostAsync(url, content))
                    {
                        if (!r.IsSuccessStatusCode)
                            Print($"[HelmAuto] POST {url} -> {(int)r.StatusCode} {r.ReasonPhrase}");
                    }
                }
                catch (Exception ex)
                {
                    Print($"[HelmAuto] POST {url} failed: {ex.GetType().Name}: {ex.Message}");
                }
            });
        }

        private static string JsonStr(string s)
        {
            if (string.IsNullOrEmpty(s)) return "";
            return s.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\n", " ").Replace("\r", " ");
        }

        // =================================================================
        //  Minimal JSON reader for the {"signals":[{...}]} queue response.
        //  Hand-rolled because NinjaScript.Custom here does not reference
        //  Newtonsoft.Json. Only the shapes we emit need to parse.
        // =================================================================
        private static List<QueueItem> ParseQueue(string body)
        {
            var items = new List<QueueItem>();
            var root = new JsonReader(body).Parse() as Dictionary<string, object>;
            if (root == null || !root.ContainsKey("signals")) return items;
            var arr = root["signals"] as List<object>;
            if (arr == null) return items;
            foreach (var o in arr)
            {
                var m = o as Dictionary<string, object>;
                if (m == null) continue;
                items.Add(new QueueItem
                {
                    Ts          = Str(m, "ts"),
                    ExecTag     = Str(m, "exec_tag"),
                    Instrument  = Str(m, "instrument"),
                    Direction   = Str(m, "direction"),
                    Entry       = Num(m, "entry"),
                    LimitPrice  = Num(m, "limit_price"),
                    AtmStrategy = Str(m, "atm_strategy"),
                    Qty         = (int)Num(m, "qty"),
                    ExpiresAt   = Num(m, "expires_at"),
                });
            }
            return items;
        }

        private static string Str(Dictionary<string, object> m, string k)
            => m.ContainsKey(k) && m[k] != null ? m[k].ToString() : null;
        private static double Num(Dictionary<string, object> m, string k)
            => m.ContainsKey(k) && m[k] is double ? (double)m[k] : 0.0;

        // Tiny recursive JSON parser -> Dictionary<string,object> / List<object>
        // / string / double / bool / null. No external dependency.
        private sealed class JsonReader
        {
            private readonly string s;
            private int i;
            public JsonReader(string text) { s = text ?? ""; i = 0; }

            public object Parse() { return ParseValue(); }

            private object ParseValue()
            {
                SkipWs();
                if (i >= s.Length) return null;
                char c = s[i];
                if (c == '{') return ParseObject();
                if (c == '[') return ParseArray();
                if (c == '"') return ParseString();
                if (c == 't') { i += 4; return true; }    // true
                if (c == 'f') { i += 5; return false; }   // false
                if (c == 'n') { i += 4; return null; }    // null
                return ParseNumber();
            }

            private Dictionary<string, object> ParseObject()
            {
                var d = new Dictionary<string, object>();
                i++;                                       // {
                SkipWs();
                if (Peek() == '}') { i++; return d; }
                while (true)
                {
                    SkipWs();
                    string key = ParseString();
                    SkipWs();
                    if (Peek() == ':') i++;
                    d[key] = ParseValue();
                    SkipWs();
                    char c = Peek();
                    if (c == ',') { i++; continue; }
                    if (c == '}') { i++; }
                    break;
                }
                return d;
            }

            private List<object> ParseArray()
            {
                var list = new List<object>();
                i++;                                       // [
                SkipWs();
                if (Peek() == ']') { i++; return list; }
                while (true)
                {
                    list.Add(ParseValue());
                    SkipWs();
                    char c = Peek();
                    if (c == ',') { i++; continue; }
                    if (c == ']') { i++; }
                    break;
                }
                return list;
            }

            private string ParseString()
            {
                var sb = new StringBuilder();
                i++;                                       // opening quote
                while (i < s.Length)
                {
                    char c = s[i++];
                    if (c == '"') break;
                    if (c == '\\' && i < s.Length)
                    {
                        char e = s[i++];
                        switch (e)
                        {
                            case '"':  sb.Append('"');  break;
                            case '\\': sb.Append('\\'); break;
                            case '/':  sb.Append('/');  break;
                            case 'n':  sb.Append('\n'); break;
                            case 't':  sb.Append('\t'); break;
                            case 'r':  sb.Append('\r'); break;
                            case 'b':  sb.Append('\b'); break;
                            case 'f':  sb.Append('\f'); break;
                            case 'u':
                                if (i + 4 <= s.Length)
                                {
                                    int code = int.Parse(s.Substring(i, 4), NumberStyles.HexNumber, CultureInfo.InvariantCulture);
                                    sb.Append((char)code);
                                    i += 4;
                                }
                                break;
                            default: sb.Append(e); break;
                        }
                    }
                    else sb.Append(c);
                }
                return sb.ToString();
            }

            private object ParseNumber()
            {
                int start = i;
                while (i < s.Length && "0123456789+-.eE".IndexOf(s[i]) >= 0) i++;
                double d;
                if (double.TryParse(s.Substring(start, i - start), NumberStyles.Any, CultureInfo.InvariantCulture, out d))
                    return d;
                return null;
            }

            private char Peek() { return i < s.Length ? s[i] : '\0'; }
            private void SkipWs() { while (i < s.Length && char.IsWhiteSpace(s[i])) i++; }
        }

        protected override void OnBarUpdate() { /* logic is timer-driven; nothing per-bar */ }
    }

    // Renders the "Allowed account" property as a dropdown. Lists connected
    // accounts first; falls back to every known account when none are connected
    // yet (e.g. configuring before login) so the list is never empty. Non-
    // exclusive: an account name can still be typed if it isn't in the list.
    public class HelmAccountNameConverter : TypeConverter
    {
        public override bool GetStandardValuesSupported(ITypeDescriptorContext context) { return true; }
        public override bool GetStandardValuesExclusive(ITypeDescriptorContext context) { return false; }

        public override StandardValuesCollection GetStandardValues(ITypeDescriptorContext context)
        {
            var names = new List<string>();
            try
            {
                lock (Account.All)
                {
                    foreach (Account a in Account.All)
                        if (a.Connection != null && a.Connection.Status == ConnectionStatus.Connected)
                            names.Add(a.Name);
                    if (names.Count == 0)
                        foreach (Account a in Account.All)
                            names.Add(a.Name);
                }
            }
            catch { /* account list not ready during type discovery */ }
            return new StandardValuesCollection(names.Distinct().ToList());
        }
    }
}
