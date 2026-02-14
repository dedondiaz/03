'use client'

import { useState } from 'react'

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

export default function SettingsPage() {
  const [token, setToken] = useState('')
  const [domains, setDomains] = useState('')
  const [cal, setCal] = useState<any>({ timezone: 'Asia/Kolkata', work_start: '10:00', work_end: '18:00', work_days: [1,2,3,4,5], slot_granularity_minutes: 15, meeting_buffer_minutes: 10, default_calendar_id: 'primary' })
  const [slackPolicy, setSlackPolicy] = useState<any>({ allowed_channel_ids: '', allow_external_shared: false })
  const [jiraPolicy, setJiraPolicy] = useState<any>({ allowed_project_keys: '', allow_write: true })
  const [automation, setAutomation] = useState<any>({ allowed_domains: '', allowed_path_prefixes: '', allow_mutations: false, max_steps: 25, max_runtime_seconds: 120, retention_days: 14 })
  const [sessions, setSessions] = useState<any[]>([])
  const [newSession, setNewSession] = useState<any>({ domain: '', storage_state: '{}' })

  async function login() {
    const res = await fetch(`${API}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: 'owner@example.com', password: 'dev-password' }) })
    setToken((await res.json()).token)
  }

  async function loadPolicy() {
    const res = await fetch(`${API}/tenant/policy`, { headers: { Authorization: `Bearer ${token}` } })
    const data = await res.json()
    setDomains((data.allowed_email_domains || []).join(','))
  }

  async function savePolicy() {
    await fetch(`${API}/tenant/policy`, { method: 'PUT', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ allowed_email_domains: domains.split(',').map((x) => x.trim()).filter(Boolean) }) })
    alert('Policy saved')
  }

  async function loadCalendar() {
    const res = await fetch(`${API}/tenant/calendar-settings`, { headers: { Authorization: `Bearer ${token}` } })
    setCal(await res.json())
  }

  async function loadSlackPolicy() {
    const res = await fetch(`${API}/tenant/slack-policy`, { headers: { Authorization: `Bearer ${token}` } })
    const data = await res.json()
    setSlackPolicy({ allowed_channel_ids: (data.allowed_channel_ids || []).join(','), allow_external_shared: !!data.allow_external_shared })
  }

  async function saveSlackPolicy() {
    await fetch(`${API}/tenant/slack-policy`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ allowed_channel_ids: String(slackPolicy.allowed_channel_ids).split(',').map((x: string) => x.trim()).filter(Boolean), allow_external_shared: !!slackPolicy.allow_external_shared })
    })
    alert('Slack policy saved')
  }

  async function loadJiraPolicy() {
    const res = await fetch(`${API}/tenant/jira-policy`, { headers: { Authorization: `Bearer ${token}` } })
    const data = await res.json()
    setJiraPolicy({ allowed_project_keys: (data.allowed_project_keys || []).join(','), allow_write: !!data.allow_write })
  }

  async function saveJiraPolicy() {
    await fetch(`${API}/tenant/jira-policy`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ allowed_project_keys: String(jiraPolicy.allowed_project_keys).split(',').map((x: string) => x.trim().toUpperCase()).filter(Boolean), allow_write: !!jiraPolicy.allow_write })
    })
    alert('Jira policy saved')
  }


  async function loadAutomationPolicy() {
    const res = await fetch(`${API}/tenant/automation-policy`, { headers: { Authorization: `Bearer ${token}` } })
    const data = await res.json()
    setAutomation({ ...data, allowed_domains: (data.allowed_domains || []).join(','), allowed_path_prefixes: (data.allowed_path_prefixes || []).join(',') })
  }

  async function saveAutomationPolicy() {
    await fetch(`${API}/tenant/automation-policy`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ ...automation, allowed_domains: String(automation.allowed_domains).split(',').map((x: string) => x.trim()).filter(Boolean), allowed_path_prefixes: String(automation.allowed_path_prefixes).split(',').map((x: string) => x.trim()).filter(Boolean) })
    })
    alert('Automation policy saved')
  }

  async function loadAutomationSessions() {
    const res = await fetch(`${API}/automation/sessions`, { headers: { Authorization: `Bearer ${token}` } })
    const data = await res.json()
    setSessions(data.sessions || [])
  }

  async function createAutomationSession() {
    await fetch(`${API}/automation/sessions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ domain: newSession.domain, storage_state: JSON.parse(newSession.storage_state) })
    })
    await loadAutomationSessions()
  }

  async function deleteAutomationSession(id: string) {
    await fetch(`${API}/automation/sessions/${id}`, { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } })
    await loadAutomationSessions()
  }

  async function saveCalendar() {
    await fetch(`${API}/tenant/calendar-settings`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ ...cal, work_days: String(cal.work_days).split(',').map((x: string) => Number(x.trim())).filter((x: number) => !isNaN(x)) })
    })
    alert('Calendar settings saved')
  }

  return (
    <main style={{ padding: 24, fontFamily: 'sans-serif' }}>
      <h1>Tenant Settings</h1>
      {!token ? <button onClick={login}>Login</button> : <p>Authenticated</p>}

      <h2>Email Domain Allowlist</h2>
      {token && <button onClick={loadPolicy}>Load Policy</button>}
      <div>
        <input style={{ width: 500 }} value={domains} onChange={(e) => setDomains(e.target.value)} placeholder='example.com,partner.com' />
      </div>
      {token && <button onClick={savePolicy}>Save Policy</button>}

      <h2>Calendar Settings</h2>
      {token && <button onClick={loadCalendar}>Load Calendar Settings</button>}
      <div><label>Timezone</label><input value={cal.timezone} onChange={(e) => setCal({ ...cal, timezone: e.target.value })} /></div>
      <div><label>Work Start</label><input value={cal.work_start} onChange={(e) => setCal({ ...cal, work_start: e.target.value })} /></div>
      <div><label>Work End</label><input value={cal.work_end} onChange={(e) => setCal({ ...cal, work_end: e.target.value })} /></div>
      <div><label>Work Days (1-7 comma)</label><input value={String(cal.work_days)} onChange={(e) => setCal({ ...cal, work_days: e.target.value })} /></div>
      <div><label>Slot Granularity Minutes</label><input value={cal.slot_granularity_minutes} onChange={(e) => setCal({ ...cal, slot_granularity_minutes: Number(e.target.value) })} /></div>
      <div><label>Meeting Buffer Minutes</label><input value={cal.meeting_buffer_minutes} onChange={(e) => setCal({ ...cal, meeting_buffer_minutes: Number(e.target.value) })} /></div>
      <div><label>Default Calendar Id</label><input value={cal.default_calendar_id} onChange={(e) => setCal({ ...cal, default_calendar_id: e.target.value })} /></div>
      {token && <button onClick={saveCalendar}>Save Calendar Settings</button>}

      <h2>Slack Safety</h2>
      {token && <button onClick={loadSlackPolicy}>Load Slack Policy</button>}
      <div><label>Allowed Channel IDs (comma separated)</label><input style={{ width: 500 }} value={slackPolicy.allowed_channel_ids} onChange={(e) => setSlackPolicy({ ...slackPolicy, allowed_channel_ids: e.target.value })} /></div>
      <div><label><input type='checkbox' checked={slackPolicy.allow_external_shared} onChange={(e) => setSlackPolicy({ ...slackPolicy, allow_external_shared: e.target.checked })} /> Allow external shared channels</label></div>
      {token && <button onClick={saveSlackPolicy}>Save Slack Policy</button>}


      <h2>Web Automation</h2>
      {token && <button onClick={loadAutomationPolicy}>Load Automation Policy</button>}
      <div><label>Allowed Domains</label><input style={{ width: 500 }} value={automation.allowed_domains} onChange={(e) => setAutomation({ ...automation, allowed_domains: e.target.value })} /></div>
      <div><label>Allowed Path Prefixes</label><input style={{ width: 500 }} value={automation.allowed_path_prefixes} onChange={(e) => setAutomation({ ...automation, allowed_path_prefixes: e.target.value })} /></div>
      <div><label><input type='checkbox' checked={automation.allow_mutations} onChange={(e) => setAutomation({ ...automation, allow_mutations: e.target.checked })} /> Allow mutations</label></div>
      <div><label>Max Steps</label><input value={automation.max_steps} onChange={(e) => setAutomation({ ...automation, max_steps: Number(e.target.value) })} /></div>
      <div><label>Max Runtime Seconds</label><input value={automation.max_runtime_seconds} onChange={(e) => setAutomation({ ...automation, max_runtime_seconds: Number(e.target.value) })} /></div>
      <div><label>Retention Days</label><input value={automation.retention_days} onChange={(e) => setAutomation({ ...automation, retention_days: Number(e.target.value) })} /></div>
      {token && <button onClick={saveAutomationPolicy}>Save Automation Policy</button>}

      {token && <div><button onClick={loadAutomationSessions}>Load Sessions</button></div>}
      <div><label>Session domain</label><input value={newSession.domain} onChange={(e) => setNewSession({ ...newSession, domain: e.target.value })} /></div>
      <div><label>Storage state JSON</label><textarea style={{ width: 600, height: 120 }} value={newSession.storage_state} onChange={(e) => setNewSession({ ...newSession, storage_state: e.target.value })} /></div>
      {token && <button onClick={createAutomationSession}>Create Session</button>}
      <ul>{sessions.map((s) => <li key={s.id}>{s.domain} <button onClick={() => deleteAutomationSession(s.id)}>Delete</button></li>)}</ul>

      <h2>Jira Safety</h2>
      {token && <button onClick={loadJiraPolicy}>Load Jira Policy</button>}
      <div><label>Allowed Project Keys (comma separated)</label><input style={{ width: 500 }} value={jiraPolicy.allowed_project_keys} onChange={(e) => setJiraPolicy({ ...jiraPolicy, allowed_project_keys: e.target.value })} /></div>
      <div><label><input type='checkbox' checked={jiraPolicy.allow_write} onChange={(e) => setJiraPolicy({ ...jiraPolicy, allow_write: e.target.checked })} /> Allow Jira writes</label></div>
      {token && <button onClick={saveJiraPolicy}>Save Jira Policy</button>}
    </main>
  )
}
