import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  accountLabel, deleteJSON, fetchJSON, putJSON,
  JOURNAL_MOODS,
  type JournalEntry, type JournalListResp, type JournalSnapshot, type SettingsResp,
} from './api'

const fmtMoney = (n: number) =>
  `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
const pnlClass = (n: number) => (n > 0 ? 'pnl-pos' : n < 0 ? 'pnl-neg' : '')

const CT_FMT = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/Chicago', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hour12: false,
})
const fmtTime = (iso?: string) => (iso ? CT_FMT.format(new Date(iso)).replace(',', '') : '—')

// Set of trade keys that already have a journal entry -- used to render the
// has-entry indicator in the trades table without an N+1 fetch.
export function useJournalKeys(): Set<string> {
  const q = useQuery<JournalListResp>({
    queryKey: ['journal'],
    queryFn: () => fetchJSON<JournalListResp>('/api/journal'),
    staleTime: 10_000,
  })
  return useMemo(
    () => new Set((q.data?.entries ?? []).map((e) => e.trade_key)),
    [q.data],
  )
}

async function fetchEntry(tradeKey: string): Promise<JournalEntry | null> {
  const r = await fetch(`/api/journal/${encodeURIComponent(tradeKey)}`)
  if (r.status === 404) return null
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
  return r.json() as Promise<JournalEntry>
}

// trade_key -> entry screenshot filename, for auto-entered trades that captured
// a chart at fill. Shared/cached so the editor doesn't refetch per row.
export function useEntryScreenshots(): Record<string, string> {
  const q = useQuery<{ screenshots: Record<string, string> }>({
    queryKey: ['journal-entry-screenshots'],
    queryFn: () => fetchJSON<{ screenshots: Record<string, string> }>('/api/journal/entry-screenshots'),
    staleTime: 30_000,
  })
  return q.data?.screenshots ?? {}
}

interface EditorProps {
  tradeKey: string
  snapshot: JournalSnapshot
  onClose?: () => void
}

export function JournalEditor({ tradeKey, snapshot, onClose }: EditorProps) {
  const qc = useQueryClient()
  const existing = useQuery({
    queryKey: ['journal', tradeKey],
    queryFn: () => fetchEntry(tradeKey),
  })
  // Friendly account names so the snapshot reads the same as the rest of the app.
  const settings = useQuery({
    queryKey: ['settings'],
    queryFn: () => fetchJSON<SettingsResp>('/api/settings'),
    staleTime: 60_000,
  })
  const names = settings.data?.settings.accounts.names
  const shots = useEntryScreenshots()
  const shot = shots[tradeKey]

  const [notes, setNotes] = useState('')
  const [discipline, setDiscipline] = useState<number | null>(null)
  const [mood, setMood] = useState('')
  const [tags, setTags] = useState<string[]>([])
  const [tagDraft, setTagDraft] = useState('')
  const [savedFlash, setSavedFlash] = useState(false)

  useEffect(() => {
    const e = existing.data
    if (e) {
      setNotes(e.notes)
      setDiscipline(e.discipline)
      setMood(e.mood)
      setTags(e.tags)
    }
  }, [existing.data])

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['journal'] })
    qc.invalidateQueries({ queryKey: ['journal', tradeKey] })
  }

  const save = useMutation({
    mutationFn: () => putJSON<JournalEntry>(`/api/journal/${encodeURIComponent(tradeKey)}`, {
      notes, discipline, mood, tags, snapshot,
    }),
    onSuccess: () => {
      invalidate()
      setSavedFlash(true)
      window.setTimeout(() => setSavedFlash(false), 1500)
    },
  })

  const remove = useMutation({
    mutationFn: () => deleteJSON(`/api/journal/${encodeURIComponent(tradeKey)}`),
    onSuccess: () => { invalidate(); onClose?.() },
  })

  const addTag = (raw: string) => {
    const t = raw.trim().toLowerCase()
    if (t && !tags.includes(t)) setTags([...tags, t])
    setTagDraft('')
  }
  const onTagKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); addTag(tagDraft) }
    else if (e.key === 'Backspace' && !tagDraft && tags.length) setTags(tags.slice(0, -1))
  }

  const hasEntry = !!existing.data

  return (
    <div className="journal-editor">
      <div className="journal-snap">
        <span><strong>{snapshot.symbol}</strong> {snapshot.direction}</span>
        <span className="subtle">{accountLabel(snapshot.account, names)}</span>
        <span className={pnlClass(snapshot.net_pnl)}>{fmtMoney(snapshot.net_pnl)}</span>
        {(snapshot.entry_price > 0 || snapshot.exit_price > 0) && (
          <span className="subtle">
            {snapshot.entry_price} &rarr; {snapshot.exit_price}
          </span>
        )}
        {snapshot.atm && <span className="subtle">ATM: {snapshot.atm}</span>}
        <span className="subtle">{fmtTime(snapshot.entry_time)} CT</span>
      </div>

      {shot && (
        <a href={`/api/screenshots/${shot}`} target="_blank" rel="noreferrer" className="journal-shot-link">
          <img className="journal-shot" src={`/api/screenshots/${shot}`} alt="Chart at auto-entry" />
        </a>
      )}

      <textarea
        className="journal-notes"
        placeholder="What happened? Plan adherence, setup quality, what to repeat or fix..."
        value={notes}
        onChange={(e) => setNotes(e.target.value)}
        rows={4}
      />

      <div className="journal-row">
        <label className="journal-label">Discipline</label>
        <div className="journal-rating">
          {[1, 2, 3, 4, 5].map((n) => (
            <button
              key={n}
              type="button"
              className={'rating-pip ' + (discipline === n ? 'on' : '')}
              onClick={() => setDiscipline(discipline === n ? null : n)}
              title={`${n} / 5`}
            >{n}</button>
          ))}
        </div>
        <label className="journal-label">Mood</label>
        <select value={mood} onChange={(e) => setMood(e.target.value)}>
          <option value="">--</option>
          {JOURNAL_MOODS.map((m) => <option key={m} value={m}>{m}</option>)}
        </select>
      </div>

      <div className="journal-row">
        <label className="journal-label">Tags</label>
        <div className="journal-tags">
          {tags.map((t) => (
            <span key={t} className="journal-tag">
              {t}<button type="button" onClick={() => setTags(tags.filter((x) => x !== t))}>x</button>
            </span>
          ))}
          <input
            className="journal-tag-input"
            placeholder="add tag, Enter"
            value={tagDraft}
            onChange={(e) => setTagDraft(e.target.value)}
            onKeyDown={onTagKey}
            onBlur={() => tagDraft && addTag(tagDraft)}
          />
        </div>
      </div>

      <div className="journal-actions">
        <button type="button" onClick={() => save.mutate()} disabled={save.isPending}>
          {save.isPending ? 'Saving...' : 'Save'}
        </button>
        {hasEntry && (
          <button type="button" className="btn-danger" onClick={() => remove.mutate()} disabled={remove.isPending}>
            Delete
          </button>
        )}
        {onClose && <button type="button" className="btn-ghost" onClick={onClose}>Close</button>}
        {savedFlash && <span className="subtle">Saved.</span>}
        {save.error && <span className="error">{String(save.error)}</span>}
      </div>
    </div>
  )
}

export function JournalPage() {
  const q = useQuery<JournalListResp>({
    queryKey: ['journal'],
    queryFn: () => fetchJSON<JournalListResp>('/api/journal'),
  })

  if (q.isLoading) return <div className="card">Loading...</div>
  if (q.error) return <div className="card error">{String(q.error)}</div>
  const entries = q.data?.entries ?? []

  return (
    <div className="card">
      <h2>Trade Journal {entries.length > 0 && `(${entries.length})`}</h2>
      {entries.length === 0 ? (
        <p className="subtle">
          No journal entries yet. Open a trade's journal from the Round-trip Trades
          table on the Trade Performance page.
        </p>
      ) : (
        <div className="journal-list">
          {entries.map((e) => (
            <div key={e.trade_key} className="journal-card">
              <JournalEditor tradeKey={e.trade_key} snapshot={e.snapshot} />
              <div className="journal-meta subtle">Updated {fmtTime(e.updated_at)} CT</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
