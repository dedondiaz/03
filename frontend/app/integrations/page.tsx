'use client'

import { useState } from 'react'

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

export default function IntegrationsPage() {
  const [token, setToken] = useState('')
  const [googleStatus, setGoogleStatus] = useState<any>(null)
  const [slackStatus, setSlackStatus] = useState<any>(null)
  const [jiraStatus, setJiraStatus] = useState<any>(null)
  const [notionStatus, setNotionStatus] = useState<any>(null)
  const [microsoftStatus, setMicrosoftStatus] = useState<any>(null)
  const [automationPolicy, setAutomationPolicy] = useState<any>(null)

  async function login() {
    const res = await fetch(`${API}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: 'owner@example.com', password: 'dev-password' }) })
    setToken((await res.json()).token)
  }

  async function loadStatus(t = token) {
    const [g, s, j, n, m, a] = await Promise.all([
      fetch(`${API}/integrations/google/status`, { headers: { Authorization: `Bearer ${t}` } }),
      fetch(`${API}/integrations/slack/status`, { headers: { Authorization: `Bearer ${t}` } }),
      fetch(`${API}/integrations/jira/status`, { headers: { Authorization: `Bearer ${t}` } }),
      fetch(`${API}/integrations/notion/status`, { headers: { Authorization: `Bearer ${t}` } }),
      fetch(`${API}/integrations/microsoft/status`, { headers: { Authorization: `Bearer ${t}` } }),
      fetch(`${API}/tenant/automation-policy`, { headers: { Authorization: `Bearer ${t}` } }),
    ])
    setGoogleStatus(await g.json())
    setSlackStatus(await s.json())
    setJiraStatus(await j.json())
    setNotionStatus(await n.json())
    setMicrosoftStatus(await m.json())
    setAutomationPolicy(await a.json())
  }

  async function connectGoogle() {
    const res = await fetch(`${API}/integrations/google/connect`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
    const data = await res.json()
    window.location.href = data.authorization_url
  }

  async function disconnectGoogle() {
    await fetch(`${API}/integrations/google/disconnect`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
    loadStatus()
  }

  async function connectSlack() {
    const res = await fetch(`${API}/integrations/slack/connect`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
    const data = await res.json()
    window.location.href = data.authorization_url
  }

  async function disconnectSlack() {
    await fetch(`${API}/integrations/slack/disconnect`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
    loadStatus()
  }

  async function connectJira() {
    const res = await fetch(`${API}/integrations/jira/connect`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
    const data = await res.json()
    window.location.href = data.authorization_url
  }

  async function disconnectJira() {
    await fetch(`${API}/integrations/jira/disconnect`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
    loadStatus()
  }

  async function connectNotion() {
    const res = await fetch(`${API}/integrations/notion/connect`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
    const data = await res.json()
    window.location.href = data.authorization_url
  }

  async function disconnectNotion() {
    await fetch(`${API}/integrations/notion/disconnect`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
    loadStatus()
  }


  async function connectMicrosoft() {
    const res = await fetch(`${API}/integrations/microsoft/connect`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
    const data = await res.json()
    window.location.href = data.authorization_url
  }

  async function disconnectMicrosoft() {
    await fetch(`${API}/integrations/microsoft/disconnect`, { method: 'POST', headers: { Authorization: `Bearer ${token}` } })
    loadStatus()
  }

  return (
    <main style={{ padding: 24, fontFamily: 'sans-serif' }}>
      <h1>Integrations</h1>
      {!token ? <button onClick={login}>Login</button> : <button onClick={() => loadStatus()}>Refresh Status</button>}

      <h2>Google</h2>
      {googleStatus && <pre>{JSON.stringify(googleStatus, null, 2)}</pre>}
      {token && <div><button onClick={connectGoogle}>Connect Google</button> <button onClick={disconnectGoogle}>Disconnect Google</button></div>}

      <h2>Slack</h2>
      {slackStatus && <pre>{JSON.stringify(slackStatus, null, 2)}</pre>}
      {token && <div><button onClick={connectSlack}>Connect Slack</button> <button onClick={disconnectSlack}>Disconnect Slack</button></div>}

      <h2>Jira</h2>
      {jiraStatus && <pre>{JSON.stringify(jiraStatus, null, 2)}</pre>}
      {token && <div><button onClick={connectJira}>Connect Jira</button> <button onClick={disconnectJira}>Disconnect Jira</button></div>}

      <h2>Notion</h2>
      {notionStatus && <pre>{JSON.stringify(notionStatus, null, 2)}</pre>}
      {token && <div><button onClick={connectNotion}>Connect Notion</button> <button onClick={disconnectNotion}>Disconnect Notion</button></div>}


      <h2>Web Automation</h2>
      {automationPolicy && <pre>{JSON.stringify({ configured: (automationPolicy.allowed_domains || []).length > 0, ...automationPolicy }, null, 2)}</pre>}
      <p>Configure domain allowlists and storage_state sessions from Settings.</p>

      <h2>Microsoft 365</h2>
      {microsoftStatus && <pre>{JSON.stringify(microsoftStatus, null, 2)}</pre>}
      {token && <div><button onClick={connectMicrosoft}>Connect Microsoft</button> <button onClick={disconnectMicrosoft}>Disconnect Microsoft</button></div>}
    </main>
  )
}

