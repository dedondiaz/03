from sqlalchemy import text
from app.db import engine
from app.integrations.google import oauth


def _connect_google(client, headers, monkeypatch):
    monkeypatch.setattr(oauth, 'exchange_code', lambda code, verifier: {
        'access_token': 'tok', 'refresh_token': 'ref', 'expires_in': 3600, 'scope': 'openid email'
    })
    client.post('/integrations/google/connect', headers=headers)
    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        state = conn.execute(text("SELECT state FROM google_oauth_states ORDER BY created_at DESC LIMIT 1")).scalar()
    client.get(f'/integrations/google/callback?state={state}&code=abc')


def test_rls_isolation_tenant_calendar_settings(client, login):
    headers = login(client)
    client.put('/tenant/calendar-settings', headers=headers, json={'timezone': 'UTC', 'work_start': '09:00', 'work_end': '17:00', 'work_days': [1,2,3,4,5], 'slot_granularity_minutes': 30, 'meeting_buffer_minutes': 5, 'default_calendar_id': 'primary'})

    switch = client.post('/tenants/switch', headers=headers, json={'tenant_id': '20000000-0000-0000-0000-000000000001'})
    beta_headers = {'Authorization': f"Bearer {switch.json()['token']}"}
    out = client.get('/tenant/calendar-settings', headers=beta_headers).json()
    assert out['timezone'] == 'Asia/Kolkata'


def test_calendar_cancel_requires_approval(client, monkeypatch, login):
    headers = login(client)
    _connect_google(client, headers, monkeypatch)

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan': ['cancel'], 'tool_calls': [{'tool': 'calendar_cancel_event', 'args': {'event_id': 'e1'}}]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})

    from app.integrations.google.calendar_client import CalendarClient
    monkeypatch.setattr(CalendarClient, 'delete_event', lambda self, calendar_id, event_id: {'cancelled': True, 'event_id': event_id})

    task = client.post('/tasks', headers=headers, json={'title': 'cancel', 'description': 'calendar cancel', 'risk_level': 'LOW'}).json()
    run = client.post(f"/tasks/{task['id']}/run", headers=headers, json={'approve': False}).json()
    assert client.get(f"/runs/{run['run_id']}", headers=headers).json()['status'] == 'PENDING_APPROVAL'


def test_calendar_create_external_is_high_and_blocked_without_approval(client, monkeypatch, login):
    headers = login(client)
    _connect_google(client, headers, monkeypatch)

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan': ['create'], 'tool_calls': [{'tool': 'calendar_create_event', 'args': {'title': 'Meet', 'start': '2026-01-01T11:00:00+00:00', 'end': '2026-01-01T11:30:00+00:00', 'attendees': ['ext@outside.com']}}]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})

    from app.integrations.google.calendar_client import CalendarClient
    monkeypatch.setattr(CalendarClient, 'create_event', lambda self, calendar_id, event: {'id': 'evt-1'})

    task = client.post('/tasks', headers=headers, json={'title': 'create', 'description': 'calendar create', 'risk_level': 'LOW'}).json()
    run = client.post(f"/tasks/{task['id']}/run", headers=headers, json={'approve': False}).json()
    detail = client.get(f"/runs/{run['run_id']}", headers=headers).json()
    assert detail['status'] == 'PENDING_APPROVAL'


def test_calendar_refresh_persists_token(client, monkeypatch, login):
    headers = login(client)
    _connect_google(client, headers, monkeypatch)

    with engine.begin() as conn:
        conn.execute(text("SET app.tenant_id = '10000000-0000-0000-0000-000000000001'"))
        conn.execute(text("UPDATE google_oauth_credentials SET expiry=now() - interval '10 minutes'"))

    refresh_calls = {'n': 0}
    from urllib import request as urllib_request
    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            refresh_calls['n'] += 1
            return b'{"access_token":"newtok","expires_in":3600}'
    monkeypatch.setattr(urllib_request, 'urlopen', lambda *a, **k: FakeResp())

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {'plan': ['slots'], 'tool_calls': [{'tool': 'calendar_find_slots', 'args': {'duration_minutes': 30, 'time_min': '2026-01-01T10:00:00+00:00', 'time_max': '2026-01-01T12:00:00+00:00', 'attendees': []}}]})
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})

    from app.integrations.google.calendar_client import CalendarClient
    monkeypatch.setattr(CalendarClient, 'freebusy', lambda self, query: {'calendars': {'primary': {'busy': []}}})

    task = client.post('/tasks', headers=headers, json={'title': 'slots', 'description': 'calendar schedule', 'risk_level': 'LOW'}).json()
    client.post(f"/tasks/{task['id']}/run", headers=headers, json={'approve': False})
    assert refresh_calls['n'] >= 1


def test_calendar_idempotency_prevents_duplicate_create_and_cancel(client, monkeypatch, login):
    headers = login(client)
    _connect_google(client, headers, monkeypatch)
    client.put('/tenant/policy', headers=headers, json={'allowed_email_domains': ['outside.com']})

    from app.llm.client import LLMClient
    monkeypatch.setattr(LLMClient, 'planner', lambda self, task, tools: {
        'plan': ['c1','c2','x1','x2'],
        'tool_calls': [
            {'tool': 'calendar_create_event', 'args': {'title': 'Meet', 'start': '2026-01-01T11:00:00+00:00', 'end': '2026-01-01T11:30:00+00:00', 'attendees': ['a@outside.com']}},
            {'tool': 'calendar_create_event', 'args': {'title': 'Meet', 'start': '2026-01-01T11:00:00+00:00', 'end': '2026-01-01T11:30:00+00:00', 'attendees': ['a@outside.com']}},
            {'tool': 'calendar_cancel_event', 'args': {'event_id': 'evt-1'}},
            {'tool': 'calendar_cancel_event', 'args': {'event_id': 'evt-1'}},
        ]
    })
    monkeypatch.setattr(LLMClient, 'verifier', lambda self, task, outputs: {'status': 'success', 'summary': 'ok', 'follow_up_questions': []})

    calls = {'create': 0, 'delete': 0}
    from app.integrations.google.calendar_client import CalendarClient
    monkeypatch.setattr(CalendarClient, 'create_event', lambda self, calendar_id, event: calls.__setitem__('create', calls['create'] + 1) or {'id': 'evt-1'})
    monkeypatch.setattr(CalendarClient, 'delete_event', lambda self, calendar_id, event_id: calls.__setitem__('delete', calls['delete'] + 1) or {'cancelled': True, 'event_id': event_id})

    task = client.post('/tasks', headers=headers, json={'title': 'ops', 'description': 'calendar ops', 'risk_level': 'HIGH'}).json()
    client.post(f"/tasks/{task['id']}/run", headers=headers, json={'approve': True})
    assert calls['create'] == 1
    assert calls['delete'] == 1
