'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'

const API = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

const PRESETS = [
  { name: 'Weekdays 9am', cron: '0 9 * * 1-5' },
  { name: 'Daily 8am', cron: '0 8 * * *' },
]

export default function AutomationsPage() {
  const [token, setToken] = useState('')
  const [templates, setTemplates] = useState<any[]>([])
  const [rules, setRules] = useState<any[]>([])
  const [selectedTemplate, setSelectedTemplate] = useState('')
  const [name, setName] = useState('')
  const [scheduleCron, setScheduleCron] = useState('0 9 * * 1-5')
  const [timezone, setTimezone] = useState('Asia/Kolkata')
  const [quietStart, setQuietStart] = useState('')
  const [quietEnd, setQuietEnd] = useState('')
  const [maxRunsPerDay, setMaxRunsPerDay] = useState(3)
  const [maxConcurrent, setMaxConcurrent] = useState(1)
  const [inputText, setInputText] = useState('{}')
  const [error, setError] = useState('')

  async function login() {
    const r = await fetch(`${API}/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: 'owner@example.com', password: 'dev-password' }) })
    const d = await r.json()
    setToken(d.token)
  }

  async function loadAll(t = token) {
    if (!t) return
    const [tRes, rRes] = await Promise.all([
      fetch(`${API}/workflows/templates`, { headers: { Authorization: `Bearer ${t}` } }),
      fetch(`${API}/automations/rules`, { headers: { Authorization: `Bearer ${t}` } }),
    ])
    const tData = await tRes.json()
    setTemplates(tData)
    if (!selectedTemplate && tData[0]?.id) setSelectedTemplate(tData[0].id)
    setRules(await rRes.json())
  }

  useEffect(() => { loadAll() }, [token])

  async function createRule() {
    setError('')
    let parsed = {}
    try {
      parsed = JSON.parse(inputText || '{}')
    } catch {
      setError('Input JSON invalid')
      return
    }
    const res = await fetch(`${API}/automations/rules`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        name: name || 'Automation Rule',
        template_id: selectedTemplate,
        input: parsed,
        trigger_type: 'schedule',
        schedule_cron: scheduleCron,
        timezone,
        quiet_hours_start: quietStart || null,
        quiet_hours_end: quietEnd || null,
        max_runs_per_day: maxRunsPerDay,
        max_concurrent_runs: maxConcurrent,
      }),
    })
    if (!res.ok) {
      const d = await res.json()
      setError(d.detail || 'Failed to create rule')
      return
    }
    setName('')
    setInputText('{}')
    await loadAll()
  }

  async function toggleRule(id: string, enabled: boolean) {
    await fetch(`${API}/automations/rules/${id}/toggle`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ enabled }),
    })
    await loadAll()
  }

  return (
    <main style={{ padding: 24, fontFamily: 'sans-serif' }}>
      <h1>Automations</h1>
      {!token ? <button onClick={login}>Login</button> : <button onClick={() => loadAll()}>Refresh</button>}

      <h2>Create Rule</h2>
      {error && <p style={{ color: 'darkred' }}>{error}</p>}
      <div><label>Name</label><input value={name} onChange={(e) => setName(e.target.value)} /></div>
      <div><label>Template</label><select value={selectedTemplate} onChange={(e) => setSelectedTemplate(e.target.value)}>{templates.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</select></div>
      <div><label>Cron</label><input value={scheduleCron} onChange={(e) => setScheduleCron(e.target.value)} /></div>
      <div>{PRESETS.map((p) => <button key={p.name} onClick={() => setScheduleCron(p.cron)}>{p.name}</button>)}</div>
      <div><label>Timezone</label><input value={timezone} onChange={(e) => setTimezone(e.target.value)} /></div>
      <div><label>Quiet Start</label><input placeholder='HH:MM' value={quietStart} onChange={(e) => setQuietStart(e.target.value)} /></div>
      <div><label>Quiet End</label><input placeholder='HH:MM' value={quietEnd} onChange={(e) => setQuietEnd(e.target.value)} /></div>
      <div><label>Max Runs/Day</label><input type='number' value={maxRunsPerDay} onChange={(e) => setMaxRunsPerDay(Number(e.target.value))} /></div>
      <div><label>Max Concurrent</label><input type='number' value={maxConcurrent} onChange={(e) => setMaxConcurrent(Number(e.target.value))} /></div>
      <div><label>Input JSON</label><textarea style={{ width: '100%', height: 120 }} value={inputText} onChange={(e) => setInputText(e.target.value)} /></div>
      <button onClick={createRule}>Create Rule</button>

      <h2>Rules</h2>
      {rules.map((r) => (
        <div key={r.id} style={{ border: '1px solid #ddd', padding: 10, marginBottom: 8 }}>
          <strong>{r.name}</strong> ({r.template_id}) [{r.enabled ? 'enabled' : 'disabled'}]
          <div>Schedule: {r.schedule_cron} ({r.timezone})</div>
          <div>Next: {r.next_run_at || '-'}</div>
          <div>Last: {r.last_run_at || '-'}</div>
          <button onClick={() => toggleRule(r.id, !r.enabled)}>{r.enabled ? 'Disable' : 'Enable'}</button>
          <Link href={`/automations/${r.id}`} style={{ marginLeft: 10 }}>Details</Link>
        </div>
      ))}
    </main>
  )
}
