import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  accountLabel, deleteJSON, fetchJSON, postJSON, putJSON,
  EXPENSE_CATEGORY_LABELS,
  type Expense, type ExpensesResp, type SettingsResp,
} from '../api'

const fmtMoney = (n: number) =>
  `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

const ENTITIES = ['llc', 'personal'] as const
const CATEGORY_KEYS = Object.keys(EXPENSE_CATEGORY_LABELS)
const catLabel = (k: string) => EXPENSE_CATEGORY_LABELS[k] ?? k

function todayISO(): string {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

type Draft = Omit<Expense, 'id' | 'created_at'>
const emptyDraft = (): Draft => ({
  date: todayISO(), category: 'eval_fee', amount: 0, entity: 'llc',
  vendor: '', account: '', recurring: false, deductible: true, note: '',
})

export function ExpensesPage() {
  const qc = useQueryClient()
  const [year, setYear] = useState<string>('')
  const [entityFilter, setEntityFilter] = useState<string>('')
  const [draft, setDraft] = useState<Draft>(emptyDraft)
  const [editingId, setEditingId] = useState<number | null>(null)

  const settings = useQuery<SettingsResp>({
    queryKey: ['settings'],
    queryFn: () => fetchJSON<SettingsResp>('/api/settings'),
    staleTime: 60_000,
  })
  const names = settings.data?.settings.accounts.names
  const llcName = settings.data?.settings.llc_name?.trim() || 'LLC'
  const entityLabel = (e: string) => (e === 'llc' ? llcName : 'Personal')

  const accts = settings.data?.settings.accounts
  const accountOptions = useMemo(() => {
    if (!accts) return []
    return Array.from(new Set([...accts.live, ...accts.evals, ...accts.paid, ...accts.simulation]))
      .filter(Boolean).sort()
  }, [accts])

  const q = useQuery<ExpensesResp>({
    queryKey: ['expenses', year, entityFilter],
    queryFn: () => {
      const p = new URLSearchParams()
      if (year) p.set('year', year)
      if (entityFilter) p.set('entity', entityFilter)
      const qs = p.toString()
      return fetchJSON<ExpensesResp>('/api/expenses' + (qs ? `?${qs}` : ''))
    },
  })

  const reset = () => { setDraft(emptyDraft()); setEditingId(null) }
  const invalidate = () => qc.invalidateQueries({ queryKey: ['expenses'] })

  const save = useMutation({
    mutationFn: () =>
      editingId === null
        ? postJSON<Expense>('/api/expenses', draft)
        : putJSON<Expense>(`/api/expenses/${editingId}`, draft),
    onSuccess: () => { invalidate(); reset() },
  })
  const remove = useMutation({
    mutationFn: (id: number) => deleteJSON(`/api/expenses/${id}`),
    onSuccess: invalidate,
  })

  const startEdit = (e: Expense) => {
    setEditingId(e.id)
    setDraft({
      date: e.date, category: e.category, amount: e.amount, entity: e.entity,
      vendor: e.vendor, account: e.account, recurring: e.recurring,
      deductible: e.deductible, note: e.note,
    })
  }

  const set = (patch: Partial<Draft>) => setDraft((d) => ({ ...d, ...patch }))

  const data = q.data
  const years = data ? Object.keys(data.summary.by_year) : []

  return (
    <>
      <div className="grid">
        <div className="card">
          <h2>{editingId === null ? 'Add expense' : 'Edit expense'}</h2>
          <div className="expense-form">
            <label><span>Date</span>
              <input type="date" value={draft.date} onChange={(e) => set({ date: e.target.value })} />
            </label>
            <label><span>Category</span>
              <select value={draft.category} onChange={(e) => set({ category: e.target.value })}>
                {CATEGORY_KEYS.map((k) => <option key={k} value={k}>{catLabel(k)}</option>)}
              </select>
            </label>
            <label><span>Amount ($)</span>
              <input type="number" min={0} step="any" value={draft.amount || ''}
                     onChange={(e) => set({ amount: Number(e.target.value) })} />
            </label>
            <label><span>Entity</span>
              <select value={draft.entity} onChange={(e) => set({ entity: e.target.value })}>
                {ENTITIES.map((en) => <option key={en} value={en}>{entityLabel(en)}</option>)}
              </select>
            </label>
            <label><span>Vendor</span>
              <input type="text" placeholder="Tradeify, NinjaTrader..." value={draft.vendor}
                     onChange={(e) => set({ vendor: e.target.value })} />
            </label>
            <label><span>Account (optional)</span>
              <select value={draft.account} onChange={(e) => set({ account: e.target.value })}>
                <option value="">-- none --</option>
                {accountOptions.map((a) => <option key={a} value={a}>{accountLabel(a, names)}</option>)}
              </select>
            </label>
            <label className="expense-check">
              <input type="checkbox" checked={draft.recurring} onChange={(e) => set({ recurring: e.target.checked })} />
              <span>Recurring (monthly)</span>
            </label>
            <label className="expense-check">
              <input type="checkbox" checked={draft.deductible} onChange={(e) => set({ deductible: e.target.checked })} />
              <span>Deductible</span>
            </label>
            <label className="expense-note"><span>Note</span>
              <input type="text" value={draft.note} onChange={(e) => set({ note: e.target.value })} />
            </label>
          </div>
          <div className="expense-actions">
            <button type="button" className="primary" onClick={() => save.mutate()}
                    disabled={save.isPending || draft.amount <= 0 || !draft.date}>
              {editingId === null ? 'Add' : 'Save'}
            </button>
            {editingId !== null && <button type="button" className="btn-ghost" onClick={reset}>Cancel</button>}
            {save.error && <span className="error">{String(save.error)}</span>}
          </div>
        </div>

        <SummaryCard data={data} entityLabel={entityLabel} names={names} />
      </div>

      <div className="card">
        <div className="expense-ledger-head">
          <h2>Expenses {data && `(${data.summary.count})`}</h2>
          <div className="expense-filters">
            <select value={year} onChange={(e) => setYear(e.target.value)}>
              <option value="">All years</option>
              {years.map((y) => <option key={y} value={y}>{y}</option>)}
            </select>
            <select value={entityFilter} onChange={(e) => setEntityFilter(e.target.value)}>
              <option value="">All entities</option>
              {ENTITIES.map((en) => <option key={en} value={en}>{entityLabel(en)}</option>)}
            </select>
            <span className="expense-total">Total: <strong>{fmtMoney(data?.summary.total ?? 0)}</strong></span>
          </div>
        </div>
        {q.isLoading ? <div>Loading...</div> : q.error ? <div className="error">{String(q.error)}</div> : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Date</th><th>Category</th><th>Vendor</th><th>Account</th>
                  <th>Entity</th><th className="num">Amount</th><th>Flags</th><th>Note</th><th></th>
                </tr>
              </thead>
              <tbody>
                {(data?.expenses ?? []).map((e) => (
                  <tr key={e.id ?? undefined}>
                    <td>{e.date}</td>
                    <td>{catLabel(e.category)}</td>
                    <td>{e.vendor || <span className="subtle">--</span>}</td>
                    <td>{e.account ? accountLabel(e.account, names) : <span className="subtle">--</span>}</td>
                    <td>{entityLabel(e.entity)}</td>
                    <td className="num">{fmtMoney(e.amount)}</td>
                    <td className="subtle">
                      {[e.recurring ? 'recurring' : '', e.deductible ? 'deductible' : ''].filter(Boolean).join(', ') || '--'}
                    </td>
                    <td className="subtle">{e.note}</td>
                    <td className="num">
                      <button type="button" className="btn-ghost" onClick={() => startEdit(e)}>Edit</button>
                      <button type="button" className="btn-danger" onClick={() => e.id !== null && remove.mutate(e.id)}>Del</button>
                    </td>
                  </tr>
                ))}
                {(data?.expenses.length ?? 0) === 0 && (
                  <tr><td colSpan={9} className="subtle" style={{ textAlign: 'center', padding: 20 }}>No expenses logged.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  )
}

function SummaryCard({ data, entityLabel, names }: {
  data: ExpensesResp | undefined
  entityLabel: (e: string) => string
  names?: Record<string, string>
}) {
  if (!data) return <div className="card"><h2>Summary</h2><div className="subtle">Loading...</div></div>
  const s = data.summary
  const cats = Object.entries(s.by_category).sort((a, b) => b[1] - a[1])
  return (
    <div className="card">
      <h2>Summary</h2>
      <div className="big"><span>{fmtMoney(s.total)}</span><span className="big-sub"> total ({s.count})</span></div>
      {ENTITIES.map((en) => (
        <div className="kv" key={en}>
          <span>{entityLabel(en)}</span><span>{fmtMoney(s.by_entity[en] ?? 0)}</span>
        </div>
      ))}
      <h3 className="subtle" style={{ margin: '12px 0 4px' }}>By category</h3>
      {cats.length === 0 ? <p className="subtle">--</p> : cats.map(([k, v]) => (
        <div className="kv" key={k}><span>{EXPENSE_CATEGORY_LABELS[k] ?? k}</span><span>{fmtMoney(v)}</span></div>
      ))}
      {Object.keys(s.by_account).length > 0 && (
        <>
          <h3 className="subtle" style={{ margin: '12px 0 4px' }}>By account</h3>
          {Object.entries(s.by_account).sort((a, b) => b[1] - a[1]).map(([k, v]) => (
            <div className="kv" key={k}><span>{accountLabel(k, names)}</span><span>{fmtMoney(v)}</span></div>
          ))}
        </>
      )}
    </div>
  )
}
