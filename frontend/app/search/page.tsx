'use client'

import { useMemo, useState } from 'react'

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
const ALL_SOURCES = ['gmail', 'slack', 'jira', 'notion'] as const
type Source = (typeof ALL_SOURCES)[number]

export default function SearchPage() {
  const [token, setToken] = useState('')
  const [query, setQuery] = useState('')
  const [limit, setLimit] = useState(20)
  const [sources, setSources] = useState<Source[]>([...ALL_SOURCES])
  const [results, setResults] = useState<any[]>([])
  const [warnings, setWarnings] = useState<any[]>([])
  const [sourceStatus, setSourceStatus] = useState<any>(null)

  async function login() {
    const res = await fetch(`${API}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: 'owner@example.com', password: 'dev-password' }) })
    const data = await res.json()
    setToken(data.token)
    await loadSourceStatus(data.token)
  }

  async function loadSourceStatus(t = token) {
    const res = await fetch(`${API}/search/sources`, { headers: { Authorization: `Bearer ${t}` } })
    setSourceStatus(await res.json())
  }

  async function runSearch() {
    const src = sources.join(',')
    const url = `${API}/search?q=${encodeURIComponent(query)}&sources=${encodeURIComponent(src)}&limit=${limit}`
    const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } })
    const data = await res.json()
    setResults(data.results || [])
    setWarnings(data.warnings || [])
  }

  const shownWarnings = useMemo(() => warnings, [warnings])

  return (
    <main style={{ padding: 24, fontFamily: 'sans-serif' }}>
      <h1>Unified Search</h1>
      {!token ? <button onClick={login}>Login</button> : <button onClick={() => loadSourceStatus()}>Refresh Source Status</button>}
      <div style={{ marginTop: 12 }}>
        <input style={{ width: 500 }} value={query} onChange={(e) => setQuery(e.target.value)} placeholder='Search query' />
        <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>{[10, 20, 30, 50].map((n) => <option key={n} value={n}>{n}</option>)}</select>
        <button disabled={!token || !query.trim()} onClick={runSearch}>Search</button>
      </div>
      <div style={{ marginTop: 8 }}>
        {ALL_SOURCES.map((s) => {
          const enabled = sources.includes(s)
          return <button key={s} onClick={() => setSources(enabled ? sources.filter((x) => x !== s) : [...sources, s])} style={{ marginRight: 8, background: enabled ? '#333' : '#ddd', color: enabled ? '#fff' : '#000' }}>{s}</button>
        })}
      </div>
      {sourceStatus && <pre>{JSON.stringify(sourceStatus, null, 2)}</pre>}
      {shownWarnings.map((w, i) => <div key={i} style={{ border: '1px solid #f5c2c7', padding: 8, marginTop: 8, background: '#f8d7da' }}><strong>{w.source}</strong>: {w.message}</div>)}
      <div style={{ marginTop: 16 }}>
        {results.map((r, i) => <div key={i} style={{ border: '1px solid #ddd', padding: 12, marginBottom: 10 }}><div><span style={{ fontSize: 12, padding: '2px 6px', background: '#eee' }}>{r.source}</span></div><strong>{r.title}</strong><p>{r.snippet}</p><small>{JSON.stringify(r.metadata || {})}</small>{r.url && <div><a href={r.url} target='_blank'>Open link</a></div>}</div>)}
      </div>
    </main>
  )
}
