import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchJSON, postJSON } from './api'

interface VersionResp {
  current_sha: string | null
  current_short: string | null
  latest_sha: string | null
  latest_short: string | null
  commits_behind: number
  update_available: boolean
  last_checked: number | null
  last_error: string | null
  is_git_checkout: boolean | null
  remote: string
  branch: string
}

interface UpdateStatus {
  stage: 'idle' | 'queued' | 'fetching' | 'pip' | 'npm' | 'build' | 'done' | 'failed' | 'unknown'
  message?: string
  step?: number
  total_steps?: number
  log_tail?: string[]
  started_at?: string | null
  finished_at?: string | null
  error?: string | null
  target_sha?: string | null
  pid?: number | null
}

const DISMISS_KEY = 'helm.version.dismissed'

function fmtChecked(ts: number | null): string {
  if (!ts) return 'never'
  const ageS = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (ageS < 60)    return `${ageS}s ago`
  if (ageS < 3600)  return `${Math.floor(ageS / 60)}m ago`
  if (ageS < 86400) return `${Math.floor(ageS / 3600)}h ago`
  return `${Math.floor(ageS / 86400)}d ago`
}

const ACTIVE_STAGES: UpdateStatus['stage'][] = ['queued', 'fetching', 'pip', 'npm', 'build']

