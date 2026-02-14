'use client'
import { useState } from 'react'
const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
export default function OpsPage() {
  const [token, setToken] = useState('')
  const [summary, setSummary] = useState<any>(null)
  const [health, setHealth] = useState<any>(null)
  const [runs, setRuns] = useState<any[]>([])
  async function login() { const r = await fetch(`${API}/auth/login`, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({email:'owner@example.com', password:'dev-password'}) }); setToken((await r.json()).token) }
  async function load() {
    const [s,h,r] = await Promise.all([
      fetch(`${API}/ops/metrics/summary`, { headers:{Authorization:`Bearer ${token}`}}),
      fetch(`${API}/ops/tenants/health`, { headers:{Authorization:`Bearer ${token}`}}),
      fetch(`${API}/ops/runs/recent?limit=25`, { headers:{Authorization:`Bearer ${token}`}}),
    ])
    setSummary(await s.json()); setHealth(await h.json()); setRuns(await r.json())
  }
  return <main style={{padding:24,fontFamily:'sans-serif'}}>
    <h1>Ops</h1>
    {!token ? <button onClick={login}>Login</button> : <button onClick={load}>Refresh</button>}
    {health && <><h2>Integration Health</h2><ul>{(health.integration_health||[]).map((x:any)=><li key={x.integration}>{x.integration}: {x.connected?'connected':'disconnected'} failures={x.consecutive_failures}</li>)}</ul><h2>Usage today vs limits</h2><pre>{JSON.stringify(health.usage_today,null,2)}</pre><h2>Recent failures</h2><pre>{JSON.stringify(health.recent_failures,null,2)}</pre></>}
    {summary && <><h2>Top failing tools</h2><pre>{JSON.stringify(summary.top_failing_tools_last_24h,null,2)}</pre><h2>Queue</h2><pre>{JSON.stringify(summary.queue,null,2)}</pre></>}
    <h2>Recent runs</h2><ul>{runs.map((r:any)=><li key={r.run_id}>{r.status} - {r.run_id}</li>)}</ul>
  </main>
}
