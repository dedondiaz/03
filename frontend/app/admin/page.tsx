'use client'

import { useState } from 'react'

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

export default function AdminPage() {
  const [token, setToken] = useState('')
  const [members, setMembers] = useState<any[]>([])
  const [invite, setInvite] = useState({ email: '', role: 'member' })
  const [policies, setPolicies] = useState<any>(null)
  const [audit, setAudit] = useState<any[]>([])
  const [usage, setUsage] = useState<any>(null)

  async function login() {
    const r = await fetch(`${API}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: 'owner@example.com', password: 'dev-password' }) })
    setToken((await r.json()).token)
  }

  async function loadAll() {
    const [m,p,a,u] = await Promise.all([
      fetch(`${API}/tenant/members`, { headers: { Authorization: `Bearer ${token}` } }),
      fetch(`${API}/tenant/policies`, { headers: { Authorization: `Bearer ${token}` } }),
      fetch(`${API}/audit/list?limit=50`, { headers: { Authorization: `Bearer ${token}` } }),
      fetch(`${API}/tenant/usage/summary?days=7`, { headers: { Authorization: `Bearer ${token}` } }),
    ])
    setMembers(await m.json())
    setPolicies(await p.json())
    setAudit(await a.json())
    setUsage(await u.json())
  }

  async function sendInvite() {
    const r = await fetch(`${API}/tenant/members/invite`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify(invite) })
    alert(`Invite token: ${(await r.json()).invite_token}`)
    loadAll()
  }

  async function updateRole(userId: string, role: string) {
    await fetch(`${API}/tenant/members/${userId}`, { method: 'PUT', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ role }) })
    loadAll()
  }

  async function removeMember(userId: string) {
    await fetch(`${API}/tenant/members/${userId}`, { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } })
    loadAll()
  }

  async function savePolicies() {
    await fetch(`${API}/tenant/policies`, { method: 'PUT', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify(policies) })
    loadAll()
  }

  return <main style={{ padding: 24, fontFamily: 'sans-serif' }}>
    <h1>Admin Essentials</h1>
    {!token ? <button onClick={login}>Login</button> : <button onClick={loadAll}>Refresh</button>}

    <h2>Members</h2>
    <div>
      <input placeholder='email' value={invite.email} onChange={(e) => setInvite({ ...invite, email: e.target.value })} />
      <select value={invite.role} onChange={(e) => setInvite({ ...invite, role: e.target.value })}><option value='member'>member</option><option value='admin'>admin</option></select>
      <button onClick={sendInvite}>Invite</button>
    </div>
    <ul>{members.map((m) => <li key={m.user_id}>{m.email} ({m.role}) <button onClick={() => updateRole(m.user_id, m.role === 'member' ? 'admin' : 'member')}>Toggle role</button> <button onClick={() => removeMember(m.user_id)}>Remove</button></li>)}</ul>

    <h2>Policies Hub</h2>
    {policies && <>
      <div>Email domains <input style={{ width: 400 }} value={(policies.email_domains?.allowed_email_domains || []).join(',')} onChange={(e) => setPolicies({ ...policies, email_domains: { allowed_email_domains: e.target.value.split(',').map(x=>x.trim()).filter(Boolean) } })} /></div>
      <div>Slack channels <input style={{ width: 400 }} value={(policies.slack_policy?.allowed_channel_ids || []).join(',')} onChange={(e) => setPolicies({ ...policies, slack_policy: { ...policies.slack_policy, allowed_channel_ids: e.target.value.split(',').map((x:string)=>x.trim()).filter(Boolean) } })} /></div>
      <div>Jira keys <input style={{ width: 400 }} value={(policies.jira_policy?.allowed_project_keys || []).join(',')} onChange={(e) => setPolicies({ ...policies, jira_policy: { ...policies.jira_policy, allowed_project_keys: e.target.value.split(',').map((x:string)=>x.trim().toUpperCase()).filter(Boolean) } })} /></div>
      <div>Notion parents <input style={{ width: 400 }} value={(policies.notion_policy?.allowed_parent_ids || []).join(',')} onChange={(e) => setPolicies({ ...policies, notion_policy: { allowed_parent_ids: e.target.value.split(',').map((x:string)=>x.trim()).filter(Boolean) } })} /></div>
      <button onClick={savePolicies}>Save Policies</button>
      <pre>{JSON.stringify(policies.plan_limits, null, 2)}</pre>
    </>}

    <h2>Audit (filtered list)</h2>
    <a href={`${API}/audit/export?limit=500`} target='_blank'>Download CSV</a>
    <pre>{JSON.stringify(audit, null, 2)}</pre>

    <h2>Usage (7 days)</h2>
    <pre>{JSON.stringify(usage, null, 2)}</pre>
  </main>
}
