import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useEffect } from 'react'
import { BrowserRouter, Navigate, NavLink, Route, Routes } from 'react-router-dom'
import './App.css'
import { HelmMark } from './HelmMark'
import { fetchJSON, type SettingsResp } from './api'
import { applyAppearance, cacheAppearance, loadCachedAppearance } from './lib/theme'
import { HealthPage } from './pages/HealthPage'
import { HomePage } from './pages/HomePage'
import { JournalPage } from './Journal'
import { ExpensesPage } from './pages/ExpensesPage'
import { SettingsPage } from './pages/SettingsPage'
import { SignalAnalysisPage } from './pages/SignalAnalysisPage'
import { SignalDetailPage } from './pages/SignalDetailPage'
import { SupportPage } from './pages/SupportPage'
import { TradePerformancePage } from './pages/TradePerformancePage'
import { ServiceAlert } from './ServiceAlert'
import { UpdateBanner, VersionBadge } from './UpdateBanner'

// Pre-paint hint: if we cached an appearance from a prior load, apply it before
// React mounts so the SPA never flashes the default palette.
const _cached = loadCachedAppearance()
if (_cached) applyAppearance(_cached)

const qc = new QueryClient({
  defaultOptions: {
    queries: {
      refetchInterval: 5000,        // mirror the recorder's poll cadence
      staleTime: 2000,
      retry: 1,
    },
  },
})

function BootSettings() {
  // Fetch settings once at boot. Applies appearance (in case the user changed
  // it on another device/tab) and caches it for next load's pre-paint.
  useEffect(() => {
    fetchJSON<SettingsResp>('/api/settings')
      .then((r) => {
        applyAppearance(r.settings.appearance)
        cacheAppearance(r.settings.appearance)
      })
      .catch(() => {
        // Backend not yet up or older build without /api/settings -- keep cache.
      })
  }, [])

  // Unsaved-settings guard for in-app NavLink clicks. useBlocker only works
  // with data routers, so we intercept at the DOM level: any anchor click
  // bound to a different in-app route prompts when the settings page has
  // dirty state.
  useEffect(() => {
    const onClickCapture = (e: MouseEvent) => {
      if (!window.__helmSettingsDirty) return
      const anchor = (e.target as HTMLElement | null)?.closest('a')
      if (!anchor) return
      const href = anchor.getAttribute('href')
      if (!href || href.startsWith('http') || href.startsWith('#')) return
      if (href === window.location.pathname) return
      if (!window.confirm('Discard unsaved settings changes?')) {
        e.preventDefault()
        e.stopPropagation()
      } else {
        window.__helmSettingsDirty = false
      }
    }
    document.addEventListener('click', onClickCapture, true)
    return () => document.removeEventListener('click', onClickCapture, true)
  }, [])

  return null
}

export default function App() {
  return (
    <QueryClientProvider client={qc}>
      <BrowserRouter>
        <BootSettings />
        <ServiceAlert />
        <UpdateBanner />
        <div className="app">
          <header>
            <span className="brand-lockup">
              <HelmMark className="brand-mark" />
              <h1>The Helm</h1>
              <VersionBadge />
            </span>
            <nav className="topnav">
              <NavLink to="/" end>Home</NavLink>
              <NavLink to="/performance">Trade Performance</NavLink>
              <NavLink to="/journal">Journal</NavLink>
              <NavLink to="/expenses">Expenses</NavLink>
              <NavLink to="/signals">Signal Analysis</NavLink>
              <NavLink to="/health">Health</NavLink>
              <NavLink to="/settings">Settings</NavLink>
              <NavLink to="/support">Support</NavLink>
            </nav>
            <span className="subtle">local mirror &mdash; auto-refreshing every 5s</span>
          </header>

          <Routes>
            <Route path="/" element={<HomePage />} />
            <Route path="/performance" element={<TradePerformancePage />} />
            <Route path="/journal" element={<JournalPage />} />
            <Route path="/expenses" element={<ExpensesPage />} />
            <Route path="/signals" element={<SignalAnalysisPage />} />
            <Route path="/signals/:timestamp" element={<SignalDetailPage />} />
            <Route path="/health" element={<HealthPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/support" element={<SupportPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </div>
      </BrowserRouter>
    </QueryClientProvider>
  )
}
         