import { useState } from 'react'
import { EMPTY_FILTERS, type Filters } from '../api'
import { FillsTable, FilterBar, StatsPanel, StatusPanel, TradesTable } from '../panels'

export function TradePerformancePage() {
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS)
  const today = new Date().toISOString().slice(0, 10)

  return (
    <>
      <FilterBar filters={filters} setFilters={setFilters} />

      <div className="grid">
        <StatusPanel />
        <StatsPanel label="Today" filters={filters} extra={{ date_from: today }} />
        <StatsPanel label="Filtered" filters={filters} />
      </div>

      <TradesTable filters={filters} />
      <FillsTable filters={filters} />
    </>
  )
}
