'use client'

import { useState } from 'react'

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

export default function AutomationRunDetail({ params }: { params: { id: string } }) {
  const [token, setToken] = useState('')
  const [run, setRun] = useState<any>(null)
  const [artifacts, setArtifacts] = useState<any[]>([])

  async function login() {
    const res = await fetch(`${API}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: 'owner@example.com', password: 'dev-password' }) })
    setToken((await res.json()).token)
  }

  async function load() {
    const r = await fetch(`${API}/automation/runs/${params.id}`, { headers: { Authorization: `Bearer ${token}` } })
    const a = await fetch(`${API}/automation/runs/${params.id}/artifacts`, { headers: { Authorization: `Bearer ${token}` } })
    setRun(await r.json())
    setArtifacts((await a.json()).artifacts || [])
  }

  return <main style={{ padding: 24, fontFamily: 'sans-serif' }}>
    <h1>Automation Run {params.id}</h1>
    {!token ? <button onClick={login}>Login</button> : <button onClick={load}>Load Run</button>}
    {run && <pre>{JSON.stringify({ final_url: run.final_url, status: run.status, errors: run.errors }, null, 2)}</pre>}
    {artifacts.map((a) => <div key={a.id}><a href={`${API}/automation/artifacts/${a.id}/download`} target='_blank'>Download {a.kind}</a></div>)}
  </main>
}
