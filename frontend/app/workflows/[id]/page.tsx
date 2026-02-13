'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

export default function WorkflowRunDetailPage({ params }: { params: { id: string } }) {
  const [token, setToken] = useState('')
  const [data, setData] = useState<any>(null)

  async function login() {
    const res = await fetch(`${API}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: 'owner@example.com', password: 'dev-password' }) })
    const j = await res.json()
    setToken(j.token)
  }

  async function cancelWorkflow() {
    await fetch(`${API}/workflows/runs/${params.id}/cancel`, { method: "POST", headers: { Authorization: `Bearer ${token}` } })
    await load()
  }

  async function load(t = token) {
    if (!t) return
    const res = await fetch(`${API}/workflows/runs/${params.id}`, { headers: { Authorization: `Bearer ${t}` } })
    if (res.ok) setData(await res.json())
  }

  useEffect(() => { load() }, [token])

  return (
    <main style={{ padding: 24, fontFamily: 'sans-serif' }}>
      <h1>Workflow Run Detail</h1>
      {!token ? <button onClick={login}>Login</button> : <button onClick={() => load()}>Refresh</button>}
      {data && (
        <section>
          <p>Status: <strong>{data.workflow_run.status}</strong></p>
          {(['queued','running','waiting_approval'] as string[]).includes(data.workflow_run.status) && <button onClick={cancelWorkflow}>Cancel Workflow Run</button>}
          {data.workflow_run.status === 'waiting_approval' && <p style={{ color: 'darkred' }}>Approval required before workflow can continue.</p>}
          <h3>Workflow Input</h3>
          <pre>{JSON.stringify(data.workflow_run.input, null, 2)}</pre>
          <h3>Summary</h3>
          <pre>{JSON.stringify(data.workflow_run.summary_text, null, 2)}</pre>
          {String(data.workflow_run.summary_text || "").includes("quota") && <p style={{ color: "darkred" }}>Quota exceeded: check tenant plan limits.</p>}
          {data.linked_run && (
            <>
              <p>Linked Run: <Link href='/'>{data.linked_run.run_id}</Link></p>
              <h3>Linked Run Detail</h3>
              <pre>{JSON.stringify(data.linked_run, null, 2)}</pre>
            </>
          )}
        </section>
      )}
    </main>
  )
}
