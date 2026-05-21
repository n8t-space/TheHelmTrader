import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
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

const DISMISS_KEY = 'helm.version.dismissed'   // localStorage key, holds the
                                               // dismissed latest_sha

function fmtChecked(ts: number | null): string {
  if (!ts) return 'never'
  const ageS = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (ageS < 60)    return `${ageS}s ago`
  if (ageS < 3600)  return `${Math.floor(ageS / 60)}m ago`
  if (ageS < 86400) return `${Math.floor(ageS / 3600)}h ago`
  return `${Math.floor(ageS / 86400)}d ago`
}

export function UpdateBanner() {
  const qc = useQueryClient()
  const [dismissed, setDismissed] = useState<string | null>(() => {
    try { return localStorage.getItem(DISMISS_KEY) } catch { return null }
  })

  const v = useQuery<VersionResp>({
    queryKey: ['version'],
    queryFn:  () => fetchJSON<VersionResp>('/api/version'),
    refetchInterval: 10 * 60 * 1000,   // poll the cache cheaply every 10m
    staleTime: 60 * 1000,
    retry: 0,
  })

  const checkNow = useMutation({
    mutationFn: () => postJSON<VersionResp>('/api/version/check'),
    onSuccess: (data) => {
      qc.setQueryData(['version'], data)
    },
  })

  // Re-show the banner if a NEW commit lands after the user dismissed an
  // older one. We compare the cached dismissed sha against whatever the
  // backend currently reports as latest.
  useEffect(() => {
    const latest = v.data?.latest_sha
    if (latest && dismissed && latest !== dismissed) {
      try { localStorage.removeItem(DISMISS_KEY) } catch { /* ignore */ }
      setDismissed(null)
    }
  }, [v.data?.latest_sha, dismissed])

  const d = v.data
  if (!d) return null
  if (!d.is_git_checkout) return null         // release-zip install -- nothing to check
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
        <Link to="/support" className="update-banner-btn update-banner-link">
          View update guide
        </Link>
        <button
          type="button"
          className="update-banner-btn"
          onClick={() => checkNow.mutate()}
          disabled={checkNow.isPending}
        >
          {checkNow.isPending ? 'Checking...' : 'Check now'}
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

      {d.last_error && (
        <div className="update-banner-error">Last check error: {d.last_error}</div>
      )}
    </div>
  )
}