export function UpdateBanner() {
  const qc = useQueryClient()
  const [dismissed, setDismissed] = useState<string | null>(() => {
    try { return localStorage.getItem(DISMISS_KEY) } catch { return null }
  })
  const [updating, setUpdating] = useState(false)

  const v = useQuery<VersionResp>({
    queryKey: ['version'],
    queryFn:  () => fetchJSON<VersionResp>('/api/version'),
    refetchInterval: updating ? 3000 : 10 * 60 * 1000,
    staleTime: 60 * 1000,
    retry: 0,
  })

  // Status poll only runs while an update is in flight. The query is tolerant
  // of transient network errors -- uvicorn dies for ~5s at the end of every
  // update and we want polling to silently survive that window.
  const status = useQuery<UpdateStatus>({
    queryKey: ['update-status'],
    queryFn:  () => fetchJSON<UpdateStatus>('/api/version/update/status'),
    enabled:  updating,
    refetchInterval: 1500,
    retry: 0,
    staleTime: 0,
  })

  const checkNow = useMutation({
    mutationFn: () => postJSON<VersionResp>('/api/version/check'),
    onSuccess: (data) => qc.setQueryData(['version'], data),
  })

  const startUpdate = useMutation({
    mutationFn: () => postJSON<{ started: boolean; pid: number }>('/api/version/update'),
    onSuccess: () => {
      setUpdating(true)
      qc.invalidateQueries({ queryKey: ['update-status'] })
    },
  })

  // Re-show banner if a NEW commit lands after the user dismissed an older one.
  useEffect(() => {
    const latest = v.data?.latest_sha
    if (latest && dismissed && latest !== dismissed) {
      try { localStorage.removeItem(DISMISS_KEY) } catch { /* ignore */ }
      setDismissed(null)
    }
  }, [v.data?.latest_sha, dismissed])

  // When the helper finishes AND the running API reports the new SHA, reload
  // the SPA so the user picks up the freshly built bundle. The grace period
  // covers the watchdog respawn + uvicorn boot window.
  const s = status.data
  useEffect(() => {
    if (!updating || !s || s.stage !== 'done') return
    const target = s.target_sha
    const current = v.data?.current_sha
    if (target && current && target === current) {
      const t = setTimeout(() => window.location.reload(), 1500)
      return () => clearTimeout(t)
    }
  }, [updating, s, v.data?.current_sha])

  // ----- progress modal (always rendered while updating) -----
  if (updating && s) {
    const pct = s.total_steps ? Math.round(((s.step ?? 0) / s.total_steps) * 100) : 0
    const failed = s.stage === 'failed'
    const done   = s.stage === 'done'
    const active = ACTIVE_STAGES.includes(s.stage)
    const headline =
      failed ? 'Update failed' :
      done   ? 'Restarting service...' :
      active ? 'Updating The Helm' : 'Waiting...'

    return (
      <div className="update-modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="update-modal-title">
        <div className="update-modal">
          <h2 id="update-modal-title" className="update-modal-title">{headline}</h2>
          <p className="update-modal-subtitle">{s.message || ' '}</p>

          <div className="update-progress" aria-hidden>
            <div className={'update-progress-bar' + (failed ? ' failed' : '')} style={{ width: `${pct}%` }} />
          </div>
          <p className="update-progress-meta">
            Step {s.step ?? 0} of {s.total_steps ?? 6}
            {s.target_sha && <> · target <code>{s.target_sha.slice(0, 7)}</code></>}
          </p>

          {s.log_tail && s.log_tail.length > 0 && (
            <pre className="update-log-tail">{s.log_tail.slice(-12).join('\n')}</pre>
          )}

          {failed && (
            <div className="update-modal-actions">
              <p className="update-error">{s.error || 'Unknown error'}</p>
              <button
                type="button"
                className="update-banner-btn"
                onClick={() => { setUpdating(false); qc.invalidateQueries({ queryKey: ['version'] }) }}
              >
                Close
              </button>
            </div>
          )}

          {done && (
            <p className="subtle">
              Service restarting. The page will reload automatically once the new build is live.
            </p>
          )}
        </div>
      </div>
    )
  }

  // ----- standard banner -----
  const d = v.data
  if (!d) return null
  if (!d.is_git_checkout) return null
  if (!d.update_available) return null
  if (d.latest_sha && d.latest_sha === dismissed) return null

  function dismiss() {
    if (!d?.latest_sha) return
    try { localStorage.setItem(DISMISS_KEY, d.latest_sha) } catch { /* ignore */ }
    setDismissed(d.latest_sha)
  }

  const behind = d.commits_behind
  const label  = behind === 1 ? '1 commit behind' : `${behind} commits behind`

  return (
    <div className="update-banner" role="status" aria-live="polite">
      <div className="update-banner-row">
        <span className="update-banner-icon" aria-hidden>&#x2191;</span>
        <span className="update-banner-text">
          <strong>Update available</strong>
          {' '}&middot;{' '}
          <code>{d.current_short ?? '?'}</code>
          {' '}&rarr;{' '}
          <code>{d.latest_short ?? '?'}</code>
          {' '}({label})
        </span>
        <span className="update-banner-meta">
          last checked {fmtChecked(d.last_checked)}
        </span>
        <button
          type="button"
          className="update-banner-btn update-banner-primary"
          onClick={() => {
            if (window.confirm(
              `Update The Helm from ${d.current_short} to ${d.latest_short}?\n\n` +
              `This will pull the latest code, rebuild the dashboard, and restart the service. ` +
              `The page will reload automatically when finished (~30-60s).`
            )) {
              startUpdate.mutate()
            }
          }}
          disabled={startUpdate.isPending}
        >
          {startUpdate.isPending ? 'Starting...' : 'Update now'}
        </button>
        <button
          type="button"
          className="update-banner-btn"
          onClick={() => checkNow.mutate()}
          disabled={checkNow.isPending}
        >
          {checkNow.isPending ? 'Checking...' : 'Re-check'}
        </button>
        <button
          type="button"
          className="update-banner-btn update-banner-dismiss"
          onClick={dismiss}
          aria-label="Dismiss until next release"
        >
          &times;
        </button>
      </div>

      {(d.last_error || startUpdate.error) && (
        <div className="update-banner-error">
          {startUpdate.error ? `Start failed: ${String(startUpdate.error)}` : `Last check error: ${d.last_error}`}
        </div>
      )}
    </div>
  )
}
