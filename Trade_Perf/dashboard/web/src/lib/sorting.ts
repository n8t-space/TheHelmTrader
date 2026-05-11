// Sort helpers shared by table panels (Trade Performance, Signal Analysis).

export type Sort<K extends string> = { key: K; dir: 'asc' | 'desc' }

export function flip<K extends string>(s: Sort<K>, k: K): Sort<K> {
  if (s.key === k) return { key: k, dir: s.dir === 'asc' ? 'desc' : 'asc' }
  return { key: k, dir: 'desc' }
}

export function arrow<K extends string>(s: Sort<K>, k: K): string {
  if (s.key !== k) return ''
  return s.dir === 'asc' ? ' ▲' : ' ▼'
}

export function sortBy<T, K extends string>(
  rows: T[],
  sort: Sort<K>,
  accessor: (r: T, k: K) => unknown,
): T[] {
  const sign = sort.dir === 'asc' ? 1 : -1
  return [...rows].sort((a, b) => {
    const av = accessor(a, sort.key)
    const bv = accessor(b, sort.key)
    if (av == null && bv == null) return 0
    if (av == null) return -sign
    if (bv == null) return sign
    if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * sign
    return String(av).localeCompare(String(bv)) * sign
  })
}
