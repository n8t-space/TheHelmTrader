// Trading-day helper for the frontend. Mirrors the backend convention:
// trading day boundary = 6 PM in the operator's configured timezone, so a
// trade closed at 7 PM local rolls into the NEXT trading day (CME-style).
//
// Uses Intl.DateTimeFormat with the configured TZ to extract local-clock
// pieces, avoids manual UTC offset math, and stays DST-correct.

const ROLL_HOUR = 18

/** Return the current trading day as YYYY-MM-DD per the configured tz. */
export function currentTradingDay(tz: string, now: Date = new Date()): string {
  const parts = partsInTz(now, tz)
  if (parts.hour >= ROLL_HOUR) {
    return shiftDate(parts.year, parts.month, parts.day, 1)
  }
  return isoDate(parts.year, parts.month, parts.day)
}

/** Return the trading day of an arbitrary timestamp. */
export function tradingDayFor(ts: Date, tz: string): string {
  return currentTradingDay(tz, ts)
}

interface DateParts {
  year:  number
  month: number
  day:   number
  hour:  number
}

function partsInTz(d: Date, tz: string): DateParts {
  // Intl always returns strings; parse to ints. The hour12=false dance keeps
  // 0-23 instead of "11 PM".
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: tz,
    year:   'numeric',
    month:  '2-digit',
    day:    '2-digit',
    hour:   '2-digit',
    hour12: false,
  })
  const out: Partial<DateParts> = {}
  for (const p of fmt.formatToParts(d)) {
    if (p.type === 'year')  out.year  = Number(p.value)
    if (p.type === 'month') out.month = Number(p.value)
    if (p.type === 'day')   out.day   = Number(p.value)
    if (p.type === 'hour')  out.hour  = Number(p.value)
  }
  // "hour" 24 happens on some Node Intl builds for midnight; normalize.
  if (out.hour === 24) out.hour = 0
  return out as DateParts
}

function isoDate(y: number, m: number, d: number): string {
  return `${y.toString().padStart(4, '0')}-${m.toString().padStart(2, '0')}-${d.toString().padStart(2, '0')}`
}

function shiftDate(y: number, m: number, d: number, deltaDays: number): string {
  const dt = new Date(Date.UTC(y, m - 1, d))
  dt.setUTCDate(dt.getUTCDate() + deltaDays)
  return isoDate(dt.getUTCFullYear(), dt.getUTCMonth() + 1, dt.getUTCDate())
}
