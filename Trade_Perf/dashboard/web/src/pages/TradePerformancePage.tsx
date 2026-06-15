import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { EMPTY_FILTERS, fetchJSON, type Filters, type SettingsResp } from '../api'
import { DrawdownsCard, FillsTable, FilterBar, StatsPanel, StatusPanel, TaxEstimateCard, TradesTable } from '../panels'
import { currentTradingDay } from '../lib/trading_day'

export function TradePerformancePage() {
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS)
  // "Current CME Session" = current trading day per the operator's TZ + 6 PM
  // CT roll. Trades closed after the local 6 PM bucket into the NEXT trading
  // day -- so 5 PM CDT trade = today's session, 7 PM CDT trade = tomorrow's
  // session. Falls back to America/Chicago while settings load.
  const settings = useQuery({
    queryKey: ['settings'],
    queryFn:  () => fetchJSON<SettingsResp>('/api/settings'),
    staleTime: 60_000,
  })
  const tz = settings.data?.settings.appearance.timezone ?? 'America/Chicago'
  const today = currentTradingDay(tz)

  return (
    <>
      <FilterBar filters={filters} setFilters={setFilters} />

      <div className="grid">
        <StatusPanel />
        <StatsPanel label="Current CME Session" filters={filters} extra={{ trading_day: today }} />
        <StatsPanel label="Calendar Day / Range" filters={filters} />
      </div>

      <DrawdownsCard />
      <TaxEstimateCard />

      <TradesTable filters={filters} />
      <FillsTable filters={filters} />
    </>
  )
}
