'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

type Task = { id: string; title: string; description: string; risk_level: string; tenant_id: string }
type RunDetail = { run_id: string; status: string; plan: any; tool_invocations: any[]; verifier: any }

export default function Home() {
  const [token, setToken] = useState('')
  const [tenants, setTenants] = useState<any[]>([])
  const [tasks, setTasks] = useState<Task[]>([])
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [riskLevel, setRiskLevel] = useState('LOW')
  const [runDetail, setRunDetail] = useState<RunDetail | null>(null)
  const [runState, setRunState] = useState<string>('')
  const [healthWarn, setHealthWarn] = useState<string[]>([])

  async function login() {
    const res = await fetch(`${API}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: 'owner@example.com', password: 'dev-password' })
    })
    const data = await res.json()
    setToken(data.token)
  }

  async function loadData(currentToken = token) {
    if (!currentToken) return
    const [tRes, taskRes, healthRes] = await Promise.all([
      fetch(`${API}/tenants/me`, { headers: { Authorization: `Bearer ${currentToken}` } }),
      fetch(`${API}/tasks`, { headers: { Authorization: `Bearer ${currentToken}` } }),
      fetch(`${API}/ops/tenants/health?limit=5`, { headers: { Authorization: `Bearer ${currentToken}` } }),
    ])
    setTenants(await tRes.json())
    setTasks(await taskRes.json())
    if (healthRes.ok) {
      const h = await healthRes.json()
      const bad = (h.integration_health || []).filter((x: any) => (x.consecutive_failures || 0) >= 3).map((x: any) => x.integration)
      setHealthWarn(bad)
    }
  }

  useEffect(() => {
    loadData()
  }, [token])

  async function createTask() {
    await fetch(`${API}/tasks`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ title, description, risk_level: riskLevel })
    })
    setTitle('')
    setDescription('')
    setRiskLevel('LOW')
    loadData()
  }

  async function runTask(id: string, approve = false) {
    const res = await fetch(`${API}/tasks/${id}/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ approve })
    })
    const run = await res.json()
    setRunState(run.status)
    if (run.status !== 'PENDING_APPROVAL') {
      await loadRun(run.run_id)
    }
  }

  async function cancelRun(runId: string) {
    await fetch(`${API}/runs/${runId}/cancel`, { method: "POST", headers: { Authorization: `Bearer ${token}` } })
    await loadRun(runId)
  }

  async function loadRun(runId: string) {
    const res = await fetch(`${API}/runs/${runId}`, { headers: { Authorization: `Bearer ${token}` } })
    if (res.ok) setRunDetail(await res.json())
  }

  async function switchTenant(tenant_id: string) {
    const res = await fetch(`${API}/tenants/switch`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ tenant_id })
    })
    const data = await res.json()
    setToken(data.token)
    setRunDetail(null)
    loadData(data.token)
  }

  return (
    <main style={{ padding: 24, fontFamily: 'sans-serif' }}>
      <h1>AI Assistant Platform (Multi-tenant)</h1>
      <p><Link href='/integrations'>Integrations</Link> | <Link href='/settings'>Settings</Link> | <Link href='/search'>Search</Link> | <Link href='/workflows'>Workflows</Link> | <Link href='/automations'>Automations</Link> | <Link href='/ops'>Ops</Link> | <Link href='/admin'>Admin</Link></p>
      {!token ? <button onClick={login}>Login (dev user)</button> : <p>Logged in</p>}
      {healthWarn.length > 0 && <p style={{color:'darkred'}}>Health warning: repeated failures in {healthWarn.join(', ')}.</p>}

      <h2>Tenant Switch</h2>
      {tenants.map((t) => (
        <div key={t.tenant_id}>
          {t.tenant_id} [{t.role}] {t.active ? '(active)' : <button onClick={() => switchTenant(t.tenant_id)}>Switch</button>}
        </div>
      ))}

      <h2>Create Task</h2>
      <input placeholder="title" value={title} onChange={(e) => setTitle(e.target.value)} />
      <input placeholder="description" value={description} onChange={(e) => setDescription(e.target.value)} />
      <select value={riskLevel} onChange={(e) => setRiskLevel(e.target.value)}>
        <option>LOW</option><option>MEDIUM</option><option>HIGH</option>
      </select>
      <button onClick={createTask}>Create</button>

      <h2>Tasks</h2>
      {tasks.map((task) => (
        <div key={task.id} style={{ border: '1px solid #ddd', margin: '8px 0', padding: 12 }}>
          <strong>{task.title}</strong> [{task.risk_level}]<br />
          {task.description}
          <div>
            <button onClick={() => runTask(task.id, false)}>Run</button>
            {task.risk_level === 'HIGH' && <button onClick={() => runTask(task.id, true)}>Approve & Run</button>}
          </div>
        </div>
      ))}

      {runState === 'PENDING_APPROVAL' && <p style={{ color: 'darkred' }}>Approval needed for HIGH-risk execution.</p>}

      {runDetail && (
        <section>
          <h2>Run Detail</h2>
          <p>Status: <strong>{runDetail.status}</strong></p>
          {(['QUEUED','RUNNING','PENDING_APPROVAL','CANCELLING'] as string[]).includes(runDetail.status) && <button onClick={() => cancelRun(runDetail.run_id)}>Cancel Run</button>}
          <h3>Plan</h3>
          <pre>{JSON.stringify(runDetail.plan, null, 2)}</pre>
          <h3>Tool Invocations</h3>
          {runDetail.tool_invocations.map((inv, idx) => (
            <pre key={idx}>{JSON.stringify(inv, null, 2)}</pre>
          ))}
          <h3>Verifier</h3>
          <pre>{JSON.stringify(runDetail.verifier, null, 2)}</pre>
          {runDetail.tool_invocations.some((i) => (i.error || '').includes('google_not_connected')) && (
            <p style={{ color: 'darkred' }}>Connect Google to use Gmail/Calendar tools. See Integrations page.</p>
          )}
          {runDetail.tool_invocations.some((i) => (i.error || '').includes('slack_not_connected')) && (
            <p style={{ color: 'darkred' }}>Connect Slack to use Slack tools. See Integrations page.</p>
          )}
          {runDetail.tool_invocations.some((i) => (i.error || '').includes('jira_not_connected')) && (
            <p style={{ color: 'darkred' }}>Connect Jira to use Jira tools. See Integrations page.</p>
          )}
          {runDetail.tool_invocations.some((i) => (i.error || '').includes('quota_exceeded')) && (<p style={{ color: 'darkred' }}>Quota exceeded. Check tenant limits and wait for reset window.</p>)}
          {runDetail.tool_invocations.some((i) => (i.error || '').includes('microsoft_not_connected')) && (
            <p style={{ color: 'darkred' }}>Connect Microsoft 365 to use Outlook tools. See Integrations page.</p>
          )}
        </section>
      )}
    </main>
  )
}
