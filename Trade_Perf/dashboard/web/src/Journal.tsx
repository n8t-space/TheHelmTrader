import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  accountLabel, deleteJSON, fetchJSON, putJSON,
  JOURNAL_MOODS,
  type JournalEntry, type JournalListResp, type JournalSnapshot, type SettingsResp,
} from './api'
import { sortBy } from './lib/sorting'

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

      <div className="journal-shot-row">
        {shot
          ? <a href={`/api/screenshots/${shot}`} target="_blank" rel="noreferrer">📎 Chart at entry &mdash; {shot} &#8599;</a>
          : <span className="subtle">No entry screenshot for this trade.</span>}
      </div>

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

type JournalKey =
  'date' | 'account' | 'instrument' | 'direction' | 'pnl' | 'discipline' | 'mood' | 'tags' | 'updated'

// Sort accessor over a journal entry. Friendly account label so the Account
// sort matches what the row shows.
function journalValue(e: JournalEntry, k: JournalKey, names?: Record<string, string>): unknown {
  switch (k) {
    case 'date':       return e.snapshot.entry_time
    case 'account':    return accountLabel(e.snapshot.account, names)
    case 'instrument': return e.snapshot.symbol
    case 'direction':  return e.snapshot.direction
    case 'pnl':        return e.snapshot.net_pnl
    case 'discipline': return e.discipline
    case 'mood':       return e.mood
    case 'tags':       return e.tags.join(', ')
    case 'updated':    return e.updated_at
  }
}

const SORT_OPTIONS: { k: JournalKey; label: string }[] = [
  { k: 'date',       label: 'Entry date' },
  { k: 'account',    label: 'Account' },
  { k: 'instrument', label: 'Instrument' },
  { k: 'pnl',        label: 'Net P&L' },
  { k: 'discipline', label: 'Discipline' },
  { k: 'mood',       label: 'Mood' },
  { k: 'tags',       label: 'Tags' },
  { k: 'updated',    label: 'Last updated' },
]

export function JournalPage() {
  const q = useQuery<JournalListResp>({
    queryKey: ['journal'],
    queryFn: () => fetchJSON<JournalListResp>('/api/journal'),
  })
  const settings = useQuery({
    queryKey: ['settings'],
    queryFn: () => fetchJSON<SettingsResp>('/api/settings'),
    staleTime: 60_000,
  })
  const names = settings.data?.settings.accounts.names

  const [sortKey, setSortKey] = useState<JournalKey>('date')
  const [dir, setDir] = useState<'asc' | 'desc'>('desc')
  const [open, setOpen] = useState<Set<string>>(new Set())
  const toggle = (k: string) =>
    setOpen((s) => { const n = new Set(s); n.has(k) ? n.delete(k) : n.add(k); return n })

  const entries = useMemo(
    () => sortBy(q.data?.entries ?? [], { key: sortKey, dir }, (e, k) => journalValue(e, k, names)),
    [q.data, sortKey, dir, names],
  )

  if (q.isLoading) return <div className="card">Loading...</div>
  if (q.error) return <div className="card error">{String(q.error)}</div>

  return (
    <div className="card">
      <h2>Trade Journal {entries.length > 0 && `(${entries.length})`}</h2>

      <div className="journal-sortbar">
        <span className="subtle">Sort by</span>
        <select value={sortKey} onChange={(e) => setSortKey(e.target.value as JournalKey)}>
          {SORT_OPTIONS.map((o) => <option key={o.k} value={o.k}>{o.label}</option>)}
        </select>
        <button type="button" onClick={() => setDir((d) => (d === 'asc' ? 'desc' : 'asc'))}>
          {dir === 'asc' ? '↑ Asc' : '↓ Desc'}
        </button>
      </div>

      {entries.length === 0 ? (
        <p className="subtle">
          No journal entries yet. Open a trade's journal from the Round-trip Trades
          table on the Trade Performance page.
        </p>
      ) : (
        <div className="journal-list">
          {entries.map((e) => {
            const isOpen = open.has(e.trade_key)
            return (
              <div className="journal-card" key={e.trade_key}>
                <button type="button" className="journal-card-head" onClick={() => toggle(e.trade_key)}>
                  <div className="journal-card-top">
                    <span>{isOpen ? '▼' : '▶'} {fmtTime(e.snapshot.entry_time)}</span>
                    <span className={pnlClass(e.snapshot.net_pnl)}>{fmtMoney(e.snapshot.net_pnl)}</span>
                  </div>
                  <div className="journal-card-sub">
                    <strong>{accountLabel(e.snapshot.account, names)}</strong>
                    <span>{[e.snapshot.symbol, e.snapshot.direction].filter(Boolean).join(' ') || '--'}</span>
                    {e.discipline != null && <span>Disc {e.discipline}/5</span>}
                    {e.mood && <span>{e.mood}</span>}
                    {e.tags.map((t) => <span key={t} className="journal-tag-pill">{t}</span>)}
                  </div>
                </button>
                {isOpen && (
                  <div className="journal-card-body">
                    <JournalEditor tradeKey={e.trade_key} snapshot={e.snapshot} onClose={() => toggle(e.trade_key)} />
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
