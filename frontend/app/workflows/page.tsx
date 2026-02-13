'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

export default function WorkflowsPage() {
  const [token, setToken] = useState('')
  const [templates, setTemplates] = useState<any[]>([])
  const [runs, setRuns] = useState<any[]>([])
  const [selected, setSelected] = useState<any>(null)
  const [rawInput, setRawInput] = useState('{}')

  async function login() {
    const res = await fetch(`${API}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: 'owner@example.com', password: 'dev-password' }) })
    const data = await res.json()
    setToken(data.token)
  }

  async function loadAll(t = token) {
    if (!t) return
    const [tplRes, runRes] = await Promise.all([
      fetch(`${API}/workflows/templates`, { headers: { Authorization: `Bearer ${t}` } }),
      fetch(`${API}/workflows/runs`, { headers: { Authorization: `Bearer ${t}` } }),
    ])
    setTemplates(await tplRes.json())
    setRuns(await runRes.json())
  }

  useEffect(() => { loadAll() }, [token])

  async function runTemplate() {
    if (!selected) return
    const input = JSON.parse(rawInput || '{}')
    const res = await fetch(`${API}/workflows/runs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ template_id: selected.id, input }),
    })
    if (!res.ok) {
      alert((await res.json()).detail || 'Failed')
      return
    }
    const data = await res.json()
    setSelected(null)
    setRawInput('{}')
    await loadAll()
    window.location.href = `/workflows/${data.id}`
  }

  return (
    <main style={{ padding: 24, fontFamily: 'sans-serif' }}>
      <h1>Workflow Packs</h1>
      {!token ? <button onClick={login}>Login</button> : <button onClick={() => loadAll()}>Refresh</button>}

      <h2>Templates</h2>
      {templates.map((t) => (
        <div key={t.id} style={{ border: '1px solid #ddd', padding: 12, marginBottom: 10 }}>
          <strong>{t.name}</strong>
          <p>{t.description}</p>
          <button onClick={() => { setSelected(t); setRawInput(JSON.stringify({}, null, 2)) }}>Run</button>
          <pre>{JSON.stringify(t.input_schema_json, null, 2)}</pre>
        </div>
      ))}

      {selected && (
        <section style={{ border: '1px solid #aaa', padding: 12, marginTop: 12 }}>
          <h3>Run: {selected.name}</h3>
          <textarea style={{ width: '100%', height: 160 }} value={rawInput} onChange={(e) => setRawInput(e.target.value)} />
          <button onClick={runTemplate}>Start Workflow</button>
        </section>
      )}

      <h2>Recent Workflow Runs</h2>
      {runs.map((r) => (
        <div key={r.id}>
          <Link href={`/workflows/${r.id}`}>{r.template_id} - {r.status}</Link>
        </div>
      ))}
    </main>
  )
}
