'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

export default function AutomationRuleDetailPage({ params }: { params: { id: string } }) {
  const [token, setToken] = useState('')
  const [rule, setRule] = useState<any>(null)
  const [executions, setExecutions] = useState<any[]>([])

  async function login() {
    const r = await fetch(`${API}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: 'owner@example.com', password: 'dev-password' }) })
    const d = await r.json()
    setToken(d.token)
  }

  async function load(t = token) {
    if (!t) return
    const [rr, ex] = await Promise.all([
      fetch(`${API}/automations/rules/${params.id}`, { headers: { Authorization: `Bearer ${t}` } }),
      fetch(`${API}/automations/executions?rule_id=${params.id}&limit=100`, { headers: { Authorization: `Bearer ${t}` } }),
    ])
    if (rr.ok) setRule(await rr.json())
    if (ex.ok) setExecutions(await ex.json())
  }

  useEffect(() => { load() }, [token])

  return (
    <main style={{ padding: 24, fontFamily: 'sans-serif' }}>
      <h1>Automation Rule Detail</h1>
      {!token ? <button onClick={login}>Login</button> : <button onClick={() => load()}>Refresh</button>}
      {rule && <pre>{JSON.stringify(rule, null, 2)}</pre>}
      <h3>Executions</h3>
      {executions.map((e) => (
        <div key={e.id} style={{ border: '1px solid #ddd', marginBottom: 8, padding: 8 }}>
          <div>{e.status} ({e.reason || 'ok'})</div>{e.status === "skipped_quota" && <div style={{color:"darkred"}}>Skipped due to quota.</div>}
          <div>Scheduled: {e.scheduled_for}</div>
          {e.workflow_run_id && <Link href={`/workflows/${e.workflow_run_id}`}>Workflow Run {e.workflow_run_id}</Link>}
        </div>
      ))}
    </main>
  )
}
